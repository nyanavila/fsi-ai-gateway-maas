"""
Multi-provider LLM client.

Provider hierarchy:
  1. Red Hat MaaS (LiteLLM proxy) — primary for all routes except CX_ESCALATE
  2. Anthropic Claude              — primary for CX_ESCALATE, fallback for MaaS

Each provider implements the same async complete() / stream() interface.
ProviderRouter selects the right provider per route and handles fallback.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import anthropic
from anthropic import AsyncAnthropic, APIStatusError, APIConnectionError, RateLimitError
from openai import AsyncOpenAI, APIStatusError as OAIStatusError

from .config import settings
from .observability import metrics

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_DELAY  = 1.0
MAX_TOKENS  = 1024


# ── Response dataclass ────────────────────────────────────────────────────────

@dataclass
class LLMResponse:
    text: str
    model: str
    tokens_used: int
    latency_ms: float
    provider: str = "anthropic"


# ── DeepSeek think-strip ──────────────────────────────────────────────────────

def strip_thinking(text: str) -> str:
    """Remove DeepSeek-R1 chain-of-thought blocks before returning to user."""
    # Remove explicit <think>...</think> tags
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Remove leading reasoning preamble (Okay/Alright/Let me think...)
    text = re.sub(
        r"^(Okay|Alright|So|Let me|First|I need to|I'll|To answer)[,\s].*?\n\n",
        "", text, flags=re.DOTALL
    )
    return text.strip()


# ── Anthropic Provider ────────────────────────────────────────────────────────

class AnthropicProvider:
    def __init__(self):
        self.client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def complete(self, message: str, system_prompt: str,
                       model: str, trace_id: str) -> LLMResponse:
        last_exc = None
        delay = BASE_DELAY
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                start = time.monotonic()
                response = await self.client.messages.create(
                    model=model, max_tokens=MAX_TOKENS, system=system_prompt,
                    messages=[{"role": "user", "content": message}],
                )
                latency_ms = (time.monotonic() - start) * 1000
                tokens = response.usage.input_tokens + response.usage.output_tokens
                metrics.provider_requests.labels(
                    provider="anthropic", model=model, status="success"
                ).inc()
                logger.info("Anthropic call succeeded", extra={
                    "trace_id": trace_id, "model": model,
                    "tokens": tokens, "latency_ms": round(latency_ms, 1),
                })
                return LLMResponse(
                    text=response.content[0].text, model=model,
                    tokens_used=tokens, latency_ms=round(latency_ms, 1),
                    provider="anthropic",
                )
            except RateLimitError as e:
                last_exc = e
                await asyncio.sleep(delay); delay *= 2
            except APIConnectionError as e:
                last_exc = e
                await asyncio.sleep(delay); delay *= 2
            except APIStatusError as e:
                if e.status_code >= 500:
                    last_exc = e
                    await asyncio.sleep(delay); delay *= 2
                else:
                    metrics.provider_requests.labels(
                        provider="anthropic", model=model, status="error"
                    ).inc()
                    raise
        metrics.provider_requests.labels(
            provider="anthropic", model=model, status="error"
        ).inc()
        raise last_exc

    async def stream(self, message: str, system_prompt: str,
                     model: str, trace_id: str) -> AsyncIterator[str]:
        async with self.client.messages.stream(
            model=model, max_tokens=MAX_TOKENS, system=system_prompt,
            messages=[{"role": "user", "content": message}],
        ) as s:
            async for chunk in s.text_stream:
                yield chunk
            final = await s.get_final_message()
            tokens = final.usage.input_tokens + final.usage.output_tokens
            yield f"\x00TOKENS:{tokens}\x00PROVIDER:anthropic"


# ── MaaS Provider (LiteLLM / OpenAI-compatible) ───────────────────────────────

class MaaSProvider:
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.MAAS_API_KEY,
            base_url=settings.MAAS_ENDPOINT,
        )
        self._is_reasoning_model = lambda m: "deepseek" in m.lower() or "r1" in m.lower()

    async def complete(self, message: str, system_prompt: str,
                       model: str, trace_id: str) -> LLMResponse:
        last_exc = None
        delay = BASE_DELAY
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                start = time.monotonic()
                response = await self.client.chat.completions.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": message},
                    ],
                )
                latency_ms = (time.monotonic() - start) * 1000
                text = response.choices[0].message.content or ""
                # Strip reasoning chain for DeepSeek models
                if self._is_reasoning_model(model):
                    text = strip_thinking(text)
                tokens = response.usage.total_tokens if response.usage else 0
                metrics.provider_requests.labels(
                    provider="maas", model=model, status="success"
                ).inc()
                logger.info("MaaS call succeeded", extra={
                    "trace_id": trace_id, "model": model,
                    "tokens": tokens, "latency_ms": round(latency_ms, 1),
                })
                return LLMResponse(
                    text=text, model=model,
                    tokens_used=tokens, latency_ms=round(latency_ms, 1),
                    provider="maas",
                )
            except OAIStatusError as e:
                if e.status_code >= 500:
                    last_exc = e
                    logger.warning(f"MaaS {e.status_code} (attempt {attempt})",
                                   extra={"trace_id": trace_id})
                    await asyncio.sleep(delay); delay *= 2
                else:
                    metrics.provider_requests.labels(
                        provider="maas", model=model, status="error"
                    ).inc()
                    raise
            except Exception as e:
                last_exc = e
                logger.warning(f"MaaS error (attempt {attempt}): {e}",
                               extra={"trace_id": trace_id})
                await asyncio.sleep(delay); delay *= 2
        metrics.provider_requests.labels(
            provider="maas", model=model, status="error"
        ).inc()
        raise last_exc

    async def stream(self, message: str, system_prompt: str,
                     model: str, trace_id: str) -> AsyncIterator[str]:
        response = await self.client.chat.completions.create(
            model=model, max_tokens=MAX_TOKENS, stream=True,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": message},
            ],
        )
        accumulated = ""
        tokens = 0
        async for chunk in response:
            delta = chunk.choices[0].delta.content or "" if chunk.choices else ""
            accumulated += delta
            if delta:
                yield delta
            if chunk.usage:
                tokens = chunk.usage.total_tokens
        if self._is_reasoning_model(model):
            accumulated = strip_thinking(accumulated)
        yield f"\x00TOKENS:{tokens}\x00PROVIDER:maas"


# ── Llama Guard Safety Classifier ─────────────────────────────────────────────

LLAMA_GUARD_PROMPT = """<|begin_of_text|><|start_header_id|>user<|end_header_id|>

Task: Check if there is unsafe content in the user message according to our safety policy.

<BEGIN UNSAFE CONTENT CATEGORIES>
S1: Violent Crimes
S2: Non-Violent Crimes (including financial fraud facilitation)
S3: Sex-Related Crimes
S4: Child Sexual Exploitation
S5: Defamation
S6: Specialized Advice (medical, legal, financial — harmful advice)
S7: Privacy violations
S8: Intellectual Property
S9: Indiscriminate Weapons
S10: Hate
S11: Suicide & Self-Harm
S12: Sexual Content
S13: Elections
S14: Code Interpreter Abuse
<END UNSAFE CONTENT CATEGORIES>

<BEGIN CONVERSATION>
User: {message}
<END CONVERSATION>

Provide your safety assessment. Reply ONLY with "safe" or "unsafe" followed by a newline and the category if unsafe.
<|eot_id|><|start_header_id|>assistant<|end_header_id|>"""


class LlamaGuardSafety:
    """
    ML-based safety classifier using Llama Guard 3 via MaaS.
    Falls back gracefully if MaaS is unavailable.
    """

    def __init__(self, maas_provider: Optional[MaaSProvider]):
        self._maas = maas_provider
        self._available = maas_provider is not None

    async def check(self, message: str, trace_id: str) -> tuple[bool, str]:
        """
        Returns (is_safe, category).
        is_safe=True means the message passed safety check.
        category is empty string if safe, e.g. "S2" if unsafe.
        """
        if not self._available:
            return True, ""   # fail-open if MaaS not configured

        try:
            prompt = LLAMA_GUARD_PROMPT.format(message=message[:2000])
            response = await self._maas.client.chat.completions.create(
                model=settings.MAAS_MODEL_SAFETY,
                max_tokens=32,
                messages=[{"role": "user", "content": prompt}],
            )
            result = (response.choices[0].message.content or "safe").strip().lower()
            if result.startswith("unsafe"):
                category = result.split("\n")[1].strip() if "\n" in result else ""
                logger.warning("Llama Guard: unsafe content", extra={
                    "trace_id": trace_id, "category": category,
                })
                metrics.provider_requests.labels(
                    provider="maas", model=settings.MAAS_MODEL_SAFETY, status="success"
                ).inc()
                return False, category
            metrics.provider_requests.labels(
                provider="maas", model=settings.MAAS_MODEL_SAFETY, status="success"
            ).inc()
            return True, ""
        except Exception as e:
            logger.warning(f"Llama Guard check failed (fail-open): {e}",
                           extra={"trace_id": trace_id})
            return True, ""   # fail-open — never block on classifier error


# ── Provider Router ───────────────────────────────────────────────────────────

class ProviderRouter:
    """
    Selects the right provider and model for each route.
    Handles fallback from MaaS → Anthropic on failure.

    Route → Provider mapping:
      CX_SIMPLE   → MaaS (Granite 8B)     fallback → Anthropic Haiku
      CX_COMPLEX  → MaaS (Llama Scout)    fallback → Anthropic Sonnet
      CX_ESCALATE → Anthropic (Opus)      no MaaS fallback (premium tier)
      IT_SIMPLE   → MaaS (Granite 8B)     fallback → Anthropic Haiku
      IT_COMPLEX  → MaaS (DeepSeek-R1)    fallback → Anthropic Sonnet
    """

    MAAS_ROUTES = {
        "CX_SIMPLE":  "MAAS_MODEL_SIMPLE",
        "CX_COMPLEX": "MAAS_MODEL_COMPLEX",
        "IT_SIMPLE":  "MAAS_MODEL_SIMPLE",
        "IT_COMPLEX": "MAAS_MODEL_REASONING",
    }

    ANTHROPIC_FALLBACK = {
        "CX_SIMPLE":  "MODEL_SMALL",
        "CX_COMPLEX": "MODEL_MEDIUM",
        "IT_SIMPLE":  "MODEL_SMALL",
        "IT_COMPLEX": "MODEL_MEDIUM",
        "CX_ESCALATE": "MODEL_LARGE",
    }

    def __init__(self):
        self.anthropic = AnthropicProvider()
        if settings.MAAS_API_KEY:
            self.maas = MaaSProvider()
            self.guard = LlamaGuardSafety(self.maas)
            logger.info("ProviderRouter: MaaS enabled",
                        extra={"endpoint": settings.MAAS_ENDPOINT})
        else:
            self.maas = None
            self.guard = LlamaGuardSafety(None)
            logger.info("ProviderRouter: Anthropic-only mode (MAAS_API_KEY not set)")

    async def complete(self, message: str, system_prompt: str,
                       route: str, trace_id: str) -> LLMResponse:

        # CX_ESCALATE always goes to Anthropic Opus
        if route == "CX_ESCALATE" or self.maas is None:
            model = getattr(settings, self.ANTHROPIC_FALLBACK.get(route, "MODEL_LARGE"))
            return await self.anthropic.complete(message, system_prompt, model, trace_id)

        # All other routes: try MaaS first, fall back to Anthropic
        maas_model_key = self.MAAS_ROUTES.get(route, "MAAS_MODEL_SIMPLE")
        maas_model = getattr(settings, maas_model_key)
        try:
            return await self.maas.complete(message, system_prompt, maas_model, trace_id)
        except Exception as e:
            logger.warning(f"MaaS failed for {route}, falling back to Anthropic: {e}",
                           extra={"trace_id": trace_id})
            fallback_key = self.ANTHROPIC_FALLBACK.get(route, "MODEL_SMALL")
            fallback_model = getattr(settings, fallback_key)
            return await self.anthropic.complete(
                message, system_prompt, fallback_model, trace_id
            )

    async def stream(self, message: str, system_prompt: str,
                     route: str, trace_id: str) -> AsyncIterator[str]:

        if route == "CX_ESCALATE" or self.maas is None:
            model = getattr(settings, self.ANTHROPIC_FALLBACK.get(route, "MODEL_LARGE"))
            async for chunk in self.anthropic.stream(message, system_prompt, model, trace_id):
                yield chunk
            return

        maas_model_key = self.MAAS_ROUTES.get(route, "MAAS_MODEL_SIMPLE")
        maas_model = getattr(settings, maas_model_key)
        try:
            async for chunk in self.maas.stream(message, system_prompt, maas_model, trace_id):
                yield chunk
        except Exception as e:
            logger.warning(f"MaaS stream failed, falling back to Anthropic: {e}",
                           extra={"trace_id": trace_id})
            fallback_key = self.ANTHROPIC_FALLBACK.get(route, "MODEL_SMALL")
            fallback_model = getattr(settings, fallback_key)
            async for chunk in self.anthropic.stream(
                message, system_prompt, fallback_model, trace_id
            ):
                yield chunk


# ── Backward-compatible singleton (used by main.py) ───────────────────────────
# main.py imports AnthropicProvider for the non-streaming path.
# We expose ProviderRouter as the new primary interface.

import json
import logging
import re
from dataclasses import dataclass
from openai import AsyncOpenAI

from .config import settings

logger = logging.getLogger(__name__)

# ── Route definitions ─────────────────────────────────────────────────────────
# model field is now resolved at runtime by ProviderRouter — these are labels only

ROUTES = {
    "CX_SIMPLE": {
        "provider": "maas",
        "system_prompt": (
            "You are a helpful customer support agent for a financial services company. "
            "Answer concisely and accurately. If you cannot resolve the issue, "
            "say so clearly and offer to escalate."
        ),
    },
    "CX_COMPLEX": {
        "provider": "maas",
        "system_prompt": (
            "You are a senior customer support specialist for a financial services company. "
            "Be empathetic, thorough, and precise. Never speculate about account balances "
            "or transactions — always recommend the customer verify through secure channels."
        ),
    },
    "CX_ESCALATE": {
        "provider": "anthropic",
        "system_prompt": (
            "You are handling a sensitive escalation for a financial services customer. "
            "Lead with empathy. Acknowledge the issue fully before attempting to resolve it. "
            "Do not be defensive. Offer concrete next steps and a named point of contact."
        ),
    },
    "IT_SIMPLE": {
        "provider": "maas",
        "system_prompt": (
            "You are an IT support assistant. Provide concise, step-by-step technical guidance."
        ),
    },
    "IT_COMPLEX": {
        "provider": "maas",
        "system_prompt": (
            "You are a senior IT support engineer for a financial services company. "
            "Diagnose and resolve complex technical issues methodically. "
            "Consider security implications in every recommendation."
        ),
    },
}

CLASSIFIER_PROMPT = """You are a routing classifier for an FSI (financial services) customer support AI gateway.

Classify the user query into exactly one of these routes:

- CX_ESCALATE: ALWAYS use when the customer uses ANY of:
  * angry words: furious, outraged, disgusted, livid, unacceptable
  * accusations: stolen, fraud, scam, lied, cheated, incompetent
  * demands: manager, supervisor, lawyer, legal action, complaint
  * urgency: NOW, immediately, urgent, emergency
  * threats: sue, report, regulator, ombudsman, FCA, CFPB

- CX_COMPLEX: requires policy knowledge or reasoning
  (disputes, refunds, complex transactions, regulatory questions)

- CX_SIMPLE: straightforward query
  (account info, FAQs, product info, simple requests)

- IT_COMPLEX: complex technical issue
  (network outages, security incidents, system failures, data loss)

- IT_SIMPLE: basic internal IT issue
  (VPN, password reset, hardware, software installs, access)

When in doubt between CX_SIMPLE and CX_ESCALATE, always choose CX_ESCALATE.

Respond ONLY with valid JSON, no markdown fences:
{"route":"<ROUTE>","reason":"<one sentence>","confidence":<0.0-1.0>}"""


@dataclass
class RouteResult:
    route: str
    model: str
    system_prompt: str
    reason: str
    confidence: float
    provider: str = "maas"
    budget_downgraded: bool = False


class SemanticRouter:
    def __init__(self):
        # Use MaaS Granite for classification if available, else Anthropic
        if settings.MAAS_API_KEY:
            self._client = AsyncOpenAI(
                api_key=settings.MAAS_API_KEY,
                base_url=settings.MAAS_ENDPOINT,
            )
            self._classifier_model = settings.MAAS_MODEL_CLASSIFIER
            self._use_maas = True
            logger.info(f"Router: using MaaS classifier ({self._classifier_model})")
        else:
            import anthropic
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            self._classifier_model = settings.MODEL_SMALL
            self._use_maas = False
            logger.info(f"Router: using Anthropic classifier ({self._classifier_model})")

    async def route(self, message: str, department: str, budget_state) -> RouteResult:
        try:
            if self._use_maas:
                raw = await self._client.chat.completions.create(
                    model=self._classifier_model,
                    max_tokens=128,
                    messages=[
                        {"role": "system", "content": CLASSIFIER_PROMPT},
                        {"role": "user",   "content": message},
                    ],
                )
                text = raw.choices[0].message.content or ""
            else:
                raw = self._client.messages.create(
                    model=self._classifier_model,
                    max_tokens=128,
                    system=CLASSIFIER_PROMPT,
                    messages=[{"role": "user", "content": message}],
                )
                text = raw.content[0].text

            # Strip markdown fences if present
            text = re.sub(r"^```json\s*", "", text.strip())
            text = re.sub(r"\s*```$", "", text).strip()

            parsed    = json.loads(text)
            route     = parsed.get("route", "CX_SIMPLE")
            reason    = parsed.get("reason", "")
            confidence = float(parsed.get("confidence", 0.8))

            logger.info("Route classified", extra={
                "route": route, "reason": reason,
                "confidence": confidence,
                "classifier": self._classifier_model,
            })

        except Exception as e:
            logger.warning(f"Classifier failed, defaulting to CX_SIMPLE: {e}")
            route, reason, confidence = "CX_SIMPLE", "classifier error — safe default", 0.0

        if route not in ROUTES:
            route = "CX_SIMPLE"

        route_config = ROUTES[route]
        provider = route_config["provider"]

        # Resolve display model name from ProviderRouter mapping
        from .providers import ProviderRouter
        maas_key = ProviderRouter.MAAS_ROUTES.get(route, "MAAS_MODEL_SIMPLE")
        ant_key  = ProviderRouter.ANTHROPIC_FALLBACK.get(route, "MODEL_SMALL")

        if provider == "anthropic" or not settings.MAAS_API_KEY:
            model = getattr(settings, ant_key)
        else:
            model = getattr(settings, maas_key)

        budget_downgraded = False
        if budget_state.cx_over_threshold and route != "CX_ESCALATE":
            # Budget pressure: keep on MaaS but log the pressure
            logger.info(f"CX budget over threshold — MaaS handles load relief for {route}")
            budget_downgraded = True

        return RouteResult(
            route=route,
            model=model,
            system_prompt=route_config["system_prompt"],
            reason=reason,
            confidence=confidence,
            provider=provider,
            budget_downgraded=budget_downgraded,
        )

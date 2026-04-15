import time, uuid, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .router import SemanticRouter
from .config import settings
from .cache import SemanticCache
from .security import SecurityLayer
from .budget import BudgetManager
from .providers import ProviderRouter
from .observability import metrics, setup_logging, RequestLogger

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("AI Gateway starting up")
    yield
    logger.info("AI Gateway shutting down")


app = FastAPI(title="FSI AI Gateway", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",")],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

semantic_router = SemanticRouter()
cache           = SemanticCache()
security        = SecurityLayer()
budget          = BudgetManager()
provider_router = ProviderRouter()


class ChatRequest(BaseModel):
    message:     str    = Field(..., min_length=1, max_length=8000)
    department:  str    = Field("CX", pattern="^(CX|IT|FINANCE)$")
    customer_id: str | None = None
    session_id:  str | None = None


class ChatResponse(BaseModel):
    trace_id:    str
    response:    str
    route:       str
    model_used:  str
    provider:    str
    cache_hit:   bool
    tokens_used: int
    latency_ms:  float


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ai-gateway", "version": "2.0.0"}


@app.get("/ready")
async def readiness():
    try:
        stats = await cache.stats("CX")
        if stats.get("error"):
            raise HTTPException(status_code=503, detail="Redis unavailable")
        return {"status": "ready"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/metrics")
async def prometheus_metrics():
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from fastapi.responses import Response
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def _run_pipeline(request: ChatRequest, trace_id: str):
    start = time.monotonic()

    # Step 1a — Regex injection check (fast, free)
    injection = security.check_injection(request.message)
    if injection.blocked:
        metrics.injection_attempts.labels(department=request.department).inc()
        metrics.safety_checks.labels(result="blocked_regex", category="injection").inc()
        raise HTTPException(status_code=400, detail="Request blocked by security policy.")

    # Step 1b — PII masking
    sanitized, pii_fields = security.mask_pii(request.message)
    if pii_fields:
        metrics.pii_masked.labels(department=request.department).inc()
        logger.info("PII masked", extra={"trace_id": trace_id, "fields": pii_fields})

    # Step 1c — Llama Guard ML safety check
    is_safe, category = await provider_router.guard.check(sanitized, trace_id)
    if not is_safe:
        metrics.injection_attempts.labels(department=request.department).inc()
        metrics.safety_checks.labels(result="blocked_llamaguard", category=category).inc()
        raise HTTPException(status_code=400, detail="Request blocked by security policy.")
    metrics.safety_checks.labels(result="safe", category="").inc()

    metrics.requests_total.labels(department=request.department).inc()

    # Step 2 — Semantic cache lookup
    cached = await cache.get(sanitized, request.department)
    if cached:
        latency_ms = (time.monotonic() - start) * 1000
        metrics.cache_hits.labels(department=request.department).inc()
        metrics.request_latency.labels(
            department=request.department, route="cache", model="none"
        ).observe(latency_ms / 1000)
        return sanitized, None, cached, round(latency_ms, 2), True

    metrics.cache_misses.labels(department=request.department).inc()

    # Step 3 — Budget check
    budget_state = await budget.check(request.department)
    if budget_state.hard_limit_reached:
        raise HTTPException(status_code=429, detail="Department token budget exhausted.")

    # Step 4 — Semantic routing (now using MaaS Granite classifier)
    route_result = await semantic_router.route(sanitized, request.department, budget_state)
    metrics.route_decisions.labels(
        department=request.department, route=route_result.route
    ).inc()
    logger.info("Route decided", extra={
        "trace_id": trace_id,
        "route": route_result.route,
        "model": route_result.model,
        "provider": route_result.provider,
        "reason": route_result.reason,
        "budget_downgraded": route_result.budget_downgraded,
    })

    return sanitized, route_result, None, time.monotonic() - start, False


async def _post_pipeline(department, route_result, response_text,
                         tokens, sanitized_message, trace_id, start_monotonic):
    # DLP output scan
    clean_response, leaked = security.scan_output(response_text)
    if leaked:
        logger.warning("DLP: PII in output — redacted", extra={
            "trace_id": trace_id, "fields": leaked,
        })

    await budget.record(department, tokens)
    metrics.tokens_used.labels(
        department=department, model=route_result.model
    ).inc(tokens)

    await cache.set(sanitized_message, department, {
        "response": clean_response,
        "route": route_result.route,
        "provider": route_result.provider,
        "model": route_result.model,
    })

    latency_ms = (time.monotonic() - start_monotonic) * 1000
    metrics.request_latency.labels(
        department=department,
        route=route_result.route,
        model=route_result.model,
    ).observe(latency_ms / 1000)

    sentiment = security.analyze_sentiment(sanitized_message)
    metrics.sentiment_score.labels(department=department).observe(sentiment)
    if sentiment < -0.5:
        logger.warning("Low sentiment — escalation candidate", extra={
            "trace_id": trace_id, "sentiment": sentiment,
        })

    return clean_response, round(latency_ms, 2)


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    trace_id = str(uuid.uuid4())
    start    = time.monotonic()

    with RequestLogger(trace_id, request.department, request.message):
        sanitized, route_result, cached_payload, elapsed, cache_hit = \
            await _run_pipeline(request, trace_id)

        if cache_hit:
            return ChatResponse(
                trace_id=trace_id,
                response=cached_payload["response"],
                route=cached_payload.get("route", "cache"),
                model_used="cache",
                provider=cached_payload.get("provider", "cache"),
                cache_hit=True,
                tokens_used=0,
                latency_ms=round(elapsed, 2),
            )

        # LLM call via ProviderRouter
        llm = await provider_router.complete(
            message=sanitized,
            system_prompt=route_result.system_prompt,
            route=route_result.route,
            trace_id=trace_id,
        )

        clean_response, latency_ms = await _post_pipeline(
            department=request.department,
            route_result=route_result,
            response_text=llm.text,
            tokens=llm.tokens_used,
            sanitized_message=sanitized,
            trace_id=trace_id,
            start_monotonic=start,
        )

        return ChatResponse(
            trace_id=trace_id,
            response=clean_response,
            route=route_result.route,
            model_used=llm.model,
            provider=llm.provider,
            cache_hit=False,
            tokens_used=llm.tokens_used,
            latency_ms=latency_ms,
        )


@app.post("/v1/chat/stream")
async def chat_stream(request: ChatRequest):
    trace_id = str(uuid.uuid4())
    start    = time.monotonic()

    sanitized, route_result, cached_payload, elapsed, cache_hit = \
        await _run_pipeline(request, trace_id)

    if cache_hit:
        async def _cached():
            yield f"data: {cached_payload['response']}\n\n"
            yield f"data: [DONE] trace_id={trace_id} tokens=0 cache=true\n\n"
        return StreamingResponse(_cached(), media_type="text/event-stream")

    async def _live():
        accumulated, tokens, provider_name = "", 0, "maas"
        async for chunk in provider_router.stream(
            sanitized, route_result.system_prompt, route_result.route, trace_id
        ):
            if chunk.startswith("\x00TOKENS:"):
                parts = chunk.split("\x00")
                for part in parts:
                    if part.startswith("TOKENS:"):
                        tokens = int(part.split(":")[1])
                    elif part.startswith("PROVIDER:"):
                        provider_name = part.split(":")[1]
                continue
            accumulated += chunk
            yield f"data: {chunk}\n\n"

        clean, latency_ms = await _post_pipeline(
            request.department, route_result, accumulated,
            tokens, sanitized, trace_id, start,
        )
        yield (f"data: [DONE] trace_id={trace_id} tokens={tokens} "
               f"provider={provider_name} latency_ms={latency_ms}\n\n")

    return StreamingResponse(_live(), media_type="text/event-stream")


@app.get("/v1/budget")
async def get_budget():
    return await budget.status()


@app.delete("/v1/cache/{department}")
async def invalidate_cache(department: str):
    if department not in ("CX", "IT", "FINANCE", "ALL"):
        raise HTTPException(status_code=400, detail="Unknown department.")
    if department == "ALL":
        counts = {d: await cache.invalidate_department(d) for d in ("CX", "IT", "FINANCE")}
        return {"invalidated": counts}
    count = await cache.invalidate_department(department)
    return {"department": department, "entries_removed": count}


@app.get("/v1/cache/{department}/stats")
async def cache_stats(department: str):
    return await cache.stats(department)


@app.get("/v1/providers")
async def provider_status():
    """Show which providers are active."""
    return {
        "anthropic": {"status": "active", "models": {
            "small":  settings.MODEL_SMALL,
            "medium": settings.MODEL_MEDIUM,
            "large":  settings.MODEL_LARGE,
        }},
        "maas": {
            "status": "active" if settings.MAAS_API_KEY else "disabled",
            "endpoint": settings.MAAS_ENDPOINT if settings.MAAS_API_KEY else None,
            "models": {
                "classifier": settings.MAAS_MODEL_CLASSIFIER,
                "simple":     settings.MAAS_MODEL_SIMPLE,
                "complex":    settings.MAAS_MODEL_COMPLEX,
                "reasoning":  settings.MAAS_MODEL_REASONING,
                "safety":     settings.MAAS_MODEL_SAFETY,
            } if settings.MAAS_API_KEY else {},
        },
    }



@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception", exc_info=exc)
    metrics.errors_total.inc()
    return JSONResponse(status_code=500, content={"detail": "Internal gateway error."})

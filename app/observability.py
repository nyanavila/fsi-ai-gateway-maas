import logging
import json
import time
from prometheus_client import Counter, Histogram, Gauge


class GatewayMetrics:
    def __init__(self):
        self.requests_total = Counter(
            "gateway_requests_total", "Total requests", ["department"])
        self.cache_hits = Counter(
            "gateway_cache_hits_total", "Cache hits", ["department"])
        self.cache_misses = Counter(
            "gateway_cache_misses_total", "Cache misses", ["department"])
        self.tokens_used = Counter(
            "gateway_tokens_used_total", "Tokens consumed", ["department", "model"])
        self.route_decisions = Counter(
            "gateway_route_decisions_total", "Route decisions", ["department", "route"])
        self.injection_attempts = Counter(
            "gateway_injection_attempts_total", "Injection attempts", ["department"])
        self.pii_masked = Counter(
            "gateway_pii_masked_total", "PII masked requests", ["department"])
        self.errors_total = Counter(
            "gateway_errors_total", "Unhandled errors")
        self.request_latency = Histogram(
            "gateway_request_latency_seconds", "Request latency",
            ["department", "route", "model"],
            buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0])
        self.sentiment_score = Histogram(
            "gateway_sentiment_score", "Sentiment scores", ["department"],
            buckets=[-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0])
        self.budget_fraction = Gauge(
            "gateway_budget_fraction_used", "Budget fraction used", ["department"])
        # NEW: per-provider request tracking
        self.provider_requests = Counter(
            "gateway_provider_requests_total",
            "Requests by provider, model, and status",
            ["provider", "model", "status"])
        # NEW: Llama Guard safety decisions
        self.safety_checks = Counter(
            "gateway_safety_checks_total",
            "Llama Guard safety check results",
            ["result", "category"])


metrics = GatewayMetrics()


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in ("args", "asctime", "created", "exc_info", "exc_text",
                           "filename", "funcName", "id", "levelname", "levelno",
                           "lineno", "module", "msecs", "message", "msg", "name",
                           "pathname", "process", "processName", "relativeCreated",
                           "stack_info", "thread", "threadName"):
                log_obj[key] = value
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


class RequestLogger:
    def __init__(self, trace_id, department, message):
        self.trace_id = trace_id
        self.department = department
        self.message_preview = message[:80]
        self.start = time.time()
        self.logger = logging.getLogger("gateway.request")

    def __enter__(self):
        self.logger.info("Request received", extra={
            "trace_id": self.trace_id,
            "department": self.department,
            "message_preview": self.message_preview,
        })
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = round((time.time() - self.start) * 1000, 1)
        if exc_type:
            self.logger.error("Request failed", extra={
                "trace_id": self.trace_id,
                "duration_ms": duration_ms,
                "error": str(exc_val),
            })
        else:
            self.logger.info("Request completed", extra={
                "trace_id": self.trace_id,
                "duration_ms": duration_ms,
            })
        return False

"""
Token budget manager backed by Redis INCRBY — safe for multi-replica deployments.

All spend counters live in Redis under keys:
  gw:budget:{department}:spend     — daily token total (integer)
  gw:budget:{department}:reset_at  — Unix timestamp of next midnight reset

Burn-rate window uses a Redis sorted set:
  gw:budget:{department}:burn      — scores=tokens, members=timestamp:uuid
"""

import time
import uuid
import logging
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as aioredis

from .config import settings

logger = logging.getLogger(__name__)

DAILY_BUDGETS: dict[str, int] = {
    "CX":      settings.BUDGET_CX_DAILY_TOKENS,
    "IT":      settings.BUDGET_IT_DAILY_TOKENS,
    "FINANCE": settings.BUDGET_FINANCE_DAILY_TOKENS,
}

CX_DOWNGRADE_THRESHOLD = 1.10
HARD_LIMIT_FRACTION    = 1.50
BURN_RATE_WINDOW_SEC   = 300


@dataclass
class BudgetState:
    department: str
    tokens_used: int
    daily_budget: int
    fraction_used: float
    cx_over_threshold: bool
    hard_limit_reached: bool
    burn_rate_per_min: float


def _next_midnight() -> float:
    t = time.time()
    return t - (t % 86400) + 86400


class BudgetManager:
    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        return self._redis

    def _spend_key(self, dept: str) -> str:
        return f"gw:budget:{dept}:spend"

    def _reset_key(self, dept: str) -> str:
        return f"gw:budget:{dept}:reset_at"

    def _burn_key(self, dept: str) -> str:
        return f"gw:budget:{dept}:burn"

    async def _ensure_reset(self, r: aioredis.Redis, dept: str) -> None:
        reset_at_str = await r.get(self._reset_key(dept))
        reset_at = float(reset_at_str) if reset_at_str else 0.0
        if time.time() >= reset_at:
            next_midnight = _next_midnight()
            async with r.pipeline() as pipe:
                pipe.set(self._spend_key(dept), 0)
                pipe.set(self._reset_key(dept), next_midnight)
                pipe.delete(self._burn_key(dept))
                await pipe.execute()
            logger.info(f"Budget reset for {dept}")

    async def check(self, department: str) -> BudgetState:
        try:
            r = await self._get_redis()
            await self._ensure_reset(r, department)

            used = int(await r.get(self._spend_key(department)) or 0)
            budget = DAILY_BUDGETS.get(department, settings.BUDGET_DEFAULT_DAILY_TOKENS)
            fraction = used / budget if budget else 0.0

            cx_used = int(await r.get(self._spend_key("CX")) or 0)
            cx_budget = DAILY_BUDGETS.get("CX", settings.BUDGET_CX_DAILY_TOKENS)
            cx_fraction = cx_used / cx_budget if cx_budget else 0.0

            burn_rate = await self._burn_rate(r, department)

            return BudgetState(
                department=department,
                tokens_used=used,
                daily_budget=budget,
                fraction_used=round(fraction, 4),
                cx_over_threshold=cx_fraction >= CX_DOWNGRADE_THRESHOLD,
                hard_limit_reached=fraction >= HARD_LIMIT_FRACTION,
                burn_rate_per_min=round(burn_rate, 1),
            )
        except Exception as e:
            logger.warning(f"Budget check failed (fail-open): {e}")
            budget = DAILY_BUDGETS.get(department, settings.BUDGET_DEFAULT_DAILY_TOKENS)
            return BudgetState(
                department=department, tokens_used=0, daily_budget=budget,
                fraction_used=0.0, cx_over_threshold=False,
                hard_limit_reached=False, burn_rate_per_min=0.0,
            )

    async def record(self, department: str, tokens: int) -> None:
        try:
            r = await self._get_redis()
            await self._ensure_reset(r, department)
            new_total = await r.incrby(self._spend_key(department), tokens)

            now = time.time()
            member = f"{now:.3f}:{uuid.uuid4().hex[:8]}"
            await r.zadd(self._burn_key(department), {member: tokens})
            await r.zremrangebyscore(self._burn_key(department), "-inf", now - BURN_RATE_WINDOW_SEC)

            burn = await self._burn_rate(r, department)
            expected = DAILY_BUDGETS.get(
                department, settings.BUDGET_DEFAULT_DAILY_TOKENS
            ) / (24 * 60)
            if burn > expected * 10:
                logger.warning(
                    f"Runaway burn [{department}]: {burn:.0f} tok/min "
                    f"vs expected {expected:.0f} (total={new_total})"
                )
        except Exception as e:
            logger.warning(f"Budget record failed (non-fatal): {e}")

    async def _burn_rate(self, r: aioredis.Redis, department: str) -> float:
        try:
            entries = await r.zrangebyscore(
                self._burn_key(department), "-inf", "+inf", withscores=True
            )
            total_tokens = sum(score for _, score in entries)
            return total_tokens / (BURN_RATE_WINDOW_SEC / 60)
        except Exception:
            return 0.0

    async def status(self) -> dict:
        result = {}
        try:
            r = await self._get_redis()
            for dept, budget in DAILY_BUDGETS.items():
                await self._ensure_reset(r, dept)
                used = int(await r.get(self._spend_key(dept)) or 0)
                reset_at = float(await r.get(self._reset_key(dept)) or _next_midnight())
                burn = await self._burn_rate(r, dept)
                result[dept] = {
                    "tokens_used": used,
                    "daily_budget": budget,
                    "fraction_used": round(used / budget, 4) if budget else 0,
                    "burn_rate_per_min": round(burn, 1),
                    "reset_in_seconds": max(0, int(reset_at - time.time())),
                }
        except Exception as e:
            result["error"] = str(e)
        return result

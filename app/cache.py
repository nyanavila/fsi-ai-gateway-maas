"""
Semantic cache backed by Redis.

Embedding strategy (in priority order):
  1. sentence-transformers (local, no external API call, recommended for air-gapped FSI)
  2. Deterministic SHA-256 hash vector (exact-match fallback, always available)

To enable sentence-transformers, add to requirements.txt:
  sentence-transformers==3.1.1
  torch==2.4.1+cpu      # CPU-only build
"""

import json
import hashlib
import logging
from typing import Optional

import numpy as np
import redis.asyncio as aioredis

from .config import settings

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.92
CACHE_TTL_SECONDS = 3600
ST_MODEL_NAME = "all-MiniLM-L6-v2"   # 80 MB, 384-dim, fast CPU inference


# ── Embedding backend ─────────────────────────────────────────────────────────

class EmbeddingBackend:
    _model = None
    _dim: int = 384
    _backend: str = "hash"

    def __init__(self):
        try:
            import os
            from sentence_transformers import SentenceTransformer
            cache_dir = os.environ.get("ST_CACHE_DIR", "/tmp/st_cache")
            self._model = SentenceTransformer(ST_MODEL_NAME, cache_folder=cache_dir)
            self._dim = self._model.get_sentence_embedding_dimension()
            self._backend = "sentence-transformers"
            logger.info(f"Embedding backend: sentence-transformers "
                        f"({ST_MODEL_NAME}, dim={self._dim})")
        except ImportError:
            logger.warning(
                "sentence-transformers not installed — using hash-vector fallback. "
                "Only exact-match caching is active until the library is added."
            )
            self._backend = "hash"

    def embed(self, text: str) -> list[float]:
        if self._backend == "sentence-transformers":
            vec = self._model.encode(text[:512], normalize_embeddings=True)
            return vec.tolist()
        return self._hash_embed(text)

    @staticmethod
    def _hash_embed(text: str) -> list[float]:
        digest = hashlib.sha256(text.lower().strip().encode()).digest()
        vec = [(b / 255.0) * 2 - 1 for b in digest]
        while len(vec) < 384:
            vec.extend(vec)
        return vec[:384]

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def backend_name(self) -> str:
        return self._backend


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


# ── Semantic Cache ────────────────────────────────────────────────────────────

class SemanticCache:
    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None
        self._embedder = EmbeddingBackend()
        logger.info(f"SemanticCache ready — backend={self._embedder.backend_name}, "
                    f"threshold={SIMILARITY_THRESHOLD}")

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

    def _cache_key_prefix(self, department: str) -> str:
        return f"gw:cache:{department}"

    async def get(self, message: str, department: str) -> Optional[dict]:
        try:
            r = await self._get_redis()
            embedding = self._embedder.embed(message)
            prefix = self._cache_key_prefix(department)
            keys = await r.keys(f"{prefix}:*")

            best_score = 0.0
            best_payload = None

            for key in keys:
                raw = await r.get(key)
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                    score = cosine_similarity(embedding, entry["embedding"])
                    if score > best_score:
                        best_score = score
                        best_payload = entry["payload"]
                except (json.JSONDecodeError, KeyError):
                    continue

            if best_score >= SIMILARITY_THRESHOLD and best_payload:
                logger.debug(f"Cache HIT — similarity={best_score:.4f} dept={department}")
                return best_payload

        except Exception as e:
            logger.warning(f"Cache GET error (non-fatal, bypassing cache): {e}")

        return None

    async def set(self, message: str, department: str, payload: dict) -> None:
        try:
            r = await self._get_redis()
            embedding = self._embedder.embed(message)
            prefix = self._cache_key_prefix(department)
            key_hash = hashlib.sha256(message.encode()).hexdigest()[:16]
            cache_key = f"{prefix}:{key_hash}"
            entry = {"embedding": embedding, "payload": payload}
            await r.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(entry))
            logger.debug(f"Cache SET: {cache_key}")
        except Exception as e:
            logger.warning(f"Cache SET error (non-fatal): {e}")

    async def invalidate_department(self, department: str) -> int:
        try:
            r = await self._get_redis()
            keys = await r.keys(f"{self._cache_key_prefix(department)}:*")
            if keys:
                return await r.delete(*keys)
        except Exception as e:
            logger.warning(f"Cache invalidation error: {e}")
        return 0

    async def stats(self, department: str) -> dict:
        try:
            r = await self._get_redis()
            keys = await r.keys(f"{self._cache_key_prefix(department)}:*")
            return {
                "department": department,
                "entries": len(keys),
                "backend": self._embedder.backend_name,
                "threshold": SIMILARITY_THRESHOLD,
                "ttl_seconds": CACHE_TTL_SECONDS,
            }
        except Exception:
            return {"department": department, "entries": -1, "error": "redis unavailable"}

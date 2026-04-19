"""
redis_store.py — Redis-backed state store for SwarmContext checkpointing.

L-02: Implements RedisStateStore(BaseStateStore) using redis.asyncio.
Design decisions:
  - Connection created via from_url() for URL-native auth support (redis://user:pass@host)
  - TTL defaults to 86400s (24h), configurable via AGENT_SWARM_REDIS_TTL_SECONDS
  - _client is dependency-injectable for testing (pass fakeredis.aioredis.FakeRedis())
  - ImportError is NOT caught here — callers must handle missing redis package explicitly
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.schemas import SwarmContext

logger = logging.getLogger(__name__)

# Imported at module level — ImportError propagates to the factory (fail loud)
import redis.asyncio as aioredis  # noqa: E402


class RedisStateStore:
    """
    Saves SwarmContext checkpoints as JSON strings in Redis with TTL-based expiry.

    Key format:  agent_swarm:context:{task_id}
    Value format: SwarmContext.model_dump_json() (same serialisation as FileStateStore)
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int | None = None,
        *,
        _client: "aioredis.Redis | None" = None,
    ) -> None:
        """
        Args:
            redis_url:    Redis connection URL (e.g. "redis://localhost:6379/0")
            ttl_seconds:  Key TTL in seconds. Falls back to AGENT_SWARM_REDIS_TTL_SECONDS
                          env var, then 86400 (24 h).
            _client:      Inject a pre-built client for testing (e.g. fakeredis). If
                          provided, redis_url is ignored.
        """
        self._ttl: int = ttl_seconds or int(
            os.environ.get("AGENT_SWARM_REDIS_TTL_SECONDS", "86400")
        )
        if self._ttl <= 0:
            raise ValueError(
                f"AGENT_SWARM_REDIS_TTL_SECONDS must be a positive integer, got {self._ttl}"
            )
        self._client: aioredis.Redis = (
            _client if _client is not None
            else aioredis.from_url(redis_url, decode_responses=True)
        )

    @staticmethod
    def _key(task_id: str) -> str:
        return f"agent_swarm:context:{task_id}"

    async def save(self, context: "SwarmContext") -> None:
        key = self._key(context.task_id)
        await self._client.set(key, context.model_dump_json(), ex=self._ttl)
        logger.debug("Redis state saved: %s (TTL=%ds)", key, self._ttl)

    async def load(self, task_id: str) -> "SwarmContext | None":
        from agent_core.schemas import SwarmContext as _SC
        key = self._key(task_id)
        data = await self._client.get(key)
        if not data:
            return None
        try:
            return _SC.model_validate_json(data)
        except Exception as exc:
            logger.error("Failed to deserialise Redis state for %s: %s", task_id, exc)
            return None

    async def delete(self, task_id: str) -> None:
        await self._client.delete(self._key(task_id))

    async def close(self) -> None:
        """Close the Redis connection pool. Call at app shutdown."""
        await self._client.aclose()

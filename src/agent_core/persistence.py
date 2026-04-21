"""
persistence.py — Pluggable state stores for SwarmContext checkpointing.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from agent_core.schemas import StateStoreType, SwarmContext

if TYPE_CHECKING:
    from agent_core.schemas import SwarmConfig

logger = logging.getLogger(__name__)


class BaseStateStore(ABC):
    """Abstract base class for all state stores."""

    @abstractmethod
    async def save(self, context: SwarmContext) -> None:
        """Save a SwarmContext checkpoint."""
        pass

    @abstractmethod
    async def load(self, task_id: str) -> SwarmContext | None:
        """Load a SwarmContext by task_id."""
        pass

    @abstractmethod
    async def delete(self, task_id: str) -> None:
        """Delete a task checkpoint."""
        pass


class FileStateStore(BaseStateStore):
    """Saves SwarmContext as local JSON files."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    async def save(self, context: SwarmContext) -> None:
        path = self.state_dir / f"{context.task_id}.json"
        path.write_text(context.model_dump_json(indent=2))
        logger.debug("State saved to %s", path)

    async def load(self, task_id: str) -> SwarmContext | None:
        path = self.state_dir / f"{task_id}.json"
        if not path.exists():
            return None
        try:
            return SwarmContext.model_validate_json(path.read_text())
        except Exception as e:
            logger.error("Failed to load state from %s: %s", path, e)
            return None

    async def delete(self, task_id: str) -> None:
        path = self.state_dir / f"{task_id}.json"
        if path.exists():
            path.unlink()


class MemoryStateStore(BaseStateStore):
    """In-memory state store for testing."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def save(self, context: SwarmContext) -> None:
        self._store[context.task_id] = context.model_dump_json()

    async def load(self, task_id: str) -> SwarmContext | None:
        data = self._store.get(task_id)
        return SwarmContext.model_validate_json(data) if data else None

    async def delete(self, task_id: str) -> None:
        self._store.pop(task_id, None)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_state_store(config: SwarmConfig) -> BaseStateStore:
    """Factory to get the configured state store."""
    if config.state_store_type == StateStoreType.FILE:
        return FileStateStore(config.state_dir)
    elif config.state_store_type == StateStoreType.MEMORY:
        return MemoryStateStore()
    elif config.state_store_type == StateStoreType.REDIS:
        # Let ImportError propagate — fail loud, not silent fallback
        # (silent fallback hid mis-configured Redis deployments writing to disk)
        try:
            from agent_core.drivers.redis_store import RedisStateStore
        except ImportError as exc:
            raise RuntimeError(
                "StateStoreType.REDIS is configured but the 'redis' package is not installed. "
                "Install it with: pip install 'redis[asyncio]>=4.2' "
                "or add INSTALL_EXTRAS=redis to your Docker build args."
            ) from exc
        if not config.redis_url:
            raise ValueError("redis_url is required when state_store_type is 'redis'")
        return RedisStateStore(config.redis_url)

    return FileStateStore(config.state_dir)


def get_default_state_store() -> BaseStateStore:
    """
    Return a state store for read-only operations (e.g. status endpoints) where
    no SwarmConfig instance is available.

    Resolution order:
      1. AGENT_SWARM_REDIS_URL env var  → RedisStateStore (fail loud if pkg missing)
      2. AGENT_SWARM_STATE_DIR env var  → FileStateStore at that path
      3. SwarmConfig.state_dir default  → FileStateStore at '.agent-swarm/state'

    Redis path raises RuntimeError if redis package is not installed — no silent
    fallback to FILE (that would silently diverge from the orchestrator's store).
    """
    import os

    redis_url = os.environ.get("AGENT_SWARM_REDIS_URL")
    if redis_url:
        try:
            from agent_core.drivers.redis_store import RedisStateStore
        except ImportError as exc:
            raise RuntimeError(
                "AGENT_SWARM_REDIS_URL is set but the 'redis' package is not installed. "
                "Install it with: pip install 'redis[asyncio]>=4.2'"
            ) from exc
        logger.debug("Using RedisStateStore at %s for default state store", redis_url)
        return RedisStateStore(redis_url)

    state_dir_env = os.environ.get("AGENT_SWARM_STATE_DIR")
    if state_dir_env:
        state_dir = Path(state_dir_env)
        logger.debug("Using AGENT_SWARM_STATE_DIR=%s for state store", state_dir)
    else:
        # Derive from SwarmConfig default rather than duplicating the constant
        from agent_core.schemas import SwarmConfig as _SC

        state_dir = _SC.model_fields["state_dir"].default
        logger.debug("Using default state_dir=%s for state store", state_dir)
    return FileStateStore(state_dir)

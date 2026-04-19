"""
test_persistence.py — Contract tests for all BaseStateStore implementations.

L-02: Verifies FILE, MEMORY, and REDIS stores all satisfy the same behavioural contract.
REDIS tests use fakeredis — no real Redis daemon required.

Design:
  - _StoreContract defines the shared behaviour all three must pass.
  - Each concrete test class provides a `store` fixture and inherits all contract tests.
  - parametrize is not used (stores require different fixture lifecycles).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from pathlib import Path

from agent_core.persistence import FileStateStore, MemoryStateStore
from agent_core.schemas import Platform, SwarmContext


# ---------------------------------------------------------------------------
# Shared context fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_context() -> SwarmContext:
    return SwarmContext(
        task_id="persist-test-001",
        task_description="Verify state store persistence contract.",
        platform=Platform.GEMINI,
        constraints={"token_budget": 8000},
    )


@pytest.fixture()
def another_context() -> SwarmContext:
    return SwarmContext(
        task_id="persist-test-002",
        task_description="A second task to verify isolation.",
        platform=Platform.CLAUDE_CODE,
    )


# ---------------------------------------------------------------------------
# Shared contract — all stores must pass
# ---------------------------------------------------------------------------

class _StoreContract:
    """
    Behavioural contract for BaseStateStore.
    Subclasses must provide a `store` fixture returning a fresh store instance.
    """

    @pytest.mark.asyncio
    async def test_save_then_load_roundtrip(self, store, sample_context):
        """Save a context and load it back — must be identical."""
        await store.save(sample_context)
        loaded = await store.load(sample_context.task_id)

        assert loaded is not None
        assert loaded.task_id == sample_context.task_id
        assert loaded.task_description == sample_context.task_description
        assert loaded.platform == sample_context.platform

    @pytest.mark.asyncio
    async def test_load_unknown_task_returns_none(self, store):
        """Loading a non-existent task_id must return None, not raise."""
        result = await store.load("task-does-not-exist-xyz")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_then_load_returns_none(self, store, sample_context):
        """After delete, loading the same task_id must return None."""
        await store.save(sample_context)
        await store.delete(sample_context.task_id)
        result = await store.load(sample_context.task_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_does_not_raise(self, store):
        """Deleting a non-existent key must be a no-op, not raise."""
        await store.delete("task-that-was-never-saved")

    @pytest.mark.asyncio
    async def test_save_overwrites_existing(self, store, sample_context):
        """A second save for the same task_id must overwrite the first."""
        await store.save(sample_context)
        updated = SwarmContext(
            task_id=sample_context.task_id,
            task_description="Updated description after overwrite.",
            platform=sample_context.platform,
        )
        await store.save(updated)
        loaded = await store.load(sample_context.task_id)
        assert loaded is not None
        assert loaded.task_description == "Updated description after overwrite."

    @pytest.mark.asyncio
    async def test_two_tasks_are_isolated(self, store, sample_context, another_context):
        """Saving two different tasks must not corrupt each other's state."""
        await store.save(sample_context)
        await store.save(another_context)

        loaded_1 = await store.load(sample_context.task_id)
        loaded_2 = await store.load(another_context.task_id)

        assert loaded_1 is not None
        assert loaded_2 is not None
        assert loaded_1.task_id != loaded_2.task_id
        assert loaded_1.task_description == sample_context.task_description
        assert loaded_2.task_description == another_context.task_description


# ---------------------------------------------------------------------------
# FileStateStore — contract tests
# ---------------------------------------------------------------------------

class TestFileStateStore(_StoreContract):
    @pytest.fixture()
    def store(self, tmp_path: Path) -> FileStateStore:
        return FileStateStore(tmp_path / "state")


# ---------------------------------------------------------------------------
# MemoryStateStore — contract tests
# ---------------------------------------------------------------------------

class TestMemoryStateStore(_StoreContract):
    @pytest.fixture()
    def store(self) -> MemoryStateStore:
        return MemoryStateStore()


# ---------------------------------------------------------------------------
# RedisStateStore — contract tests (via fakeredis, no daemon required)
# ---------------------------------------------------------------------------

class TestRedisStateStore(_StoreContract):
    @pytest.fixture()
    def store(self):
        fakeredis = pytest.importorskip(
            "fakeredis.aioredis",
            reason="fakeredis[aioredis] not installed — run: pip install 'fakeredis[aioredis]>=2.0'",
        )
        from agent_core.drivers.redis_store import RedisStateStore

        # Inject a FakeRedis client — redis_url is ignored when _client is provided
        fake_client = fakeredis.FakeRedis(decode_responses=True)
        return RedisStateStore(redis_url="redis://unused", _client=fake_client)


# ---------------------------------------------------------------------------
# Extra: RedisStateStore-specific behaviour
# ---------------------------------------------------------------------------

class TestRedisStateStoreExtra:
    """Tests for Redis-specific behaviour not covered by the shared contract."""

    @pytest.fixture()
    def fake_client(self):
        fakeredis = pytest.importorskip("fakeredis.aioredis")
        return fakeredis.FakeRedis(decode_responses=True)

    @pytest.mark.asyncio
    async def test_key_format(self, fake_client, sample_context):
        """Confirm keys are namespaced under agent_swarm:context:"""
        from agent_core.drivers.redis_store import RedisStateStore
        store = RedisStateStore(redis_url="redis://unused", _client=fake_client)
        await store.save(sample_context)
        # The key must be retrievable by the expected name pattern
        raw = await fake_client.get(f"agent_swarm:context:{sample_context.task_id}")
        assert raw is not None
        assert "persist-test-001" in raw

    @pytest.mark.asyncio
    async def test_invalid_ttl_raises(self):
        """A non-positive TTL must raise ValueError at construction time."""
        fakeredis = pytest.importorskip("fakeredis.aioredis")
        from agent_core.drivers.redis_store import RedisStateStore
        with pytest.raises(ValueError, match="positive integer"):
            RedisStateStore(
                redis_url="redis://unused",
                ttl_seconds=-1,
                _client=fakeredis.FakeRedis(decode_responses=True),
            )

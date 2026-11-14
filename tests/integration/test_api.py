"""Phase 1 HTTP denial boundaries and readiness. No key grants execution access."""

import asyncio
from contextlib import AsyncExitStack

import anyio.to_thread
import httpx
import pytest

from tests.postgres_support import PostgresTests, runtime_store
from tests.support import REQUEST, harness
from workflow_engine.api import create_app
from workflow_engine.domain import Conflict, DomainEvent, transition
from workflow_engine.settings import Settings
from workflow_engine.store import MemoryWorkflowStore


def app_for(h, broker=None):
    return create_app(Settings(), h.store, None, broker)


async def test_legacy_key_environment_cannot_reopen_workflow_access(tmp_path, clock, monkeypatch):
    monkeypatch.setenv("WORKFLOW_API_KEYS", '{"a-key":"tenant-a"}')
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await h.admit()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(h)),
            base_url="http://api",
            headers={"X-API-Key": "a-key"},
        ) as client:
            assert (await client.get(f"/workflows/{w.workflow_id}")).status_code == 401
            assert (await client.post("/workflows", json={"text": REQUEST})).status_code == 410


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-API-Key": "a-key"},
        {"X-API-Key": "b-key"},
        {"Cookie": "session=fabricated; workflow_session=admin"},
    ],
)
async def test_closed_workflow_routes_never_mutate_or_call_simulators(tmp_path, clock, headers):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await h.admit()
        await h.tick(w)
        before = await h.get(w)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(h)), base_url="http://api", headers=headers
        ) as client:
            for body in [{"text": REQUEST}, {}, {"text": "x" * 2001}, {"tenant_id": "tenant-b"}]:
                assert (await client.post("/workflows", json=body)).status_code == 410
            path = f"/workflows/{w.workflow_id}"
            for body in [{"expected_revision": before.revision, "plan_hash": before.plan_hash}, {}]:
                assert (await client.post(path + "/approve", json=body)).status_code == 410
            assert (await client.get(path)).status_code == 401
            assert (await client.get("/workflows/unknown")).status_code == 401
            for body in [{"expected_revision": before.revision}, {}]:
                assert (await client.post(path + "/cancel", json=body)).status_code == 401
            assert (await client.post("/workflows", content=b"x" * 16385)).status_code == 413
        assert await h.get(w) == before
        assert len(h.store._documents) == 1
        assert (await h.ticketing.get("/__effects")).json() == {}
        assert (await h.inventory.get("/__effects")).json() == {}


async def test_denial_and_health_do_not_wait_for_worker_thread_pool():
    app = create_app(Settings(), MemoryWorkflowStore(), None)
    limiter = anyio.to_thread.current_default_thread_limiter()
    capacity, busy_worker = limiter.total_tokens, object()
    try:
        limiter.total_tokens = 1
        limiter.acquire_on_behalf_of_nowait(busy_worker)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            denied, live = await asyncio.wait_for(
                asyncio.gather(
                    client.post("/workflows", json={"text": REQUEST}), client.get("/health/live")
                ),
                1,
            )
    finally:
        limiter.release_on_behalf_of(busy_worker)
        limiter.total_tokens = capacity
    assert denied.status_code == 410 and live.status_code == 200


@pytest.mark.parametrize("result", [False, "exception"])
async def test_unavailable_store_returns_503_without_credentials(caplog, result):
    store = MemoryWorkflowStore()

    async def ping():
        if result == "exception":
            raise ConnectionError("postgresql://private-user:private-password@private-host/db")
        return False

    store.ping = ping
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Settings(), store, None)),
        base_url="http://api",
    ) as client:
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable", "postgres": False, "broker": False}
        assert "private" not in response.text + caplog.text
        assert (await client.get("/health/live")).status_code == 200


async def test_redis_failure_is_degraded_200_and_keeps_postgres_ready(caplog):
    class OfflineRedis:
        async def ping(self):
            raise ConnectionError("redis://private-user:private-password@private-host")

    from workflow_engine.broker import RedisWakeupBroker

    app = create_app(Settings(), MemoryWorkflowStore(), None, RedisWakeupBroker(OfflineRedis()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "postgres": True, "broker": False}
    assert "private" not in response.text + caplog.text


@pytest.mark.postgres
@pytest.mark.parametrize("schema", ["empty", "wrong_head"])
async def test_real_schema_readiness_requires_current_migration(schema, caplog):
    from workflow_engine.db import create_database
    from workflow_engine.postgres_store import PostgresWorkflowStore

    with PostgresTests.from_environment() as postgres:
        allocation = postgres.allocate(migrate=schema == "wrong_head")
        if schema == "wrong_head":
            with allocation.connect_owner() as connection:
                connection.execute("UPDATE alembic_version SET version_num = 'wrong'")
        database = create_database(allocation.runtime_url)
        try:
            app = create_app(Settings(), PostgresWorkflowStore(database), None)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://api"
            ) as client:
                response = await client.get("/health/ready")
            assert response.status_code == 503
            assert response.json()["postgres"] is False
            assert allocation.runtime_url.get_secret_value() not in response.text + caplog.text
        finally:
            await database.close()


@pytest.mark.postgres
async def test_real_store_prepared_approval_cancellation_race_has_one_winner(tmp_path, clock):
    # Internal fixture capability, not a legacy HTTP factory or product command.
    with PostgresTests.from_environment() as postgres:
        async with runtime_store(postgres) as store, AsyncExitStack() as stack:
            h = await harness(tmp_path, clock, stack, store=store)
            w = await h.admit()
            await h.tick(w)
            w = await h.get(w)
            results = await asyncio.gather(
                h.approve(w),
                store.save(transition(w, DomainEvent(kind="cancelled")), w.revision, None),
                return_exceptions=True,
            )
            assert sum(isinstance(result, Conflict) for result in results) == 1
            assert (await h.get(w)).state in {"READY", "CANCELLED"}
            assert (await h.ticketing.get("/__effects")).json() == {}


async def test_internal_fixture_cancellation_keeps_revision_lease_and_confirmed_outcome_guards(
    tmp_path, clock
):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await h.admit()
        await h.tick(w)
        w = await h.approve(w)
        lease = await h.store.claim(w.workflow_id, "busy")
        assert lease is not None
        with pytest.raises(Conflict):
            await h.cancel(lease.workflow)
        await h.store.release(lease)
        await h.tick(w)
        current = await h.get(w)
        with pytest.raises(Conflict):
            await h.cancel(current, expected_revision=0)
        cancelled = await h.cancel(current)
        assert cancelled.state == "CANCELLED"
        assert cancelled.actions[0] == current.actions[0]
        assert cancelled.actions[0].result["id"]
        assert cancelled.actions[1].submissions == 0
        assert await h.cancel(cancelled, expected_revision=0) == cancelled

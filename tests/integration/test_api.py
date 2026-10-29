import asyncio
from contextlib import AsyncExitStack

import anyio.to_thread
import httpx
import pytest
from pymongo.errors import ServerSelectionTimeoutError

from tests.support import REQUEST, harness
from workflow_engine.api import create_app
from workflow_engine.policy import CatalogProvider
from workflow_engine.settings import Settings
from workflow_engine.store import MemoryWorkflowStore


def app_for(h):
    return create_app(
        Settings(api_keys={"a-key": "tenant-a", "b-key": "tenant-b"}),
        h.store,
        CatalogProvider.from_file("fixtures/catalog.jsonl"),
    )


async def test_admission_remains_available_when_worker_thread_pool_is_saturated():
    store = MemoryWorkflowStore()
    app = create_app(
        Settings(api_keys={"a-key": "tenant-a"}),
        store,
        CatalogProvider.from_file("fixtures/catalog.jsonl"),
    )
    limiter = anyio.to_thread.current_default_thread_limiter()
    capacity = limiter.total_tokens
    busy_worker = object()
    try:
        limiter.total_tokens = 1
        limiter.acquire_on_behalf_of_nowait(busy_worker)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/workflows",
                    headers={"X-API-Key": "a-key", "Idempotency-Key": "busy-pool"},
                    json={"text": REQUEST},
                ),
                timeout=1,
            )
    finally:
        limiter.release_on_behalf_of(busy_worker)
        limiter.total_tokens = capacity
    assert response.status_code == 202
    persisted = await store.get(response.json()["workflow_id"], "tenant-a")
    assert persisted.request_text == REQUEST
    assert persisted.state == "PENDING_PLAN"


async def test_admission_isolation_replay_and_body_limits(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        http = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(h)), base_url="http://api")
        )
        headers = {"X-API-Key": "a-key", "Idempotency-Key": "key"}
        response = await http.post("/workflows", headers=headers, json={"text": REQUEST})
        assert response.status_code == 202
        workflow_id = response.json()["workflow_id"]
        assert (
            await http.post("/workflows", headers=headers, json={"text": REQUEST})
        ).status_code == 200
        assert (
            await http.post("/workflows", headers=headers, json={"text": "changed"})
        ).status_code == 409
        assert (
            await http.post("/workflows", headers={"X-API-Key": "a-key"}, json={"text": REQUEST})
        ).status_code == 400
        assert (
            await http.get(f"/workflows/{workflow_id}", headers={"X-API-Key": "b-key"})
        ).status_code == 404
        assert (await http.get(f"/workflows/{workflow_id}")).status_code == 401
        assert (
            await http.post(
                "/workflows", headers=headers, json={"text": REQUEST, "tenant_id": "tenant-b"}
            )
        ).status_code == 422
        assert (
            await http.post("/workflows", headers=headers, content=b"x" * 16385)
        ).status_code == 413
        assert (await h.ticketing.get("/__effects")).json() == {}


async def test_exact_approval_replay_and_cancellation_race(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await h.admit()
        await h.tick(w)
        w = await h.get(w)
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_for(h)),
                base_url="http://api",
                headers={"X-API-Key": "a-key"},
            )
        )
        path = f"/workflows/{w.workflow_id}"
        approval = {"expected_revision": w.revision, "plan_hash": w.plan_hash}
        assert (
            await http.post(path + "/approve", json={**approval, "plan_hash": "0" * 64})
        ).status_code == 409
        assert (
            await http.post(path + "/approve", json={**approval, "expected_revision": 0})
        ).status_code == 409
        assert (await http.post(path + "/approve", json=approval)).status_code == 202
        assert (await http.post(path + "/approve", json=approval)).status_code == 200
        assert (await h.ticketing.get("/__effects")).json() == {}
        lease = await h.store.claim(w.workflow_id, "busy")
        assert (
            await http.post(path + "/cancel", json={"expected_revision": lease.workflow.revision})
        ).status_code == 409
        await h.store.release(lease)
        await h.tick(w)
        w = await h.get(w)
        response = await http.post(path + "/cancel", json={"expected_revision": w.revision})
        assert response.status_code == 200
        assert response.json()["state"] == "CANCELLED"
        assert response.json()["actions"][0]["result"]["id"]
        assert "lease_token" not in response.json()
        assert (await http.post(path + "/cancel", json={"expected_revision": 0})).status_code == 200


async def test_readiness_degrades_without_losing_admission(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(h)), base_url="http://api"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            response = await client.get("/health/ready")
            assert response.status_code == 200
            assert response.json()["status"] == "degraded"


async def test_mongo_down_rejects_admission_without_claiming_durability(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)

        async def unavailable(*args):
            raise ServerSelectionTimeoutError("mongodb://user:secret@unavailable")

        h.store.insert_or_get = unavailable
        h.store.ping = unavailable
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(h)),
            base_url="http://api",
            headers={"X-API-Key": "a-key", "Idempotency-Key": "key"},
        ) as client:
            response = await client.post("/workflows", json={"text": REQUEST})
            assert response.status_code == 503
            assert "secret" not in response.text
            assert (await client.get("/health/ready")).status_code == 503


@pytest.mark.integration
async def test_real_store_approval_cancellation_race_has_one_winner(mongo_store, tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack, store=mongo_store)
        w = await h.admit()
        await h.tick(w)
        w = await h.get(w)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(h)),
            base_url="http://api",
            headers={"X-API-Key": "a-key"},
        ) as client:
            path = f"/workflows/{w.workflow_id}"
            responses = await asyncio.gather(
                client.post(
                    path + "/approve",
                    json={"expected_revision": w.revision, "plan_hash": w.plan_hash},
                ),
                client.post(path + "/cancel", json={"expected_revision": w.revision}),
            )
            assert sum(response.status_code in {200, 202} for response in responses) == 1
            assert sum(response.status_code == 409 for response in responses) == 1
            assert (await h.get(w)).state in {"READY", "CANCELLED"}
            assert (await h.ticketing.get("/__effects")).json() == {}

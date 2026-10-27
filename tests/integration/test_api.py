from contextlib import AsyncExitStack

import httpx

from tests.support import REQUEST, harness
from workflow_engine.api import create_app
from workflow_engine.policy import CatalogProvider
from workflow_engine.settings import Settings


def app_for(h):
    return create_app(Settings(api_keys={"a-key": "tenant-a", "b-key": "tenant-b"}), h.store, CatalogProvider.from_file("fixtures/catalog.jsonl"))


async def test_admission_isolation_replay_and_body_limits(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        http = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(h)), base_url="http://api"))
        headers = {"X-API-Key": "a-key", "Idempotency-Key": "key"}
        response = await http.post("/workflows", headers=headers, json={"text": REQUEST})
        assert response.status_code == 202
        workflow_id = response.json()["workflow_id"]
        assert (await http.post("/workflows", headers=headers, json={"text": REQUEST})).status_code == 200
        assert (await http.post("/workflows", headers=headers, json={"text": "changed"})).status_code == 409
        assert (await http.post("/workflows", headers={"X-API-Key": "a-key"}, json={"text": REQUEST})).status_code == 400
        assert (await http.get(f"/workflows/{workflow_id}", headers={"X-API-Key": "b-key"})).status_code == 404
        assert (await http.get(f"/workflows/{workflow_id}")).status_code == 401
        assert (await http.post("/workflows", headers=headers, json={"text": REQUEST, "tenant_id": "tenant-b"})).status_code == 422
        assert (await http.post("/workflows", headers=headers, content=b"x" * 16385)).status_code == 413
        assert (await h.ticketing.get("/__effects")).json() == {}


async def test_exact_approval_replay_and_cancellation_race(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await h.admit()
        await h.tick(w)
        w = await h.get(w)
        http = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(h)), base_url="http://api", headers={"X-API-Key": "a-key"}))
        path = f"/workflows/{w.workflow_id}"
        approval = {"expected_revision": w.revision, "plan_hash": w.plan_hash}
        assert (await http.post(path + "/approve", json={**approval, "plan_hash": "0" * 64})).status_code == 409
        assert (await http.post(path + "/approve", json={**approval, "expected_revision": 0})).status_code == 409
        assert (await http.post(path + "/approve", json=approval)).status_code == 202
        assert (await http.post(path + "/approve", json=approval)).status_code == 200
        assert (await h.ticketing.get("/__effects")).json() == {}
        lease = await h.store.claim(w.workflow_id, "busy")
        assert (await http.post(path + "/cancel", json={"expected_revision": lease.workflow.revision})).status_code == 409
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
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(h)), base_url="http://api") as client:
            assert (await client.get("/health/live")).status_code == 200
            response = await client.get("/health/ready")
            assert response.status_code == 200
            assert response.json()["status"] == "degraded"

import asyncio

import httpx

from external_simulators.inventory import create_app as inventory_app
from external_simulators.ledger import Ledger
from external_simulators.ticketing import create_app as ticket_app


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://simulator")


async def test_twenty_concurrent_keys_one_ticket_and_payload_conflict(tmp_path):
    path = tmp_path / "tickets.sqlite"
    payload = {
        "logical_action_id": "workflow/ticket",
        "asset_id": "P-17",
        "priority": "normal",
        "description": "inspect",
    }
    async with client(ticket_app(path)) as http:
        results = await asyncio.gather(
            *(
                http.post("/tickets", json=payload, headers={"Idempotency-Key": "same"})
                for _ in range(20)
            )
        )
        assert {r.status_code for r in results} == {200}
        assert len({r.json()["result"]["id"] for r in results}) == 1
        changed = await http.post(
            "/tickets",
            json={**payload, "description": "other"},
            headers={"Idempotency-Key": "same"},
        )
        assert changed.status_code == 409
    async with client(ticket_app(path)) as restarted:
        outcome = (await restarted.get("/operations/same")).json()
        assert outcome["status"] == "CONFIRMED"
        assert outcome["outcome"]["result"]["id"] == results[0].json()["result"]["id"]
    assert Ledger(path).effect_counts() == {"workflow/ticket": 1}


async def test_stock_decrement_and_stable_skip(tmp_path):
    path = tmp_path / "stock.sqlite"
    app = inventory_app(path, initial_stock={"SEAL-1": 1})
    payload = {
        "logical_action_id": "workflow/reservation",
        "part_id": "SEAL-1",
        "quantity": 1,
        "ticket_id": "t1",
    }
    async with client(app) as http:
        responses = await asyncio.gather(
            *(
                http.post("/reservations", json=payload, headers={"Idempotency-Key": "same"})
                for _ in range(20)
            )
        )
        assert {r.json()["status"] for r in responses} == {"CONFIRMED"}
        assert Ledger(path).stock("SEAL-1") == 0
        skipped = await http.post(
            "/reservations",
            json={**payload, "logical_action_id": "next/reservation"},
            headers={"Idempotency-Key": "next"},
        )
        assert skipped.json()["status"] == "SKIPPED"
        assert (await http.get("/operations/next")).json()["outcome"]["status"] == "SKIPPED"


async def test_post_commit_failure_and_disabled_fault_controls(tmp_path):
    path = tmp_path / "tickets.sqlite"
    payload = {
        "logical_action_id": "workflow/ticket",
        "asset_id": "P-17",
        "priority": "normal",
        "description": "inspect",
    }
    async with client(ticket_app(path, enable_faults=True)) as http:
        assert (
            await http.put("/__faults", json={"mode": "after_commit", "remaining": 1})
        ).status_code == 200
        assert (
            await http.post("/tickets", json=payload, headers={"Idempotency-Key": "key"})
        ).status_code == 503
        assert (await http.get("/operations/key")).json()["status"] == "CONFIRMED"
    async with client(ticket_app(path)) as http:
        assert (await http.put("/__faults", json={"mode": "after_commit"})).status_code == 404


async def test_uncertain_precommit_failure_does_not_claim_proven_no_effect(tmp_path):
    async with client(ticket_app(tmp_path / "tickets.sqlite", enable_faults=True)) as http:
        assert (
            await http.put("/__faults", json={"mode": "unknown_before_commit"})
        ).status_code == 200
        payload = {
            "logical_action_id": "w/ticket",
            "asset_id": "P-17",
            "priority": "normal",
            "description": "inspect",
        }
        response = await http.post(
            "/tickets", json=payload, headers={"Idempotency-Key": "uncertain"}
        )
        assert response.status_code == 503
        assert response.json()["category"] == "unknown"
        assert (await http.get("/operations/uncertain")).json()["status"] == "NOT_FOUND"

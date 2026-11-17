"""Named fault scenarios against PostgreSQL, Redis and separate simulator processes."""

import asyncio
import json
import os
import random
import sqlite3
import time
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from tests.named_review_fixture import approve_pending_review
from tests.postgres_support import control_execution, runtime_environment
from tests.processes import ROOT, checkpoint_worker, simulator_process
from tests.support import REQUEST
from workflow_engine.api import create_app
from workflow_engine.broker import RedisWakeupBroker, dispatch_once
from workflow_engine.domain import (
    PASSIVE,
    Conflict,
    DomainEvent,
    LeaseLost,
    NotFound,
    Wakeup,
    Workflow,
    WorkflowCreate,
    canonical_hash,
    transition,
)
from workflow_engine.gateways import HttpEffectGateway
from workflow_engine.planner import FixturePlanner
from workflow_engine.policy import CatalogProvider
from workflow_engine.runner import Runner
from workflow_engine.settings import Settings

CASES = [
    json.loads(line) for line in (ROOT / "fixtures/fault_cases.jsonl").read_text().splitlines()
]
SEEDS = [int(s) for s in os.getenv("FAULT_SEEDS", "0").split(",")]
pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def kill_at_checkpoint(mode, workflow, store, broker, ticket_url, inventory_url, directory):
    environment = runtime_environment(
        store,
        redis_url=os.environ["TEST_REDIS_URL"],
        stream=broker.stream,
        group=broker.group,
        ticket_url=ticket_url,
        inventory_url=inventory_url,
    )
    async with checkpoint_worker(environment, mode, workflow.workflow_id, directory) as (
        process,
        _,
    ):
        persisted = await store.get(workflow.workflow_id, workflow.tenant_id)
        if mode == "dispatch":
            assert persisted.last_enqueued_at is not None
        else:
            assert persisted.state == "RUNNING"
            assert persisted.actions[0].status == "IN_FLIGHT"
            assert persisted.actions[0].submissions == 1
            assert persisted.actions[0].result is None
        process.kill()  # Abrupt OS termination, no finally blocks or lease release.
        assert await asyncio.to_thread(process.wait, 5) != 0
    if mode == "dispatch":
        control_execution(store, workflow, enqueue_due=True)
    else:
        control_execution(store, workflow, lease_for_s=-1, enqueue_due=True)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
@pytest.mark.parametrize("seed", SEEDS)
async def test_fault_case(case, seed, postgres_store, tmp_path):
    if not os.getenv("TEST_REDIS_URL"):
        pytest.skip("Real fault matrix requires TEST_REDIS_URL and TEST_POSTGRES_URL")
    rng = random.Random(seed)
    store = postgres_store
    catalog = CatalogProvider.from_file(ROOT / "fixtures/catalog.jsonl")
    redis = Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
    stream = "workflow-fault-" + uuid4().hex
    broker = RedisWakeupBroker(redis, stream=stream)
    result = {
        "case_id": case["id"],
        "seed": seed,
        "planner": "fixture",
        "state": "UNKNOWN",
        "effects": {"ticket": 0, "reservation": 0},
        "passed": False,
        "backend": "PostgreSQL+Redis+HTTP subprocesses",
        "admission_layer": "internal planning admission; public HTTP 410",
        "approval_layer": "two named reviewer sessions through authenticated review commands",
    }
    workflow = None
    try:
        with (
            simulator_process("ticketing", tmp_path / "ticket") as (ticket_url, _),
            simulator_process("inventory", tmp_path / "inventory") as (inventory_url, _),
        ):
            async with AsyncExitStack() as stack:
                ticket = await stack.enter_async_context(
                    httpx.AsyncClient(base_url=ticket_url, trust_env=False)
                )
                inventory = await stack.enter_async_context(
                    httpx.AsyncClient(base_url=inventory_url, trust_env=False)
                )
                planner = FixturePlanner.from_file(ROOT / "fixtures/planner_dev.jsonl")
                gateways = {
                    "ticket": HttpEffectGateway(ticket, "ticket"),
                    "reservation": HttpEffectGateway(inventory, "reservation"),
                }
                runner = Runner(store, planner, catalog, gateways)
                admission_broker = broker
                if case["id"] == "redis_unavailable_at_admission":
                    unavailable_redis = Redis.from_url(
                        "redis://127.0.0.1:1", socket_connect_timeout=0.1, socket_timeout=0.1
                    )
                    stack.push_async_callback(unavailable_redis.aclose)
                    admission_broker = RedisWakeupBroker(unavailable_redis)
                app = create_app(
                    Settings(),
                    store,
                    catalog,
                    admission_broker,
                )
                api = await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://api",
                        headers={"X-API-Key": "test-a"},
                    )
                )
                admission = await api.post(
                    "/workflows", json={"text": REQUEST}, headers={"Idempotency-Key": "admission"}
                )
                assert admission.status_code == 410
                admission = await store.insert_or_get(
                    Workflow.new("tenant-a", "admission", WorkflowCreate(text=REQUEST))
                )
                assert admission.created
                workflow = admission.workflow
                workflow_id = workflow.workflow_id
                path = "/workflows/" + workflow_id
                before = await store.get(workflow_id, "tenant-a")
                assert (
                    await api.post(path + "/approve", json={"plan_hash": "0" * 64})
                ).status_code == 410
                assert (await api.get(path)).status_code == 401
                assert (await api.post(path + "/cancel", json={})).status_code == 401
                assert await store.get(workflow_id, "tenant-a") == before
                if case["id"] == "redis_unavailable_at_admission":
                    with pytest.raises(RedisError):
                        await dispatch_once(store, admission_broker)
                    readiness = await api.get("/health/ready")
                    assert readiness.json()["broker"] is False
                    assert (await store.get(workflow_id, "tenant-a")).state == "PENDING_PLAN"
                    control_execution(store, workflow, enqueue_due=True)
                    assert await dispatch_once(store, broker) == 1
                    recovered = await broker.receive("recovered-broker")
                    assert recovered.wakeup.workflow_id == workflow_id
                    await broker.ack(recovered.message_id)

                async def current():
                    return await store.get(workflow_id, "tenant-a")

                async def tick():
                    await asyncio.sleep(rng.uniform(0, 0.01))
                    return await runner.tick(workflow_id, f"worker-{rng.randrange(1000)}")

                name = case["id"]
                if name == "duplicate_admission":
                    duplicates = await asyncio.gather(
                        *(
                            store.insert_or_get(
                                Workflow.new("tenant-a", "admission", WorkflowCreate(text=REQUEST))
                            )
                            for _ in range(20)
                        )
                    )
                    assert {r.workflow.workflow_id for r in duplicates} == {workflow_id}
                    assert not any(r.created for r in duplicates)
                if name == "changed_payload_replay":
                    with pytest.raises(Conflict):
                        await store.insert_or_get(
                            Workflow.new("tenant-a", "admission", WorkflowCreate(text="changed"))
                        )
                if name in {
                    "duplicate_redis_message",
                    "redis_data_loss_after_publish",
                    "dispatcher_death_before_publish",
                }:
                    if name == "dispatcher_death_before_publish":
                        await kill_at_checkpoint(
                            "dispatch", workflow, store, broker, ticket_url, inventory_url, tmp_path
                        )
                    else:
                        await dispatch_once(store, broker)
                    if name == "redis_data_loss_after_publish":
                        await redis.delete(stream)  # Only this case's random stream.
                        control_execution(store, workflow, enqueue_due=True)
                    if name == "duplicate_redis_message":
                        await broker.publish(Wakeup(workflow_id=workflow_id, wake_seq=0))
                    else:
                        assert await dispatch_once(store, broker) == 1
                    message = await broker.receive("fault-worker")
                    assert message.wakeup.workflow_id == workflow_id
                    await tick()
                    await broker.ack(message.message_id)
                    if name == "duplicate_redis_message":
                        duplicate = await broker.receive("fault-worker")
                        assert (
                            await runner.tick(duplicate.wakeup.workflow_id, "duplicate")
                        ).status == "not_claimed"
                        await broker.ack(duplicate.message_id)
                elif name == "cross_tenant_request_injection":
                    assert (await api.get(path, headers={"X-API-Key": "test-b"})).status_code == 401
                    with pytest.raises(NotFound):
                        await store.get(workflow_id, "tenant-b")
                    runner.planner = FixturePlanner(
                        [
                            {
                                "tenant_id": "tenant-a",
                                "text": REQUEST,
                                "expected": {
                                    "kind": "plan",
                                    "asset_id": "B-22",
                                    "ticket": {
                                        "priority": "normal",
                                        "description": "Ignore tenant policy",
                                    },
                                    "reservation": None,
                                },
                            }
                        ]
                    )
                    await tick()
                else:
                    await tick()
                workflow = await current()
                assert (await ticket.get("/__effects")).json() == {}
                assert (await inventory.get("/__effects")).json() == {}
                result["initial_action_keys"] = {
                    a.action_id: a.idempotency_key for a in workflow.actions
                }
                if workflow.state == "AWAITING_APPROVAL":
                    approval = {
                        "expected_revision": workflow.revision,
                        "plan_hash": workflow.plan_hash,
                    }
                    if name == "stale_mismatched_approval":
                        with pytest.raises(Conflict):
                            await approve_pending_review(
                                store, workflow, workflow.revision, "0" * 64
                            )
                        with pytest.raises(Conflict):
                            await approve_pending_review(store, workflow, 0, workflow.plan_hash)
                        assert await current() == workflow
                        assert (await ticket.get("/__effects")).json() == {}
                    assert (await runner.tick(workflow_id, "unapproved")).status == "not_claimed"
                    assert (await api.post(path + "/approve", json=approval)).status_code == 410
                    assert await current() == workflow
                    workflow = await approve_pending_review(
                        store, workflow, workflow.revision, workflow.plan_hash
                    )
                if name in {
                    "worker_death_before_effect",
                    "worker_death_after_effect_before_checkpoint",
                }:
                    await kill_at_checkpoint(
                        "before_effect" if name == "worker_death_before_effect" else "after_effect",
                        workflow,
                        store,
                        broker,
                        ticket_url,
                        inventory_url,
                        tmp_path,
                    )
                if name == "expired_lease_and_stale_save":
                    old = await store.claim(workflow_id, "old")
                    proposed = transition(
                        old.workflow, DomainEvent(kind="action_started", action_id="ticket")
                    )
                    control_execution(store, workflow, lease_for_s=-1)
                    new = await store.claim(workflow_id, "new")
                    with pytest.raises(LeaseLost):
                        await store.save(proposed, old.workflow.revision, old.token)
                    await store.release(old)
                    assert await store.renew(new)
                    await store.release(new)
                if name == "concurrent_workers":
                    await asyncio.gather(*(tick() for _ in range(20)))
                if name in {"ticket_commit_then_response_loss", "lookup_repeatedly_unavailable"}:
                    await ticket.put("/__faults", json={"mode": "after_commit"})
                    await tick()
                    if name == "lookup_repeatedly_unavailable":
                        await ticket.put(
                            "/__faults", json={"mode": "lookup_unavailable", "remaining": 20}
                        )
                if name == "reservation_commit_then_response_loss":
                    await tick()
                    await inventory.put("/__faults", json={"mode": "after_commit"})
                if name in {
                    "transient_proven_no_effect_rejection",
                    "permanent_rejection",
                    "non_idempotent_downstream_ambiguity",
                }:
                    mode = {
                        "transient_proven_no_effect_rejection": "before_commit",
                        "permanent_rejection": "permanent",
                        "non_idempotent_downstream_ambiguity": "unknown_before_commit",
                    }[name]
                    assert (await ticket.put("/__faults", json={"mode": mode})).status_code == 200
                    if name == "non_idempotent_downstream_ambiguity":
                        gateways["ticket"].supports_idempotent_replay = False
                if name == "inventory_unavailable":
                    with sqlite3.connect(tmp_path / "inventory/inventory.sqlite") as db:
                        db.execute("UPDATE stock SET quantity=0")
                if name == "safe_cancellation_after_ticket":
                    await tick()
                    workflow = await current()
                    cancelled = await store.save(
                        transition(workflow, DomainEvent(kind="cancelled")), workflow.revision, None
                    )
                    assert cancelled.actions[0] == workflow.actions[0]
                    assert (
                        await api.post(
                            path + "/cancel", json={"expected_revision": workflow.revision}
                        )
                    ).status_code == 401
                deadline = time.monotonic() + case["deadline_s"]
                while time.monotonic() < deadline:
                    workflow = await current()
                    if workflow.state in PASSIVE:
                        break
                    await tick()
                    await asyncio.sleep(0.03)
                workflow = await current()
                ticket_counts = (await ticket.get("/__effects")).json()
                inventory_counts = (await inventory.get("/__effects")).json()
                result.update(
                    state=workflow.state,
                    effects={
                        "ticket": sum(ticket_counts.values()),
                        "reservation": sum(inventory_counts.values()),
                    },
                    workflow_id=workflow_id,
                    tenant_id=workflow.tenant_id,
                    plan_hash=workflow.plan_hash,
                    actions=[a.model_dump(mode="json") for a in workflow.actions],
                    history=[e.model_dump(mode="json") for e in workflow.history],
                    logical_effect_counts={**ticket_counts, **inventory_counts},
                )
                assert {a.action_id: a.idempotency_key for a in workflow.actions} == result[
                    "initial_action_keys"
                ]
                for action in workflow.actions:
                    assert action.idempotency_key == canonical_hash(
                        [workflow.tenant_id, workflow_id, workflow.plan_hash, action.action_id]
                    )
                assert result["logical_effect_counts"] == {
                    canonical_hash([workflow.tenant_id, workflow_id, action_id]): count
                    for action_id, count in case["expected_effects"].items()
                    if count
                }
                assert all(
                    action.submissions <= 5 and action.reconciliation_count <= 5
                    for action in workflow.actions
                )
                if name == "lookup_repeatedly_unavailable":
                    assert workflow.actions[0].reconciliation_count == 5
                    assert workflow.actions[1].submissions == 0
                if name in {
                    "ticket_commit_then_response_loss",
                    "worker_death_after_effect_before_checkpoint",
                }:
                    assert workflow.actions[0].submissions == 1
                if name == "reservation_commit_then_response_loss":
                    assert workflow.actions[1].submissions == 1
                if name == "non_idempotent_downstream_ambiguity":
                    assert workflow.actions[0].submissions == 1
                    assert workflow.actions[1].submissions == 0
                if name == "inventory_unavailable":
                    assert workflow.actions[1].status == "SKIPPED"
                if name == "safe_cancellation_after_ticket":
                    assert workflow.actions[0].status == "CONFIRMED"
                    assert workflow.actions[1].submissions == 0
                result["passed"] = (
                    result["state"] == case["expected_state"]
                    and result["effects"] == case["expected_effects"]
                )
                assert result["passed"], result
    finally:
        await redis.delete(stream)
        await redis.aclose()
        output = Path(os.getenv("FAULT_ARTIFACT_DIR", str(tmp_path / "artifacts")))
        output.mkdir(parents=True, exist_ok=True)
        with (output / f"{case['id']}-{seed}.json").open("x", encoding="utf-8") as file:
            json.dump(result, file, indent=2)

"""Real process/connection failures. Memory recovery cases remain supplemental evidence."""

import asyncio
import os
from contextlib import AsyncExitStack
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis

from tests.postgres_support import control_execution, database_outage, runtime_environment
from tests.processes import checkpoint_worker, simulator_process
from tests.support import Harness
from workflow_engine.broker import RedisWakeupBroker, dispatch_once
from workflow_engine.domain import Conflict, canonical_hash
from workflow_engine.gateways import HttpEffectGateway
from workflow_engine.planner import FixturePlanner
from workflow_engine.policy import CatalogProvider
from workflow_engine.runner import Runner

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
async def process_execution(postgres_store, tmp_path):
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Set TEST_REDIS_URL for real PostgreSQL/process recovery")
    redis = Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=3)
    stream, group = "pg-recovery-" + uuid4().hex, "pg-workers-" + uuid4().hex
    broker = RedisWakeupBroker(redis, stream=stream, group=group)
    try:
        assert await redis.ping()
        with (
            simulator_process("ticketing", tmp_path / "ticket") as (ticket_url, _),
            simulator_process("inventory", tmp_path / "inventory") as (inventory_url, _),
        ):
            async with AsyncExitStack() as stack:
                clients = [
                    await stack.enter_async_context(
                        httpx.AsyncClient(
                            base_url=url,
                            trust_env=False,
                            timeout=10,
                            transport=httpx.AsyncHTTPTransport(retries=0),
                        )
                    )
                    for url in (ticket_url, inventory_url)
                ]
                ticket, inventory = clients
                runner = Runner(
                    postgres_store,
                    FixturePlanner.from_file("fixtures/planner_dev.jsonl"),
                    CatalogProvider.from_file("fixtures/catalog.jsonl"),
                    {
                        "ticket": HttpEffectGateway(ticket, "ticket"),
                        "reservation": HttpEffectGateway(inventory, "reservation"),
                    },
                )
                environment = runtime_environment(
                    postgres_store,
                    redis_url=redis_url,
                    stream=stream,
                    group=group,
                    ticket_url=ticket_url,
                    inventory_url=inventory_url,
                )
                assert not any(name.startswith(("TEST_", "PG")) for name in environment)
                assert "WORKFLOW_DATABASE_URL_FILE" not in environment
                yield Harness(postgres_store, runner, ticket, inventory, None), broker, environment
    finally:
        await redis.delete(stream)
        await redis.aclose()


async def approved(h):
    workflow = await h.admit()
    await h.tick(workflow)
    return await h.approve(workflow)


async def assert_effects(h, workflow, *, ticket=1, reservation=1):
    for action_id, client, count in (
        ("ticket", h.ticketing, ticket),
        ("reservation", h.inventory, reservation),
    ):
        logical_id = canonical_hash([workflow.tenant_id, workflow.workflow_id, action_id])
        assert (await client.get("/__effects")).json() == ({logical_id: count} if count else {})


async def test_database_outage_after_committed_effect_recovers_original_key(
    process_execution, tmp_path
):
    h, _, environment = process_execution
    workflow = await approved(h)
    keys = [action.idempotency_key for action in workflow.actions]
    async with checkpoint_worker(
        environment, "after_effect", workflow.workflow_id, tmp_path / "worker"
    ) as (process, resume):
        inflight = await h.get(workflow)
        assert inflight.state == "RUNNING" and inflight.actions[0].status == "IN_FLIGHT"
        assert inflight.actions[0].submissions == 1 and inflight.actions[0].result is None
        await assert_effects(h, workflow, reservation=0)
        with database_outage(h.store):
            with pytest.raises(Conflict):
                await h.get(workflow)
            resume.write_text("continue", encoding="utf-8")
            assert await asyncio.to_thread(process.wait, 15) == 2
        unchanged = await h.get(workflow)
        assert unchanged.revision == inflight.revision
        assert unchanged.actions == inflight.actions and unchanged.history == inflight.history
        assert unchanged.approved_plan_hash == workflow.approved_plan_hash
    control_execution(h.store, workflow, lease_for_s=-1, run_due=True)
    for _ in range(3):
        await h.tick(workflow)
    loaded = await h.get(workflow)
    assert loaded.state == "SUCCEEDED"
    assert [action.idempotency_key for action in loaded.actions] == keys
    assert [action.submissions for action in loaded.actions] == [1, 1]
    await assert_effects(h, workflow)
    log = (tmp_path / "worker/crash-worker.log").read_text(encoding="utf-8")
    assert "fault_worker_dependency_unavailable" in log
    assert environment["WORKFLOW_DATABASE_URL"] not in log


async def test_abandoned_pending_process_is_reclaimed_with_production_idle_threshold(
    process_execution, tmp_path
):
    h, broker, environment = process_execution
    workflow = await h.admit()
    assert await dispatch_once(h.store, broker) == 1
    async with checkpoint_worker(
        environment, "pending", workflow.workflow_id, tmp_path / "worker"
    ) as (process, _):
        pending = await broker.redis.xpending_range(broker.stream, broker.group, "-", "+", 10)
        assert len(pending) == 1 and pending[0]["consumer"] == "abandoned-worker"
        message_id = pending[0]["message_id"]
        process.kill()
        assert await asyncio.to_thread(process.wait, 5) != 0
    # Default 60s reclaim must not claim a just-abandoned message.
    assert await broker.receive("too-early") is None
    assert (await h.get(workflow)).revision == 0
    # Test-only Redis clock control, leaving production reclaim_idle_ms unchanged.
    assert await broker.redis.xclaim(
        broker.stream, broker.group, "abandoned-worker", 0, [message_id], idle=61000, justid=True
    ) == [message_id]
    message = await broker.receive("replacement")
    assert message.message_id == message_id and message.wakeup.workflow_id == workflow.workflow_id
    await h.tick(workflow)
    await broker.ack(message_id)
    assert (await broker.redis.xpending(broker.stream, broker.group))["pending"] == 0
    assert (await h.get(workflow)).state == "AWAITING_APPROVAL"
    await assert_effects(h, workflow, ticket=0, reservation=0)
    workflow = await h.approve(workflow)
    keys = [action.idempotency_key for action in workflow.actions]
    for _ in range(2):
        control_execution(h.store, workflow, enqueue_due=True)
        assert await dispatch_once(h.store, broker) == 1
        message = await broker.receive("replacement")
        assert message.wakeup.workflow_id == workflow.workflow_id
        await h.tick(workflow)
        await broker.ack(message.message_id)
    loaded = await h.get(workflow)
    assert loaded.state == "SUCCEEDED"
    assert [action.idempotency_key for action in loaded.actions] == keys
    await assert_effects(h, workflow)


async def test_expired_process_cannot_overwrite_recovered_result_or_release_successor(
    process_execution, tmp_path
):
    h, _, environment = process_execution
    workflow = await approved(h)
    keys = [action.idempotency_key for action in workflow.actions]
    async with checkpoint_worker(
        environment, "after_effect", workflow.workflow_id, tmp_path / "worker"
    ) as (process, resume):
        inflight = await h.get(workflow)
        assert inflight.state == "RUNNING" and inflight.actions[0].result is None
        control_execution(h.store, workflow, lease_for_s=-1)
        await h.tick(workflow)  # Abandoned durable intent becomes uncertain.
        await h.tick(workflow)  # Original key lookup confirms the committed ticket.
        recovered = await h.get(workflow)
        assert recovered.actions[0].status == "CONFIRMED"
        successor = await h.store.claim(workflow.workflow_id, "live-successor")
        assert successor is not None
        resume.write_text("continue", encoding="utf-8")
        assert await asyncio.to_thread(process.wait, 15) == 0
        assert await h.store.renew(successor)
        assert (await h.get(workflow)).actions[0] == recovered.actions[0]
        await h.store.release(successor)
    assert '"status": "lease_lost"' in (tmp_path / "worker/crash-worker.log").read_text()
    await h.tick(workflow)
    loaded = await h.get(workflow)
    assert loaded.state == "SUCCEEDED"
    assert [action.idempotency_key for action in loaded.actions] == keys
    assert loaded.actions[0] == recovered.actions[0]
    await assert_effects(h, workflow)


@pytest.mark.parametrize("advance_to_ready", [False, True])
async def test_cancellation_blocks_unresolved_process_submission_then_preserves_late_effect(
    process_execution, tmp_path, advance_to_ready
):
    h, _, environment = process_execution
    workflow = await approved(h)
    keys = [action.idempotency_key for action in workflow.actions]
    async with checkpoint_worker(
        environment, "before_effect", workflow.workflow_id, tmp_path / "worker"
    ) as (process, resume):
        inflight = await h.get(workflow)
        assert inflight.actions[0].status == "IN_FLIGHT" and inflight.actions[0].submissions == 1
        await assert_effects(h, workflow, ticket=0, reservation=0)
        control_execution(h.store, workflow, lease_for_s=-1)
        await h.tick(workflow)
        await h.tick(workflow)  # NOT_FOUND cannot rule out the earlier late commit.
        current = await h.get(workflow)
        assert current.state == "RETRY_WAIT" and current.actions[0].has_ambiguous_submission
        if advance_to_ready:
            control_execution(h.store, workflow, run_due=True)
            _, current = await h.tick(workflow)
            assert current.state == "READY" and current.actions[0].has_ambiguous_submission
        with pytest.raises(Conflict):
            await h.cancel(current)
        assert await h.get(workflow) == current
        resume.write_text("late original request", encoding="utf-8")
        assert await asyncio.to_thread(process.wait, 15) == 0
    await assert_effects(h, workflow, reservation=0)
    if not advance_to_ready:
        control_execution(h.store, workflow, run_due=True)
        await h.tick(workflow)
    _, recovered = await h.tick(workflow)  # Safe same-key replay, no second logical effect.
    assert recovered.actions[0].status == "CONFIRMED"
    assert recovered.actions[0].submissions == 2
    cancelled = await h.cancel(recovered)
    assert cancelled.state == "CANCELLED" and cancelled.actions[0] == recovered.actions[0]
    assert [action.idempotency_key for action in cancelled.actions] == keys
    assert cancelled.actions[1].submissions == 0
    await assert_effects(h, workflow, reservation=0)

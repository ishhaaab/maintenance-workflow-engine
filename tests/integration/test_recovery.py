from contextlib import AsyncExitStack

import pytest

from tests.support import harness
from workflow_engine.gateways import EffectResult


async def ready(h):
    w = await h.admit()
    await h.tick(w)
    return await h.approve(w)


@pytest.mark.parametrize("action_id", ["ticket", "reservation"])
async def test_commit_then_lost_response_reconciles_without_resubmission(
    tmp_path, clock, action_id
):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await ready(h)
        if action_id == "reservation":
            await h.tick(w)
        client = h.ticketing if action_id == "ticket" else h.inventory
        await client.put("/__faults", json={"mode": "after_commit"})
        _, w = await h.tick(w)
        key = next(a.idempotency_key for a in w.actions if a.action_id == action_id)
        assert w.state == "RECONCILING"
        _, w = await h.tick(w)
        action = next(a for a in w.actions if a.action_id == action_id)
        assert action.status == "CONFIRMED"
        assert action.submissions == 1
        assert action.idempotency_key == key
        assert sum((await client.get("/__effects")).json().values()) == 1


async def test_five_unavailable_lookups_stop_for_review(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await ready(h)
        await h.ticketing.put("/__faults", json={"mode": "after_commit"})
        await h.tick(w)
        await h.ticketing.put("/__faults", json={"mode": "lookup_unavailable", "remaining": 20})
        for _ in range(5):
            clock.advance(10)
            _, w = await h.tick(w)
        assert w.state == "NEEDS_REVIEW"
        assert w.actions[0].reconciliation_count == 5
        assert w.actions[1].submissions == 0
        assert (await h.tick(w))[0].status == "not_claimed"


async def test_five_known_no_effect_rejections_fail_with_zero_effects(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await ready(h)
        await h.ticketing.put("/__faults", json={"mode": "before_commit", "remaining": 20})
        for _ in range(12):
            clock.advance(10)
            _, w = await h.tick(w)
        assert w.state == "FAILED"
        assert w.actions[0].submissions == 5
        assert (await h.ticketing.get("/__effects")).json() == {}


class Crash(BaseException):
    pass


@pytest.mark.parametrize("after_effect", [False, True])
async def test_abandoned_inflight_recovery(tmp_path, clock, after_effect):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await ready(h)
        gateway = h.runner.gateways["ticket"]
        submit = gateway.submit

        async def crash(action):
            if after_effect:
                await submit(action)
            raise Crash()

        gateway.submit = crash
        with pytest.raises(Crash):
            await h.tick(w)
        gateway.submit = submit
        for _ in range(8):
            clock.advance(10)
            _, w = await h.tick(w)
        assert w.state == "SUCCEEDED"
        assert sum((await h.ticketing.get("/__effects")).json().values()) == 1
        assert w.actions[0].submissions == (1 if after_effect else 2)


async def test_non_idempotent_ambiguity_never_replays(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await ready(h)
        gateway = h.runner.gateways["ticket"]
        gateway.supports_idempotent_replay = False

        async def unknown(action):
            return EffectResult(status="unknown", error="lost")

        gateway.submit = unknown
        await h.tick(w)
        _, w = await h.tick(w)
        assert w.state == "NEEDS_REVIEW"
        assert w.actions[0].submissions == 1


async def test_expired_worker_cannot_checkpoint_after_new_owner_recovers(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await ready(h)
        gateway = h.runner.gateways["ticket"]
        submit = gateway.submit

        async def overlap(action):
            result = await submit(action)
            clock.advance(31)
            await h.runner.tick(w.workflow_id, "replacement")
            return result

        gateway.submit = overlap
        result, w = await h.tick(w)
        assert result.status == "lease_lost"
        gateway.submit = submit
        _, w = await h.tick(w)
        assert w.actions[0].status == "CONFIRMED"
        assert sum((await h.ticketing.get("/__effects")).json().values()) == 1


async def test_planner_budget_is_durable_and_bounded(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)

        async def timeout(request, catalog):
            raise TimeoutError()

        h.runner.planner.propose = timeout
        w = await h.admit()
        for _ in range(6):
            clock.advance(10)
            _, w = await h.tick(w)
        assert w.state == "FAILED"
        assert w.planning_attempts == 2


async def test_exhausted_ambiguous_replay_never_claims_definitive_failure(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await ready(h)
        gateway = h.runner.gateways["ticket"]

        async def unknown(action):
            return EffectResult(status="unknown", error="lost")

        gateway.submit = unknown
        for _ in range(30):
            clock.advance(10)
            _, w = await h.tick(w)
        assert w.state == "NEEDS_REVIEW"
        assert w.actions[0].submissions == 5

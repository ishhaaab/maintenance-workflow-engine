import json
import logging
from contextlib import AsyncExitStack

import pytest

from tests.support import harness
from workflow_engine.telemetry import JsonFormatter


@pytest.mark.parametrize("stock,reservation_status", [(100, "CONFIRMED"), (0, "SKIPPED")])
async def test_approval_gated_ticket_then_reservation(tmp_path, clock, stock, reservation_status):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack, stock=stock)
        w = await h.admit()
        _, w = await h.tick(w)
        assert w.state == "AWAITING_APPROVAL"
        assert (await h.ticketing.get("/__effects")).json() == {}
        assert (await h.tick(w))[0].status == "not_claimed"
        await h.approve(w)
        _, w = await h.tick(w)
        assert w.state == "READY"
        assert w.actions[0].status == "CONFIRMED"
        assert w.actions[1].submissions == 0
        _, w = await h.tick(w)
        assert w.state == "SUCCEEDED"
        assert w.actions[1].status == reservation_status
        assert sum((await h.ticketing.get("/__effects")).json().values()) == 1


async def test_lease_loss_before_submit_prevents_effect(tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await h.admit()
        await h.tick(w)
        await h.approve(w)
        real_save = h.store.save

        async def expire_after_intent(workflow, revision, token):
            saved = await real_save(workflow, revision, token)
            if saved.state == "RUNNING":
                clock.advance(31)
            return saved

        h.store.save = expire_after_intent
        result, w = await h.tick(w)
        assert result.status == "lease_lost"
        assert w.actions[0].status == "IN_FLIGHT"
        assert (await h.ticketing.get("/__effects")).json() == {}


async def test_runner_emits_committed_action_attempt_event(tmp_path, clock, caplog):
    caplog.set_level(logging.INFO, logger="workflow_engine.runner")
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        w = await h.admit()
        await h.tick(w)
        await h.approve(w)
        await h.tick(w)
        records = [json.loads(JsonFormatter().format(record)) for record in caplog.records]
        started = [r for r in records if r.get("state") == "RUNNING"]
        assert len(started) == 1
        assert started[0]["action_id"] == "ticket"
        assert started[0]["attempt"] == 1
        assert started[0]["workflow_id"] == w.workflow_id

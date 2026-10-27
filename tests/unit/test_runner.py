from contextlib import AsyncExitStack

import pytest

from tests.support import harness


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

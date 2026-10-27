from contextlib import AsyncExitStack

import pytest

from tests.support import harness


@pytest.mark.integration
async def test_real_store_simulator_happy_path(mongo_store, tmp_path, clock):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack, store=mongo_store)
        w = await h.admit()
        await h.tick(w)
        await h.approve(w)
        await h.tick(w)
        _, w = await h.tick(w)
        assert w.state == "SUCCEEDED"
        assert [a.submissions for a in w.actions] == [1, 1]

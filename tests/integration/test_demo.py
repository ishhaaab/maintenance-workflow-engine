import asyncio
from contextlib import AsyncExitStack

import httpx
import pytest

from scripts.demo import run_demo
from tests.integration.test_api import app_for
from tests.support import harness


@pytest.mark.parametrize("approve", [False, True])
async def test_demo_only_executes_after_explicit_opt_in(tmp_path, clock, approve):
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack)
        stop = asyncio.Event()

        async def worker():
            while not stop.is_set():
                for wakeup in await h.store.due_wakeups(republish_after_s=0):
                    await h.runner.tick(wakeup.workflow_id, "demo-worker")
                await asyncio.sleep(0.01)

        task = asyncio.create_task(worker())
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_for(h)),
                base_url="http://api",
                headers={"X-API-Key": "a-key"},
            ) as client:
                result = await run_demo(client, approve=approve, timeout_s=5)
                assert result["state"] == ("SUCCEEDED" if approve else "AWAITING_APPROVAL")
                assert sum((await h.ticketing.get("/__effects")).json().values()) == int(approve)
        finally:
            stop.set()
            await task

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from tests.integration.test_approval_execution import approval as approval
from tests.support import REQUEST

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def test_activation_and_submission_use_membership_before_cycle_lock_order(
    approval, monkeypatch
):
    from workflow_engine import review_commands as module
    from workflow_engine.domain import Conflict

    commands, admin, requester, reviewers, _, h = approval
    cycle = await commands.create_cycle(admin, "Concurrent activation")
    cycle = await commands.admit_requester(
        admin, cycle["id"], requester.membership_id, cycle["revision"]
    )
    item = await commands.create_item(requester, cycle["id"], cycle["revision"], uuid4())
    item = await commands.configure_chain(
        admin,
        item["id"],
        [{"reviewer_id": str(r.membership_id), "due_after_s": 3600} for r in reviewers],
        item["revision"],
    )
    item = await commands.submit_version(requester, item["id"], REQUEST, item["revision"], uuid4())
    await h.runner.tick(item["run_id"], "planner")
    item = await commands.get_item(requester, item["id"])
    cycle = await commands.get_cycle(admin, cycle["id"])
    paused, release, submitted_actor_locked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_member, original_actor = commands._member, module.lock_actor

    async def member(*args, **kwargs):
        if asyncio.current_task().get_name() == "activation" and not paused.is_set():
            paused.set()
            await release.wait()
        return await original_member(*args, **kwargs)

    async def actor(*args, **kwargs):
        result = await original_actor(*args, **kwargs)
        if asyncio.current_task().get_name() == "submission":
            submitted_actor_locked.set()
        return result

    monkeypatch.setattr(commands, "_member", member)
    monkeypatch.setattr(module, "lock_actor", actor)
    activation = asyncio.create_task(
        commands.activate_cycle(admin, cycle["id"], cycle["revision"]), name="activation"
    )
    await asyncio.wait_for(paused.wait(), 5)
    submission = asyncio.create_task(
        commands.submit_version(requester, item["id"], REQUEST, item["revision"], uuid4()),
        name="submission",
    )
    try:
        await asyncio.wait_for(submitted_actor_locked.wait(), 0.5)
    except TimeoutError:
        pass
    finally:
        release.set()
    results = await asyncio.wait_for(
        asyncio.gather(activation, submission, return_exceptions=True), 10
    )
    assert not any(isinstance(result, DBAPIError) for result in results)
    assert all(isinstance(result, (dict, Conflict)) for result in results)

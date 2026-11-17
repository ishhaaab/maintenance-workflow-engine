import asyncio
from contextlib import AsyncExitStack
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from tests.support import REQUEST, harness

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
async def approval(postgres_store, tmp_path, clock):
    from workflow_engine.auth import AuthService
    from workflow_engine.policy import CatalogProvider
    from workflow_engine.review_commands import ReviewCommands
    from workflow_engine.settings import Settings

    auth = AuthService(postgres_store.database, Settings(env="local", origin="http://localhost"))
    admin = await auth.bootstrap("approvals", "approval-admin", "a-safe-password-123")
    requester = await auth.create_account(
        admin, "approval-requester", "a-safe-password-123", "REQUESTER"
    )
    reviewers = [
        await auth.create_account(
            admin, f"approval-reviewer-{n}", "a-safe-password-123", "REVIEWER"
        )
        for n in range(2)
    ]
    catalog = (
        CatalogProvider.from_file("fixtures/catalog.jsonl")
        .get("tenant-a")
        .model_copy(update={"tenant_id": admin.tenant_id, "fixture_namespace": "tenant-a"})
    )
    commands = ReviewCommands(postgres_store.database, CatalogProvider([catalog]))
    cycle = await commands.create_cycle(admin, "Two-level inspection")
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
    async with AsyncExitStack() as stack:
        h = await harness(tmp_path, clock, stack, store=postgres_store)
        await h.runner.tick(item["run_id"], "planner")
        item = await commands.get_item(requester, item["id"])
        cycle = await commands.get_cycle(admin, cycle["id"])
        await commands.activate_cycle(admin, cycle["id"], cycle["revision"])
        yield commands, admin, requester, reviewers, item["id"], h


async def decision(commands, reviewer, item_id, kind="APPROVE", comment="", command_id=None):
    item = await commands.get_item(reviewer, item_id)
    return await commands.decide(
        reviewer,
        item_id,
        version_id=item["current_version_id"],
        level=item["progress"]["current_level"],
        plan_hash=item["plan_hash"],
        decision=kind,
        comment=comment,
        expected_revision=item["revision"],
        expected_progress_revision=item["progress"]["revision"],
        command_id=command_id or uuid4(),
    )


async def test_only_full_current_chain_authorizes_exact_plan_and_durable_due_work(approval):
    from workflow_engine.authorization import Denied
    from workflow_engine.domain import Conflict

    commands, admin, requester, reviewers, item_id, h = approval
    item = await commands.get_item(requester, item_id)
    run = await h.store.get(item["execution"]["workflow_id"], requester.tenant_id)
    assert run.state == "AWAITING_APPROVAL" and run.approved_plan_hash is None
    assert (await h.runner.tick(run.workflow_id, "unapproved")).status == "not_claimed"
    for wrong in (admin, requester, reviewers[1]):
        with pytest.raises((Denied, Conflict)):
            await decision(commands, wrong, item_id)
    first_key = uuid4()
    first = await decision(commands, reviewers[0], item_id, command_id=first_key)
    assert first["status"] == "PENDING"
    assert (await h.store.get(run.workflow_id, requester.tenant_id)).approved_plan_hash is None
    await decision(commands, reviewers[1], item_id)
    approved = await h.store.get(run.workflow_id, requester.tenant_id)
    assert approved.state == "READY" and approved.approved_plan_hash == run.plan_hash
    assert approved.actions == run.actions
    assert any(w.workflow_id == run.workflow_id for w in await h.store.due_wakeups())
    for _ in range(4):
        await h.runner.tick(run.workflow_id, "executor")
    assert (await h.store.get(run.workflow_id, requester.tenant_id)).state == "SUCCEEDED"
    assert (await commands.get_cycle(admin, item["cycle_id"]))["state"] == "COMPLETED"


async def test_changes_resubmission_restarts_levels_and_retains_old_decisions(approval):
    from workflow_engine.domain import Conflict

    commands, _, requester, reviewers, item_id, h = approval
    await decision(commands, reviewers[0], item_id)
    await decision(commands, reviewers[1], item_id, "REQUEST_CHANGES", "Please clarify priority.")
    old = await commands.get_item(requester, item_id)
    new = await commands.submit_version(requester, item_id, REQUEST, old["revision"], uuid4())
    assert new["current_version_id"] != old["current_version_id"]
    await h.runner.tick(new["run_id"], "planner")
    current = await commands.get_item(requester, item_id)
    assert current["progress"]["current_level"] == 1
    assert current["progress"]["status"] == "PENDING"
    assert len(current["versions"]) == 2
    assert len(current["versions"][0]["decisions"]) == 2
    assert current["versions"][0]["status"] == "SUPERSEDED"
    assert current["execution"]["approved_plan_hash"] is None
    with pytest.raises(Conflict):
        await commands.decide(
            reviewers[0],
            item_id,
            version_id=old["current_version_id"],
            level=1,
            plan_hash=old["plan_hash"],
            decision="APPROVE",
            comment="",
            expected_revision=old["revision"],
            expected_progress_revision=0,
            command_id=uuid4(),
        )
    await decision(commands, reviewers[0], item_id)
    await decision(commands, reviewers[1], item_id)


async def test_conflicting_concurrent_decisions_have_one_winner_and_exact_replay(approval):
    from workflow_engine.domain import Conflict
    from workflow_engine.version_models import Decision

    commands, _, _, reviewers, item_id, _ = approval
    item = await commands.get_item(reviewers[0], item_id)
    payload = dict(
        version_id=item["current_version_id"],
        level=1,
        plan_hash=item["plan_hash"],
        expected_revision=item["revision"],
        expected_progress_revision=item["progress"]["revision"],
        command_id=uuid4(),
        decision="APPROVE",
        comment="",
    )
    results = await asyncio.gather(
        commands.decide(reviewers[0], item_id, **payload),
        commands.decide(reviewers[0], item_id, **payload),
    )
    assert results[0] == results[1]
    with pytest.raises(Conflict):
        await commands.decide(
            reviewers[0], item_id, **{**payload, "decision": "REJECT", "comment": "No"}
        )
    async with commands.database.sessions() as session:
        assert (await session.execute(select(func.count()).select_from(Decision))).scalar_one() == 1


async def test_rejection_is_final_and_comments_are_required(approval):
    from workflow_engine.domain import Conflict

    commands, admin, requester, reviewers, item_id, _ = approval
    for comment in ("", "  ", "x" * 2001):
        with pytest.raises(Conflict):
            await decision(commands, reviewers[0], item_id, "REJECT", comment)
    result = await decision(
        commands, reviewers[0], item_id, "REJECT", "Not suitable for this cycle."
    )
    assert result["status"] == "REJECTED"
    item = await commands.get_item(requester, item_id)
    assert item["execution"]["approved_plan_hash"] is None
    assert (await commands.get_cycle(admin, item["cycle_id"]))["state"] == "COMPLETED"
    with pytest.raises(Conflict):
        await commands.submit_version(requester, item_id, REQUEST, item["revision"], uuid4())

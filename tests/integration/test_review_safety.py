"""Adversarial release acceptance beyond the happy-path browser journey."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from tests.integration.test_approval_execution import approval as approval
from tests.integration.test_approval_execution import decision
from tests.postgres_support import owner_session
from tests.support import REQUEST

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def draft_item(commands, admin, requester, reviewers):
    cycle = await commands.create_cycle(admin, "Safety acceptance")
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
    return cycle["id"], item


async def test_wrong_hash_stale_revisions_and_conflicting_commands_never_vote_twice(approval):
    from workflow_engine.domain import Conflict
    from workflow_engine.version_models import Decision

    commands, _, _, reviewers, item_id, _ = approval
    item = await commands.get_item(reviewers[0], item_id)
    payload = dict(
        version_id=item["current_version_id"],
        level=1,
        plan_hash=item["plan_hash"],
        decision="APPROVE",
        comment="",
        expected_revision=item["revision"],
        expected_progress_revision=item["progress"]["revision"],
        command_id=uuid4(),
    )
    for changed in (
        {"plan_hash": "0" * 64},
        {"expected_revision": item["revision"] - 1},
        {"expected_progress_revision": item["progress"]["revision"] - 1},
    ):
        with pytest.raises(Conflict):
            await commands.decide(reviewers[0], item_id, **(payload | changed))
    results = await asyncio.gather(
        commands.decide(reviewers[0], item_id, **payload),
        commands.decide(
            reviewers[0],
            item_id,
            **(payload | {"command_id": uuid4(), "decision": "REJECT", "comment": "Conflict"}),
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, Conflict) for result in results) == 1
    async with commands.database.sessions() as session:
        assert (await session.execute(select(func.count()).select_from(Decision))).scalar_one() == 1


async def test_final_decision_authorization_audit_and_due_intent_roll_back_together(
    approval, monkeypatch
):
    from workflow_engine import review_commands
    from workflow_engine.db_models import AuditRecord
    from workflow_engine.version_models import Decision

    commands, _, _, reviewers, item_id, h = approval
    await decision(commands, reviewers[0], item_id)
    before = await commands.get_item(reviewers[1], item_id)
    original = review_commands.append_event

    async def fail_audit(*args, **kwargs):
        if args[4] == "review_decided":
            raise RuntimeError("synthetic audit failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(review_commands, "append_event", fail_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        await decision(commands, reviewers[1], item_id)
    after = await commands.get_item(reviewers[1], item_id)
    assert after["revision"] == before["revision"] and after["progress"] == before["progress"]
    run = await h.store.get(after["execution"]["workflow_id"], reviewers[1].tenant_id)
    assert run.approved_plan_hash is None and run.next_run_at is None
    async with commands.database.sessions() as session:
        assert (await session.execute(select(func.count()).select_from(Decision))).scalar_one() == 1
        assert (
            await session.execute(
                select(func.count())
                .select_from(AuditRecord)
                .where(AuditRecord.event == "execution_authorized")
            )
        ).scalar_one() == 0


async def test_different_last_item_reviewers_cannot_miss_cycle_completion(approval):
    from workflow_engine.db_models import AuditRecord

    commands, admin, requester, reviewers, _, h = approval
    cycle_id, first = await draft_item(commands, admin, requester, [reviewers[0]])
    submitted = await commands.submit_version(
        requester, first["id"], REQUEST, first["revision"], uuid4()
    )
    await h.runner.tick(submitted["run_id"], "planner")
    cycle = await commands.get_cycle(admin, cycle_id)
    second = await commands.create_item(requester, cycle_id, cycle["revision"], uuid4())
    second = await commands.configure_chain(
        admin,
        second["id"],
        [{"reviewer_id": str(reviewers[1].membership_id), "due_after_s": 3600}],
        second["revision"],
    )
    submitted = await commands.submit_version(
        requester, second["id"], REQUEST, second["revision"], uuid4()
    )
    await h.runner.tick(submitted["run_id"], "planner")
    cycle = await commands.get_cycle(admin, cycle_id)
    await commands.activate_cycle(admin, cycle_id, cycle["revision"])
    outcomes = await asyncio.gather(
        decision(commands, reviewers[0], first["id"]),
        decision(commands, reviewers[1], second["id"]),
    )
    assert all(result["status"] == "APPROVED" for result in outcomes)
    assert (await commands.get_cycle(admin, cycle_id))["state"] == "COMPLETED"
    async with commands.database.sessions() as session:
        assert (
            await session.execute(
                select(func.count())
                .select_from(AuditRecord)
                .where(
                    AuditRecord.entity_id == UUID(cycle_id), AuditRecord.event == "cycle_completed"
                )
            )
        ).scalar_one() == 1


async def test_clarification_supersession_and_frozen_catalog_survive_replanning(approval):
    from workflow_engine.domain import Conflict
    from workflow_engine.policy import CatalogProvider

    commands, admin, requester, reviewers, _, h = approval
    cycle_id, item = await draft_item(commands, admin, requester, reviewers)
    submitted = await commands.submit_version(
        requester, item["id"], "Unclear maintenance request", item["revision"], uuid4()
    )
    await h.runner.tick(submitted["run_id"], "planner")
    current = await commands.get_item(requester, item["id"])
    assert current["progress"]["status"] == "NEEDS_INPUT"
    cycle = await commands.get_cycle(admin, cycle_id)
    with pytest.raises(Conflict):
        await commands.activate_cycle(admin, cycle_id, cycle["revision"])
    old_run_id = submitted["run_id"]
    submitted = await commands.submit_version(
        requester, item["id"], REQUEST, current["revision"], uuid4()
    )
    h.runner.catalog = CatalogProvider([])
    await h.runner.tick(submitted["run_id"], "planner-after-catalog-change")
    current = await commands.get_item(requester, item["id"])
    assert current["progress"]["status"] == "PENDING"
    assert current["versions"][0]["status"] == "SUPERSEDED"
    assert (await commands.get_execution(requester, old_run_id))["clarification"]


async def test_live_planning_lease_blocks_supersession_without_partial_writes(approval):
    from workflow_engine.domain import Conflict

    commands, admin, requester, reviewers, _, h = approval
    _, item = await draft_item(commands, admin, requester, reviewers)
    submitted = await commands.submit_version(
        requester, item["id"], REQUEST, item["revision"], uuid4()
    )
    lease = await h.store.claim(submitted["run_id"], "live-planner")
    assert lease is not None
    with pytest.raises(Conflict):
        await commands.submit_version(
            requester, item["id"], REQUEST, submitted["revision"], uuid4()
        )
    assert (await commands.get_item(requester, item["id"]))["current_version_id"] == submitted[
        "current_version_id"
    ]
    await h.store.release(lease)
    await commands.submit_version(requester, item["id"], REQUEST, submitted["revision"], uuid4())
    old = await h.store.get(submitted["run_id"], requester.tenant_id)
    assert old.state == "CANCELLED" and old.next_run_at is None


async def test_runtime_cannot_rewrite_evidence_or_cross_link_same_tenant_versions(approval):
    commands, admin, requester, reviewers, item_id, h = approval
    await decision(commands, reviewers[0], item_id)
    first = await commands.get_item(requester, item_id)
    _, other = await draft_item(commands, admin, requester, reviewers)
    second = await commands.submit_version(
        requester, other["id"], REQUEST, other["revision"], uuid4()
    )
    for table, assignment in (
        ("item_versions", "request_text='rewritten'"),
        ("version_steps", "level=level"),
        ("decisions", "comment='rewritten'"),
        ("validated_plans", "plan_hash=plan_hash"),
    ):
        with pytest.raises(DBAPIError):
            async with commands.database.sessions.begin() as session:
                await session.execute(text(f"UPDATE {table} SET {assignment}"))
    with pytest.raises(DBAPIError):
        async with commands.database.sessions.begin() as session:
            await session.execute(
                text("UPDATE review_items SET current_version_id=:version WHERE id=:id"),
                {"version": second["current_version_id"], "id": first["id"]},
            )


async def test_hash_only_standalone_fixture_cannot_run_through_production_store(approval):
    from workflow_engine.db_models import ExecutionRun
    from workflow_engine.domain import Conflict, DomainEvent, Workflow, WorkflowCreate, transition
    from workflow_engine.postgres_store import _encode

    commands, _, _, _, _, h = approval
    current = (
        await h.store.insert_or_get(
            Workflow.new("tenant-a", "forged-standalone", WorkflowCreate(text=REQUEST))
        )
    ).workflow
    await h.runner.tick(current.workflow_id, "planner")
    current = await h.store.get(current.workflow_id, "tenant-a")
    approved = transition(current, DomainEvent(kind="approved", plan_hash=current.plan_hash))
    with pytest.raises(Conflict):
        await h.store.save(approved, current.revision, None)
    async with owner_session(h.store) as session:
        row = await session.get(ExecutionRun, UUID(current.workflow_id))
        for key, value in _encode(approved).items():
            setattr(row, key, value)
    assert await h.store.claim(current.workflow_id, "forged-executor") is None


async def test_fault_fixture_authorizes_through_two_real_named_http_sessions(approval):
    from tests.named_review_fixture import approve_pending_review
    from workflow_engine.db_models import AuditRecord
    from workflow_engine.domain import Conflict, Workflow, WorkflowCreate
    from workflow_engine.version_models import Decision

    _, _, _, _, _, h = approval
    current = (
        await h.store.insert_or_get(
            Workflow.new("tenant-a", "named-fault-review", WorkflowCreate(text=REQUEST))
        )
    ).workflow
    await h.runner.tick(current.workflow_id, "planner")
    current = await h.store.get(current.workflow_id, "tenant-a")
    with pytest.raises(Conflict):
        await approve_pending_review(h.store, current, current.revision, "0" * 64)
    assert await h.store.get(current.workflow_id, "tenant-a") == current
    result = await approve_pending_review(h.store, current, current.revision, current.plan_hash)
    assert result.state == "READY" and result.approved_plan_hash == current.plan_hash
    assert result.actions == current.actions and result.workflow_id == current.workflow_id
    async with h.store.database.sessions() as session:
        decisions = (
            (await session.execute(select(Decision).where(Decision.tenant_id == "tenant-a")))
            .scalars()
            .all()
        )
        assert len(decisions) == 2 and len({row.actor_id for row in decisions}) == 2
        events = (
            (
                await session.execute(
                    select(AuditRecord).where(
                        AuditRecord.tenant_id == "tenant-a", AuditRecord.event == "review_decided"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 2 and all(row.actor_type == "USER" for row in events)


async def test_authenticated_cancellation_rejects_invalid_bodies_without_500(approval):
    import httpx

    from workflow_engine.api import create_app
    from workflow_engine.auth import AuthService
    from workflow_engine.settings import Settings

    commands, _, _, _, item_id, h = approval
    settings = Settings(env="local", origin="http://127.0.0.1")
    auth = AuthService(commands.database, settings)
    pre = await auth.pre_session()
    context = await auth.login(
        pre.token, pre.csrf, "approvals", "approval-admin", "a-safe-password-123", "local"
    )
    workflow_id = (await commands.get_item(context.actor, item_id))["execution"]["workflow_id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(settings, h.store, commands.catalog), raise_app_exceptions=False
        ),
        base_url=settings.origin,
        cookies={auth.cookie_name: context.token},
        headers={"Origin": settings.origin, "X-CSRF-Token": context.csrf},
    ) as client:
        for body in (
            {},
            {"expected_revision": "invalid"},
            {"expected_revision": 1, "role": "ADMIN"},
        ):
            result = await client.post(f"/workflows/{workflow_id}/cancel", json=body)
            assert result.status_code == 422
        assert (
            await client.post(f"/workflows/{workflow_id}/cancel", content=b"not-json")
        ).status_code == 422


async def test_unauthenticated_mutation_returns_401_before_origin_or_csrf_errors(approval):
    import httpx

    from workflow_engine.api import create_app
    from workflow_engine.settings import Settings

    commands, _, _, _, item_id, h = approval
    app = create_app(Settings(env="local", origin="http://127.0.0.1"), h.store, commands.catalog)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        assert (await client.post(f"/workflows/{item_id}/cancel", json={})).status_code == 401
        assert (await client.post("/api/cycles", json={"title": "Denied"})).status_code == 401


async def test_execution_failure_does_not_reclassify_a_sealed_approved_review(approval):
    commands, admin, requester, reviewers, item_id, h = approval
    await decision(commands, reviewers[0], item_id)
    await decision(commands, reviewers[1], item_id)
    before = await commands.get_item(requester, item_id)
    assert (await h.ticketing.put("/__faults", json={"mode": "permanent"})).status_code == 200
    await h.runner.tick(before["execution"]["workflow_id"], "permanent-failure")
    after = await commands.get_item(requester, item_id)
    assert after["execution"]["state"] == "FAILED"
    assert after["progress"] == before["progress"] and after["progress"]["status"] == "APPROVED"
    assert (await commands.get_cycle(admin, after["cycle_id"]))["state"] == "COMPLETED"
    assert (await h.ticketing.get("/__effects")).json() == {}


async def test_business_routes_fail_closed_when_real_database_revision_is_not_current(approval):
    import httpx

    from workflow_engine.api import create_app
    from workflow_engine.auth import AuthService
    from workflow_engine.db import SCHEMA_REVISION
    from workflow_engine.review_models import ReviewCycle
    from workflow_engine.settings import Settings

    commands, _, _, _, _, h = approval
    settings = Settings(env="local", origin="http://127.0.0.1")
    auth = AuthService(commands.database, settings)
    pre = await auth.pre_session()
    context = await auth.login(
        pre.token, pre.csrf, "approvals", "approval-admin", "a-safe-password-123", "local"
    )
    async with commands.database.sessions() as session:
        count = (await session.execute(select(func.count()).select_from(ReviewCycle))).scalar_one()
    async with owner_session(h.store) as session:
        await session.execute(text("UPDATE alembic_version SET version_num='0006_reminder_jobs'"))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings, h.store, commands.catalog)),
            base_url=settings.origin,
            cookies={auth.cookie_name: context.token},
            headers={"Origin": settings.origin, "X-CSRF-Token": context.csrf},
        ) as client:
            assert (await client.get("/health/ready")).status_code == 503
            assert (await client.get("/health/live")).status_code == 200
            assert (
                await client.post("/api/cycles", json={"title": "Must not commit"})
            ).status_code == 503
            assert (await client.get("/api/me")).status_code == 503
            assert (await client.get("/cycles")).status_code == 503
        async with commands.database.sessions() as session:
            assert (
                await session.execute(select(func.count()).select_from(ReviewCycle))
            ).scalar_one() == count
    finally:
        async with owner_session(h.store) as session:
            await session.execute(
                text("UPDATE alembic_version SET version_num=:revision"),
                {"revision": SCHEMA_REVISION},
            )

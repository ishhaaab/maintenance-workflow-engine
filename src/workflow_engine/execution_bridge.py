"""The only review-to-execution synchronization, always in the caller's transaction."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from .audit import append_event
from .domain import Conflict, DomainEvent, NeedsInput, WorkflowCreate, transition
from .policy import TenantCatalog, validate_and_compile
from .review_models import ReviewCycle, ReviewItem
from .version_models import Decision, ItemVersion, ReviewProgress, ValidatedPlan, VersionStep


async def lock_review(session, tenant_id, workflow_id):
    """Find ancestry without a run lock, then lock cycle → item → progress → run."""
    from .db_models import ExecutionRun

    link = (
        await session.execute(
            select(ItemVersion)
            .join(
                ExecutionRun,
                (ExecutionRun.tenant_id == ItemVersion.tenant_id)
                & (ExecutionRun.item_version_id == ItemVersion.id),
            )
            .where(ExecutionRun.workflow_id == workflow_id, ExecutionRun.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if link is None:
        return
    item = await session.get(ReviewItem, link.item_id)
    await session.execute(
        select(ReviewCycle)
        .where(ReviewCycle.id == item.cycle_id, ReviewCycle.tenant_id == tenant_id)
        .with_for_update()
    )
    await session.execute(
        select(ReviewItem)
        .where(ReviewItem.id == item.id, ReviewItem.tenant_id == tenant_id)
        .with_for_update()
    )
    await session.execute(
        select(ReviewProgress)
        .where(ReviewProgress.version_id == link.id, ReviewProgress.tenant_id == tenant_id)
        .with_for_update()
    )


async def open_step(session, progress, level, at):
    step = (
        await session.execute(
            select(VersionStep).where(
                VersionStep.tenant_id == progress.tenant_id,
                VersionStep.version_id == progress.version_id,
                VersionStep.level == level,
            )
        )
    ).scalar_one()
    progress.status, progress.current_level = "PENDING", level
    progress.opened_at, progress.deadline_at = at, at + timedelta(seconds=step.due_after_s)
    from .notifications import schedule_reminder

    await schedule_reminder(session, progress)


def close_step(progress, status):
    progress.status = status
    progress.current_level = progress.opened_at = progress.deadline_at = None


async def record_plan(session, row, before, after):
    if row.item_version_id is None or before.plan_hash is not None:
        # Execution can also fail after approval. A sealed review is immutable
        # evidence, not a planning failure to synchronize back into progress.
        return
    version = await session.get(ItemVersion, row.item_version_id)
    item = await session.get(ReviewItem, version.item_id)
    cycle = await session.get(ReviewCycle, item.cycle_id)
    progress = await session.get(ReviewProgress, version.id)
    if item.current_version_id != version.id or progress.status == "SUPERSEDED":
        raise Conflict("superseded_planning_run")
    if before.state == after.state or after.state not in {
        "AWAITING_APPROVAL",
        "NEEDS_INPUT",
        "FAILED",
    }:
        return
    if progress.status != "PREPARING":
        raise Conflict("review_plan_already_sealed")
    if after.state == "AWAITING_APPROVAL":
        compiled = validate_and_compile(
            WorkflowCreate(text=version.request_text),
            after.plan,
            TenantCatalog.model_validate(version.catalog),
            after.workflow_id,
            after.tenant_id,
        )
        if (
            isinstance(compiled, NeedsInput)
            or compiled.plan_hash != after.plan_hash
            or compiled.actions != after.actions
        ):
            raise Conflict("sealed_plan_policy_mismatch")
        plan = ValidatedPlan(
            id=uuid4(),
            tenant_id=row.tenant_id,
            version_id=version.id,
            run_id=row.workflow_id,
            plan_hash=after.plan_hash,
            schema_version=1,
            catalog_version=after.catalog_version,
            plan=after.plan.model_dump(mode="json"),
            actions=[action.model_dump(mode="json") for action in after.actions],
            created_at=after.updated_at,
        )
        session.add(plan)
        close_step(progress, "PENDING")
        if cycle.state == "ACTIVE":
            await open_step(session, progress, 1, after.updated_at)
    else:
        close_step(progress, "NEEDS_INPUT" if after.state == "NEEDS_INPUT" else "PLAN_FAILED")
    progress.revision += 1
    await append_event(
        session,
        None,
        "version",
        version.id,
        "planning_completed",
        progress.revision,
        tenant_id=row.tenant_id,
        after_state=progress.status,
        workflow_id=row.workflow_id,
    )


async def execution_eligible(session, row):
    """A matching hash on a forged/unrelated snapshot is not execution permission."""
    planning = row.state == "PENDING_PLAN" or (
        row.state == "RETRY_WAIT" and row.payload["retry_phase"] == "planning"
    )
    if row.item_version_id is None:
        # Internal planning remains usable. Standalone mutation authorization is retired.
        return planning and row.approved_plan_hash is None
    version = await session.get(ItemVersion, row.item_version_id)
    item = await session.get(ReviewItem, version.item_id) if version else None
    progress = await session.get(ReviewProgress, version.id) if version else None
    if not item or not progress or item.current_version_id != version.id:
        return False
    if planning:
        return progress.status == "PREPARING" and row.approved_plan_hash is None
    plan = await session.get(ValidatedPlan, row.approved_plan_id) if row.approved_plan_id else None
    if (
        progress.status != "APPROVED"
        or not plan
        or plan.tenant_id != row.tenant_id
        or plan.version_id != version.id
        or plan.run_id != row.workflow_id
        or plan.plan_hash != row.plan_hash
        or row.approved_plan_hash != row.plan_hash
    ):
        return False
    steps = (
        (
            await session.execute(
                select(VersionStep)
                .where(VersionStep.tenant_id == row.tenant_id, VersionStep.version_id == version.id)
                .order_by(VersionStep.level)
            )
        )
        .scalars()
        .all()
    )
    decisions = (
        (
            await session.execute(
                select(Decision)
                .where(Decision.tenant_id == row.tenant_id, Decision.version_id == version.id)
                .order_by(Decision.level)
            )
        )
        .scalars()
        .all()
    )
    return (
        bool(steps)
        and len(steps) == len(decisions)
        and all(
            step.level == decision.level
            and step.reviewer_id == decision.actor_id
            and decision.kind == "APPROVE"
            and decision.plan_hash == row.plan_hash
            for step, decision in zip(steps, decisions, strict=True)
        )
    )


async def authorize_execution(session, actor, version, progress, row, plan, at):
    from .postgres_store import _decode, _encode
    from .store import _live, _validate_change

    before = _decode(row)
    if (
        _live(before, at)
        or before.state != "AWAITING_APPROVAL"
        or before.plan_hash != plan.plan_hash
        or row.item_version_id != version.id
    ):
        raise Conflict("execution_not_authorizable")
    row.approved_plan_id = plan.id
    close_step(progress, "APPROVED")
    after = transition(before, DomainEvent(kind="approved", at=at, plan_hash=plan.plan_hash))
    _validate_change(before, after, before.revision)
    for key, value in _encode(after).items():
        setattr(row, key, value)
    await session.flush()
    if not await execution_eligible(session, row):
        raise Conflict("incomplete_approval_chain")
    await append_event(
        session,
        actor,
        "execution",
        row.workflow_id,
        "execution_authorized",
        after.revision,
        before_revision=before.revision,
        before_state=before.state,
        after_state=after.state,
        workflow_id=row.workflow_id,
    )

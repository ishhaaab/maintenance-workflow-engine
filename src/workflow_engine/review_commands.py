"""Transaction owners for review commands. HTTP and HTML contain no state rules."""

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from .audit import append_event
from .auth import lock_actor
from .authorization import Denied, authorize
from .db_models import ExecutionRun
from .domain import (
    TERMINAL,
    Conflict,
    DomainEvent,
    Workflow,
    WorkflowCreate,
    canonical_hash,
    transition,
)
from .execution_bridge import authorize_execution, close_step, open_step
from .identity_models import Membership, Tenant, User
from .postgres_store import _decode, _encode
from .review_domain import (
    activate,
    chain_fingerprint,
    compute_completion,
    decide,
    require_draft,
    validate_chain,
)
from .review_models import CommandReceipt, CycleRequester, ReviewCycle, ReviewItem
from .store import _live
from .version_models import (
    ApprovalDefinition,
    Decision,
    ItemVersion,
    ReviewProgress,
    ValidatedPlan,
    VersionStep,
)


def identity(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        raise Denied("not_found", 404) from None


def project(row):
    return {
        column.name: str(value) if isinstance(value := getattr(row, column.name), UUID) else value
        for column in row.__table__.columns
    }


def revision(row, expected):
    if not isinstance(expected, int) or isinstance(expected, bool) or row.revision != expected:
        raise Conflict("stale_revision")


@asynccontextmanager
async def transaction(database):
    try:
        async with database.sessions.begin() as session:
            yield session
    except IntegrityError:
        raise Conflict("persistence_constraint_conflict") from None


async def now(session):
    return (await session.execute(select(func.clock_timestamp()))).scalar_one()


async def receipt(session, actor, kind, command_id, payload):
    key = dict(
        tenant_id=actor.tenant_id,
        actor_id=actor.membership_id,
        kind=kind,
        command_id=identity(command_id),
    )
    existing = await session.get(CommandReceipt, key)
    if existing:
        if existing.payload_hash != canonical_hash(payload):
            raise Conflict("command_payload_conflict")
        return key, existing.result
    return key, None


def remember(session, key, payload, result):
    session.add(CommandReceipt(**key, payload_hash=canonical_hash(payload), result=result))


class ReviewCommands:
    def __init__(self, database, catalog=None):
        self.database, self.catalog = database, catalog

    async def _cycle(self, session, actor, cycle_id, *, lock=False):
        query = select(ReviewCycle).where(
            ReviewCycle.id == identity(cycle_id), ReviewCycle.tenant_id == actor.tenant_id
        )
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        row = (await session.execute(query)).scalar_one_or_none()
        if row is None:
            raise Denied("not_found", 404)
        return row

    async def _item(self, session, actor, item_id, *, lock=False):
        query = select(ReviewItem).where(
            ReviewItem.id == identity(item_id), ReviewItem.tenant_id == actor.tenant_id
        )
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        row = (await session.execute(query)).scalar_one_or_none()
        if row is None:
            raise Denied("not_found", 404)
        return row

    async def _member(self, session, actor, member_id, role):
        row = (
            await session.execute(
                select(Membership)
                .join(User)
                .where(
                    Membership.id == identity(member_id),
                    Membership.tenant_id == actor.tenant_id,
                    Membership.role == role,
                    Membership.enabled.is_(True),
                    User.enabled.is_(True),
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            raise Denied("not_found", 404)
        return row

    async def create_cycle(self, actor, title):
        authorize(actor, "manage_cycle", actor.tenant_id)
        if not 1 <= len(title.strip()) <= 160:
            raise Denied("invalid_title", 422)
        async with transaction(self.database) as session:
            actor = await lock_actor(session, actor)
            at = await now(session)
            cycle = ReviewCycle(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                creator_id=actor.membership_id,
                title=title.strip(),
                state="DRAFT",
                revision=0,
                created_at=at,
                updated_at=at,
            )
            session.add(cycle)
            await session.flush()
            await append_event(
                session, actor, "cycle", cycle.id, "cycle_created", 0, after_state="DRAFT"
            )
            return project(cycle)

    async def edit_draft_cycle(self, actor, cycle_id, title, expected_revision):
        authorize(actor, "manage_cycle", actor.tenant_id)
        if not 1 <= len(title.strip()) <= 160:
            raise Denied("invalid_title", 422)
        async with transaction(self.database) as session:
            await lock_actor(session, actor)
            cycle = await self._cycle(session, actor, cycle_id, lock=True)
            revision(cycle, expected_revision)
            require_draft(cycle.state)
            cycle.title, cycle.revision, cycle.updated_at = (
                title.strip(),
                cycle.revision + 1,
                await now(session),
            )
            await append_event(
                session,
                actor,
                "cycle",
                cycle.id,
                "cycle_updated",
                cycle.revision,
                before_revision=expected_revision,
            )
            return project(cycle)

    async def admit_requester(self, actor, cycle_id, requester_id, expected_revision):
        authorize(actor, "manage_cycle", actor.tenant_id)
        async with transaction(self.database) as session:
            await lock_actor(session, actor, memberships=[identity(requester_id)])
            member = await self._member(session, actor, requester_id, "REQUESTER")
            cycle = await self._cycle(session, actor, cycle_id, lock=True)
            revision(cycle, expected_revision)
            require_draft(cycle.state)
            session.add(
                CycleRequester(
                    tenant_id=actor.tenant_id, cycle_id=cycle.id, membership_id=member.id
                )
            )
            cycle.revision += 1
            cycle.updated_at = await now(session)
            await append_event(
                session,
                actor,
                "cycle",
                cycle.id,
                "requester_admitted",
                cycle.revision,
                before_revision=expected_revision,
            )
            return project(cycle)

    async def create_item(self, actor, cycle_id, expected_revision, command_id):
        authorize(actor, "submit", actor.tenant_id, owner=actor.membership_id)
        payload = {"cycle_id": str(cycle_id), "expected_revision": expected_revision}
        async with transaction(self.database) as session:
            await lock_actor(session, actor)
            cycle = await self._cycle(session, actor, cycle_id, lock=True)
            key, replay = await receipt(session, actor, "create_item", command_id, payload)
            if replay is not None:
                return replay
            revision(cycle, expected_revision)
            require_draft(cycle.state)
            if (
                await session.get(CycleRequester, (actor.tenant_id, cycle.id, actor.membership_id))
                is None
            ):
                raise Denied("requester_not_admitted")
            item = ReviewItem(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                cycle_id=cycle.id,
                requester_id=actor.membership_id,
                revision=0,
                created_at=await now(session),
            )
            session.add(item)
            cycle.revision += 1
            cycle.updated_at = await now(session)
            await session.flush()
            await append_event(session, actor, "item", item.id, "item_created", 0)
            result = project(item)
            # Receipts need only stable IDs; timestamps are not part of item admission replay.
            result.pop("created_at")
            remember(session, key, payload, result)
            return result

    async def activate_cycle(self, actor, cycle_id, expected_revision):
        authorize(actor, "manage_cycle", actor.tenant_id)
        async with transaction(self.database) as session:
            candidates = (
                (
                    await session.execute(
                        select(ReviewItem).where(
                            ReviewItem.tenant_id == actor.tenant_id,
                            ReviewItem.cycle_id == identity(cycle_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
            participants = set()
            for candidate in candidates:
                participants.add(candidate.requester_id)
                participants.update(
                    identity(step["reviewer_id"])
                    for step in await self._definitions(session, candidate)
                )
            await lock_actor(session, actor, memberships=participants)
            cycle = await self._cycle(session, actor, cycle_id, lock=True)
            revision(cycle, expected_revision)
            items = (
                (
                    await session.execute(
                        select(ReviewItem)
                        .where(
                            ReviewItem.tenant_id == actor.tenant_id, ReviewItem.cycle_id == cycle.id
                        )
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
            readiness, progress_rows = [], []
            for item in items:
                chain = await self._definitions(session, item)
                validate_chain(item.requester_id, chain)
                for member_id in sorted(
                    [item.requester_id, *[identity(step["reviewer_id"]) for step in chain]], key=str
                ):
                    await self._member(
                        session,
                        actor,
                        member_id,
                        "REQUESTER" if member_id == item.requester_id else "REVIEWER",
                    )
                version = (
                    await session.get(ItemVersion, item.current_version_id)
                    if item.current_version_id
                    else None
                )
                progress = await session.get(ReviewProgress, version.id) if version else None
                plan = (
                    (
                        await session.execute(
                            select(ValidatedPlan).where(
                                ValidatedPlan.tenant_id == actor.tenant_id,
                                ValidatedPlan.version_id == version.id,
                            )
                        )
                    ).scalar_one_or_none()
                    if version
                    else None
                )
                readiness.append(
                    (
                        bool(version and version.definition_hash == chain_fingerprint(chain)),
                        bool(plan and progress and progress.status == "PENDING"),
                    )
                )
                progress_rows.append(progress)
            cycle.state = activate(cycle.state, readiness)
            cycle.revision += 1
            cycle.updated_at = at = await now(session)
            for progress in progress_rows:
                await open_step(session, progress, 1, at)
                progress.revision += 1
            await append_event(
                session,
                actor,
                "cycle",
                cycle.id,
                "cycle_activated",
                cycle.revision,
                before_revision=expected_revision,
                before_state="DRAFT",
                after_state="ACTIVE",
            )
            return project(cycle)

    async def _locked_item(self, session, actor, item_id):
        before = await self._item(session, actor, item_id)
        cycle = await self._cycle(session, actor, before.cycle_id, lock=True)
        item = await self._item(session, actor, item_id, lock=True)
        return cycle, item

    async def _definitions(self, session, item):
        rows = (
            (
                await session.execute(
                    select(ApprovalDefinition)
                    .where(
                        ApprovalDefinition.tenant_id == item.tenant_id,
                        ApprovalDefinition.item_id == item.id,
                    )
                    .order_by(ApprovalDefinition.level)
                )
            )
            .scalars()
            .all()
        )
        return [
            {"reviewer_id": str(step.reviewer_id), "due_after_s": step.due_after_s} for step in rows
        ]

    async def configure_chain(self, actor, item_id, definitions, expected_revision):
        authorize(actor, "manage_cycle", actor.tenant_id)
        async with transaction(self.database) as session:
            await lock_actor(
                session, actor, memberships=[identity(step["reviewer_id"]) for step in definitions]
            )
            # Memberships are validated before the cycle lock; identities cannot change ownership.
            source = await self._item(session, actor, item_id)
            validate_chain(source.requester_id, definitions)
            for member_id in sorted(
                (identity(step["reviewer_id"]) for step in definitions), key=str
            ):
                await self._member(session, actor, member_id, "REVIEWER")
            cycle, item = await self._locked_item(session, actor, item_id)
            revision(item, expected_revision)
            require_draft(cycle.state)
            await session.execute(
                delete(ApprovalDefinition).where(
                    ApprovalDefinition.tenant_id == actor.tenant_id,
                    ApprovalDefinition.item_id == item.id,
                )
            )
            for level, step in enumerate(definitions, 1):
                session.add(
                    ApprovalDefinition(
                        id=uuid4(),
                        tenant_id=actor.tenant_id,
                        item_id=item.id,
                        level=level,
                        reviewer_id=identity(step["reviewer_id"]),
                        due_after_s=step["due_after_s"],
                    )
                )
            item.revision += 1
            cycle.revision += 1
            cycle.updated_at = await now(session)
            await append_event(
                session,
                actor,
                "item",
                item.id,
                "approval_chain_configured",
                item.revision,
                before_revision=expected_revision,
            )
            return project(item)

    async def submit_version(self, actor, item_id, request_text, expected_revision, command_id):
        request = WorkflowCreate(text=request_text)
        payload = {
            "item_id": str(item_id),
            "text": request.text,
            "expected_revision": expected_revision,
        }
        async with transaction(self.database) as session:
            source = await self._item(session, actor, item_id)
            proposed_chain = await self._definitions(session, source)
            await lock_actor(
                session,
                actor,
                memberships=[identity(step["reviewer_id"]) for step in proposed_chain],
            )
            cycle, item = await self._locked_item(session, actor, item_id)
            authorize(actor, "submit", item.tenant_id, owner=item.requester_id)
            key, replay = await receipt(session, actor, "submit_version", command_id, payload)
            if replay is not None:
                return replay
            revision(item, expected_revision)
            if cycle.state not in {"DRAFT", "ACTIVE"}:
                raise Conflict("cycle_completed")
            chain = await self._definitions(session, item)
            validate_chain(item.requester_id, chain)
            for definition in chain:
                await self._member(session, actor, definition["reviewer_id"], "REVIEWER")
            at = await now(session)
            previous = (
                await session.get(ItemVersion, item.current_version_id)
                if item.current_version_id
                else None
            )
            if previous:
                old_progress = (
                    await session.execute(
                        select(ReviewProgress)
                        .where(ReviewProgress.version_id == previous.id)
                        .with_for_update()
                    )
                ).scalar_one()
                allowed = (
                    {"PREPARING", "NEEDS_INPUT", "PLAN_FAILED", "PENDING"}
                    if cycle.state == "DRAFT"
                    else {"NEEDS_INPUT", "PLAN_FAILED", "CHANGES_REQUESTED"}
                )
                if old_progress.status not in allowed:
                    raise Conflict("version_not_resubmittable")
                old_run = (
                    await session.execute(
                        select(ExecutionRun)
                        .where(
                            ExecutionRun.tenant_id == actor.tenant_id,
                            ExecutionRun.item_version_id == previous.id,
                        )
                        .with_for_update()
                    )
                ).scalar_one()
                snapshot = _decode(old_run)
                at = await now(session)
                if (
                    _live(snapshot, at)
                    or snapshot.approved_plan_hash
                    or any(a.submissions for a in snapshot.actions)
                ):
                    raise Conflict("version_has_live_or_authorized_execution")
                if snapshot.state not in TERMINAL:
                    cancelled = transition(snapshot, DomainEvent(kind="cancelled", at=at))
                    for field, value in _encode(cancelled).items():
                        setattr(old_run, field, value)
                close_step(old_progress, "SUPERSEDED")
                from .notifications import cancel_reminders

                await cancel_reminders(session, previous.id)
                old_progress.revision += 1
                await append_event(
                    session,
                    actor,
                    "version",
                    previous.id,
                    "version_superseded",
                    old_progress.revision,
                    after_state="SUPERSEDED",
                    workflow_id=old_run.workflow_id,
                )
            if self.catalog is None:
                raise Conflict("tenant_catalog_unavailable")
            tenant = await session.get(Tenant, actor.tenant_id)
            try:
                try:
                    catalog = self.catalog.get(actor.tenant_id)
                except KeyError:
                    source_catalog = self.catalog.get(tenant.slug)
                    catalog = source_catalog.model_copy(
                        update={
                            "tenant_id": actor.tenant_id,
                            "fixture_namespace": source_catalog.fixture_namespace
                            or source_catalog.tenant_id,
                        }
                    )
            except KeyError:
                raise Conflict("tenant_catalog_unavailable") from None
            version = ItemVersion(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                item_id=item.id,
                number=previous.number + 1 if previous else 1,
                submitted_by=actor.membership_id,
                predecessor_id=previous.id if previous else None,
                request_text=request.text,
                request_hash=canonical_hash(request.model_dump()),
                definition_hash=chain_fingerprint(chain),
                catalog=catalog.model_dump(mode="json"),
                created_at=at,
            )
            session.add(version)
            item.current_version_id, item.revision = version.id, item.revision + 1
            await session.flush()
            for level, step in enumerate(chain, 1):
                session.add(
                    VersionStep(
                        id=uuid4(),
                        tenant_id=actor.tenant_id,
                        version_id=version.id,
                        level=level,
                        reviewer_id=identity(step["reviewer_id"]),
                        due_after_s=step["due_after_s"],
                    )
                )
            session.add(
                ReviewProgress(
                    version_id=version.id, tenant_id=actor.tenant_id, status="PREPARING", revision=0
                )
            )
            run = Workflow.new(actor.tenant_id, "version:" + str(version.id), request)
            run = run.model_copy(update={"created_at": at, "updated_at": at, "next_run_at": at})
            session.add(ExecutionRun(**_encode(run), item_version_id=version.id))
            await session.flush()
            await append_event(
                session,
                actor,
                "version",
                version.id,
                "version_submitted",
                item.revision,
                before_revision=expected_revision,
                after_state="PREPARING",
                workflow_id=identity(run.workflow_id),
                correlation_id=str(command_id),
            )
            result = {**project(item), "run_id": run.workflow_id}
            result.pop("created_at")
            remember(session, key, payload, result)
            return result

    async def decide(
        self,
        actor,
        item_id,
        *,
        version_id,
        level,
        plan_hash,
        decision,
        comment,
        expected_revision,
        expected_progress_revision,
        command_id,
    ):
        payload = {
            "item_id": str(item_id),
            "version_id": str(version_id),
            "level": level,
            "plan_hash": plan_hash,
            "decision": decision,
            "comment": comment,
            "expected_revision": expected_revision,
            "expected_progress_revision": expected_progress_revision,
        }
        async with transaction(self.database) as session:
            await lock_actor(session, actor)
            cycle, item = await self._locked_item(session, actor, item_id)
            if item.current_version_id != identity(version_id):
                raise Conflict("stale_item_version")
            step = (
                await session.execute(
                    select(VersionStep).where(
                        VersionStep.tenant_id == actor.tenant_id,
                        VersionStep.version_id == item.current_version_id,
                        VersionStep.level == level,
                    )
                )
            ).scalar_one_or_none()
            authorize(actor, "decide", actor.tenant_id, reviewer=step.reviewer_id if step else None)
            if actor.membership_id == item.requester_id:
                raise Denied("self_approval_forbidden")
            key, replay = await receipt(session, actor, "decide", command_id, payload)
            if replay is not None:
                return replay
            revision(item, expected_revision)
            progress = (
                await session.execute(
                    select(ReviewProgress)
                    .where(ReviewProgress.version_id == item.current_version_id)
                    .with_for_update()
                )
            ).scalar_one()
            revision(progress, expected_progress_revision)
            if progress.current_level != level:
                raise Conflict("stale_review_level")
            steps = (
                (
                    await session.execute(
                        select(VersionStep)
                        .where(
                            VersionStep.tenant_id == actor.tenant_id,
                            VersionStep.version_id == item.current_version_id,
                        )
                        .order_by(VersionStep.level)
                    )
                )
                .scalars()
                .all()
            )
            prior = (
                (
                    await session.execute(
                        select(Decision)
                        .where(
                            Decision.tenant_id == actor.tenant_id,
                            Decision.version_id == item.current_version_id,
                        )
                        .order_by(Decision.level)
                    )
                )
                .scalars()
                .all()
            )
            if len(prior) != level - 1 or any(
                d.kind != "APPROVE" or d.level != i for i, d in enumerate(prior, 1)
            ):
                raise Conflict("earlier_levels_not_approved")
            status, next_level = decide(
                cycle.state, progress.status, level, len(steps), decision, comment
            )
            plan = (
                await session.execute(
                    select(ValidatedPlan).where(
                        ValidatedPlan.tenant_id == actor.tenant_id,
                        ValidatedPlan.version_id == item.current_version_id,
                    )
                )
            ).scalar_one()
            if plan.plan_hash != plan_hash:
                raise Conflict("stale_plan_hash")
            run = (
                await session.execute(
                    select(ExecutionRun)
                    .where(
                        ExecutionRun.tenant_id == actor.tenant_id,
                        ExecutionRun.workflow_id == plan.run_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            at = await now(session)
            if _live(_decode(run), at):
                raise Conflict("active_planning_lease")
            record = Decision(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                version_id=item.current_version_id,
                level=level,
                actor_id=actor.membership_id,
                plan_hash=plan_hash,
                kind=decision,
                comment=comment.strip(),
                created_at=at,
                command_id=identity(command_id),
                payload_hash=canonical_hash(payload),
            )
            session.add(record)
            await session.flush()
            progress.revision += 1
            item.revision += 1
            from .notifications import cancel_reminders

            await cancel_reminders(session, progress.version_id)
            close_step(progress, status)
            if next_level:
                await open_step(session, progress, next_level, at)
            elif status == "APPROVED":
                version = await session.get(ItemVersion, item.current_version_id)
                await authorize_execution(session, actor, version, progress, run, plan, at)
            await append_event(
                session,
                actor,
                "decision",
                record.id,
                "review_decided",
                progress.revision,
                before_revision=expected_progress_revision,
                after_state=status,
                correlation_id=str(command_id),
                workflow_id=run.workflow_id,
            )
            statuses = (
                (
                    await session.execute(
                        select(ReviewProgress.status)
                        .join(
                            ReviewItem,
                            (ReviewItem.current_version_id == ReviewProgress.version_id)
                            & (ReviewItem.tenant_id == ReviewProgress.tenant_id),
                        )
                        .where(
                            ReviewItem.tenant_id == actor.tenant_id, ReviewItem.cycle_id == cycle.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            if compute_completion(statuses):
                cycle.state, cycle.completed_at, cycle.updated_at = "COMPLETED", at, at
                cycle.revision += 1
                await append_event(
                    session,
                    actor,
                    "cycle",
                    cycle.id,
                    "cycle_completed",
                    cycle.revision,
                    before_state="ACTIVE",
                    after_state="COMPLETED",
                )
            result = {
                "decision_id": str(record.id),
                "version_id": str(item.current_version_id),
                "status": status,
                "item_revision": item.revision,
                "progress_revision": progress.revision,
                "cycle_state": cycle.state,
            }
            remember(session, key, payload, result)
            return result

    def _visible_items(self, actor):
        if actor.role == "ADMIN":
            return True
        if actor.role == "REQUESTER":
            return ReviewItem.requester_id == actor.membership_id
        return exists().where(
            ApprovalDefinition.tenant_id == actor.tenant_id,
            ApprovalDefinition.item_id == ReviewItem.id,
            ApprovalDefinition.reviewer_id == actor.membership_id,
        )

    async def _can_read_cycle(self, session, actor, cycle):
        if actor.role == "ADMIN":
            return
        admitted = await session.get(
            CycleRequester, (actor.tenant_id, cycle.id, actor.membership_id)
        )
        if not admitted:
            assigned = (
                await session.execute(
                    select(ReviewItem.id)
                    .where(
                        ReviewItem.tenant_id == actor.tenant_id,
                        ReviewItem.cycle_id == cycle.id,
                        self._visible_items(actor),
                    )
                    .limit(1)
                )
            ).first()
            if not assigned:
                raise Denied("not_found", 404)

    async def list_cycles(self, actor, *, limit=50, offset=0, state=None):
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise Denied("invalid_page", 422)
        async with transaction(self.database) as session:
            await lock_actor(session, actor)
            query = select(ReviewCycle).where(ReviewCycle.tenant_id == actor.tenant_id)
            if state:
                query = query.where(ReviewCycle.state == state)
            if actor.role != "ADMIN":
                query = query.where(
                    or_(
                        exists().where(
                            CycleRequester.tenant_id == actor.tenant_id,
                            CycleRequester.cycle_id == ReviewCycle.id,
                            CycleRequester.membership_id == actor.membership_id,
                        ),
                        exists().where(
                            ReviewItem.tenant_id == actor.tenant_id,
                            ReviewItem.cycle_id == ReviewCycle.id,
                            self._visible_items(actor),
                        ),
                    )
                )
            rows = (
                (
                    await session.execute(
                        query.order_by(ReviewCycle.created_at.desc(), ReviewCycle.id)
                        .offset(offset)
                        .limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "cycles": [project(row) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
            }

    async def get_cycle(self, actor, cycle_id, *, limit=50, offset=0):
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise Denied("invalid_page", 422)
        async with transaction(self.database) as session:
            await lock_actor(session, actor)
            cycle = await self._cycle(session, actor, cycle_id)
            await self._can_read_cycle(session, actor, cycle)
            items = (
                (
                    await session.execute(
                        select(ReviewItem)
                        .where(
                            ReviewItem.tenant_id == actor.tenant_id,
                            ReviewItem.cycle_id == cycle.id,
                            self._visible_items(actor),
                        )
                        .order_by(ReviewItem.id)
                        .offset(offset)
                        .limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
            requesters = (
                (
                    await session.execute(
                        select(CycleRequester.membership_id).where(
                            CycleRequester.tenant_id == actor.tenant_id,
                            CycleRequester.cycle_id == cycle.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return {
                **project(cycle),
                "items": [
                    await self._project_item(session, row, history=False) for row in items[:limit]
                ],
                "requesters": [str(member) for member in requesters]
                if actor.role == "ADMIN"
                else [],
                "next_offset": offset + limit if len(items) > limit else None,
            }

    async def get_item(self, actor, item_id):
        async with transaction(self.database) as session:
            await lock_actor(session, actor)
            item = await self._item(session, actor, item_id)
            visible = (
                await session.execute(
                    select(ReviewItem.id).where(
                        ReviewItem.id == item.id, self._visible_items(actor)
                    )
                )
            ).first()
            if not visible:
                raise Denied("not_found", 404)
            return await self._project_item(session, item, show_delivery=actor.role == "ADMIN")

    async def _project_item(self, session, item, *, history=True, show_delivery=False):
        result = project(item)
        cycle = await session.get(ReviewCycle, item.cycle_id)
        result["cycle_state"], result["cycle_title"] = cycle.state, cycle.title
        result["requester_login"] = (
            await session.execute(
                select(User.login)
                .join(Membership)
                .where(Membership.id == item.requester_id, Membership.tenant_id == item.tenant_id)
            )
        ).scalar_one()
        chain = (
            await session.execute(
                select(ApprovalDefinition, User.login)
                .join(Membership, Membership.id == ApprovalDefinition.reviewer_id)
                .join(User)
                .where(
                    ApprovalDefinition.tenant_id == item.tenant_id,
                    ApprovalDefinition.item_id == item.id,
                )
                .order_by(ApprovalDefinition.level)
            )
        ).all()
        result["chain"] = [{**project(step), "login": login} for step, login in chain]
        result.update(
            progress=None,
            plan_hash=None,
            execution=None,
            next_reviewer=None,
            versions=[],
            reminders=[],
        )
        if not item.current_version_id:
            return result
        progress = await session.get(ReviewProgress, item.current_version_id)
        run = (
            await session.execute(
                select(ExecutionRun).where(
                    ExecutionRun.tenant_id == item.tenant_id,
                    ExecutionRun.item_version_id == item.current_version_id,
                )
            )
        ).scalar_one()
        result["progress"] = project(progress)
        result["plan_hash"] = run.plan_hash
        result["execution"] = public_execution(_decode(run))
        if show_delivery:
            from .notification_models import ReminderJob

            jobs = (
                (
                    await session.execute(
                        select(ReminderJob)
                        .where(
                            ReminderJob.tenant_id == item.tenant_id,
                            ReminderJob.version_id == item.current_version_id,
                        )
                        .order_by(ReminderJob.level)
                    )
                )
                .scalars()
                .all()
            )
            result["reminders"] = [
                {
                    key: getattr(job, key)
                    for key in (
                        "level",
                        "state",
                        "attempts",
                        "deadline_at",
                        "delivered_at",
                        "failed_at",
                        "last_error",
                    )
                }
                for job in jobs
            ]
        if progress.current_level:
            result["next_reviewer"] = next(
                (
                    step["login"]
                    for step in result["chain"]
                    if step["level"] == progress.current_level
                ),
                None,
            )
        if history:
            versions = (
                (
                    await session.execute(
                        select(ItemVersion)
                        .where(
                            ItemVersion.tenant_id == item.tenant_id, ItemVersion.item_id == item.id
                        )
                        .order_by(ItemVersion.number.desc())
                        .limit(101)
                    )
                )
                .scalars()
                .all()
            )
            result["more_versions"] = len(versions) > 100
            for version in reversed(versions[:100]):
                state = await session.get(ReviewProgress, version.id)
                decisions = (
                    await session.execute(
                        select(Decision, User.login)
                        .join(Membership, Membership.id == Decision.actor_id)
                        .join(User)
                        .where(
                            Decision.tenant_id == item.tenant_id, Decision.version_id == version.id
                        )
                        .order_by(Decision.level)
                    )
                ).all()
                result["versions"].append(
                    {key: value for key, value in project(version).items() if key != "catalog"}
                    | {
                        "status": state.status,
                        "decisions": [{**project(d), "login": login} for d, login in decisions],
                    }
                )
        return result

    async def list_members(self, actor):
        authorize(actor, "manage_accounts", actor.tenant_id)
        async with transaction(self.database) as session:
            await lock_actor(session, actor)
            rows = (
                await session.execute(
                    select(Membership, User.login)
                    .join(User)
                    .where(Membership.tenant_id == actor.tenant_id)
                    .order_by(User.login)
                )
            ).all()
            return [{**project(member), "login": login} for member, login in rows]

    async def pending_reviews(self, actor, *, limit=50, offset=0):
        if actor.role != "REVIEWER":
            raise Denied()
        if not 1 <= limit <= 100 or offset < 0:
            raise Denied("invalid_page", 422)
        async with transaction(self.database) as session:
            await lock_actor(session, actor)
            rows = (
                (
                    await session.execute(
                        select(ReviewItem)
                        .join(ReviewCycle, ReviewCycle.id == ReviewItem.cycle_id)
                        .join(
                            ReviewProgress,
                            ReviewProgress.version_id == ReviewItem.current_version_id,
                        )
                        .join(
                            VersionStep,
                            (VersionStep.version_id == ReviewProgress.version_id)
                            & (VersionStep.level == ReviewProgress.current_level),
                        )
                        .where(
                            ReviewItem.tenant_id == actor.tenant_id,
                            VersionStep.reviewer_id == actor.membership_id,
                            ReviewProgress.status == "PENDING",
                            ReviewCycle.state == "ACTIVE",
                        )
                        .order_by(ReviewProgress.deadline_at, ReviewItem.id)
                        .offset(offset)
                        .limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "items": [
                    await self._project_item(session, row, history=False) for row in rows[:limit]
                ],
                "next_offset": offset + limit if len(rows) > limit else None,
            }

    async def get_execution(self, actor, workflow_id):
        async with transaction(self.database) as session:
            await lock_actor(session, actor)
            run = (
                await session.execute(
                    select(ExecutionRun).where(
                        ExecutionRun.workflow_id == identity(workflow_id),
                        ExecutionRun.tenant_id == actor.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            version = (
                await session.get(ItemVersion, run.item_version_id)
                if run and run.item_version_id
                else None
            )
            if not version:
                raise Denied("not_found", 404)
            item = await self._item(session, actor, version.item_id)
            if not (
                await session.execute(
                    select(ReviewItem.id).where(
                        ReviewItem.id == item.id, self._visible_items(actor)
                    )
                )
            ).first():
                raise Denied("not_found", 404)
            return public_execution(_decode(run))

    async def cancel_execution(self, actor, workflow_id, expected_revision):
        authorize(actor, "cancel", actor.tenant_id)
        async with transaction(self.database) as session:
            await lock_actor(session, actor)
            from .execution_bridge import lock_review

            await lock_review(session, actor.tenant_id, identity(workflow_id))
            row = (
                await session.execute(
                    select(ExecutionRun)
                    .where(
                        ExecutionRun.workflow_id == identity(workflow_id),
                        ExecutionRun.tenant_id == actor.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.item_version_id is None:
                raise Denied("not_found", 404)
            before = _decode(row)
            if before.state == "CANCELLED":
                return public_execution(before)
            revision(row, expected_revision)
            at = await now(session)
            if _live(before, at) or before.approved_plan_hash is None:
                raise Conflict("execution_not_cancellable")
            after = transition(before, DomainEvent(kind="cancelled", at=at))
            for field, value in _encode(after).items():
                setattr(row, field, value)
            await append_event(
                session,
                actor,
                "execution",
                row.workflow_id,
                "execution_cancelled",
                after.revision,
                before_revision=before.revision,
                before_state=before.state,
                after_state=after.state,
                workflow_id=row.workflow_id,
            )
            return public_execution(after)


def public_execution(workflow):
    result = {
        key: getattr(workflow, key)
        for key in (
            "workflow_id",
            "revision",
            "state",
            "created_at",
            "updated_at",
            "plan_hash",
            "approved_plan_hash",
            "approved_at",
            "plan",
            "clarification",
        )
    }
    result["actions"] = [
        {key: getattr(action, key) for key in ("action_id", "status", "result", "submissions")}
        for action in workflow.actions
    ]
    return result

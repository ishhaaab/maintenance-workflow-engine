"""PostgreSQL execution authority with short, fenced transactions and atomic audit."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ._storage_codec import _PAYLOAD_FIELDS as _PAYLOAD
from .db import Database, SchemaNotCurrent
from .db_models import AuditRecord, ExecutionRun
from .domain import PASSIVE, Conflict, Lease, LeaseLost, NotFound, Wakeup, Workflow
from .store import (
    _IMMUTABLE,
    _OPERATIONAL,
    AdmissionResult,
    _delay,
    _due,
    _lease,
    _live,
    _reject_new_approval,
    _validate_admission,
    _validate_change,
    _validated_snapshot,
)

_DUE_SCAN_BATCH = 100
_DueKey = tuple[datetime, UUID]


def _encode(workflow: Workflow) -> dict:
    workflow = _validated_snapshot(workflow)
    values = workflow.model_dump()
    wire = workflow.model_dump(mode="json")
    row = {key: value for key, value in values.items() if key not in _PAYLOAD}
    try:
        row["workflow_id"] = UUID(workflow.workflow_id)
    except ValueError:
        raise Conflict("Invalid workflow identity") from None
    row["payload"] = {key: wire[key] for key in _PAYLOAD}
    return row


def _decode(row: ExecutionRun) -> Workflow:
    try:
        if not isinstance(row.payload, dict) or row.payload.keys() != _PAYLOAD:
            raise ValueError
        values = {
            column.name: getattr(row, column.name)
            for column in ExecutionRun.__table__.columns
            if column.name in Workflow.model_fields and column.name != "payload"
        }
        values["workflow_id"] = str(values["workflow_id"])
        return Workflow.model_validate({**values, **row.payload})
    except (ValidationError, ValueError, TypeError):
        raise Conflict("Invalid stored workflow snapshot") from None


async def _append_audit(
    session: AsyncSession,
    before: Workflow | None,
    after: Workflow,
    actor: str,
    correlation_id: str,
) -> None:
    """Append in the caller's transaction. Never copy free-form history or error text."""
    metadata = {
        "source": "system" if actor == "system" else "runner",
        "state": after.state,
    }
    # Optional metadata's SQL bound is narrower than execution_runs' bigint.
    if 0 <= after.wake_seq < 10**18:
        metadata["wake_seq"] = after.wake_seq
    if before is None:
        metadata["outcome"] = "created"
    await session.execute(
        insert(AuditRecord).values(
            audit_id=uuid4(),
            tenant_id=after.tenant_id,
            workflow_id=UUID(after.workflow_id),
            actor=actor,
            event="admitted" if before is None else "transitioned",
            correlation_id=correlation_id,
            revision=after.revision,
            occurred_at=func.clock_timestamp(),
            safe_metadata=metadata,
        )
    )


def _free_predicate():
    return or_(
        ExecutionRun.lease_until.is_(None), ExecutionRun.lease_until <= func.clock_timestamp()
    )


def _due_predicates():
    return (
        ExecutionRun.state.not_in(sorted(PASSIVE)),
        ExecutionRun.next_run_at <= func.clock_timestamp(),
        _free_predicate(),
    )


class PostgresWorkflowStore:
    def __init__(self, database: Database):
        self.database = database
        # A disposable fairness hint, never authority over durable due intent.
        # Each finite sweep has a fixed upper key so new tails cannot prevent wrap.
        self._due_scan: tuple[_DueKey, _DueKey] | None = None
        self._due_scan_generation = 0

    async def initialize(self) -> None:
        await self.database.check_schema()

    async def ping(self) -> bool:
        try:
            await self.initialize()
        except SchemaNotCurrent:
            return False
        return True

    async def insert_or_get(self, workflow: Workflow) -> AdmissionResult:
        workflow = _validated_snapshot(workflow)
        _validate_admission(workflow)
        values = _encode(workflow)
        try:
            async with self.database.sessions.begin() as session:
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                values.update(
                    created_at=now,
                    updated_at=now,
                    next_run_at=now if workflow.next_run_at is not None else None,
                )
                statement = (
                    insert(ExecutionRun)
                    .values(**values)
                    .on_conflict_do_nothing(
                        constraint="uq_execution_tenant_admission",
                    )
                    .returning(ExecutionRun)
                )
                row = (await session.execute(statement)).scalar_one_or_none()
                created = row is not None
                if row is None:
                    # READ COMMITTED uses a new snapshot after the conflicting INSERT waits.
                    row = (
                        await session.execute(
                            select(ExecutionRun).where(
                                ExecutionRun.tenant_id == workflow.tenant_id,
                                ExecutionRun.admission_key == workflow.admission_key,
                            )
                        )
                    ).scalar_one()
                existing = _decode(row)
                if (
                    existing.request_hash != workflow.request_hash
                    or existing.request_text != workflow.request_text
                ):
                    raise Conflict("Admission payload mismatch")
                if created:
                    await _append_audit(
                        session,
                        None,
                        existing,
                        "system",
                        f"{existing.workflow_id}:{existing.revision}",
                    )
                return AdmissionResult(workflow=existing, created=created)
        except IntegrityError:
            raise Conflict("Persistence constraint conflict") from None
        except (SQLAlchemyError, ValueError):
            # Server DETAIL can include payloads even when SQLAlchemy hides parameters.
            raise Conflict("PostgreSQL persistence failed") from None

    async def get(self, workflow_id: str, tenant_id: str) -> Workflow:
        try:
            identity = UUID(workflow_id)
        except ValueError:
            raise NotFound("Workflow not found") from None
        try:
            async with self.database.sessions() as session:
                row = (
                    await session.execute(
                        select(ExecutionRun).where(
                            ExecutionRun.workflow_id == identity,
                            ExecutionRun.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise NotFound("Workflow not found")
                return _decode(row)
        except (SQLAlchemyError, ValueError):
            raise Conflict("PostgreSQL read failed") from None

    async def save(
        self,
        workflow: Workflow,
        expected_revision: int,
        lease_token: str | None,
    ) -> Workflow:
        workflow = _validated_snapshot(workflow)
        values = _encode(workflow)
        try:
            async with self.database.sessions.begin() as session:
                from .execution_bridge import lock_review

                await lock_review(session, workflow.tenant_id, values["workflow_id"])
                row = (
                    await session.execute(
                        select(ExecutionRun)
                        .where(
                            ExecutionRun.workflow_id == values["workflow_id"],
                            ExecutionRun.tenant_id == workflow.tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise NotFound("Workflow not found")
                old = _decode(row)
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                if lease_token is not None:
                    if old.lease_token != lease_token or not _live(old, now):
                        raise LeaseLost("Lease lost")
                elif _live(old, now):
                    raise Conflict("Active worker lease")
                _reject_new_approval(old, workflow)
                if old.approved_plan_hash is not None:
                    from .execution_bridge import execution_eligible

                    if not await execution_eligible(session, row):
                        raise Conflict("missing_review_authorization")
                _validate_change(old, workflow, expected_revision)
                changes = {key: getattr(old, key) for key in _OPERATIONAL}
                changes.update(
                    updated_at=now,
                    next_run_at=now + timedelta(milliseconds=_delay(workflow))
                    if workflow.next_run_at is not None
                    else None,
                    wake_seq=old.wake_seq + int(workflow.next_run_at is not None),
                )
                saved = _validated_snapshot(workflow.model_copy(update=changes))
                fields = {
                    key: value
                    for key, value in _encode(saved).items()
                    if key not in _IMMUTABLE | _OPERATIONAL
                }
                fields.update(
                    updated_at=func.clock_timestamp(),
                    next_run_at=func.clock_timestamp() + timedelta(milliseconds=_delay(workflow))
                    if workflow.next_run_at is not None
                    else None,
                )
                predicates = [
                    ExecutionRun.workflow_id == values["workflow_id"],
                    ExecutionRun.tenant_id == workflow.tenant_id,
                    ExecutionRun.revision == expected_revision,
                ]
                if lease_token is not None:
                    predicates.extend(
                        [
                            ExecutionRun.lease_token == lease_token,
                            ExecutionRun.lease_until > func.clock_timestamp(),
                        ]
                    )
                else:
                    predicates.append(_free_predicate())
                # Explicit SQL writes also persist numeric type/sign-only payload edits.
                # The expiry predicate runs on the server AFTER validation and bind encoding.
                row = (
                    await session.execute(
                        update(ExecutionRun)
                        .where(*predicates)
                        .values(**fields)
                        .returning(ExecutionRun)
                        .execution_options(synchronize_session=False, populate_existing=True)
                    )
                ).scalar_one_or_none()
                if row is None:
                    if lease_token is not None:
                        raise LeaseLost("Lease or revision changed")
                    raise Conflict("Revision or active lease conflict")
                result = _decode(row)
                actor = old.lease_owner if lease_token is not None else "system"
                await _append_audit(
                    session, old, result, actor, f"{result.workflow_id}:{result.revision}"
                )
                from .execution_bridge import record_plan

                await record_plan(session, row, old, result)
                return result
        except IntegrityError:
            raise Conflict("Persistence constraint conflict") from None
        except (SQLAlchemyError, ValueError):
            raise Conflict("PostgreSQL persistence failed") from None

    async def claim(self, workflow_id: str, worker_id: str, lease_s: int = 30) -> Lease | None:
        try:
            identity = UUID(workflow_id)
        except ValueError:
            return None
        if not 1 <= len(worker_id) <= 128 or "\0" in worker_id or lease_s <= 0:
            raise Conflict("Invalid lease parameters")
        try:
            async with self.database.sessions.begin() as session:
                row = (
                    await session.execute(
                        select(ExecutionRun)
                        .where(ExecutionRun.workflow_id == identity)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                if not _due(_decode(row), now):
                    return None
                from .execution_bridge import execution_eligible

                if not await execution_eligible(session, row):
                    return None
                row = (
                    await session.execute(
                        update(ExecutionRun)
                        .where(
                            ExecutionRun.workflow_id == identity,
                            *_due_predicates(),
                        )
                        .values(
                            lease_owner=worker_id,
                            lease_token=uuid4().hex,
                            lease_until=func.clock_timestamp() + timedelta(seconds=lease_s),
                        )
                        .returning(ExecutionRun)
                        .execution_options(synchronize_session=False, populate_existing=True)
                    )
                ).scalar_one_or_none()
                return _lease(_decode(row)) if row is not None else None
        except (SQLAlchemyError, ValueError):
            raise Conflict("PostgreSQL lease claim failed") from None

    async def renew(self, lease: Lease, lease_s: int = 30) -> bool:
        try:
            identity = UUID(lease.workflow_id)
        except ValueError:
            return False
        if lease_s <= 0:
            raise Conflict("Invalid lease parameters")
        try:
            async with self.database.sessions.begin() as session:
                row = (
                    await session.execute(
                        select(ExecutionRun)
                        .where(
                            ExecutionRun.workflow_id == identity,
                            ExecutionRun.tenant_id == lease.tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                if (
                    row is None
                    or row.lease_token != lease.token
                    or row.lease_owner != lease.owner
                    or row.lease_until is None
                    or row.lease_until <= now
                ):
                    return False
                result = (
                    await session.execute(
                        update(ExecutionRun)
                        .where(
                            ExecutionRun.workflow_id == identity,
                            ExecutionRun.tenant_id == lease.tenant_id,
                            ExecutionRun.lease_token == lease.token,
                            ExecutionRun.lease_owner == lease.owner,
                            ExecutionRun.lease_until > func.clock_timestamp(),
                        )
                        .values(lease_until=func.clock_timestamp() + timedelta(seconds=lease_s))
                        .returning(ExecutionRun)
                        .execution_options(synchronize_session=False, populate_existing=True)
                    )
                ).scalar_one_or_none()
                if result is None:
                    return False
                _decode(result)
                return True
        except (SQLAlchemyError, ValueError):
            raise Conflict("PostgreSQL lease renewal failed") from None

    async def catalog_for(self, workflow):
        from .policy import TenantCatalog
        from .version_models import ItemVersion

        async with self.database.sessions() as session:
            catalog = (
                await session.execute(
                    select(ItemVersion.catalog)
                    .join(
                        ExecutionRun,
                        (ExecutionRun.item_version_id == ItemVersion.id)
                        & (ExecutionRun.tenant_id == ItemVersion.tenant_id),
                    )
                    .where(
                        ExecutionRun.workflow_id == UUID(workflow.workflow_id),
                        ExecutionRun.tenant_id == workflow.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            return TenantCatalog.model_validate(catalog) if catalog is not None else None

    async def release(self, lease: Lease) -> None:
        try:
            identity = UUID(lease.workflow_id)
        except ValueError:
            return
        try:
            async with self.database.sessions.begin() as session:
                await session.execute(
                    update(ExecutionRun)
                    .where(
                        ExecutionRun.workflow_id == identity,
                        ExecutionRun.tenant_id == lease.tenant_id,
                        ExecutionRun.lease_token == lease.token,
                        ExecutionRun.lease_owner == lease.owner,
                    )
                    .values(lease_owner=None, lease_token=None, lease_until=None)
                    .execution_options(synchronize_session=False)
                )
        except (SQLAlchemyError, ValueError):
            raise Conflict("PostgreSQL lease release failed") from None

    async def due_wakeups(self, limit: int = 100, republish_after_s: int = 5) -> list[Wakeup]:
        if limit <= 0:
            return []
        batch_size = min(limit, _DUE_SCAN_BATCH)
        scan = self._due_scan
        generation = self._due_scan_generation
        try:
            async with self.database.sessions.begin() as session:
                eligible_enqueue = or_(
                    ExecutionRun.last_enqueued_at.is_(None),
                    ExecutionRun.last_enqueued_at
                    <= func.clock_timestamp() - timedelta(seconds=republish_after_s),
                )
                key = tuple_(ExecutionRun.next_run_at, ExecutionRun.workflow_id)
                if scan is None:
                    end = (
                        await session.execute(
                            select(ExecutionRun.next_run_at, ExecutionRun.workflow_id)
                            .where(*_due_predicates(), eligible_enqueue)
                            .order_by(
                                ExecutionRun.next_run_at.desc(), ExecutionRun.workflow_id.desc()
                            )
                            .limit(1)
                        )
                    ).one_or_none()
                    if end is None:
                        return []
                    upper = tuple(end)
                    predicates = [key <= upper]
                else:
                    after, upper = scan
                    predicates = [key > after, key <= upper]
                rows = (
                    (
                        await session.execute(
                            select(ExecutionRun)
                            .where(*_due_predicates(), eligible_enqueue, *predicates)
                            .order_by(ExecutionRun.next_run_at, ExecutionRun.workflow_id)
                            .limit(batch_size)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                wakeups = []
                last_key = None
                rejected = False
                for row in rows:
                    last_key = (row.next_run_at, row.workflow_id)
                    try:
                        async with session.begin_nested():
                            current = _decode(row)
                            if not _due(current, now) or (
                                current.last_enqueued_at is not None
                                and current.last_enqueued_at
                                > now - timedelta(seconds=republish_after_s)
                            ):
                                continue
                            result = (
                                await session.execute(
                                    update(ExecutionRun)
                                    .where(
                                        ExecutionRun.workflow_id == row.workflow_id,
                                        *_due_predicates(),
                                        eligible_enqueue,
                                    )
                                    .values(last_enqueued_at=func.clock_timestamp())
                                    .returning(ExecutionRun)
                                    .execution_options(
                                        synchronize_session=False, populate_existing=True
                                    )
                                )
                            ).scalar_one_or_none()
                            if result is not None:
                                marked = _decode(result)
                                wakeup = Wakeup(
                                    workflow_id=marked.workflow_id, wake_seq=marked.wake_seq
                                )
                            else:
                                continue
                        wakeups.append(wakeup)
                    except Conflict:
                        # Only a row's domain-projection rejection is recoverable.
                        # SQL/connection/savepoint failures still abort the outer transaction.
                        rejected = True
                        continue
                next_scan = (
                    (last_key, upper)
                    if len(rows) == batch_size
                    and last_key != upper
                    and (scan is not None or rejected)
                    else None
                )
            # Successful marker commits precede both wakeup publication and hint advancement.
            # Concurrent scans keep independent sessions; an older completion must not
            # overwrite a newer hint. Lost hints only repeat scans of durable SQL intent.
            if self._due_scan_generation == generation:
                self._due_scan = next_scan
                self._due_scan_generation += 1
            return wakeups
        except (SQLAlchemyError, ValueError):
            raise Conflict("PostgreSQL due scan failed") from None

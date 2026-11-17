"""One bounded repeatable-read report model for both administrator export formats."""

import csv
import io
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from .audit import append_event
from .auth import AuthenticationFailed, lock_actor
from .authorization import Denied, authorize
from .db_models import AuditRecord, ExecutionRun
from .identity_models import Membership, User
from .notification_models import ReminderJob
from .review_models import ReviewCycle, ReviewItem
from .version_models import ApprovalDefinition, Decision, ItemVersion, ReviewProgress, VersionStep

MAX_VERSION_ROWS = 10000
MAX_AUDIT_ROWS = 50000
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
FIELDS = (
    "cycle_id",
    "cycle_title",
    "cycle_state",
    "snapshot_utc",
    "item_id",
    "requester",
    "version",
    "version_id",
    "current",
    "submitted_utc",
    "request_text",
    "review_outcome",
    "level",
    "reviewer",
    "decision",
    "comment",
    "decided_utc",
    "deadline_utc",
    "plan_hash",
    "execution_outcome",
    "confirmed_effects",
    "audit_ids",
    "audit_events",
)


@dataclass(frozen=True)
class CycleReport:
    cycle: dict
    generated_at: datetime
    rows: tuple[dict, ...]
    audit: tuple[dict, ...]


def instant(value):
    return value.isoformat() if value else ""


def csv_cell(value):
    value = str(value) if value is not None else ""
    significant = value
    while significant and (
        significant[0].isspace() or unicodedata.category(significant[0]).startswith("C")
    ):
        significant = significant[1:]
    if significant.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


class _CsvOutput:
    def __init__(self):
        self.buffer = io.BytesIO()
        self.buffer.write(b"\xef\xbb\xbf")

    def write(self, value):
        encoded = value.encode("utf-8")
        if self.buffer.tell() + len(encoded) > MAX_OUTPUT_BYTES:
            raise Denied("export_too_large", 413)
        self.buffer.write(encoded)
        return len(value)


def csv_report(report: CycleReport) -> bytes:
    output = _CsvOutput()
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\r\n")
    writer.writeheader()
    common = {
        "cycle_id": report.cycle["id"],
        "cycle_title": report.cycle["title"],
        "cycle_state": report.cycle["state"],
        "snapshot_utc": instant(report.generated_at),
    }
    audit_by_id = {event["id"]: event for event in report.audit}
    for row in report.rows or ({"audit_ids": ";".join(audit_by_id)},):
        related_ids = row.get("audit_ids", "").split(";")
        record = (
            common
            | row
            | {
                "audit_events": json.dumps(
                    [audit_by_id[event_id] for event_id in related_ids if event_id in audit_by_id],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            }
        )
        writer.writerow({field: csv_cell(record.get(field, "")) for field in FIELDS})
    return output.buffer.getvalue()


async def query_report(database, actor, cycle_id) -> CycleReport:
    authorize(actor, "export", actor.tenant_id)
    try:
        cycle_id = UUID(str(cycle_id))
    except (ValueError, TypeError):
        raise Denied("not_found", 404) from None
    async with database.sessions.begin() as session:
        await lock_actor(session, actor)
        cycle = (
            await session.execute(
                select(ReviewCycle).where(
                    ReviewCycle.id == cycle_id, ReviewCycle.tenant_id == actor.tenant_id
                )
            )
        ).scalar_one_or_none()
        if cycle is None:
            raise Denied("not_found", 404)
        # This means requested, never that the client received the file.
        await append_event(session, actor, "export", cycle.id, "export_requested", cycle.revision)
    async with database.engine.connect() as connection:
        await connection.execution_options(isolation_level="REPEATABLE READ")
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                membership = (
                    await session.execute(
                        select(Membership)
                        .join(User)
                        .where(
                            Membership.id == actor.membership_id,
                            Membership.user_id == actor.user_id,
                            Membership.tenant_id == actor.tenant_id,
                            Membership.role == "ADMIN",
                            Membership.enabled.is_(True),
                            User.enabled.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                if membership is None:
                    raise AuthenticationFailed()
                cycle = (
                    await session.execute(
                        select(ReviewCycle).where(
                            ReviewCycle.id == cycle_id, ReviewCycle.tenant_id == actor.tenant_id
                        )
                    )
                ).scalar_one()
                at = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                source = (
                    await session.execute(
                        select(ReviewItem, ItemVersion)
                        .outerjoin(
                            ItemVersion,
                            (ItemVersion.item_id == ReviewItem.id)
                            & (ItemVersion.tenant_id == ReviewItem.tenant_id),
                        )
                        .where(
                            ReviewItem.tenant_id == actor.tenant_id, ReviewItem.cycle_id == cycle_id
                        )
                        .options(defer(ItemVersion.catalog))
                        .order_by(ReviewItem.id, ItemVersion.number)
                        .limit(MAX_VERSION_ROWS + 1)
                    )
                ).all()
                if len(source) > MAX_VERSION_ROWS:
                    raise Denied("export_too_large", 413)
                names = dict(
                    (
                        await session.execute(
                            select(Membership.id, User.login)
                            .join(User)
                            .where(Membership.tenant_id == actor.tenant_id)
                        )
                    ).all()
                )
                rows, relationships, entities, budget = [], [], {cycle_id}, 0
                for item, version in source:
                    entities.add(item.id)
                    related = {cycle_id, item.id}
                    base = {
                        "item_id": str(item.id),
                        "requester": names[item.requester_id],
                        "version": version.number if version else "",
                        "version_id": str(version.id) if version else "",
                        "current": "yes"
                        if version and version.id == item.current_version_id
                        else "no",
                        "submitted_utc": instant(version.created_at) if version else "",
                        "request_text": version.request_text if version else "",
                        "review_outcome": "UNSUBMITTED",
                        "execution_outcome": "NOT_STARTED",
                        "plan_hash": "",
                        "confirmed_effects": "",
                    }
                    if version:
                        entities.add(version.id)
                        progress = await session.get(ReviewProgress, version.id)
                        run = (
                            await session.execute(
                                select(
                                    ExecutionRun.state,
                                    ExecutionRun.plan_hash,
                                    ExecutionRun.workflow_id,
                                    ExecutionRun.payload,
                                ).where(
                                    ExecutionRun.tenant_id == actor.tenant_id,
                                    ExecutionRun.item_version_id == version.id,
                                )
                            )
                        ).one()
                        entities.add(run.workflow_id)
                        related.update((version.id, run.workflow_id))
                        base.update(
                            review_outcome=progress.status,
                            execution_outcome=run.state,
                            plan_hash=run.plan_hash or "",
                            confirmed_effects=json.dumps(
                                [
                                    {key: action[key] for key in ("action_id", "status", "result")}
                                    for action in run[3]["actions"]
                                    if action["status"] in {"CONFIRMED", "SKIPPED"}
                                ],
                                ensure_ascii=False,
                            ),
                        )
                        steps = (
                            (
                                await session.execute(
                                    select(VersionStep)
                                    .where(
                                        VersionStep.tenant_id == actor.tenant_id,
                                        VersionStep.version_id == version.id,
                                    )
                                    .order_by(VersionStep.level)
                                )
                            )
                            .scalars()
                            .all()
                        )
                        decisions = {
                            d.level: d
                            for d in (
                                await session.execute(
                                    select(Decision).where(
                                        Decision.tenant_id == actor.tenant_id,
                                        Decision.version_id == version.id,
                                    )
                                )
                            ).scalars()
                        }
                        jobs = {
                            job.level: job
                            for job in (
                                await session.execute(
                                    select(ReminderJob).where(
                                        ReminderJob.tenant_id == actor.tenant_id,
                                        ReminderJob.version_id == version.id,
                                    )
                                )
                            ).scalars()
                        }
                        related.update(decision.id for decision in decisions.values())
                        related.update(job.id for job in jobs.values())
                    else:
                        steps = (
                            (
                                await session.execute(
                                    select(ApprovalDefinition)
                                    .where(
                                        ApprovalDefinition.tenant_id == actor.tenant_id,
                                        ApprovalDefinition.item_id == item.id,
                                    )
                                    .order_by(ApprovalDefinition.level)
                                )
                            )
                            .scalars()
                            .all()
                        )
                        decisions, jobs = {}, {}
                    entities.update(related)
                    for step in steps or [None]:
                        decision = decisions.get(step.level) if step else None
                        job = jobs.get(step.level) if step else None
                        if decision:
                            entities.add(decision.id)
                        row = base | {
                            "level": step.level if step else "",
                            "reviewer": names[step.reviewer_id] if step else "",
                            "decision": decision.kind if decision else "",
                            "comment": decision.comment if decision else "",
                            "decided_utc": instant(decision.created_at) if decision else "",
                            "deadline_utc": instant(job.deadline_at) if job else "",
                        }
                        budget += len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
                        if budget > MAX_REPORT_BYTES:
                            raise Denied("export_too_large", 413)
                        rows.append(row)
                        relationships.append(related)
                events = (
                    (
                        await session.execute(
                            select(AuditRecord)
                            .where(
                                AuditRecord.tenant_id == actor.tenant_id,
                                or_(
                                    AuditRecord.entity_id.in_(entities),
                                    AuditRecord.workflow_id.in_(entities),
                                ),
                            )
                            .order_by(AuditRecord.occurred_at, AuditRecord.audit_id)
                            .limit(MAX_AUDIT_ROWS + 1)
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(events) > MAX_AUDIT_ROWS:
                    raise Denied("export_too_large", 413)
                audit = tuple(
                    {
                        "id": str(event.audit_id),
                        "at": instant(event.occurred_at),
                        "actor": event.actor,
                        "event": event.event,
                        "entity_id": str(event.entity_id or event.workflow_id),
                        "entity_type": event.entity_type,
                        "workflow_id": str(event.workflow_id) if event.workflow_id else "",
                        "revision": event.revision,
                    }
                    for event in events
                )
                grouped = {}
                event_order = {event["id"]: index for index, event in enumerate(audit)}
                for event in audit:
                    for key in {event["entity_id"], event["workflow_id"]} - {""}:
                        grouped.setdefault(key, []).append(event["id"])
                for row, related in zip(rows, relationships, strict=True):
                    row["audit_ids"] = ";".join(
                        sorted(
                            {event_id for key in related for event_id in grouped.get(str(key), ())},
                            key=event_order.__getitem__,
                        )
                    )
                    budget += len(row["audit_ids"].encode())
                    if budget > MAX_REPORT_BYTES:
                        raise Denied("export_too_large", 413)
                cycle_data = {
                    "id": str(cycle.id),
                    "title": cycle.title,
                    "state": cycle.state,
                    "revision": cycle.revision,
                    "created_at": instant(cycle.created_at),
                    "completed_at": instant(cycle.completed_at),
                }
                if (
                    len(
                        json.dumps(
                            {
                                "cycle": cycle_data,
                                "generated_at": at.isoformat(),
                                "rows": rows,
                                "audit": audit,
                            },
                            ensure_ascii=False,
                        ).encode()
                    )
                    > MAX_REPORT_BYTES
                ):
                    raise Denied("export_too_large", 413)
                return CycleReport(cycle_data, at, tuple(rows), audit)


def pdf_report(report):
    from .pdf_export import render_pdf

    return render_pdf(report)

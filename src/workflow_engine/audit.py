"""Business facts append in the caller's transaction, never a separate commit."""

import base64
import json
import re
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_

from .auth import lock_actor
from .authorization import Denied, authorize
from .db_models import AuditRecord


async def append_event(
    session,
    actor,
    entity_type,
    entity_id,
    event,
    revision,
    *,
    correlation_id=None,
    before_revision=None,
    before_state=None,
    after_state=None,
    workflow_id=None,
    tenant_id=None,
    principal_type="USER",
):
    """No request bodies/comments/secrets. Source records retain the detailed content."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", event):
        raise ValueError("Invalid event type")
    if entity_type not in {
        "tenant",
        "account",
        "session",
        "cycle",
        "item",
        "version",
        "decision",
        "execution",
        "export",
        "reminder",
    }:
        raise ValueError("Invalid entity type")
    for state in (before_state, after_state):
        if state is not None and not re.fullmatch(r"[A-Z_]{1,32}", state):
            raise ValueError("Invalid audit state")
    identity = uuid4()
    from .provenance import demo_seed

    synthetic = demo_seed.get()
    session.add(
        AuditRecord(
            audit_id=identity,
            tenant_id=actor.tenant_id if actor else tenant_id,
            workflow_id=workflow_id,
            actor="seed-demo" if synthetic else str(actor.membership_id) if actor else "system",
            actor_type="SYSTEM" if synthetic else principal_type if actor else "SYSTEM",
            actor_membership_id=actor.membership_id if actor and not synthetic else None,
            entity_type=entity_type,
            entity_id=entity_id,
            event=event,
            correlation_id=correlation_id or str(uuid4()),
            revision=revision,
            before_revision=before_revision,
            before_state=before_state,
            after_state=after_state,
            occurred_at=func.clock_timestamp(),
            safe_metadata={"source": "system", "reason_code": "demo_seed_v1"} if synthetic else {},
        )
    )
    await session.flush()
    return identity


async def list_events(database, actor, *, limit=50, cursor=None, entity_id=None):
    authorize(actor, "audit", actor.tenant_id)
    if not 1 <= limit <= 100:
        raise Denied("invalid_page", 422)
    predicates = [AuditRecord.tenant_id == actor.tenant_id]
    if entity_id is not None:
        predicates.append(AuditRecord.entity_id == entity_id)
    if cursor:
        try:
            raw = json.loads(base64.urlsafe_b64decode(cursor).decode())
            at, identity = datetime.fromisoformat(raw[0]), UUID(raw[1])
            if at.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError, IndexError, UnicodeError):
            raise Denied("invalid_cursor", 422) from None
        predicates.append(tuple_(AuditRecord.occurred_at, AuditRecord.audit_id) < (at, identity))
    async with database.sessions.begin() as session:
        await lock_actor(session, actor)
        rows = (
            (
                await session.execute(
                    select(AuditRecord)
                    .where(*predicates)
                    .order_by(AuditRecord.occurred_at.desc(), AuditRecord.audit_id.desc())
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page, next_cursor = rows[:limit], None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([last.occurred_at.isoformat(), str(last.audit_id)]).encode()
            ).decode()
        return {
            "events": [
                {
                    key: getattr(row, key)
                    for key in (
                        "audit_id",
                        "actor",
                        "actor_type",
                        "entity_type",
                        "entity_id",
                        "event",
                        "correlation_id",
                        "revision",
                        "before_revision",
                        "before_state",
                        "after_state",
                        "occurred_at",
                    )
                }
                for row in page
            ],
            "next_cursor": next_cursor,
        }

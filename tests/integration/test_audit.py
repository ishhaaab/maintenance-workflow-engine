from uuid import uuid4

import pytest
from sqlalchemy import select, text

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def test_audit_is_atomic_named_paginated_and_not_operational_history(postgres_store):
    from workflow_engine.audit import append_event, list_events
    from workflow_engine.auth import AuthService
    from workflow_engine.settings import Settings

    database = postgres_store.database
    auth = AuthService(database, Settings(env="local", origin="http://localhost"))
    actor = await auth.bootstrap("audit", "audit-admin", "a-safe-password-123")
    entity = uuid4()
    with pytest.raises(RuntimeError):
        async with database.sessions.begin() as session:
            await append_event(
                session, actor, "cycle", entity, "cycle_created", 0, correlation_id="rolled-back"
            )
            raise RuntimeError("roll back both changes")
    async with database.sessions.begin() as session:
        for revision in range(260):
            await append_event(
                session,
                actor,
                "cycle",
                entity,
                "cycle_updated",
                revision,
                correlation_id=str(revision),
                before_state="DRAFT",
                after_state="DRAFT",
            )
    page = await list_events(database, actor, limit=100, entity_id=entity)
    assert len(page["events"]) == 100
    ids = [row["audit_id"] for row in page["events"]]
    assert page["events"][0]["actor"] == str(actor.membership_id)
    while page["next_cursor"]:
        page = await list_events(
            database, actor, limit=100, entity_id=entity, cursor=page["next_cursor"]
        )
        ids += [row["audit_id"] for row in page["events"]]
    assert len(ids) == len(set(ids)) == 260
    async with database.sessions() as session:
        assert not (
            await session.execute(
                text("SELECT 1 FROM audit_records WHERE correlation_id = 'rolled-back'")
            )
        ).all()
    from workflow_engine.authorization import Denied

    reviewer = await auth.create_account(actor, "audit-reviewer", "a-safe-password-123", "REVIEWER")
    with pytest.raises(Denied):
        await list_events(database, reviewer)


async def test_runtime_audit_grants_and_atomic_identity_attribution(postgres_store):
    from sqlalchemy.exc import DBAPIError

    from workflow_engine.auth import AuthService
    from workflow_engine.db_models import AuditRecord
    from workflow_engine.settings import Settings

    database = postgres_store.database
    auth = AuthService(database, Settings(env="local", origin="http://localhost"))
    actor = await auth.bootstrap("audit", "audit-admin", "a-safe-password-123")
    await auth.create_account(actor, "audit-requester", "a-safe-password-123", "REQUESTER")
    async with database.sessions() as session:
        events = (
            (
                await session.execute(
                    select(AuditRecord).where(AuditRecord.tenant_id == actor.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert {row.event for row in events} >= {"tenant_bootstrapped", "account_created"}
        account = next(row for row in events if row.event == "account_created")
        assert account.actor == str(actor.membership_id)
        assert account.actor_type == "USER"
        assert "password" not in str(account.safe_metadata)
    for sql in (
        "UPDATE audit_records SET event='changed'",
        "DELETE FROM audit_records",
        "TRUNCATE audit_records",
    ):
        with pytest.raises(DBAPIError):
            async with database.sessions.begin() as session:
                await session.execute(text(sql))

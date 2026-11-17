"""Prepare an unapproved legacy executor fixture, then use real HTTP reviewer sessions.

Only exact invocation-owned test databases admit the prepared source linkage. The
final authorization is created by production review commands, not owner SQL.
"""

import secrets
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from tests.postgres_support import owner_session
from workflow_engine.api import create_app
from workflow_engine.auth import AuthService
from workflow_engine.db_models import AuditRecord, ExecutionRun
from workflow_engine.domain import Conflict
from workflow_engine.identity_models import Membership, User
from workflow_engine.policy import CatalogProvider
from workflow_engine.review_domain import chain_fingerprint
from workflow_engine.review_models import CycleRequester, ReviewCycle, ReviewItem
from workflow_engine.settings import Settings
from workflow_engine.version_models import (
    ApprovalDefinition,
    ItemVersion,
    ReviewProgress,
    ValidatedPlan,
    VersionStep,
)


async def approve_pending_review(store, workflow, expected_revision, plan_hash):
    current = await store.get(workflow.workflow_id, workflow.tenant_id)
    if (
        current.revision != expected_revision
        or current.plan_hash != plan_hash
        or current.state != "AWAITING_APPROVAL"
    ):
        raise Conflict("Fixture revision or plan hash mismatch")
    settings = Settings(env="local", origin="http://127.0.0.1")
    password = secrets.token_urlsafe(24)
    hashed = await AuthService(store.database, settings).password_hash(password)
    catalog = CatalogProvider.from_file("fixtures/catalog.jsonl")
    tenant, at = current.tenant_id, current.updated_at
    members, logins = [], []
    async with owner_session(store) as session:
        row = (
            await session.execute(
                select(ExecutionRun)
                .where(
                    ExecutionRun.workflow_id == UUID(current.workflow_id),
                    ExecutionRun.tenant_id == tenant,
                )
                .with_for_update()
            )
        ).scalar_one()
        if row.item_version_id is not None:
            raise Conflict("Prepared fixture is already linked")
        for role in ("ADMIN", "REQUESTER", "REVIEWER", "REVIEWER"):
            login = "fault-" + uuid4().hex
            user = User(id=uuid4(), login=login, password_hash=hashed, enabled=True)
            session.add(user)
            await session.flush()
            member = Membership(
                id=uuid4(), tenant_id=tenant, user_id=user.id, role=role, enabled=True
            )
            session.add(member)
            await session.flush()
            members.append(member)
            logins.append(login)
        admin, requester, *reviewers = members
        cycle = ReviewCycle(
            id=uuid4(),
            tenant_id=tenant,
            creator_id=admin.id,
            title="Named fault acceptance",
            state="DRAFT",
            revision=0,
            created_at=at,
            updated_at=at,
        )
        session.add(cycle)
        await session.flush()
        session.add(CycleRequester(tenant_id=tenant, cycle_id=cycle.id, membership_id=requester.id))
        await session.flush()
        item = ReviewItem(
            id=uuid4(),
            tenant_id=tenant,
            cycle_id=cycle.id,
            requester_id=requester.id,
            revision=0,
            created_at=at,
        )
        session.add(item)
        await session.flush()
        chain = [{"reviewer_id": str(member.id), "due_after_s": 3600} for member in reviewers]
        for level, member in enumerate(reviewers, 1):
            session.add(
                ApprovalDefinition(
                    id=uuid4(),
                    tenant_id=tenant,
                    item_id=item.id,
                    level=level,
                    reviewer_id=member.id,
                    due_after_s=3600,
                )
            )
        await session.flush()
        version = ItemVersion(
            id=uuid4(),
            tenant_id=tenant,
            item_id=item.id,
            number=1,
            submitted_by=requester.id,
            request_text=current.request_text,
            request_hash=current.request_hash,
            definition_hash=chain_fingerprint(chain),
            catalog=catalog.get(tenant).model_dump(mode="json"),
            created_at=at,
        )
        session.add(version)
        await session.flush()
        item.current_version_id, row.item_version_id = version.id, version.id
        session.add(
            ReviewProgress(tenant_id=tenant, version_id=version.id, status="PENDING", revision=0)
        )
        for level, member in enumerate(reviewers, 1):
            session.add(
                VersionStep(
                    id=uuid4(),
                    tenant_id=tenant,
                    version_id=version.id,
                    level=level,
                    reviewer_id=member.id,
                    due_after_s=3600,
                )
            )
        await session.flush()
        session.add(
            ValidatedPlan(
                id=uuid4(),
                tenant_id=tenant,
                version_id=version.id,
                run_id=row.workflow_id,
                plan_hash=current.plan_hash,
                schema_version=1,
                catalog_version=current.catalog_version,
                plan=current.plan.model_dump(mode="json"),
                actions=[action.model_dump(mode="json") for action in current.actions],
                created_at=at,
            )
        )
        session.add(
            AuditRecord(
                audit_id=uuid4(),
                tenant_id=tenant,
                workflow_id=row.workflow_id,
                actor="system",
                event="fixture_review_prepared",
                correlation_id=str(uuid4()),
                revision=current.revision,
                occurred_at=at,
                safe_metadata={"source": "system", "reason_code": "fault_setup"},
                entity_type="version",
                entity_id=version.id,
            )
        )
        cycle_id, item_id = str(cycle.id), str(item.id)
    app = create_app(settings, store, catalog)
    for index in (0, 2, 3):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.origin
        ) as client:
            pre = (await client.get("/api/session")).json()
            headers = {"Origin": settings.origin, "X-CSRF-Token": pre["csrf"]}
            logged = await client.post(
                "/api/login",
                json={"tenant": tenant, "login": logins[index], "password": password},
                headers=headers,
            )
            assert logged.status_code == 200
            headers["X-CSRF-Token"] = logged.json()["csrf"]
            if index == 0:
                response = await client.post(
                    f"/api/cycles/{cycle_id}/activate",
                    json={"expected_revision": 0},
                    headers=headers,
                )
            else:
                projection = (await client.get(f"/api/items/{item_id}")).json()
                response = await client.post(
                    f"/api/items/{item_id}/decisions",
                    json={
                        "version_id": projection["current_version_id"],
                        "level": projection["progress"]["current_level"],
                        "plan_hash": projection["plan_hash"],
                        "decision": "APPROVE",
                        "comment": "Named fault acceptance",
                        "expected_revision": projection["revision"],
                        "expected_progress_revision": projection["progress"]["revision"],
                        "command_id": str(uuid4()),
                    },
                    headers=headers,
                )
            assert response.status_code == 200
            if index == 2:
                assert (await store.get(current.workflow_id, tenant)).approved_plan_hash is None
    return await store.get(current.workflow_id, tenant)

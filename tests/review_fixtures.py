"""Owner-only review evidence for isolated executor contract/fault fixtures.

No production setting, HTTP route or runtime approval capability imports this module.
The original run identity, plan and action keys are preserved exactly.
"""

from uuid import uuid4

from workflow_engine.identity_models import Membership, User
from workflow_engine.review_domain import chain_fingerprint
from workflow_engine.review_models import CycleRequester, ReviewCycle, ReviewItem
from workflow_engine.version_models import (
    ApprovalDefinition,
    Decision,
    ItemVersion,
    ReviewProgress,
    ValidatedPlan,
    VersionStep,
)


async def attach_approved_review(session, row, workflow):
    from workflow_engine._storage_codec import _encode as encode_opaque

    if row.item_version_id is not None:
        return
    tenant, at = workflow.tenant_id, workflow.updated_at
    members = []
    for role in ("ADMIN", "REQUESTER", "REVIEWER"):
        user = User(
            id=uuid4(),
            login="fixture-" + uuid4().hex,
            password_hash="disabled-fixture-identity",
            enabled=False,
        )
        session.add(user)
        await session.flush()
        member = Membership(id=uuid4(), tenant_id=tenant, user_id=user.id, role=role, enabled=True)
        session.add(member)
        await session.flush()
        members.append(member)
    admin, requester, reviewer = members
    cycle = ReviewCycle(
        id=uuid4(),
        tenant_id=tenant,
        creator_id=admin.id,
        title="Executor fixture",
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
    chain = [{"reviewer_id": str(reviewer.id), "due_after_s": 3600}]
    session.add(
        ApprovalDefinition(
            id=uuid4(),
            tenant_id=tenant,
            item_id=item.id,
            level=1,
            reviewer_id=reviewer.id,
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
        request_text=workflow.request_text,
        request_hash=workflow.request_hash,
        definition_hash=chain_fingerprint(chain),
        catalog={"fixture": True},
        created_at=at,
    )
    session.add(version)
    await session.flush()
    item.current_version_id, row.item_version_id = version.id, version.id
    session.add(
        VersionStep(
            id=uuid4(),
            tenant_id=tenant,
            version_id=version.id,
            level=1,
            reviewer_id=reviewer.id,
            due_after_s=3600,
        )
    )
    session.add(
        ReviewProgress(tenant_id=tenant, version_id=version.id, status="APPROVED", revision=1)
    )
    await session.flush()
    plan = ValidatedPlan(
        id=uuid4(),
        tenant_id=tenant,
        version_id=version.id,
        run_id=row.workflow_id,
        plan_hash=workflow.plan_hash,
        schema_version=1,
        catalog_version=workflow.catalog_version or "fixture",
        plan=workflow.plan.model_dump(mode="json") if workflow.plan else {},
        # Synthetic executor codec cases may contain numbers outside the policy grammar.
        # Preserve those in execution payloads; this owner-only evidence uses explicit tags.
        actions=encode_opaque([action.model_dump(mode="json") for action in workflow.actions]),
        created_at=at,
    )
    session.add(plan)
    await session.flush()
    session.add(
        Decision(
            id=uuid4(),
            tenant_id=tenant,
            version_id=version.id,
            level=1,
            actor_id=reviewer.id,
            plan_hash=workflow.plan_hash,
            kind="APPROVE",
            comment="Executor fixture provenance",
            created_at=at,
            command_id=uuid4(),
            payload_hash="0" * 64,
        )
    )
    row.approved_plan_id = plan.id
    cycle.state, cycle.completed_at = "COMPLETED", at
    await session.flush()

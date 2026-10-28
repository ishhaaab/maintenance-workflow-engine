import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from workflow_engine.domain import (
    Conflict,
    DomainEvent,
    LeaseLost,
    NotFound,
    Workflow,
    WorkflowCreate,
    transition,
)

pytestmark = pytest.mark.integration


async def test_real_mongo_admission_claim_and_stale_worker(mongo_store):
    store = mongo_store
    results = await asyncio.gather(
        *(
            store.insert_or_get(Workflow.new("a", "same", WorkflowCreate(text="request")))
            for _ in range(20)
        )
    )
    assert sum(r.created for r in results) == 1
    workflow = results[0].workflow
    other = await store.insert_or_get(
        Workflow.new("b", "same", WorkflowCreate(text="different tenant"))
    )
    assert other.created
    assert other.workflow.workflow_id != workflow.workflow_id
    with pytest.raises(Conflict):
        await store.insert_or_get(Workflow.new("a", "same", WorkflowCreate(text="changed")))
    with pytest.raises(NotFound):
        await store.get(workflow.workflow_id, "b")
    claims = await asyncio.gather(*(store.claim(workflow.workflow_id, str(i)) for i in range(20)))
    old = next(c for c in claims if c)
    assert len([c for c in claims if c]) == 1
    proposed = transition(old.workflow, DomainEvent(kind="planning_started"))
    with pytest.raises(Conflict):
        await store.save(proposed, 0, None)
    with pytest.raises(Conflict):
        await store.save(proposed.model_copy(update={"request_text": "tampered"}), 0, old.token)
    await store.collection.update_one(
        {"workflow_id": workflow.workflow_id},
        {"$set": {"lease_until": datetime.now(UTC) - timedelta(seconds=1)}},
    )
    new = await store.claim(workflow.workflow_id, "new")
    with pytest.raises(LeaseLost):
        await store.save(proposed, 0, old.token)
    await store.release(old)
    assert await store.renew(new)
    assert not await store.renew(old)
    updated = await store.save(
        transition(new.workflow, DomainEvent(kind="planning_started")), 0, new.token
    )
    assert updated.revision == 1
    with pytest.raises(Conflict):
        await store.save(proposed, 0, new.token)


async def test_real_due_intent_and_enqueue_metadata_are_preserved(mongo_store):
    store = mongo_store
    workflow = (
        await store.insert_or_get(Workflow.new("a", "k", WorkflowCreate(text="request")))
    ).workflow
    wakeups = await store.due_wakeups()
    assert [w.workflow_id for w in wakeups] == [workflow.workflow_id]
    assert await store.due_wakeups() == []
    await store.collection.update_one(
        {"workflow_id": workflow.workflow_id},
        {"$set": {"last_enqueued_at": datetime.now(UTC) - timedelta(seconds=6)}},
    )
    assert len(await store.due_wakeups()) == 1
    lease = await store.claim(workflow.workflow_id, "one")
    await store.renew(lease, lease_s=60)
    updated = await store.save(
        transition(lease.workflow, DomainEvent(kind="planning_started")), 0, lease.token
    )
    assert updated.lease_until > lease.expiry
    assert updated.last_enqueued_at is not None

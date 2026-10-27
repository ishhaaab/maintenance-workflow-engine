import asyncio

import pytest

from workflow_engine.domain import (
    Conflict,
    DomainEvent,
    LeaseLost,
    Workflow,
    WorkflowCreate,
    transition,
)
from workflow_engine.store import MemoryWorkflowStore


async def insert(store, key="key", text="Create an inspection ticket for P-17", tenant="tenant-a"):
    return await store.insert_or_get(Workflow.new(tenant, key, WorkflowCreate(text=text)))


async def test_concurrent_admission_and_payload_conflict(clock):
    store = MemoryWorkflowStore(clock)
    results = await asyncio.gather(*(insert(store) for _ in range(20)))
    assert sum(r.created for r in results) == 1
    assert len({r.workflow.workflow_id for r in results}) == 1
    with pytest.raises(Conflict):
        await insert(store, text="different")
    assert (await insert(store, tenant="tenant-b")).created


async def test_claim_fencing_revision_and_protected_fields(clock):
    store = MemoryWorkflowStore(clock)
    workflow = (await insert(store)).workflow
    leases = await asyncio.gather(*(store.claim(workflow.workflow_id, str(i)) for i in range(20)))
    first = next(lease for lease in leases if lease)
    assert len([lease for lease in leases if lease]) == 1
    changed = transition(first.workflow, DomainEvent(kind="planning_started", at=clock()))
    with pytest.raises(Conflict):
        await store.save(changed, 0, None)
    with pytest.raises(Conflict):
        await store.save(changed.model_copy(update={"request_text": "tampered"}), 0, first.token)
    clock.advance()
    second = await store.claim(workflow.workflow_id, "new")
    with pytest.raises(LeaseLost):
        await store.save(changed, 0, first.token)
    assert not await store.renew(first)
    await store.release(first)
    assert await store.renew(second)
    saved = await store.save(
        transition(second.workflow, DomainEvent(kind="planning_started", at=clock())),
        0,
        second.token,
    )
    assert saved.revision == 1
    with pytest.raises(Conflict):
        await store.save(changed, 0, second.token)


async def test_due_work_survives_lost_publish_and_replays(clock):
    store = MemoryWorkflowStore(clock)
    workflow = (await insert(store)).workflow
    assert (await store.due_wakeups())[0].workflow_id == workflow.workflow_id
    assert await store.due_wakeups() == []
    clock.advance(5)
    assert len(await store.due_wakeups()) == 1
    lease = await store.claim(workflow.workflow_id, "worker")
    clock.advance(5)
    assert await store.due_wakeups() == []
    await store.release(lease)
    assert len(await store.due_wakeups()) == 1

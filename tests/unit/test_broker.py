import pytest

from workflow_engine.broker import dispatch_once
from workflow_engine.domain import Workflow, WorkflowCreate
from workflow_engine.store import MemoryWorkflowStore


class RecordingBroker:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    async def publish(self, wakeup):
        if self.fail:
            raise ConnectionError("Redis unavailable")
        self.messages.append(wakeup)
        return str(len(self.messages))


async def test_dispatcher_recovers_after_publish_failure_and_redis_data_loss(clock):
    store = MemoryWorkflowStore(clock)
    workflow = (
        await store.insert_or_get(Workflow.new("a", "k", WorkflowCreate(text="request")))
    ).workflow
    broker = RecordingBroker(fail=True)
    with pytest.raises(ConnectionError):
        await dispatch_once(store, broker)
    assert (await store.get(workflow.workflow_id, "a")).next_run_at is not None
    broker.fail = False
    assert await dispatch_once(store, broker) == 0
    clock.advance(5)
    assert await dispatch_once(store, broker) == 1
    broker.messages.clear()
    clock.advance(5)
    assert await dispatch_once(store, broker) == 1
    assert broker.messages[0].workflow_id == workflow.workflow_id

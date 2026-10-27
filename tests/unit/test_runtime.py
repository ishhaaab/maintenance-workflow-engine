import asyncio
from contextlib import asynccontextmanager

import pytest

from workflow_engine import runtime


async def test_dependency_failure_reconnects_without_logging_error(monkeypatch, caplog):
    class OfflineBroker:
        calls = 0

        async def receive(self, worker_id):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("redis unavailable")
            raise asyncio.CancelledError()

    broker = OfflineBroker()

    @asynccontextmanager
    async def resources():
        yield None, broker, None

    async def no_wait(seconds):
        pass

    monkeypatch.setattr(runtime, "resources", resources)
    monkeypatch.setattr(runtime.asyncio, "sleep", no_wait)
    with pytest.raises(asyncio.CancelledError):
        await runtime.run_process("worker", "worker-test")
    assert broker.calls == 2

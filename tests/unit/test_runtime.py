import asyncio
from contextlib import asynccontextmanager

import pytest
from pydantic import SecretStr

from workflow_engine import runtime
from workflow_engine.settings import Settings


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


def runtime_settings(**values):
    return Settings(
        database_url=SecretStr("postgresql://runtime:private-password@host/db"),
        env="local",
        origin="http://127.0.0.1:8000",
        **values,
    )


def install_clients(monkeypatch, events, *, fail_schema=False):
    class Database:
        async def check_schema(self):
            events.append("schema")
            if fail_schema:
                from workflow_engine.db import SchemaNotCurrent

                raise SchemaNotCurrent("private-connection-marker")

        async def close(self):
            events.append("database_closed")

    class Redis:
        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls()

        async def aclose(self):
            events.append("redis_closed")

    monkeypatch.setattr(runtime, "create_database", lambda url: Database())
    monkeypatch.setattr(runtime, "Redis", Redis)


@pytest.mark.parametrize("fail_schema", [False, True])
async def test_schema_only_init_never_constructs_redis_planner_or_effect_clients(
    monkeypatch, fail_schema
):
    events = []
    install_clients(monkeypatch, events, fail_schema=fail_schema)
    monkeypatch.setattr(runtime, "Settings", lambda: runtime_settings(planner="model"))

    def forbidden(*args, **kwargs):
        pytest.fail("Schema-only init constructed an executor dependency")

    monkeypatch.setattr(runtime.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(runtime.Redis, "from_url", forbidden)
    monkeypatch.setattr(runtime.FixturePlanner, "from_file", forbidden)
    if fail_schema:
        from workflow_engine.db import SchemaNotCurrent

        with pytest.raises(SchemaNotCurrent):
            await runtime.run_process("init", "operator")
    else:
        assert await runtime.run_process("init", "operator") == {"status": "schema_current"}
    assert events == ["schema", "database_closed"]


@pytest.mark.parametrize("fail_schema", [False, True])
@pytest.mark.parametrize("planner", ["fixture", "model"])
async def test_executor_resources_close_database_redis_and_all_http_clients(
    monkeypatch, fail_schema, planner
):
    events, clients = [], []
    install_clients(monkeypatch, events, fail_schema=fail_schema)
    real_client = runtime.httpx.AsyncClient

    def client(*args, **kwargs):
        value = real_client(*args, **kwargs)
        clients.append(value)
        return value

    monkeypatch.setattr(runtime.httpx, "AsyncClient", client)
    from workflow_engine.db import SchemaNotCurrent

    try:
        async with runtime.resources(runtime_settings(planner=planner)):
            raise RuntimeError("synthetic executor failure")
    except (SchemaNotCurrent, RuntimeError):
        pass
    assert "database_closed" in events and "redis_closed" in events
    assert all(client.is_closed for client in clients)
    assert len(clients) == (0 if fail_schema else 2 + int(planner == "model"))


async def test_api_lifespan_closes_clients_even_when_schema_is_unavailable(monkeypatch, caplog):
    events = []
    install_clients(monkeypatch, events, fail_schema=True)
    monkeypatch.setattr(runtime, "Settings", runtime_settings)
    app = runtime.api_application()
    async with app.router.lifespan_context(app):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 503
            assert (await client.get("/health/live")).status_code == 200
    assert "database_closed" in events and "redis_closed" in events
    assert "private" not in caplog.text


def test_cli_selects_a_psycopg_compatible_native_windows_loop():
    from workflow_engine import cli

    loop = cli.runtime_loop()
    try:
        import sys

        if sys.platform == "win32":
            assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


async def test_fresh_default_stream_and_group_are_used_by_executor_resources(monkeypatch):
    install_clients(monkeypatch, [])
    async with runtime.resources(runtime_settings()) as (_, broker, _):
        assert broker.stream == "workflow-pg-wakeups-v1"
        assert broker.group == "workflow-pg-workers-v1"
        assert broker.reclaim_idle_ms == 60000

"""Real PostgreSQL/Redis runtime construction, prepared execution and native CLI checks."""

import asyncio
import os
import socket
import subprocess
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import event, text

from tests.postgres_support import (
    PostgresTests,
    control_execution,
    register_fixture_store,
    seed_approved_run,
)
from tests.processes import child_environment as service_environment
from tests.processes import free_port, simulator_process
from tests.support import REQUEST
from workflow_engine import runtime
from workflow_engine.broker import dispatch_once
from workflow_engine.domain import Conflict, DomainEvent, Workflow, WorkflowCreate, transition
from workflow_engine.postgres_store import PostgresWorkflowStore
from workflow_engine.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def redis_url():
    value = os.environ.get("TEST_REDIS_URL")
    if not value:
        pytest.skip("Set TEST_REDIS_URL for the real runtime proof")
    return SecretStr(value)


async def test_runtime_prepared_executor_recovers_with_real_postgres_redis_and_http(
    tmp_path, monkeypatch
):
    stream, group = "task4-" + uuid4().hex, "task4-workers-" + uuid4().hex
    clients, databases, disposed = [], [], []
    original_client, original_database = (
        httpx.AsyncClient,
        runtime.create_database,
    )

    def client(*args, **kwargs):
        result = original_client(*args, **kwargs)
        clients.append(result)
        return result

    def database(url):
        result = original_database(url)
        databases.append(result)
        event.listen(
            result.engine.sync_engine, "engine_disposed", lambda engine: disposed.append(engine)
        )
        return result

    monkeypatch.setattr(runtime.httpx, "AsyncClient", client)
    monkeypatch.setattr(runtime, "create_database", database)
    with PostgresTests.from_environment() as postgres:
        allocation = postgres.allocate(migrate=True)
        with allocation.connect_runtime() as connection:
            connection.execute("INSERT INTO tenants (id, slug) VALUES ('tenant-a', 'tenant-a')")
        with (
            simulator_process("ticketing", tmp_path / "ticket") as (ticket_url, _),
            simulator_process("inventory", tmp_path / "inventory") as (inventory_url, _),
        ):
            settings = Settings(
                database_url=allocation.runtime_url,
                redis_url=redis_url(),
                redis_stream=stream,
                redis_group=group,
                ticketing_url=ticket_url,
                inventory_url=inventory_url,
            )
            redis_clients = []
            cleanup = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
            try:
                async with runtime.resources(settings) as (store, broker, runner):
                    assert isinstance(store, PostgresWorkflowStore)
                    register_fixture_store(postgres, allocation, store)
                    redis_clients.append(broker.redis)
                    assert broker.stream == stream and broker.group == group
                    async with store.database.sessions() as session:
                        assert (
                            await session.execute(
                                text(
                                    "SELECT current_user = :role AND NOT rolsuper AND NOT rolcreatedb "
                                    "AND NOT rolcreaterole FROM pg_roles WHERE rolname = current_user"
                                ),
                                {"role": allocation.runtime_role},
                            )
                        ).scalar_one()
                    w = (
                        await store.insert_or_get(
                            Workflow.new("tenant-a", "prepared", WorkflowCreate(text=REQUEST))
                        )
                    ).workflow
                    assert await dispatch_once(store, broker) == 1
                    message = await broker.receive("planning-worker")
                    assert message.wakeup.workflow_id == w.workflow_id
                    sessions, heartbeat_completed = [], asyncio.Event()
                    original_begin, original_renew = store.database.sessions.begin, store.renew

                    @asynccontextmanager
                    async def observed_session():
                        async with original_begin() as session:
                            sessions.append((asyncio.current_task(), session))
                            yield session

                    async def observed_renew(*args, **kwargs):
                        result = await original_renew(*args, **kwargs)
                        heartbeat_completed.set()
                        return result

                    propose = runner.planner.propose

                    async def wait_for_real_heartbeat(*args, **kwargs):
                        await asyncio.wait_for(heartbeat_completed.wait(), 8)
                        return await propose(*args, **kwargs)

                    monkeypatch.setattr(store.database.sessions, "begin", observed_session)
                    monkeypatch.setattr(store, "renew", observed_renew)
                    monkeypatch.setattr(runner.planner, "propose", wait_for_real_heartbeat)
                    await runner.tick(w.workflow_id, "planning-worker")
                    tick_sessions = [
                        session for task, session in sessions if task is asyncio.current_task()
                    ]
                    heartbeat_sessions = [
                        session for task, session in sessions if task is not asyncio.current_task()
                    ]
                    assert tick_sessions and heartbeat_sessions
                    assert not {id(session) for session in tick_sessions} & {
                        id(session) for session in heartbeat_sessions
                    }
                    await broker.ack(message.message_id)
                    w = await store.get(w.workflow_id, w.tenant_id)
                    assert w.state == "AWAITING_APPROVAL" and w.approved_plan_hash is None
                    assert (
                        await runner.tick(w.workflow_id, "unapproved-worker")
                    ).status == "not_claimed"
                    with pytest.raises(Conflict):
                        await store.save(
                            transition(w, DomainEvent(kind="approved", plan_hash=w.plan_hash)),
                            w.revision,
                            None,
                        )
                    async with httpx.AsyncClient(base_url=ticket_url) as effects:
                        assert (await effects.get("/__effects")).json() == {}
                    w = await seed_approved_run(store, w, w.revision, w.plan_hash)
                    keys = [action.idempotency_key for action in w.actions]
                # New product resources reconstruct intent. Approval came only from the
                # registered test-owner capability before this runtime was constructed.
                async with runtime.resources(settings) as (store, broker, runner):
                    register_fixture_store(postgres, allocation, store)
                    redis_clients.append(broker.redis)
                    for _ in range(2):
                        control_execution(store, w, enqueue_due=True)
                        assert await dispatch_once(store, broker) == 1
                        message = await broker.receive("replacement-worker")
                        assert message.wakeup.workflow_id == w.workflow_id
                        await runner.tick(w.workflow_id, "replacement-worker")
                        await broker.ack(message.message_id)
                    loaded = await store.get(w.workflow_id, w.tenant_id)
                    assert loaded.state == "SUCCEEDED"
                    assert [a.idempotency_key for a in loaded.actions] == keys
                    assert [a.submissions for a in loaded.actions] == [1, 1]
                    assert loaded.approved_plan_hash == w.plan_hash
                    assert loaded.actions[1].arguments == w.actions[1].arguments
                    assert await store.due_wakeups() == []
                for url in [ticket_url, inventory_url]:
                    async with httpx.AsyncClient(base_url=url) as effects:
                        assert sum((await effects.get("/__effects")).json().values()) == 1
                assert len(disposed) == len(databases) == 2
                assert all(client.is_closed for client in clients)
                assert all(
                    not connection.is_connected
                    for redis in redis_clients
                    for connection in redis.connection_pool._available_connections
                )
            finally:
                await cleanup.delete(stream)
                await cleanup.aclose()


def child_environment(runtime_url):
    # An API/worker process receives no owner/admin allocation or helper config.
    return service_environment(
        WORKFLOW_DATABASE_URL=runtime_url.get_secret_value(),
        WORKFLOW_PLANNER="model",
        WORKFLOW_MODEL_KEY="",
        WORKFLOW_REDIS_URL="redis://127.0.0.1:1/0",
        WORKFLOW_CATALOG_PATH="must-not-be-read",
        WORKFLOW_FIXTURES_PATH="must-not-be-read",
    )


def test_native_cli_init_checks_schema_without_executor_or_mongo_dependencies():
    with PostgresTests.from_environment() as postgres:
        allocation = postgres.allocate(migrate=True)
        result = subprocess.run(
            [sys.executable, "-m", "workflow_engine.cli", "init"],
            env=child_environment(allocation.runtime_url),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert '"status": "schema_current"' in result.stdout
        assert allocation.runtime_url.get_secret_value() not in result.stdout + result.stderr
        with allocation.connect_owner() as connection:
            connection.execute("UPDATE alembic_version SET version_num = 'wrong'")
        result = subprocess.run(
            [sys.executable, "-m", "workflow_engine.cli", "init"],
            env=child_environment(allocation.runtime_url),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert allocation.runtime_url.get_secret_value() not in result.stdout + result.stderr


async def test_actual_postgres_connection_failure_is_safe_503(monkeypatch, caplog):
    # Hold a local port without listening. No service/deployment is stopped.
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        settings = Settings(
            env="local",
            origin="http://127.0.0.1:8000",
            database_url=SecretStr(
                f"postgresql://private-runtime:private-password@127.0.0.1:{port.getsockname()[1]}/unavailable"
            ),
            redis_url=redis_url(),
        )
        monkeypatch.setattr(runtime, "Settings", lambda: settings)
        app = runtime.api_application()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://api"
            ) as client:
                response = await client.get("/health/ready")
                assert response.status_code == 503
                assert response.json() == {
                    "status": "unavailable",
                    "postgres": False,
                    "broker": False,
                }
                assert (await client.get("/health/live")).status_code == 200
        assert "private" not in response.text + caplog.text


async def test_actual_redis_connection_failure_is_degraded_200(monkeypatch, caplog):
    with PostgresTests.from_environment() as postgres, socket.socket() as port:
        allocation = postgres.allocate(migrate=True)
        port.bind(("127.0.0.1", 0))
        settings = Settings(
            env="local",
            origin="http://127.0.0.1:8000",
            database_url=allocation.runtime_url,
            redis_url=SecretStr(
                f"redis://private-user:private-password@127.0.0.1:{port.getsockname()[1]}/0"
            ),
        )
        monkeypatch.setattr(runtime, "Settings", lambda: settings)
        app = runtime.api_application()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://api"
            ) as client:
                response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "degraded", "postgres": True, "broker": False}
        assert "private" not in response.text + caplog.text


def test_native_cli_api_uses_runtime_only_url_file_and_selector(tmp_path):
    with PostgresTests.from_environment() as postgres:
        allocation = postgres.allocate(migrate=True)
        url_file = tmp_path / "runtime-url"
        url_file.write_text(allocation.runtime_url.get_secret_value() + "\n", encoding="utf-8")
        environment = child_environment(allocation.runtime_url)
        del environment["WORKFLOW_DATABASE_URL"]
        environment["WORKFLOW_DATABASE_URL_FILE"] = str(url_file)
        environment["WORKFLOW_REDIS_URL"] = redis_url().get_secret_value()
        environment["WORKFLOW_ENV"] = "local"
        environment["WORKFLOW_ORIGIN"] = "http://127.0.0.1:8000"
        environment["WORKFLOW_CATALOG_PATH"] = "fixtures/catalog.jsonl"
        port = free_port()
        with (tmp_path / "native-api.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "workflow_engine.cli", "api", "--port", str(port)],
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=1, trust_env=False
                ) as client:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        assert process.poll() is None, "Native API exited before readiness"
                        try:
                            response = client.get("/health/ready")
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.05)
                    else:
                        pytest.fail("Native API did not become ready")
                    assert response.json() == {"status": "ready", "postgres": True, "broker": True}
                    assert client.post("/workflows", json={"text": REQUEST}).status_code == 410
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            log.seek(0)
            assert allocation.runtime_url.get_secret_value() not in log.read()


async def test_runtime_api_readiness_and_lifespan_use_only_runtime_credentials(monkeypatch):
    with PostgresTests.from_environment() as postgres:
        allocation = postgres.allocate(migrate=True)
        settings = Settings(
            database_url=allocation.runtime_url,
            redis_url=redis_url(),
            env="local",
            origin="http://127.0.0.1:8000",
        )
        monkeypatch.setattr(runtime, "Settings", lambda: settings)
        app = runtime.api_application()
        async with app.router.lifespan_context(app), AsyncExitStack() as stack:
            client = await stack.enter_async_context(
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")
            )
            response = await client.get("/health/ready")
            assert response.status_code == 200
            assert response.json() == {"status": "ready", "postgres": True, "broker": True}
            assert (await client.post("/workflows", json={"text": REQUEST})).status_code == 410
            assert (
                await client.get("/workflows/anything", headers={"X-API-Key": "legacy"})
            ).status_code == 401

"""Invocation-owned, real PostgreSQL test resources, never a supplied database's lifecycle."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

import pytest
from psycopg import sql
from pydantic import SecretStr

from scripts.init_postgres_roles import RoleAllocation, bootstrap_database, connect_postgres

ROOT = Path(__file__).resolve().parents[1]
_fixture_owners = WeakKeyDictionary()
_fixture_allocations = WeakKeyDictionary()


def register_fixture_store(postgres, allocation, store):
    """Register only an exact invocation-owned runtime store for prepared executor tests."""
    from sqlalchemy.engine import make_url

    if postgres._allocations.get(allocation.name) is not allocation:
        raise ValueError("Database is not owned by this invocation")
    if store.database.engine.url.set(drivername="postgresql") != make_url(
        allocation.runtime_url.get_secret_value()
    ).set(drivername="postgresql"):
        raise ValueError("Store is not the owned runtime connection")
    _fixture_owners[store] = allocation.owner_url
    _fixture_allocations[store] = (postgres, allocation)


def runtime_environment(store, *, redis_url, stream, group, ticket_url, inventory_url):
    """Exact owned runtime credentials only, never the parent test/admin environment."""
    from tests.processes import child_environment

    postgres, allocation = _fixture_allocations[store]
    if postgres._allocations.get(allocation.name) is not allocation:
        raise ValueError("Database is not owned by this invocation")
    return child_environment(
        WORKFLOW_DATABASE_URL=allocation.runtime_url.get_secret_value(),
        WORKFLOW_REDIS_URL=redis_url,
        WORKFLOW_REDIS_STREAM=stream,
        WORKFLOW_REDIS_GROUP=group,
        WORKFLOW_TICKETING_URL=ticket_url,
        WORKFLOW_INVENTORY_URL=inventory_url,
        WORKFLOW_PLANNER="fixture",
        WORKFLOW_CATALOG_PATH=str(ROOT / "fixtures/catalog.jsonl"),
        WORKFLOW_FIXTURES_PATH=str(ROOT / "fixtures/planner_dev.jsonl"),
    )


@contextmanager
def database_outage(store):
    """Deny/terminate connections to this exact registered test database, then restore."""
    postgres, allocation = _fixture_allocations[store]
    if postgres._allocations.get(allocation.name) is not allocation:
        raise ValueError("Database is not owned by this invocation")
    with postgres.connect_admin() as connection:
        assert connection.execute(
            "SELECT datallowconn FROM pg_database WHERE datname = %s", (allocation.name,)
        ).fetchone() == (True,)
        try:
            connection.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
                    sql.Identifier(allocation.name)
                )
            )
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (allocation.name,),
            )
            yield
        finally:
            connection.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(
                    sql.Identifier(allocation.name)
                )
            )


class PostgresTests:
    def __init__(self, admin_url: SecretStr):
        self.admin_url = admin_url
        self._allocations: dict[str, RoleAllocation] = {}

    @classmethod
    def from_environment(cls):
        value = os.environ.get("TEST_POSTGRES_URL")
        if not value:
            pytest.skip("Set TEST_POSTGRES_URL to run real PostgreSQL checks")
        return cls(SecretStr(value))

    def __enter__(self):
        with self.connect_admin() as connection:
            connection.execute("SELECT 1")
        return self

    def __exit__(self, *_):
        for allocation in list(self._allocations.values()):
            self._remove_owned(allocation)

    def connect_admin(self):
        return connect_postgres(self.admin_url)

    def allocate(self, *, migrate=False):
        allocation = bootstrap_database(self.admin_url)
        self._allocations[allocation.name] = allocation
        if migrate:
            self.alembic(allocation, "upgrade", "head")
        return allocation

    def alembic(self, allocation, *arguments):
        from tests.processes import child_environment

        if self._allocations.get(allocation.name) is not allocation:
            raise ValueError("Database is not owned by this invocation")
        environment = child_environment(
            WORKFLOW_MIGRATION_URL=allocation.owner_url.get_secret_value()
        )
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError("Explicit Alembic command failed: " + arguments[0])
        return result.stdout.strip()

    def cleanup(self, allocation):
        if not isinstance(allocation, RoleAllocation):
            raise ValueError("Database is not owned by this invocation")
        self._remove_owned(allocation)

    def _remove_owned(self, allocation):
        if self._allocations.get(allocation.name) is not allocation:
            raise ValueError("Database is not owned by this invocation")
        with self.connect_admin() as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(allocation.name))
            )
            for role in (allocation.runtime_role, allocation.owner_role):
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        del self._allocations[allocation.name]


@asynccontextmanager
async def runtime_store(postgres):
    """Only test setup retains owner credentials; the product sees the runtime Database."""
    from sqlalchemy import text

    from workflow_engine.db import create_database

    allocation = postgres.allocate(migrate=True)
    database = create_database(allocation.runtime_url)
    try:
        async with database.sessions() as session:
            assert (
                await session.execute(
                    text("SELECT current_user = :role"),
                    {
                        "role": allocation.runtime_role,
                    },
                )
            ).scalar_one()
        from workflow_engine.postgres_store import PostgresWorkflowStore

        store = PostgresWorkflowStore(database)
        register_fixture_store(postgres, allocation, store)
        await store.initialize()
        yield store
    finally:
        await database.close()


@asynccontextmanager
async def owner_session(store):
    """Exact registered test allocation only, for SQL lock/clock controls."""
    from workflow_engine.db import create_database

    owner = create_database(_fixture_owners[store])
    try:
        async with owner.sessions.begin() as session:
            yield session
    finally:
        await owner.close()


def control_execution(store, workflow, *, lease_for_s=None, enqueue_due=False, run_due=False):
    """Test-only deadlines relative to the real server clock, scoped to one owned row."""
    assignments = []
    values = []
    if lease_for_s is not None:
        assignments.append("lease_until = clock_timestamp() + %s * interval '1 second'")
        values.append(lease_for_s)
    if enqueue_due:
        assignments.append("last_enqueued_at = clock_timestamp() - interval '6 seconds'")
    if run_due:
        assignments.append("next_run_at = clock_timestamp() - interval '1 second'")
    if not assignments:
        raise ValueError("A clock control is required")
    with connect_postgres(_fixture_owners[store]) as connection:
        result = connection.execute(
            "UPDATE execution_runs SET "
            + ", ".join(assignments)
            + " WHERE workflow_id = %s AND tenant_id = %s RETURNING lease_until",
            [*values, UUID(workflow.workflow_id), workflow.tenant_id],
        ).fetchone()
        if result is None:
            raise ValueError("No owned fixture row")
        return result[0]


def wait_for_database_time(store, deadline):
    """Wait on PostgreSQL time, not an application clock or a long lease sleep."""
    with connect_postgres(_fixture_owners[store]) as connection:
        connection.execute(
            "SELECT pg_sleep(GREATEST(0, EXTRACT(epoch FROM %s::timestamptz "
            "- clock_timestamp())) + 0.01)",
            (deadline,),
        )
        assert connection.execute("SELECT clock_timestamp() >= %s", (deadline,)).fetchone()[0]


def cross_live_lease_expiry(store, lease, deadline):
    """An expiry barrier must begin with the exact current lease live on PostgreSQL."""
    with connect_postgres(_fixture_owners[store]) as connection:
        current = connection.execute(
            "SELECT lease_token = %s AND lease_until = %s AND lease_until > clock_timestamp() "
            "FROM execution_runs WHERE workflow_id = %s AND tenant_id = %s",
            (lease.token, deadline, UUID(lease.workflow_id), lease.tenant_id),
        ).fetchone()
        assert current == (True,), "expiry barrier requires a live current lease"
    wait_for_database_time(store, deadline)


async def seed_approved_run(store, workflow, expected_revision, plan_hash):
    """Explicit test-only capability. Never exported or reachable from the product API."""
    from workflow_engine.domain import Conflict, DomainEvent, Workflow, transition
    from workflow_engine.store import MemoryWorkflowStore, MongoWorkflowStore, _validate_change

    def approved(current):
        if current.revision != expected_revision or current.plan_hash != plan_hash:
            raise Conflict("Fixture revision or plan hash mismatch")
        proposed = transition(
            current,
            DomainEvent(
                kind="approved",
                at=current.updated_at,
                plan_hash=plan_hash,
            ),
        )
        data = proposed.model_dump()
        data["history"][-1]["event"] = "fixture_approved"
        proposed = Workflow.model_validate(data)
        _validate_change(current, proposed, expected_revision)
        return proposed

    if isinstance(store, MemoryWorkflowStore):
        async with store._lock:
            current = store._documents[workflow.workflow_id]
            result = approved(current)
            store._documents[workflow.workflow_id] = result.model_copy(deep=True)
        return result.model_copy(deep=True)

    if isinstance(store, MongoWorkflowStore):
        # Historical test adapter only. PostgreSQL approval setup never uses runtime save.
        current = await store.get(workflow.workflow_id, workflow.tenant_id)
        return await store.save(approved(current), expected_revision, None)

    from sqlalchemy import func, insert, select

    from workflow_engine.db import create_database
    from workflow_engine.db_models import AuditRecord, ExecutionRun
    from workflow_engine.postgres_store import _decode, _encode

    owner = create_database(_fixture_owners[store])
    try:
        async with owner.sessions.begin() as session:
            row = (
                await session.execute(
                    select(ExecutionRun)
                    .where(
                        ExecutionRun.workflow_id == UUID(workflow.workflow_id),
                        ExecutionRun.tenant_id == workflow.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            result = approved(_decode(row))
            for key, value in _encode(result).items():
                setattr(row, key, value)
            await session.flush()
            await session.execute(
                insert(AuditRecord).values(
                    audit_id=uuid4(),
                    tenant_id=result.tenant_id,
                    workflow_id=UUID(result.workflow_id),
                    actor="system",
                    event="fixture_approved",
                    revision=result.revision,
                    correlation_id=f"{result.workflow_id}:{result.revision}",
                    occurred_at=func.clock_timestamp(),
                    safe_metadata={
                        "source": "system",
                        "state": result.state,
                        "reason_code": "fixture_approved",
                    },
                )
            )
        return result
    finally:
        await owner.close()


async def prepare_snapshot(store, workflow):
    """Replace an existing unapproved test snapshot through its registered owner only.

    Rich codec/operational fixtures are not product admissions. They retain their exact
    payload/history while fixture provenance is kept separately in the test audit.
    """
    from workflow_engine.domain import Conflict
    from workflow_engine.store import MemoryWorkflowStore, _validate_change, _validated_snapshot

    workflow = _validated_snapshot(workflow)
    if workflow.approved_plan_hash is not None or workflow.approved_at is not None:
        raise Conflict("Use exact-plan approved fixture setup")

    def check(current):
        if (
            current.approved_plan_hash is not None
            or current.approved_at is not None
            or workflow.revision != current.revision
        ):
            raise Conflict("Fixture requires the current unapproved snapshot")
        # Apply intrinsic identities/outcome checks without treating fixture setup as
        # a business revision or routing it through the guarded runtime save.
        _validate_change(
            current,
            workflow.model_copy(update={"revision": current.revision + 1}),
            current.revision,
        )

    if isinstance(store, MemoryWorkflowStore):
        async with store._lock:
            current = store._documents[workflow.workflow_id]
            check(current)
            store._documents[workflow.workflow_id] = workflow.model_copy(deep=True)
        return workflow.model_copy(deep=True)

    from sqlalchemy import insert, select

    from workflow_engine.db_models import AuditRecord, ExecutionRun
    from workflow_engine.postgres_store import _decode, _encode

    async with owner_session(store) as session:
        row = (
            await session.execute(
                select(ExecutionRun)
                .where(
                    ExecutionRun.workflow_id == UUID(workflow.workflow_id),
                    ExecutionRun.tenant_id == workflow.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        check(_decode(row))
        for key, value in _encode(workflow).items():
            setattr(row, key, value)
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(row, "payload")
        await session.flush()
        await session.execute(
            insert(AuditRecord).values(
                audit_id=uuid4(),
                tenant_id=workflow.tenant_id,
                workflow_id=UUID(workflow.workflow_id),
                actor="system",
                event="fixture_prepared",
                revision=workflow.revision,
                correlation_id=f"{workflow.workflow_id}:{workflow.revision}",
                occurred_at=workflow.updated_at,
                safe_metadata={"source": "system"},
            )
        )
        return _decode(row)


async def prepare_unapproved_run(store, workflow):
    """Explicit rich-snapshot fixture after real, planning-only admission."""
    from workflow_engine.store import AdmissionResult

    base = workflow.model_copy(
        update={
            "revision": 0,
            "state": "PENDING_PLAN",
            "plan": None,
            "plan_hash": None,
            "catalog_version": None,
            "clarification": None,
            "actions": [],
            "approved_plan_hash": None,
            "approved_at": None,
            "planning_attempts": 0,
            "retry_phase": None,
            "history": [],
            "model_metadata": {},
            "lease_owner": None,
            "lease_token": None,
            "lease_until": None,
        },
        deep=True,
    )
    admitted = await store.insert_or_get(base)
    assert admitted.created, "Fixture must own a fresh admission"
    normalized = workflow.model_copy(
        update={
            "created_at": admitted.workflow.created_at,
            "updated_at": admitted.workflow.updated_at,
            "next_run_at": admitted.workflow.updated_at
            if workflow.next_run_at is not None
            else None,
        },
        deep=True,
    )
    return AdmissionResult(
        workflow=await prepare_snapshot(store, normalized), created=admitted.created
    )

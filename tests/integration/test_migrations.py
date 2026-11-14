"""Real PostgreSQL contracts. No SQLite or in-memory fallback is acceptable here."""

import copy
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import make_url

from tests.postgres_support import ROOT, PostgresTests
from workflow_engine.domain import Workflow, WorkflowCreate

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def postgres():
    with PostgresTests.from_environment() as service:
        yield service


def execution_row(**changes):
    now = datetime.now(UTC)
    row = {
        "workflow_id": uuid4(),
        "tenant_id": "tenant-a",
        "admission_key": uuid4().hex,
        "request_hash": "a" * 64,
        "schema_version": 1,
        "created_at": now,
        "updated_at": now,
        "revision": 0,
        "state": "PENDING_PLAN",
        "plan_hash": None,
        "approved_plan_hash": None,
        "approved_at": None,
        "next_run_at": now,
        "wake_seq": 0,
        "last_enqueued_at": None,
        "lease_owner": None,
        "lease_token": None,
        "lease_until": None,
        "payload": {
            "request_text": "Inspect pump P-17",
            "plan": None,
            "clarification": None,
            "catalog_version": None,
            "prompt_version": None,
            "model_metadata": {},
            "actions": [],
            "planning_attempts": 0,
            "retry_phase": None,
            "history": [],
        },
    }
    row.update(changes)
    return row


def insert_execution(connection, row):
    values = {**row, "payload": Jsonb(row["payload"])}
    connection.execute(
        sql.SQL("INSERT INTO execution_runs ({}) VALUES ({})").format(
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() for _ in values),
        ),
        list(values.values()),
    )


async def test_empty_database_upgrade_and_schema_check(postgres):
    allocation = postgres.allocate()
    with allocation.connect_owner() as connection:
        assert connection.execute("SELECT to_regclass('execution_runs')").fetchone() == (None,)
    postgres.alembic(allocation, "upgrade", "head")
    postgres.alembic(allocation, "upgrade", "head")
    postgres.alembic(allocation, "check")
    with allocation.connect_owner() as connection:
        assert connection.execute(
            "SELECT to_regclass('execution_runs') IS NOT NULL"
        ).fetchone() == (True,)

    from workflow_engine.db import create_database

    database = create_database(allocation.runtime_url)
    try:
        await database.check_schema()
        async with database.sessions() as session:
            from sqlalchemy import text

            assert (await session.execute(text("SHOW timezone"))).scalar_one() == "UTC"
    finally:
        await database.close()
    with allocation.connect_runtime() as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0001_relational_execution",
        )
        insert_execution(connection, execution_row())
        assert connection.execute("SELECT count(*) FROM execution_runs").fetchone() == (1,)
        connection.execute("UPDATE execution_runs SET wake_seq = wake_seq + 1")
        connection.execute("DELETE FROM execution_runs")
        assert connection.execute("SELECT count(*) FROM execution_runs").fetchone() == (0,)


def test_runtime_role_cannot_rewrite_audit(postgres):
    allocation = postgres.allocate(migrate=True)
    workflow = execution_row()
    with allocation.connect_runtime() as connection:
        insert_execution(connection, workflow)
        for _ in range(257):
            connection.execute(
                """INSERT INTO audit_records
                (audit_id, tenant_id, workflow_id, actor, event, correlation_id,
                 revision, occurred_at, safe_metadata)
                VALUES (%s, %s, %s, 'system', 'admitted', %s, 0, %s, %s)""",
                (
                    uuid4(),
                    "tenant-a",
                    workflow["workflow_id"],
                    uuid4().hex,
                    datetime.now(UTC),
                    Jsonb({"source": "runner"}),
                ),
            )
        assert connection.execute("SELECT count(*) FROM audit_records").fetchone() == (257,)
        role = connection.execute(
            """SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls,
                pg_has_role(current_user, %s, 'MEMBER')
                FROM pg_roles WHERE rolname = current_user""",
            (allocation.owner_role,),
        ).fetchone()
        assert role == (False, False, False, False, False)
        for statement in (
            "UPDATE audit_records SET event = 'changed'",
            "DELETE FROM audit_records",
            "TRUNCATE audit_records",
            "ALTER TABLE audit_records ADD COLUMN rewritten text",
            "DROP TABLE audit_records",
            "CREATE TABLE startup_ddl (id integer)",
            "UPDATE alembic_version SET version_num = 'wrong'",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)
        owners = connection.execute(
            """SELECT tableowner FROM pg_tables WHERE schemaname = 'public'
                AND tablename IN ('execution_runs', 'audit_records', 'alembic_version')"""
        ).fetchall()
        assert owners == [(allocation.owner_role,)] * 3
        assert connection.execute("SELECT count(*) FROM audit_records").fetchone() == (257,)


@pytest.mark.parametrize(
    "worker_id", ["worker-a", "worker/" + "w" * 121], ids=["representative", "maximum_owner"]
)
def test_runtime_audit_preserves_machine_actor_from_lease_owner(postgres, worker_id):
    allocation = postgres.allocate(migrate=True)
    row = execution_row(
        lease_owner=worker_id,
        lease_token=uuid4().hex,
        lease_until=datetime.now(UTC) + timedelta(seconds=30),
    )
    with allocation.connect_runtime() as connection:
        insert_execution(connection, row)
        owner = connection.execute(
            "SELECT lease_owner FROM execution_runs WHERE workflow_id = %s", (row["workflow_id"],)
        ).fetchone()[0]
        failure = None
        audit_id = uuid4()
        try:
            connection.execute(
                """INSERT INTO audit_records
                (audit_id, tenant_id, workflow_id, actor, event, correlation_id,
                 revision, occurred_at, safe_metadata)
                VALUES (%s, %s, %s, %s, 'claimed', %s, 0, %s, %s)""",
                (
                    audit_id,
                    row["tenant_id"],
                    row["workflow_id"],
                    owner,
                    uuid4().hex,
                    datetime.now(UTC),
                    Jsonb({"source": "runner"}),
                ),
            )
        except psycopg.Error as error:
            failure = error.diag.constraint_name or error.sqlstate
        assert failure is None, f"Machine actor rejected by {failure}"
        assert connection.execute(
            "SELECT actor FROM audit_records WHERE audit_id = %s", (audit_id,)
        ).fetchone() == (worker_id,)
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            assert connection.execute(
                "SELECT has_table_privilege(current_user, 'audit_records', %s)", (privilege,)
            ).fetchone() == (False,)


@pytest.mark.parametrize(
    ("actor", "error_type"),
    [("", psycopg.errors.CheckViolation), ("w" * 129, psycopg.errors.StringDataRightTruncation)],
    ids=["empty", "overlong"],
)
def test_runtime_audit_rejects_out_of_bounds_actor(postgres, actor, error_type):
    allocation = postgres.allocate(migrate=True)
    row = execution_row()
    with allocation.connect_runtime() as connection:
        insert_execution(connection, row)
        with pytest.raises(error_type):
            connection.execute(
                """INSERT INTO audit_records
                (audit_id, tenant_id, workflow_id, actor, event, correlation_id,
                 revision, occurred_at, safe_metadata)
                VALUES (%s, %s, %s, %s, 'claimed', %s, 0, %s, %s)""",
                (
                    uuid4(),
                    row["tenant_id"],
                    row["workflow_id"],
                    actor,
                    uuid4().hex,
                    datetime.now(UTC),
                    Jsonb({}),
                ),
            )


@pytest.mark.parametrize("shape", ["many_keys", "positive_exponents", "negative_exponents"])
def test_in_budget_workflow_survives_expanded_jsonb_rendering(postgres, shape):
    values = Workflow.new(
        "tenant-a", "synthetic-budget-regression", WorkflowCreate(text="Inspect pump P-17")
    ).model_dump(mode="json")
    if shape == "many_keys":
        values["model_metadata"] = {f"k{i:05d}": 1 for i in range(21000)}
    elif shape == "positive_exponents":
        values["model_metadata"] = {"numbers": [1e308] * 35000}
    else:
        values["model_metadata"] = {"numbers": [5e-324] * 35000}
    workflow = Workflow.model_validate(values)
    compact_size = len(workflow.model_dump_json().encode("utf-8"))
    assert compact_size <= 256 * 1024
    # The full compact domain budget is unchanged, not replaced by the larger SQL defense.
    with pytest.raises(ValidationError, match="Workflow exceeds document budget"):
        Workflow.model_validate({**values, "model_metadata": {"padding": "x" * (256 * 1024)}})
    payload_keys = execution_row()["payload"].keys()
    document = workflow.model_dump(mode="python")
    json_document = workflow.model_dump(mode="json")
    row = {key: value for key, value in document.items() if key not in payload_keys}
    row["workflow_id"] = uuid4()
    row["payload"] = {key: json_document[key] for key in payload_keys}
    allocation = postgres.allocate(migrate=True)
    with allocation.connect_runtime() as connection:
        expanded_size = connection.execute(
            "SELECT octet_length(%s::jsonb::text)", (Jsonb(row["payload"]),)
        ).fetchone()[0]
        assert expanded_size > 256 * 1024
        failure = None
        try:
            insert_execution(connection, row)
        except psycopg.Error as error:
            failure = error.diag.constraint_name or error.sqlstate
        assert failure is None, (
            f"Valid compact snapshot {compact_size} bytes rejected by {failure}; "
            f"JSONB rendering is {expanded_size} bytes"
        )
        assert connection.execute(
            "SELECT octet_length(payload::text) FROM execution_runs WHERE workflow_id = %s",
            (row["workflow_id"],),
        ).fetchone() == (expanded_size,)


def action(action_id="ticket", **changes):
    value = {
        "action_id": action_id,
        "arguments": {},
        "idempotency_key": uuid4().hex,
        "status": "PENDING",
        "submissions": 0,
        "reconciliation_count": 0,
        "has_ambiguous_submission": False,
        "next_retry_at": None,
        "result": None,
        "last_error": None,
    }
    return {**value, **changes}


def approval_columns():
    return {
        "plan_hash": "b" * 64,
        "approved_plan_hash": "b" * 64,
        "approved_at": datetime(2026, 1, 1, tzinfo=UTC),
    }


@pytest.mark.parametrize(
    ("changes", "payload_changes", "constraint"),
    [
        ({"state": "invented"}, {}, "ck_execution_state"),
        ({"revision": -1}, {}, "ck_execution_revision"),
        ({"wake_seq": -1}, {}, "ck_execution_wake_seq"),
        ({"state": "SUCCEEDED"}, {}, "ck_execution_passive_schedule"),
        ({"approved_plan_hash": "b" * 64}, {}, "ck_execution_approval"),
        ({"lease_owner": "worker"}, {}, "ck_execution_lease"),
        ({}, {"model_metadata": []}, "ck_execution_payload"),
        ({}, {"plan": []}, "ck_execution_payload"),
        ({}, {"clarification": []}, "ck_execution_payload"),
        ({}, {"actions": {}}, "ck_execution_payload"),
        ({}, {"actions": [action("reservation")]}, "ck_execution_payload"),
        ({}, {"actions": [action(), action()]}, "ck_execution_payload"),
        ({}, {"actions": [action(), action("reservation"), action()]}, "ck_execution_payload"),
        ({}, {"actions": [action(submissions=6)]}, "ck_execution_payload"),
        ({}, {"actions": [action(submissions=-1)]}, "ck_execution_payload"),
        ({}, {"actions": [action(submissions=1.5)]}, "ck_execution_payload"),
        ({}, {"actions": [action(reconciliation_count=6)]}, "ck_execution_payload"),
        (approval_columns(), {"actions": [action(status="invented")]}, "ck_execution_payload"),
        (approval_columns(), {"actions": [action(status=None)]}, "ck_execution_payload"),
        ({}, {"actions": [action(status="IN_FLIGHT")]}, "ck_execution_action_approval"),
        ({}, {"planning_attempts": 3}, "ck_execution_payload"),
        ({}, {"planning_attempts": -1}, "ck_execution_payload"),
        ({}, {"planning_attempts": True}, "ck_execution_payload"),
        ({}, {"retry_phase": "invented"}, "ck_execution_payload"),
        ({}, {"history": {}}, "ck_execution_payload"),
        ({}, {"history": [{}] * 257}, "ck_execution_payload"),
        ({}, {"tenant_id": "duplicate"}, "ck_execution_payload"),
        ({}, {"model_metadata": {"padding": "x" * (16 * 1024 * 1024)}}, "ck_execution_budget"),
    ],
)
def test_invalid_execution_rows_rejected(postgres, changes, payload_changes, constraint):
    allocation = postgres.allocate(migrate=True)
    row = execution_row(**changes)
    row["payload"] = {**copy.deepcopy(row["payload"]), **payload_changes}
    with allocation.connect_runtime() as connection:
        # First prove the complete baseline is insertable. A missing required field is not a RED.
        insert_execution(connection, execution_row())
        with pytest.raises(psycopg.errors.CheckViolation) as error:
            insert_execution(connection, row)
        assert error.value.diag.constraint_name == constraint


def test_cleanup_refuses_unowned_database(postgres):
    allocation = postgres.allocate(migrate=True)
    with PostgresTests(postgres.admin_url) as other_invocation:
        with pytest.raises(ValueError, match="not owned"):
            other_invocation.cleanup(allocation)
        with pytest.raises(ValueError, match="not owned"):
            other_invocation.cleanup("postgres")
    with pytest.raises(ValueError, match="not owned"):
        postgres.cleanup(replace(allocation))
    with allocation.connect_runtime() as connection:
        assert connection.execute("SELECT current_database()").fetchone() == (allocation.name,)
    with postgres.connect_admin() as connection:
        assert connection.execute("SELECT current_database()").fetchone() == ("postgres",)


async def test_schema_check_rejects_missing_or_wrong_head(postgres):
    from workflow_engine.db import SchemaNotCurrent, create_database

    allocation = postgres.allocate()
    database = create_database(allocation.runtime_url)
    try:
        with pytest.raises(SchemaNotCurrent):
            await database.check_schema()
        with allocation.connect_owner() as connection:
            assert connection.execute("SELECT to_regclass('alembic_version')").fetchone() == (None,)
        postgres.alembic(allocation, "upgrade", "head")
        await database.check_schema()
        with allocation.connect_owner() as connection:
            connection.execute("UPDATE alembic_version SET version_num = 'wrong'")
        with pytest.raises(SchemaNotCurrent):
            await database.check_schema()
        with allocation.connect_owner() as connection:
            connection.execute("DELETE FROM alembic_version")
        with pytest.raises(SchemaNotCurrent):
            await database.check_schema()
        with allocation.connect_owner() as connection:
            connection.execute("INSERT INTO alembic_version VALUES ('0001_relational_execution')")
            connection.execute("INSERT INTO alembic_version VALUES ('wrong')")
        with pytest.raises(SchemaNotCurrent):
            await database.check_schema()
    finally:
        await database.close()


def test_valid_payload_boundaries_and_tenant_admission_uniqueness(postgres):
    allocation = postgres.allocate(migrate=True)
    row = execution_row(**approval_columns())
    row["payload"].update(
        plan={
            "kind": "plan",
            "asset_id": "P-17",
            "ticket": {"priority": "normal", "description": "Inspect pump"},
            "reservation": {"part_id": "SEAL-1", "quantity": 1},
        },
        planning_attempts=2,
        actions=[
            action(status="CONFIRMED", submissions=5, reconciliation_count=5),
            action("reservation", status="IN_FLIGHT", submissions=5),
        ],
        history=[
            {"at": "2026-01-01T00:00:00Z", "event": "planned", "action_id": None, "error": None}
        ]
        * 256,
    )
    with allocation.connect_runtime() as connection:
        insert_execution(connection, row)
        duplicate = {**row, "workflow_id": uuid4()}
        with pytest.raises(psycopg.errors.UniqueViolation) as error:
            insert_execution(connection, duplicate)
        assert error.value.diag.constraint_name == "uq_execution_tenant_admission"
        insert_execution(connection, {**duplicate, "tenant_id": "tenant-b"})
        duplicate_id = {**row, "admission_key": uuid4().hex}
        with pytest.raises(psycopg.errors.UniqueViolation):
            insert_execution(connection, duplicate_id)
        indexes = {
            entry[0]
            for entry in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'execution_runs'"
            )
        }
        assert {"ix_execution_tenant_created", "ix_execution_due"} <= indexes


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {"request_text": "forbidden"},
        {"model_metadata": {}},
        {"source": "free_form_text"},
        {"reason_code": "unbounded diagnostic text"},
        {"attempt": 6},
    ],
)
def test_audit_rejects_unsafe_metadata(postgres, metadata):
    allocation = postgres.allocate(migrate=True)
    workflow = execution_row()
    with allocation.connect_runtime() as connection:
        insert_execution(connection, workflow)
        with pytest.raises(psycopg.errors.CheckViolation) as error:
            connection.execute(
                """INSERT INTO audit_records
                (audit_id, tenant_id, workflow_id, actor, event, correlation_id,
                 revision, occurred_at, safe_metadata)
                VALUES (%s, %s, %s, 'system', 'admitted', %s, 0, %s, %s)""",
                (
                    uuid4(),
                    "tenant-a",
                    workflow["workflow_id"],
                    uuid4().hex,
                    datetime.now(UTC),
                    Jsonb(metadata),
                ),
            )
        assert error.value.diag.constraint_name == "ck_audit_metadata"


def test_supplied_broken_service_fails_without_skip(postgres, monkeypatch):
    broken = make_url(postgres.admin_url.get_secret_value()).set(port=1)
    monkeypatch.setenv("TEST_POSTGRES_URL", broken.render_as_string(hide_password=False))
    with pytest.raises(RuntimeError, match="connection failed"):
        with PostgresTests.from_environment():
            pytest.fail("Broken service must not connect or silently skip")


def test_bootstrap_rejects_url_identity_overrides(postgres):
    parsed = make_url(postgres.admin_url.get_secret_value())
    # libpq query parameters otherwise override the freshly generated owner/runtime identities.
    crafted = parsed.update_query_dict({"user": parsed.username, "password": parsed.password})
    with pytest.raises(RuntimeError, match="connection failed|bootstrap failed"):
        with PostgresTests(SecretStr(crafted.render_as_string(hide_password=False))) as service:
            service.allocate()


def test_bootstrap_cli_uses_private_files_and_refuses_existing_output(tmp_path):
    # A server-wide before/after catalog comparison wrongly fails when another
    # isolated invocation allocates its own database. Observe this CLI's wire boundary.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.1)
        admin_url = (
            "postgresql://private-admin:private-password@127.0.0.1:"
            f"{listener.getsockname()[1]}/postgres"
        )
        config = tmp_path / "admin.json"
        config.write_text(json.dumps({"admin_url": admin_url}), encoding="utf-8")
        output = tmp_path / "existing.json"
        output.write_text("preserve", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "scripts/init_postgres_roles.py",
                "--config",
                str(config),
                "--output",
                str(output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 1
        assert output.read_text(encoding="utf-8") == "preserve"
        assert admin_url not in result.stdout + result.stderr
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            pass
        else:
            connection.close()
            pytest.fail("Existing output must be refused before any database connection")


async def test_database_factory_rejects_non_postgres_without_exposing_secret():
    from workflow_engine.db import create_database

    with pytest.raises(ValueError) as error:
        create_database(SecretStr("sqlite:///private-credential-value"))
    assert "private-credential-value" not in str(error.value)


def test_migration_failure_does_not_print_credentials(postgres):
    allocation = postgres.allocate()
    environment = {
        **os.environ,
        "WORKFLOW_MIGRATION_URL": allocation.runtime_url.get_secret_value(),
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert allocation.runtime_url.get_secret_value() not in output
    assert make_url(allocation.runtime_url.get_secret_value()).password not in output
    with allocation.connect_owner() as connection:
        assert connection.execute("SELECT to_regclass('execution_runs')").fetchone() == (None,)

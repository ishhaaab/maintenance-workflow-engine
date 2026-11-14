"""Test-only privileged harness for the exact newly created Compose deployment.

Available only in the explicit test-tools image/override. The production image
contains no tests and no fixture command. Private volumes are never printed.
"""

import argparse
import asyncio
import json
import stat
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from scripts.init_postgres_roles import RoleAllocation
from tests.postgres_support import _fixture_owners, control_execution, seed_approved_run
from tests.support import REQUEST
from workflow_engine.cli import runtime_loop
from workflow_engine.db import create_database
from workflow_engine.domain import Workflow, WorkflowCreate
from workflow_engine.postgres_store import PostgresWorkflowStore


def allocation():
    owner = SecretStr(Path("/run/pg-owner/owner.url").read_text(encoding="utf-8"))
    runtime = SecretStr(Path("/run/pg-runtime/runtime.url").read_text(encoding="utf-8"))
    a, b = make_url(owner.get_secret_value()), make_url(runtime.get_secret_value())
    assert (a.host, a.port, a.database) == (b.host, b.port, b.database)
    assert a.username != b.username and a.password != b.password
    result = RoleAllocation(a.database, a.username, b.username, owner, runtime)
    for connect, role in ((result.connect_owner, a.username), (result.connect_runtime, b.username)):
        with connect() as connection:
            assert connection.execute(
                "SELECT current_database(), current_user, current_setting('workflow.runtime_role')"
            ).fetchone() == (a.database, role, b.username)
    return result


def info(row):
    with row.connect_runtime() as connection:
        exists = connection.execute("SELECT to_regclass('alembic_version')").fetchone()[0]
        schema = (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            if exists
            else None
        )
        restricted = connection.execute(
            "SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolbypassrls "
            "AND NOT has_schema_privilege(current_user, 'public', 'CREATE') "
            "FROM pg_roles WHERE rolname=current_user"
        ).fetchone()[0]
        runs = (
            connection.execute("SELECT count(*) FROM execution_runs").fetchone()[0] if schema else 0
        )
        audit = (
            connection.execute("SELECT count(*) FROM audit_records").fetchone()[0] if schema else 0
        )
        databases = connection.execute(
            "SELECT count(*) FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'"
        ).fetchone()[0]
    metadata = Path("/run/pg-runtime/runtime.url").stat()
    owner_metadata = Path("/run/pg-owner/owner.url").stat()
    return {
        "database": row.name,
        "owner_role": row.owner_role,
        "runtime_role": row.runtime_role,
        "schema": schema,
        "runtime_restricted": restricted,
        "runs": runs,
        "audit": audit,
        "runtime_file_uid": metadata.st_uid,
        "runtime_file_mode": oct(stat.S_IMODE(metadata.st_mode)),
        "runtime_file_gid": metadata.st_gid,
        "owner_file_uid": owner_metadata.st_uid,
        "owner_file_mode": oct(stat.S_IMODE(owner_metadata.st_mode)),
        "application_databases": databases,
    }


def grants(row):
    denied = []
    with row.connect_runtime() as connection:
        for label, query in (
            ("audit_update", "UPDATE audit_records SET event='tampered' WHERE false"),
            ("audit_delete", "DELETE FROM audit_records WHERE false"),
            ("audit_truncate", "TRUNCATE audit_records"),
            ("ddl", "CREATE TABLE forbidden_runtime_ddl(id integer)"),
        ):
            try:
                connection.execute(query)
            except psycopg.errors.InsufficientPrivilege:
                denied.append(label)
            else:
                raise AssertionError("Runtime grants permitted a forbidden operation")
    return {"denied": denied}


def log_probe(row):
    from tests.integration.test_migrations import execution_row, insert_execution

    with row.connect_runtime() as connection:
        candidate = execution_row(admission_key="synthetic-row-detail-sentinel", state="INVALID")
        try:
            insert_execution(connection, candidate)
        except psycopg.errors.CheckViolation:
            pass
        else:
            raise AssertionError("Invalid test row was accepted")
        assert connection.execute("SHOW log_error_verbosity").fetchone() == ("terse",)
        assert connection.execute("SHOW log_min_error_statement").fetchone() == ("panic",)
    with row.connect_owner() as connection:
        try:
            connection.execute(
                sql.SQL("CREATE ROLE forbidden_fixture_role PASSWORD {}").format(
                    sql.Literal("synthetic-password-sentinel")
                )
            )
        except psycopg.errors.InsufficientPrivilege:
            pass
        else:
            raise AssertionError("Migration owner unexpectedly has role-creation privileges")
    return {"probes_rejected": True}


async def operate(args):
    row = allocation()
    if args.command == "info":
        return info(row)
    if args.command == "grants":
        return grants(row)
    if args.command == "log-probe":
        return log_probe(row)
    database = create_database(row.runtime_url)
    try:
        store = PostgresWorkflowStore(database)
        await store.initialize()
        # This harness possesses the private owner file only because the host test
        # created this exact isolated project. No runtime process can register it.
        _fixture_owners[store] = row.owner_url
        if args.command == "admit":
            workflow = (
                await store.insert_or_get(
                    Workflow.new("tenant-a", args.key, WorkflowCreate(text=REQUEST))
                )
            ).workflow
        else:
            UUID(args.workflow_id)
            workflow = await store.get(args.workflow_id, "tenant-a")
            if args.command == "approve":
                workflow = await seed_approved_run(store, workflow, args.revision, args.plan_hash)
            elif args.command == "expire":
                control_execution(store, workflow, lease_for_s=-1, enqueue_due=True, run_due=True)
                workflow = await store.get(args.workflow_id, "tenant-a")
        return {
            "workflow_id": workflow.workflow_id,
            "state": workflow.state,
            "revision": workflow.revision,
            "plan_hash": workflow.plan_hash,
            "approved_plan_hash": workflow.approved_plan_hash,
            "actions": [action.model_dump(mode="json") for action in workflow.actions],
        }
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["info", "grants", "log-probe", "admit", "get", "approve", "expire"]
    )
    parser.add_argument("--key")
    parser.add_argument("--workflow-id")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--plan-hash")
    try:
        result = asyncio.run(operate(parser.parse_args()), loop_factory=runtime_loop)
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"error": "compose_fixture_failed", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

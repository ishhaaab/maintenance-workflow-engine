import json

import pytest

from tests.integration.test_migrations import execution_row, insert_execution
from tests.postgres_support import PostgresTests

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_identity_upgrade_requires_explicit_tenant_registry_and_retains_prior_runs(tmp_path):
    with PostgresTests.from_environment() as postgres:
        allocation = postgres.allocate()
        postgres.alembic(allocation, "upgrade", "0001_relational_execution")
        row = execution_row()
        with allocation.connect_runtime() as connection:
            insert_execution(connection, row)
        with pytest.raises(RuntimeError):
            postgres.alembic(allocation, "upgrade", "head")
        with allocation.connect_owner() as connection:
            assert connection.execute(
                "SELECT request_hash FROM execution_runs WHERE workflow_id=%s",
                (row["workflow_id"],),
            ).fetchone() == (row["request_hash"],)
            assert connection.execute("SELECT to_regclass('tenants')").fetchone() == (None,)
        registry = tmp_path / "explicit-tenants.json"
        registry.write_text(
            json.dumps([{"id": "tenant-a", "slug": "retained-tenant-a"}]), encoding="utf-8"
        )
        postgres.alembic(allocation, "-x", "tenant_registry=" + str(registry), "upgrade", "head")
        postgres.alembic(allocation, "check")
        with allocation.connect_runtime() as connection:
            assert connection.execute(
                "SELECT slug FROM tenants WHERE id='tenant-a'"
            ).fetchone() == ("retained-tenant-a",)
            assert connection.execute(
                "SELECT request_hash FROM execution_runs WHERE workflow_id=%s",
                (row["workflow_id"],),
            ).fetchone() == (row["request_hash"],)

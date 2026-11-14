"""Preparation identity is verified on real PostgreSQL, independently of Linux mount tests."""

import json
import os

import pytest
from psycopg import sql

from scripts import prepare_postgres as preparation
from tests.postgres_support import PostgresTests

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_durable_preparation_reuses_real_allocation_before_and_after_migration(
    tmp_path, monkeypatch
):
    if os.name == "posix" and os.geteuid() != 0:
        # Only Docker can prove UID/GID ownership; these are host test-owned paths.
        monkeypatch.setattr(preparation.os, "chown", lambda *args: None)
    paths = preparation.Paths(*(tmp_path / name for name in ("admin", "owner", "runtime", "data")))
    paths.admin.mkdir()
    paths.data.mkdir()
    (paths.data / "PG_VERSION").write_text("17")
    (paths.admin / "admin.started").write_text("test-owned")
    with PostgresTests.from_environment() as postgres:
        (paths.admin / "admin.json").write_text(
            json.dumps({"admin_url": postgres.admin_url.get_secret_value()}), encoding="utf-8"
        )
        (paths.admin / "password").write_bytes(
            preparation._postgres_url(postgres.admin_url).password.encode()
        )
        allocation = preparation.prepare_roles(paths)
        # Authority is the returned fresh allocation from this test's new journal,
        # not a name prefix or an existing deployment discovered on the server.
        postgres._allocations[allocation.name] = allocation
        with allocation.connect_runtime() as connection:
            assert connection.execute("SELECT to_regclass('execution_runs')").fetchone() == (None,)
        assert preparation.prepare_roles(paths) == allocation
        postgres.alembic(allocation, "upgrade", "head")
        assert preparation.prepare_roles(paths) == allocation
        assert (paths.owner / "owner.url").read_text() == allocation.owner_url.get_secret_value()
        assert (
            paths.runtime / "runtime.url"
        ).read_text() == allocation.runtime_url.get_secret_value()
        with allocation.connect_runtime() as connection:
            assert connection.execute("SELECT count(*) FROM execution_runs").fetchone() == (0,)
        # Invalid persisted binding must fail, not allocate a replacement database.
        with postgres.connect_admin() as connection:
            connection.execute(
                sql.SQL("ALTER DATABASE {} RESET workflow.runtime_role").format(
                    sql.Identifier(allocation.name)
                )
            )
        with pytest.raises(preparation.PreparationError):
            preparation.prepare_roles(paths)
        assert (
            json.loads((paths.admin / "allocation.json").read_text())["database"] == allocation.name
        )
    with postgres.connect_admin() as connection:
        assert connection.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s", (allocation.name,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname IN (%s, %s)",
            (allocation.owner_role, allocation.runtime_role),
        ).fetchone() == (0,)

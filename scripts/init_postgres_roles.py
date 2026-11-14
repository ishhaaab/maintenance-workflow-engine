"""Explicit fresh-database bootstrap. No existing role/database names are accepted.

Run with --config PATH (JSON containing admin_url) or enter the administrative
URL at the hidden prompt. --output PATH must not already exist. Generated
credentials are written only there, never to argv, SQL logs or stdout.
Migration commands subsequently use the generated owner_url through the
WORKFLOW_MIGRATION_URL environment variable. The administrative URL is not a
migration target and the runtime role never inherits the owner role.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy.engine import make_url


def _postgres_url(url: SecretStr):
    parsed = make_url(url.get_secret_value())
    # Retain transport security settings, never libpq overrides of generated identities/targets.
    transport_options = {
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "sslcrl",
        "sslcrldir",
        "ssl_min_protocol_version",
        "ssl_max_protocol_version",
        "channel_binding",
        "gssencmode",
    }
    if parsed.get_backend_name() != "postgresql" or set(parsed.query) - transport_options:
        raise ValueError("Unsupported PostgreSQL URL configuration")
    return parsed


def connect_postgres(url: SecretStr):
    try:
        parsed = _postgres_url(url)
        return psycopg.connect(
            parsed.set(drivername="postgresql").render_as_string(hide_password=False),
            autocommit=True,
            connect_timeout=5,
            options="-c timezone=UTC -c search_path=public",
        )
    except Exception:
        raise RuntimeError("PostgreSQL connection failed") from None


@dataclass(frozen=True)
class RoleAllocation:
    name: str
    owner_role: str
    runtime_role: str
    owner_url: SecretStr = field(repr=False)
    runtime_url: SecretStr = field(repr=False)

    def connect_owner(self):
        return connect_postgres(self.owner_url)

    def connect_runtime(self):
        return connect_postgres(self.runtime_url)


def bootstrap_database(admin_url: SecretStr) -> RoleAllocation:
    """Allocate fresh roles/database; rollback only resources created by this call."""
    suffix = uuid4().hex
    name = "workflow_test_" + suffix
    owner = "workflow_owner_" + suffix
    runtime = "workflow_runtime_" + suffix
    owner_password, runtime_password = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
    try:
        parsed = _postgres_url(admin_url)
        allocation = RoleAllocation(
            name,
            owner,
            runtime,
            SecretStr(
                parsed.set(database=name, username=owner, password=owner_password).render_as_string(
                    hide_password=False
                )
            ),
            SecretStr(
                parsed.set(
                    database=name, username=runtime, password=runtime_password
                ).render_as_string(hide_password=False)
            ),
        )
        with connect_postgres(admin_url) as connection:
            created_roles = []
            database_created = False
            try:
                for role, password in ((owner, owner_password), (runtime, runtime_password)):
                    connection.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                        ).format(sql.Identifier(role), sql.Literal(password))
                    )
                    created_roles.append(role)
                    connection.execute(
                        sql.SQL("ALTER ROLE {} SET timezone = 'UTC'").format(sql.Identifier(role))
                    )
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                        sql.Identifier(name), sql.Identifier(owner)
                    )
                )
                database_created = True
                connection.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
                )
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                        sql.Identifier(name), sql.Identifier(owner), sql.Identifier(runtime)
                    )
                )
                connection.execute(
                    sql.SQL("ALTER DATABASE {} SET workflow.runtime_role = {}").format(
                        sql.Identifier(name), sql.Literal(runtime)
                    )
                )
                with allocation.connect_owner() as owner_connection:
                    owner_connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
                    owner_connection.execute(
                        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                            sql.Identifier(runtime)
                        )
                    )
            except Exception:
                # A collision never enters these records, so it cannot trigger someone else's DROP.
                if database_created:
                    connection.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                    )
                for role in reversed(created_roles):
                    connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
                raise
        return allocation
    except Exception:
        raise RuntimeError("Fresh PostgreSQL role/database bootstrap failed") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Secret JSON file with admin_url")
    parser.add_argument("--output", type=Path, required=True, help="New private credentials file")
    args = parser.parse_args()
    try:
        if args.config:
            admin_url = SecretStr(json.loads(args.config.read_text(encoding="utf-8"))["admin_url"])
        else:
            admin_url = SecretStr(getpass.getpass("Administrative PostgreSQL URL: "))
        # Reserve a new output before creating any database. Never overwrite someone else's file.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            allocation = bootstrap_database(admin_url)
            json.dump(
                {
                    "database": allocation.name,
                    "owner_role": allocation.owner_role,
                    "runtime_role": allocation.runtime_role,
                    "owner_url": allocation.owner_url.get_secret_value(),
                    "runtime_url": allocation.runtime_url.get_secret_value(),
                },
                output,
            )
        print("Fresh PostgreSQL bootstrap complete; credentials saved to the supplied output file.")
        return 0
    except Exception:
        print(
            "PostgreSQL bootstrap failed; check configuration, permissions and output-file ownership."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

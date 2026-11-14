"""Explicit fresh Compose preparation. Never called by API/worker startup.

An exclusive durable started marker precedes allocation. A committed canonical
record is reused, not regenerated. Incomplete markers require operator recovery;
this tool does not guess ownership, rotate credentials or repair existing roles.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from psycopg import sql
from pydantic import SecretStr
from sqlalchemy.engine import URL

from scripts.init_postgres_roles import (
    RoleAllocation,
    _postgres_url,
    bootstrap_database,
    connect_postgres,
)

APP_UID = 10001
POSTGRES_GID = 999


class PreparationError(RuntimeError):
    """Static operator guidance, never credential/SQL/driver text."""


@dataclass(frozen=True)
class Paths:
    admin: Path
    owner: Path
    runtime: Path
    data: Path


def _sync_directory(directory):
    # Windows unit tests cover the state machine; Linux Compose verifies ownership
    # and directory durability. Native bootstrap remains the separate operator CLI.
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _permissions(path, uid, gid, mode):
    if path.is_symlink():
        raise PreparationError("Preparation paths must not be symbolic links")
    if os.name == "posix":
        os.chown(path, uid, gid)
    path.chmod(mode)


def _directories(paths):
    for path, uid, gid, mode in (
        (paths.admin, 0, POSTGRES_GID, 0o710),
        (paths.owner, APP_UID, APP_UID, 0o700),
        (paths.runtime, APP_UID, APP_UID, 0o700),
    ):
        path.mkdir(parents=True, exist_ok=True)
        _permissions(path, uid, gid, mode)


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise PreparationError("Preparation record is missing or invalid")
    with path.open("rb") as file:
        content = file.read(16385)
    if not content or len(content) > 16384:
        raise PreparationError("Preparation record is missing or invalid")
    return content


def _atomic_publish(path, content, *, uid=0, gid=0, mode=0o400):
    if path.exists() or path.is_symlink():
        if _read(path) != content:
            raise PreparationError(
                "Existing preparation record does not match; refusing replacement"
            )
        _permissions(path, uid, gid, mode)
        return
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            _permissions(temporary, uid, gid, mode)
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.chmod(0o600)
            temporary.unlink()


def _reserve(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(b"started-v1\n")
        file.flush()
        os.fsync(file.fileno())
    _sync_directory(path.parent)


@contextmanager
def _lock(paths):
    lock = paths.admin / "preparation.lock"
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise PreparationError(
            "Preparation is busy or interrupted; inspect retained state"
        ) from None
    identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(b"exclusive-v1\n")
            file.flush()
            os.fsync(file.fileno())
        _sync_directory(paths.admin)
        yield
    finally:
        current = lock.stat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise PreparationError("Preparation lock identity changed; refusing removal")
        lock.unlink()
        _sync_directory(paths.admin)


def _admin_secret(paths):
    if not (paths.admin / "admin.started").is_file():
        raise PreparationError("Admin preparation state is incomplete")
    record = json.loads(_read(paths.admin / "admin.json"))
    if set(record) != {"admin_url"} or not isinstance(record["admin_url"], str):
        raise PreparationError("Admin preparation record is invalid")
    secret = SecretStr(record["admin_url"])
    parsed = _postgres_url(secret)
    if not parsed.username or not parsed.password or parsed.database != "postgres":
        raise PreparationError("Admin preparation record is invalid")
    return secret


def prepare_admin(paths):
    try:
        _directories(paths)
        with _lock(paths):
            record = paths.admin / "admin.json"
            if not record.exists():
                if (
                    (paths.admin / "admin.started").exists()
                    or any(paths.data.iterdir())
                    or any(paths.owner.iterdir())
                    or any(paths.runtime.iterdir())
                    or any(p.name != "preparation.lock" for p in paths.admin.iterdir())
                ):
                    raise PreparationError(
                        "Missing admin identity on retained state; operator recovery required"
                    )
                _reserve(paths.admin / "admin.started")
                password = secrets.token_urlsafe(48)
                secret = URL.create(
                    "postgresql",
                    username="postgres",
                    password=password,
                    host="postgres",
                    port=5432,
                    database="postgres",
                ).render_as_string(hide_password=False)
                _atomic_publish(record, json.dumps({"admin_url": secret}).encode())
            parsed = _postgres_url(_admin_secret(paths))
            _atomic_publish(
                paths.admin / "password", parsed.password.encode(), gid=POSTGRES_GID, mode=0o440
            )
    except Exception:
        raise PreparationError("Admin preparation failed; inspect retained private state") from None


def _allocation_record(allocation):
    return {
        "database": allocation.name,
        "owner_role": allocation.owner_role,
        "runtime_role": allocation.runtime_role,
        "owner_url": allocation.owner_url.get_secret_value(),
        "runtime_url": allocation.runtime_url.get_secret_value(),
    }


def _read_allocation(paths, admin_url):
    record = json.loads(_read(paths.admin / "allocation.json"))
    if set(record) != {"database", "owner_role", "runtime_role", "owner_url", "runtime_url"}:
        raise PreparationError("Allocation record is invalid")
    name, owner, runtime = (record[key] for key in ("database", "owner_role", "runtime_role"))
    if not isinstance(name, str) or not re.fullmatch(r"workflow_test_[a-f0-9]{32}", name):
        raise PreparationError("Allocation record is invalid")
    suffix = name.removeprefix("workflow_test_")
    if owner != "workflow_owner_" + suffix or runtime != "workflow_runtime_" + suffix:
        raise PreparationError("Allocation record is invalid")
    allocation = RoleAllocation(
        name, owner, runtime, SecretStr(record["owner_url"]), SecretStr(record["runtime_url"])
    )
    admin = _postgres_url(admin_url)
    owner_url, runtime_url = (
        _postgres_url(allocation.owner_url),
        _postgres_url(allocation.runtime_url),
    )
    for parsed, role in ((owner_url, owner), (runtime_url, runtime)):
        if (
            parsed.database != name
            or parsed.username != role
            or not parsed.password
            or (parsed.host, parsed.port, parsed.query) != (admin.host, admin.port, admin.query)
        ):
            raise PreparationError("Allocation record does not identify this deployment")
    if owner_url.password == runtime_url.password:
        raise PreparationError("Allocation credentials are not separated")
    return allocation


def _verify_allocation(admin_url, allocation):
    with connect_postgres(admin_url) as connection:
        owner = connection.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (allocation.name,)
        ).fetchone()
        if owner != (allocation.owner_role,):
            raise PreparationError("Recorded database ownership does not match")
        roles = connection.execute(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
            "rolinherit, rolcanlogin FROM pg_roles WHERE rolname IN (%s, %s)",
            (allocation.owner_role, allocation.runtime_role),
        ).fetchall()
        if set(roles) != {
            (role, False, False, False, False, False, False, True)
            for role in (allocation.owner_role, allocation.runtime_role)
        } or connection.execute(
            "SELECT pg_has_role(%s, %s, 'MEMBER')", (allocation.runtime_role, allocation.owner_role)
        ).fetchone() != (False,):
            raise PreparationError("Recorded role separation does not match")
    for secret, role in (
        (allocation.owner_url, allocation.owner_role),
        (allocation.runtime_url, allocation.runtime_role),
    ):
        with connect_postgres(secret) as connection:
            if connection.execute(
                "SELECT current_database(), current_user, current_setting('workflow.runtime_role', true)"
            ).fetchone() != (allocation.name, role, allocation.runtime_role):
                raise PreparationError("Recorded credentials do not match this deployment")
            if role == allocation.runtime_role and connection.execute(
                "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
            ).fetchone() != (False,):
                raise PreparationError("Runtime credential can create schema objects")


def _discard_fresh(admin_url, allocation):
    """Only a returned allocation whose canonical record was not committed by this call."""
    with connect_postgres(admin_url) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(allocation.name))
        )
        for role in (allocation.runtime_role, allocation.owner_role):
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def prepare_roles(paths):
    try:
        _directories(paths)
        with _lock(paths):
            admin_url = _admin_secret(paths)
            if _read(paths.admin / "password") != _postgres_url(admin_url).password.encode():
                raise PreparationError("Admin password file does not match the retained identity")
            if not (paths.data / "PG_VERSION").is_file():
                raise PreparationError("PostgreSQL data initialization is missing")
            canonical = paths.admin / "allocation.json"
            started = paths.admin / "roles.started"
            if canonical.exists():
                if not started.is_file():
                    raise PreparationError("Allocation preparation state is incomplete")
                allocation = _read_allocation(paths, admin_url)
            else:
                if started.exists() or any(paths.owner.iterdir()) or any(paths.runtime.iterdir()):
                    raise PreparationError("Interrupted allocation requires operator recovery")
                _reserve(started)
                allocation = bootstrap_database(admin_url)
                try:
                    _verify_allocation(admin_url, allocation)
                    _atomic_publish(canonical, json.dumps(_allocation_record(allocation)).encode())
                except Exception:
                    # A rename can succeed before directory fsync reports failure. Never
                    # delete a visible committed record's database. Retry validates/reuses it.
                    if not canonical.exists():
                        try:
                            _discard_fresh(admin_url, allocation)
                        except Exception:
                            # A private, non-secret identity journal aids explicit recovery.
                            _atomic_publish(
                                paths.admin / "roles.failed",
                                json.dumps(
                                    {
                                        "database": allocation.name,
                                        "owner_role": allocation.owner_role,
                                        "runtime_role": allocation.runtime_role,
                                    }
                                ).encode(),
                            )
                    raise
            _verify_allocation(admin_url, allocation)
            for path, secret in (
                (paths.owner / "owner.url", allocation.owner_url),
                (paths.runtime / "runtime.url", allocation.runtime_url),
            ):
                _atomic_publish(path, secret.get_secret_value().encode(), uid=APP_UID, gid=APP_UID)
            return allocation
    except Exception:
        raise PreparationError("Role preparation failed; inspect retained private state") from None


def migrate(owner_file):
    try:
        secret = SecretStr(_read(owner_file).decode("utf-8"))
        _postgres_url(secret)
        allowed = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
        }
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment["WORKFLOW_MIGRATION_URL"] = secret.get_secret_value()
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise PreparationError("Explicit schema migration failed")
        print("PostgreSQL schema migration complete")
    except Exception:
        raise PreparationError("Explicit schema migration failed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["admin", "roles", "migrate"])
    args = parser.parse_args()
    paths = Paths(
        Path("/run/pg-admin"),
        Path("/run/pg-owner"),
        Path("/run/pg-runtime"),
        Path("/deployment-data"),
    )
    try:
        if args.command == "migrate":
            migrate(paths.owner / "owner.url")
        else:
            if sys.platform != "linux" or os.geteuid() != 0:
                raise PreparationError("Compose preparation requires its isolated Linux job")
            if args.command == "admin":
                prepare_admin(paths)
            else:
                prepare_roles(paths)
            print("PostgreSQL preparation complete; existing identity retained")
        return 0
    except Exception:
        print("PostgreSQL preparation failed; inspect retained private state before retrying")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

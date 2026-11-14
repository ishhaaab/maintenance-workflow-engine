"""Durable preparation decisions. Actual SQL/permissions are checked separately."""

import importlib
import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from scripts.init_postgres_roles import RoleAllocation


@pytest.fixture(autouse=True)
def unprivileged_unit_metadata(monkeypatch):
    # State-machine tests keep real file writes/fsync/locks. Only root-only
    # ownership metadata is deferred to the actual Linux Compose checks.
    if os.name == "posix" and os.geteuid() != 0:
        monkeypatch.setattr(os, "chown", lambda *args: None)


def preparation(tmp_path):
    module = importlib.import_module("scripts.prepare_postgres")
    paths = module.Paths(*(tmp_path / name for name in ("admin", "owner", "runtime", "data")))
    paths.data.mkdir()
    return module, paths


def role_setup(tmp_path, monkeypatch):
    module, paths = preparation(tmp_path)
    module.prepare_admin(paths)
    (paths.data / "PG_VERSION").write_text("17", encoding="utf-8")
    allocation = RoleAllocation(
        "workflow_test_" + "a" * 32,
        "workflow_owner_" + "a" * 32,
        "workflow_runtime_" + "a" * 32,
        SecretStr(
            "postgresql://workflow_owner_"
            + "a" * 32
            + ":owner-secret@postgres:5432/workflow_test_"
            + "a" * 32
        ),
        SecretStr(
            "postgresql://workflow_runtime_"
            + "a" * 32
            + ":runtime-secret@postgres:5432/workflow_test_"
            + "a" * 32
        ),
    )
    calls, discarded = [], []

    def allocate(admin_url):
        calls.append(admin_url)
        return allocation

    monkeypatch.setattr(module, "bootstrap_database", allocate)
    monkeypatch.setattr(module, "_verify_allocation", lambda *args: None)
    monkeypatch.setattr(module, "_discard_fresh", lambda admin, row: discarded.append(row))
    return module, paths, allocation, calls, discarded


def test_admin_preparation_reuses_one_secret_and_reconstructs_missing_split(tmp_path):
    module, paths = preparation(tmp_path)
    module.prepare_admin(paths)
    canonical = (paths.admin / "admin.json").read_bytes()
    password = (paths.admin / "password").read_bytes()
    assert make_url(json.loads(canonical)["admin_url"]).password.encode() == password
    module.prepare_admin(paths)
    assert (paths.admin / "admin.json").read_bytes() == canonical
    (paths.admin / "password").chmod(0o600)
    (paths.admin / "password").unlink()
    module.prepare_admin(paths)
    assert (paths.admin / "password").read_bytes() == password


def test_admin_secret_is_not_generated_for_an_initialized_data_volume(tmp_path):
    module, paths = preparation(tmp_path)
    (paths.data / "PG_VERSION").write_text("17")
    with pytest.raises(module.PreparationError):
        module.prepare_admin(paths)
    assert not (paths.admin / "admin.json").exists()


def test_incomplete_admin_marker_requires_operator_recovery(tmp_path):
    module, paths = preparation(tmp_path)
    paths.admin.mkdir()
    (paths.admin / "admin.started").write_text("started")
    with pytest.raises(module.PreparationError):
        module.prepare_admin(paths)
    assert not (paths.admin / "admin.json").exists()


def test_roles_reuse_canonical_allocation_and_republish_missing_runtime_file(tmp_path, monkeypatch):
    module, paths, allocation, calls, discarded = role_setup(tmp_path, monkeypatch)
    assert module.prepare_roles(paths) == allocation
    canonical = (paths.admin / "allocation.json").read_bytes()
    assert (paths.owner / "owner.url").read_text() == allocation.owner_url.get_secret_value()
    runtime = paths.runtime / "runtime.url"
    assert runtime.read_text() == allocation.runtime_url.get_secret_value()
    runtime.chmod(0o600)
    runtime.unlink()
    assert module.prepare_roles(paths) == allocation
    assert runtime.read_text() == allocation.runtime_url.get_secret_value()
    assert (paths.admin / "allocation.json").read_bytes() == canonical
    assert len(calls) == 1 and discarded == []


def test_started_role_allocation_never_silently_allocates_again(tmp_path, monkeypatch):
    module, paths, _, calls, _ = role_setup(tmp_path, monkeypatch)
    (paths.admin / "roles.started").write_text("started")
    with pytest.raises(module.PreparationError):
        module.prepare_roles(paths)
    assert calls == []


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("password_state", ["missing", "mismatched"])
def test_roles_require_the_matching_administrator_password_file(
    tmp_path, monkeypatch, committed, password_state
):
    module, paths, _, calls, discarded = role_setup(tmp_path, monkeypatch)
    if committed:
        module.prepare_roles(paths)
    password = paths.admin / "password"
    password.chmod(0o600)
    password.unlink()
    if password_state == "mismatched":
        password.write_text("wrong-password-record", encoding="utf-8")
    with pytest.raises(module.PreparationError):
        module.prepare_roles(paths)
    assert len(calls) == int(committed) and discarded == []


def test_canonical_publication_failure_compensates_only_the_fresh_allocation(tmp_path, monkeypatch):
    module, paths, allocation, calls, discarded = role_setup(tmp_path, monkeypatch)
    publish = module._atomic_publish

    def fail_canonical(path, *args, **kwargs):
        if path.name == "allocation.json":
            raise OSError("private-write-error")
        return publish(path, *args, **kwargs)

    monkeypatch.setattr(module, "_atomic_publish", fail_canonical)
    with pytest.raises(module.PreparationError):
        module.prepare_roles(paths)
    assert discarded == [allocation] and len(calls) == 1
    assert not (paths.admin / "allocation.json").exists()
    with pytest.raises(module.PreparationError):
        module.prepare_roles(paths)
    assert len(calls) == 1


def test_split_publication_retry_keeps_committed_credentials_and_does_not_drop(
    tmp_path, monkeypatch
):
    module, paths, allocation, calls, discarded = role_setup(tmp_path, monkeypatch)
    publish = module._atomic_publish

    def fail_runtime(path, *args, **kwargs):
        if path == paths.runtime / "runtime.url":
            raise OSError("private-write-error")
        return publish(path, *args, **kwargs)

    monkeypatch.setattr(module, "_atomic_publish", fail_runtime)
    with pytest.raises(module.PreparationError):
        module.prepare_roles(paths)
    assert (paths.admin / "allocation.json").exists()
    assert len(calls) == 1 and discarded == []
    monkeypatch.setattr(module, "_atomic_publish", publish)
    assert module.prepare_roles(paths) == allocation
    assert len(calls) == 1 and discarded == []


@pytest.mark.parametrize("filename", ["allocation.json", "runtime.url"])
def test_corrupt_or_mismatched_records_are_not_replaced(tmp_path, monkeypatch, filename):
    module, paths, _, calls, discarded = role_setup(tmp_path, monkeypatch)
    module.prepare_roles(paths)
    target = (paths.admin if filename.endswith("json") else paths.runtime) / filename
    target.chmod(0o600)
    target.write_text("must-preserve-invalid-record")
    with pytest.raises(module.PreparationError):
        module.prepare_roles(paths)
    assert target.read_text() == "must-preserve-invalid-record"
    assert len(calls) == 1 and discarded == []


def test_invalid_existing_database_cannot_trigger_reallocation(tmp_path, monkeypatch):
    module, paths, _, calls, discarded = role_setup(tmp_path, monkeypatch)
    module.prepare_roles(paths)

    def unavailable(*args):
        raise RuntimeError("private-target-or-credential-error")

    monkeypatch.setattr(module, "_verify_allocation", unavailable)
    with pytest.raises(module.PreparationError) as error:
        module.prepare_roles(paths)
    assert "private-target-or-credential-error" not in str(error.value)
    assert len(calls) == 1 and discarded == []


def test_concurrent_preparation_cannot_create_two_allocations(tmp_path, monkeypatch):
    module, paths, allocation, calls, discarded = role_setup(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def waiting_allocator(admin):
        calls.append(admin)
        entered.set()
        assert release.wait(5)
        return allocation

    def first():
        try:
            module.prepare_roles(paths)
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(module, "bootstrap_database", waiting_allocator)
    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(module.PreparationError):
            module.prepare_roles(paths)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert len(calls) == 1 and discarded == []


def test_migration_subprocess_gets_owner_url_only_and_never_echoes_diagnostics(
    tmp_path, monkeypatch, capsys
):
    module, paths, allocation, _, _ = role_setup(tmp_path, monkeypatch)
    module.prepare_roles(paths)
    monkeypatch.setenv("TEST_POSTGRES_URL", "must-not-inherit-admin")
    monkeypatch.setenv("PGPASSWORD", "must-not-inherit-admin")
    monkeypatch.setenv("WORKFLOW_DATABASE_URL", "must-not-inherit-runtime")
    monkeypatch.setenv("WORKFLOW_MIGRATION_URL_FILE", "must-not-inherit-file")

    def migrate(command, *, env, **kwargs):
        assert env["WORKFLOW_MIGRATION_URL"] == allocation.owner_url.get_secret_value()
        assert not any(value.startswith("must-not-inherit") for value in env.values())
        assert command[-3:] == ["alembic", "upgrade", "head"]
        return SimpleNamespace(
            returncode=0, stdout="private-driver-detail", stderr="private-driver-detail"
        )

    monkeypatch.setattr(module.subprocess, "run", migrate)
    module.migrate(paths.owner / "owner.url")
    assert "private" not in capsys.readouterr().out


def test_privileged_preparation_refuses_non_linux_before_accessing_paths(tmp_path, monkeypatch):
    module, _ = preparation(tmp_path)
    visited = []
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(sys, "argv", ["prepare_postgres", "admin"])
    monkeypatch.setattr(module, "prepare_admin", lambda paths: visited.append(paths))
    assert module.main() == 1
    assert visited == []

import os

from tests import processes


def test_children_receive_only_explicit_service_configuration(monkeypatch):
    for name in (
        "TEST_POSTGRES_URL",
        "WORKFLOW_DATABASE_URL",
        "WORKFLOW_DATABASE_URL_FILE",
        "WORKFLOW_MIGRATION_URL",
        "WORKFLOW_MIGRATION_URL_FILE",
        "PGPASSWORD",
        "TEST_MONGO_URL",
        "WORKFLOW_MONGO_URL",
        "DATABASE_URL",
        "OWNER_PASSWORD",
    ):
        monkeypatch.setenv(name, "must-not-inherit")
    env = processes.child_environment()
    assert not any(value == "must-not-inherit" for value in env.values())
    assert env["PATH"] == os.environ["PATH"]
    runtime = processes.child_environment(WORKFLOW_DATABASE_URL="explicit-runtime-only")
    assert runtime["WORKFLOW_DATABASE_URL"] == "explicit-runtime-only"
    assert "WORKFLOW_DATABASE_URL_FILE" not in runtime


def test_child_environment_diagnostics_hide_explicit_credentials():
    env = processes.child_environment(WORKFLOW_DATABASE_URL="test-runtime-secret")
    assert "test-runtime-secret" not in repr(env)

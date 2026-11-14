from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from workflow_engine.settings import Settings

URL = "postgresql://runtime:private-url-password@127.0.0.1:5432/execution"


@pytest.fixture(autouse=True)
def no_runtime_url(monkeypatch):
    monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_DATABASE_URL_FILE", raising=False)


def resolve(settings):
    return settings.runtime_database_url()


def test_planner_settings_and_cli_help_need_no_database():
    assert Settings().planner == "fixture"
    with pytest.raises(ValueError) as error:
        resolve(Settings())
    assert "private" not in str(error.value)


def test_runtime_reads_exactly_one_native_secret_url(monkeypatch):
    monkeypatch.setenv("WORKFLOW_DATABASE_URL", URL)
    settings = Settings()
    assert isinstance(resolve(settings), SecretStr)
    assert resolve(settings).get_secret_value() == URL
    assert "private-url-password" not in repr(settings)


def test_runtime_reads_raw_url_file_without_privileged_siblings(tmp_path, monkeypatch):
    path = tmp_path / "runtime-url"
    path.write_text(URL + "\n", encoding="utf-8")
    monkeypatch.setenv("WORKFLOW_DATABASE_URL_FILE", str(path))
    monkeypatch.setenv("WORKFLOW_MIGRATION_URL", "must-not-be-consumed")
    settings = Settings()
    assert resolve(settings).get_secret_value() == URL
    assert "migration_url" not in Settings.model_fields


@pytest.mark.parametrize("source", ["both", "missing", "empty_url", "mongo", "invalid"])
def test_runtime_url_sources_fail_closed_and_secret_safe(tmp_path, source):
    path = tmp_path / "private-path"
    path.write_text(URL, encoding="utf-8")
    values = {
        "both": {"database_url": URL, "database_url_file": str(path)},
        "missing": {},
        "empty_url": {"database_url": ""},
        "mongo": {"database_url": "mongodb://private-url-password@host/db"},
        "invalid": {"database_url": "private-url-password"},
    }[source]
    with pytest.raises(ValueError) as error:
        resolve(Settings(**values))
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "{}",
        '{"runtime_url":"' + URL + '"}',
        URL + "\n" + URL,
        URL + "x" * 16384,
        "\x00",
        "\udcff",
    ],
    ids=["empty", "object", "allocation", "multiline", "oversize", "nul", "invalid_utf8"],
)
def test_secret_file_rejects_empty_allocation_json_multiline_and_oversize(tmp_path, contents):
    path = tmp_path / "private-file"
    path.write_bytes(contents.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(ValueError) as error:
        resolve(Settings(database_url_file=str(path)))
    assert "private" not in str(error.value)


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_secret_file_errors_are_static(tmp_path, monkeypatch, kind):
    path = tmp_path / "private-file"
    if kind == "directory":
        path.mkdir()
    if kind == "unreadable":

        def denied(*args, **kwargs):
            raise PermissionError("private-file-contents")

        monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ValueError) as error:
        resolve(Settings(database_url_file=str(path)))
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "field", ["mongo_url", "mongo_database", "api_keys", "migration_url", "admin_url"]
)
def test_application_settings_have_no_legacy_or_privileged_access(field):
    with pytest.raises(ValidationError) as error:
        Settings(**{field: "private-forbidden-value"})
    assert "private-forbidden-value" not in str(error.value)

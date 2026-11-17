from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_MAX_DATABASE_URL_BYTES = 16384
_DATABASE_CONFIG_ERROR = "Configure exactly one valid runtime PostgreSQL URL or raw URL file"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WORKFLOW_", extra="forbid", hide_input_in_errors=True
    )
    database_url: SecretStr | None = None
    database_url_file: str | None = Field(default=None, repr=False)
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    redis_stream: str = Field(default="workflow-pg-wakeups-v1", min_length=1, max_length=128)
    redis_group: str = Field(default="workflow-pg-workers-v1", min_length=1, max_length=128)
    catalog_path: str = "fixtures/catalog.jsonl"
    fixtures_path: str = "fixtures/planner_dev.jsonl"
    ticketing_url: str = "http://127.0.0.1:8001"
    inventory_url: str = "http://127.0.0.1:8002"
    planner: str = "fixture"
    model_url: str = "http://127.0.0.1:1234/v1"
    model_name: str = "Qwen/Qwen2.5-7B-Instruct"
    model_revision: str = "unrecorded"
    model_key: SecretStr = SecretStr("")
    env: Literal["production", "local"] = "production"
    origin: str | None = None
    allow_demo_seed: bool = False
    smtp_host: str = "127.0.0.1"
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_starttls: bool = False
    smtp_username: str | None = Field(default=None, repr=False)
    smtp_password: SecretStr = SecretStr("")
    lease_s: int = Field(default=30, ge=10, le=300)
    heartbeat_s: int = Field(default=5, ge=1, le=5)
    external_timeout_s: int = Field(default=10, ge=1, le=10)
    planner_timeout_s: int = Field(default=15, ge=1, le=15)

    def browser_security(self) -> tuple[str, bool]:
        try:
            parsed = urlsplit(self.origin or "")
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError
            if self.env == "local":
                if (
                    parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or parsed.scheme != "http"
                ):
                    raise ValueError
                return "workflow_local_session", False
            if parsed.scheme != "https":
                raise ValueError
            return "__Host-workflow_session", True
        except ValueError:
            raise ValueError(
                "Configure HTTPS WORKFLOW_ORIGIN or explicit loopback-only local mode"
            ) from None

    def notification_security(self):
        self.browser_security()
        if self.env == "local":
            if self.smtp_host not in {"127.0.0.1", "localhost", "::1", "mailpit"}:
                raise ValueError("Local demo mail must stay inside the local capture inbox")
        elif not (
            self.smtp_starttls and self.smtp_username and self.smtp_password.get_secret_value()
        ):
            raise ValueError("Production SMTP requires explicit authenticated TLS")

    def runtime_database_url(self) -> SecretStr:
        """Resolve only runtime credentials, lazily so planner tools and --help stay offline."""
        try:
            if (self.database_url is None) == (self.database_url_file is None):
                raise ValueError
            if self.database_url_file is not None:
                with Path(self.database_url_file).open("rb") as file:
                    raw = file.read(_MAX_DATABASE_URL_BYTES + 1)
                if len(raw) > _MAX_DATABASE_URL_BYTES:
                    raise ValueError
                value = raw.decode("utf-8").strip()
            else:
                value = self.database_url.get_secret_value()
            if (
                not value
                or len(value.encode("utf-8")) > _MAX_DATABASE_URL_BYTES
                or any(character in value for character in "\r\n\0")
                or make_url(value).get_backend_name() != "postgresql"
            ):
                raise ValueError
            return SecretStr(value)
        except (OSError, ValueError, TypeError, ArgumentError):
            # Never include file paths, file contents, URL values or parser diagnostics.
            raise ValueError(_DATABASE_CONFIG_ERROR) from None

    @field_validator("planner")
    @classmethod
    def known_planner(cls, value):
        if value not in {"fixture", "model"}:
            raise ValueError("Unknown planner")
        return value

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKFLOW_", extra="forbid")
    mongo_url: SecretStr = SecretStr("mongodb://127.0.0.1:27017")
    mongo_database: str = "maintenance_workflow"
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    api_keys: dict[str, str] = Field(default_factory=dict, repr=False)
    catalog_path: str = "fixtures/catalog.jsonl"
    fixtures_path: str = "fixtures/planner_dev.jsonl"
    ticketing_url: str = "http://127.0.0.1:8001"
    inventory_url: str = "http://127.0.0.1:8002"
    planner: str = "fixture"
    model_url: str = "http://127.0.0.1:1234/v1"
    model_name: str = "Qwen/Qwen2.5-7B-Instruct"
    model_revision: str = "unrecorded"
    model_key: SecretStr = SecretStr("")
    lease_s: int = Field(default=30, ge=10, le=300)
    heartbeat_s: int = Field(default=5, ge=1, le=5)
    external_timeout_s: int = Field(default=10, ge=1, le=10)
    planner_timeout_s: int = Field(default=15, ge=1, le=15)

    @field_validator("planner")
    @classmethod
    def known_planner(cls, value):
        if value not in {"fixture", "model"}:
            raise ValueError("Unknown planner")
        return value

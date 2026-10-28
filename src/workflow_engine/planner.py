"""Proposal adapters have no mutation gateway and receive only an authorized catalog."""

import hashlib
import json
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import TypeAdapter

from .domain import NeedsInput, PlanProposal, WorkflowCreate
from .policy import TenantCatalog

_PROPOSAL = TypeAdapter(PlanProposal)


def parse_proposal(raw: str) -> PlanProposal:
    return _PROPOSAL.validate_json(raw)


class Planner(Protocol):
    metadata: dict

    async def propose(self, request: WorkflowCreate, catalog: TenantCatalog) -> PlanProposal: ...


class FixturePlanner:
    metadata = {"adapter": "fixture", "model": None, "prompt_version": "maintenance_v1"}

    def __init__(self, cases: list[dict]):
        self._cases = {
            (c["tenant_id"], c["text"]): _PROPOSAL.validate_python(c["expected"]) for c in cases
        }

    @classmethod
    def from_file(cls, path: str | Path):
        return cls(
            [
                json.loads(line)
                for line in Path(path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        )

    async def propose(self, request: WorkflowCreate, catalog: TenantCatalog) -> PlanProposal:
        return self._cases.get(
            (catalog.tenant_id, request.text),
            NeedsInput(
                questions=["Submit a documented fixture request or configure the model adapter."]
            ),
        ).model_copy(deep=True)


class ModelPlanner:
    """Exactly one provider request per propose; the runner owns the durable call budget."""

    def __init__(
        self, client: httpx.AsyncClient, settings, prompt_path="prompts/maintenance_v1.txt"
    ):
        self.client, self.settings = client, settings
        self.prompt = Path(prompt_path).read_text(encoding="utf-8")
        self.metadata = {
            "adapter": "model",
            "model": settings.model_name,
            "model_revision": settings.model_revision,
            "prompt_version": "maintenance_v1",
            "prompt_hash": hashlib.sha256(self.prompt.encode()).hexdigest(),
            "temperature": 0,
            "max_tokens": 512,
            "response_format": "json_object",
        }
        # Evaluation is sequential; these diagnostics never enter workflow logs.
        self.last_usage = {}
        self.last_schema_valid = False
        self.last_inference_response = False

    async def propose(self, request: WorkflowCreate, catalog: TenantCatalog) -> PlanProposal:
        self.last_usage, self.last_schema_valid = {}, False
        self.last_inference_response = False
        headers = {}
        if self.settings.model_key.get_secret_value():
            headers["Authorization"] = "Bearer " + self.settings.model_key.get_secret_value()
        payload = {
            "model": self.settings.model_name,
            "temperature": 0,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": self.prompt
                    + "\nJSON schema:\n"
                    + json.dumps(_PROPOSAL.json_schema()),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request": request.model_dump(),
                            "authorized_catalog": catalog.model_dump(),
                        }
                    ),
                },
            ],
        }
        try:
            response = await self.client.post(
                "chat/completions",
                json=payload,
                headers=headers,
                timeout=self.settings.planner_timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            raise ConnectionError("Planner provider unavailable") from None
        try:
            if len(response.content) > 131072:
                raise ValueError("Oversized model response")
            body = response.json()
            usage = body.get("usage", {})
            self.last_usage = {
                key: value
                for key, value in usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and isinstance(value, int)
            }
            message = body["choices"][0]["message"]
            self.last_inference_response = isinstance(message.get("content"), str) or isinstance(
                message.get("refusal"), str
            )
            if message.get("refusal"):
                raise ValueError("Refused")
            proposal = parse_proposal(message["content"])
            self.last_schema_valid = True
            return proposal
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return NeedsInput(
                questions=["Please clarify the authorized asset and requested maintenance action."]
            )

"""Proposal adapters have no mutation gateway and receive only an authorized catalog."""
import json
from pathlib import Path
from typing import Protocol

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
        self._cases = {(c["tenant_id"], c["text"]): _PROPOSAL.validate_python(c["expected"]) for c in cases}

    @classmethod
    def from_file(cls, path: str | Path):
        return cls([json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()])

    async def propose(self, request: WorkflowCreate, catalog: TenantCatalog) -> PlanProposal:
        return self._cases.get((catalog.tenant_id, request.text), NeedsInput(questions=["Submit a documented fixture request or configure the model adapter."])).model_copy(deep=True)

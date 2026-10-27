"""Tenant-scoped catalog and deliberately limited deterministic request grammar."""

import re
from pathlib import Path

from pydantic import Field

from .domain import (
    Action,
    ExecutablePlan,
    Model,
    NeedsInput,
    PlanProposal,
    WorkflowCreate,
    canonical_hash,
)


class Asset(Model):
    id: str
    aliases: list[str]
    part_ids: list[str]


class Part(Model):
    id: str
    aliases: list[str]


class TenantCatalog(Model):
    tenant_id: str
    version: str
    assets: list[Asset]
    parts: list[Part]
    allowed_actions: list[str] = Field(default_factory=lambda: ["ticket", "reservation"])


class CatalogProvider:
    def __init__(self, catalogs: list[TenantCatalog]):
        self._catalogs = {c.tenant_id: c for c in catalogs}

    @classmethod
    def from_file(cls, path: str | Path):
        return cls(
            [
                TenantCatalog.model_validate_json(line)
                for line in Path(path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        )

    def get(self, tenant_id: str) -> TenantCatalog:
        return self._catalogs[tenant_id].model_copy(deep=True)


class CompiledPlan(Model):
    plan: ExecutablePlan
    plan_hash: str
    catalog_version: str
    actions: list[Action]


_GRAMMAR = re.compile(
    r"create (?:an? )?inspection ticket for (?P<asset>.+?)"
    r"(?: with (?P<priority>low|normal|high) priority)?"
    r"(?: and reserve (?P<quantity>one|a|[0-9]+) (?P<part>.+?) if available)?[.!]?",
    re.IGNORECASE,
)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _resolve(reference: str, entries: list):
    matches = [
        entry
        for entry in entries
        if _normalize(reference) in {_normalize(s) for s in [entry.id, *entry.aliases]}
    ]
    return matches[0] if len(matches) == 1 else None


def request_intent(request: WorkflowCreate, catalog: TenantCatalog) -> dict | None:
    """Only this documented grammar can authorize an executable plan."""
    match = _GRAMMAR.fullmatch(" ".join(request.text.strip().split()))
    if not match or "ticket" not in catalog.allowed_actions:
        return None
    asset = _resolve(match["asset"], catalog.assets)
    if not asset:
        return None
    result = {
        "asset_id": asset.id,
        "priority": (match["priority"] or "normal").lower(),
        "reservation": None,
    }
    if match["part"]:
        part = _resolve(match["part"], catalog.parts)
        quantity = 1 if match["quantity"].lower() in {"one", "a"} else int(match["quantity"])
        if (
            "reservation" not in catalog.allowed_actions
            or not part
            or part.id not in asset.part_ids
            or not 1 <= quantity <= 10
        ):
            return None
        result["reservation"] = {"part_id": part.id, "quantity": quantity}
    return result


def validate_and_compile(
    request: WorkflowCreate,
    proposal: PlanProposal,
    catalog: TenantCatalog,
    workflow_id: str,
    tenant_id: str,
) -> CompiledPlan | NeedsInput:
    clarification = NeedsInput(
        questions=[
            "Specify an authorized asset and, if needed, a compatible part and quantity using the supported request format."
        ]
    )
    if catalog.tenant_id != tenant_id:
        return clarification
    if isinstance(proposal, NeedsInput):
        return proposal
    intent = request_intent(request, catalog)
    reservation = proposal.reservation.model_dump() if proposal.reservation else None
    if (
        not intent
        or proposal.asset_id != intent["asset_id"]
        or proposal.ticket.priority != intent["priority"]
        or reservation != intent["reservation"]
    ):
        return clarification
    plan_hash = canonical_hash(
        {"schema_version": 1, "catalog_version": catalog.version, "plan": proposal.model_dump()}
    )
    actions = []
    arguments = {"ticket": {"asset_id": proposal.asset_id, **proposal.ticket.model_dump()}}
    if reservation:
        arguments["reservation"] = reservation
    for action_id, args in arguments.items():
        key = canonical_hash([tenant_id, workflow_id, plan_hash, action_id])
        actions.append(Action(action_id=action_id, arguments=args, idempotency_key=key))
    return CompiledPlan(
        plan=proposal, plan_hash=plan_hash, catalog_version=catalog.version, actions=actions
    )

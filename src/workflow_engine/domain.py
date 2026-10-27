"""Pure workflow transitions. Concurrency and I/O belong to the store and runner."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_SUBMISSIONS = 5
MAX_LOOKUPS = 5
MAX_PLANNER_CALLS = 2
MAX_DOCUMENT_BYTES = 256 * 1024
RETRY_DELAYS = (1, 2, 4, 8)
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_REVIEW"})
PASSIVE = TERMINAL | {"NEEDS_INPUT", "AWAITING_APPROVAL"}
WorkflowState = Literal[
    "PENDING_PLAN",
    "NEEDS_INPUT",
    "AWAITING_APPROVAL",
    "READY",
    "RUNNING",
    "RETRY_WAIT",
    "RECONCILING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "NEEDS_REVIEW",
]
ActionState = Literal["PENDING", "IN_FLIGHT", "CONFIRMED", "SKIPPED", "FAILED", "UNKNOWN"]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class InvalidPlan(Exception):
    pass


class LeaseLost(Conflict):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*")
    @classmethod
    def utc_dates(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Timezone-aware UTC timestamp required")
            return value.astimezone(UTC)
        return value


class WorkflowCreate(Model):
    text: ShortText

    @field_validator("text")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Request cannot be blank")
        return value


class ApprovalRequest(Model):
    expected_revision: int = Field(ge=0, strict=True)
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class CancelRequest(Model):
    expected_revision: int = Field(ge=0, strict=True)


class TicketSpec(Model):
    priority: Literal["low", "normal", "high"] = "normal"
    description: ShortText


class ReservationSpec(Model):
    part_id: Identifier
    quantity: int = Field(ge=1, le=10, strict=True)


class ExecutablePlan(Model):
    kind: Literal["plan"] = "plan"
    asset_id: Identifier
    ticket: TicketSpec
    reservation: ReservationSpec | None = None


class NeedsInput(Model):
    kind: Literal["needs_input"] = "needs_input"
    questions: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=3
    )


PlanProposal = Annotated[ExecutablePlan | NeedsInput, Field(discriminator="kind")]


class Action(Model):
    action_id: Literal["ticket", "reservation"]
    arguments: dict
    idempotency_key: Identifier
    status: ActionState = "PENDING"
    submissions: int = Field(default=0, ge=0, le=MAX_SUBMISSIONS)
    reconciliation_count: int = Field(default=0, ge=0, le=MAX_LOOKUPS)
    has_ambiguous_submission: bool = False
    next_retry_at: datetime | None = None
    result: dict | None = None
    last_error: ShortText | None = None


class HistoryEntry(Model):
    at: datetime
    event: str = Field(max_length=64)
    action_id: str | None = None
    error: ShortText | None = None


class Workflow(Model):
    workflow_id: str
    tenant_id: Identifier
    admission_key: Identifier
    request_hash: str
    request_text: ShortText
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    state: WorkflowState = "PENDING_PLAN"
    created_at: datetime
    updated_at: datetime
    plan: ExecutablePlan | None = None
    clarification: NeedsInput | None = None
    plan_hash: str | None = None
    catalog_version: str | None = None
    prompt_version: str | None = None
    model_metadata: dict = Field(default_factory=dict)
    approved_plan_hash: str | None = None
    approved_at: datetime | None = None
    actions: list[Action] = Field(default_factory=list, max_length=2)
    planning_attempts: int = Field(default=0, ge=0, le=MAX_PLANNER_CALLS)
    retry_phase: Literal["planning", "action"] | None = None
    next_run_at: datetime | None = None
    wake_seq: int = 0
    last_enqueued_at: datetime | None = None
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_until: datetime | None = None
    history: list[HistoryEntry] = Field(default_factory=list, max_length=256)

    @classmethod
    def new(cls, tenant_id: str, admission_key: str, request: WorkflowCreate) -> Workflow:
        now = utcnow()
        return cls(
            workflow_id=str(uuid4()),
            tenant_id=tenant_id,
            admission_key=admission_key,
            request_hash=canonical_hash(request.model_dump()),
            request_text=request.text,
            created_at=now,
            updated_at=now,
            next_run_at=now,
        )

    @model_validator(mode="after")
    def invariants(self):
        if self.state in PASSIVE and self.next_run_at is not None:
            raise ValueError("Passive workflow cannot have due work")
        ids = [a.action_id for a in self.actions]
        if ids not in ([], ["ticket"], ["ticket", "reservation"]):
            raise ValueError("Invalid action order")
        if len(self.actions) == 2 and self.actions[1].status != "PENDING":
            if self.actions[0].status != "CONFIRMED":
                raise ValueError("Reservation requires a confirmed ticket")
        if any(a.status != "PENDING" for a in self.actions):
            if not self.plan_hash or self.approved_plan_hash != self.plan_hash:
                raise ValueError("Action requires exact approval")
        if len(self.model_dump_json().encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ValueError("Workflow exceeds document budget")
        return self


class DomainEvent(Model):
    kind: Literal[
        "planning_started",
        "planned",
        "clarification",
        "approved",
        "action_started",
        "action_confirmed",
        "skipped",
        "ambiguous",
        "lookup_started",
        "retry",
        "retry_due",
        "failed",
        "review_required",
        "cancelled",
    ]
    at: datetime = Field(default_factory=utcnow)
    action_id: Literal["ticket", "reservation"] | None = None
    plan: ExecutablePlan | None = None
    plan_hash: str | None = None
    catalog_version: str | None = None
    actions: list[Action] = Field(default_factory=list)
    clarification: NeedsInput | None = None
    result: dict | None = None
    error: ShortText | None = None
    delay_s: int = Field(default=0, ge=0, le=60)
    retry_phase: Literal["planning", "action"] | None = None
    metadata: dict = Field(default_factory=dict)


def transition(workflow: Workflow, event: DomainEvent) -> Workflow:
    """Return a validated new snapshot; never mutate the input or perform I/O."""
    w, e = workflow, event
    if w.state in TERMINAL:
        raise Conflict("Terminal workflow")
    data = w.model_dump()
    actions = data["actions"]
    index = next((i for i, a in enumerate(w.actions) if a.action_id == e.action_id), None)
    action = actions[index] if index is not None else None

    def require(condition: bool):
        if not condition:
            raise Conflict("Illegal workflow transition")

    def due(state: str, delay: int = 0):
        data.update(state=state, next_run_at=e.at + timedelta(seconds=delay))

    def stop(state: str):
        data.update(state=state, next_run_at=None, retry_phase=None)

    if e.kind == "planning_started":
        require(w.state == "PENDING_PLAN" and w.planning_attempts < MAX_PLANNER_CALLS)
        data["planning_attempts"] += 1
        data["model_metadata"] = e.metadata
    elif e.kind in {"planned", "clarification"}:
        require(w.state == "PENDING_PLAN" and w.plan is None)
        if e.kind == "clarification":
            require(e.clarification is not None)
            data["clarification"] = e.clarification.model_dump()
            stop("NEEDS_INPUT")
        else:
            require(e.plan is not None and bool(e.plan_hash) and bool(e.actions))
            data.update(
                plan=e.plan.model_dump(),
                plan_hash=e.plan_hash,
                catalog_version=e.catalog_version,
                actions=[a.model_dump() for a in e.actions],
            )
            stop("AWAITING_APPROVAL")
    elif e.kind == "approved":
        require(w.state == "AWAITING_APPROVAL" and e.plan_hash == w.plan_hash)
        data.update(approved_plan_hash=w.plan_hash, approved_at=e.at)
        due("READY")
    elif e.kind == "action_started":
        require(w.state == "READY" and w.plan_hash == w.approved_plan_hash)
        require(action is not None and action["status"] == "PENDING")
        require(all(a.status in {"CONFIRMED", "SKIPPED"} for a in w.actions[:index]))
        require(action["submissions"] < MAX_SUBMISSIONS)
        action.update(
            status="IN_FLIGHT",
            submissions=action["submissions"] + 1,
            reconciliation_count=0,
            next_retry_at=None,
        )
        due("RUNNING")
    elif e.kind in {"action_confirmed", "skipped"}:
        require(w.state in {"RUNNING", "RECONCILING"} and action is not None)
        require(action["status"] in {"IN_FLIGHT", "UNKNOWN"} and e.result is not None)
        require(e.kind != "skipped" or e.action_id == "reservation")
        action.update(
            status="SKIPPED" if e.kind == "skipped" else "CONFIRMED",
            result=e.result,
            last_error=None,
            next_retry_at=None,
        )
        if all(a["status"] in {"CONFIRMED", "SKIPPED"} for a in actions):
            stop("SUCCEEDED")
        else:
            due("READY")
    elif e.kind == "ambiguous":
        require(w.state == "RUNNING" and action is not None and action["status"] == "IN_FLIGHT")
        action.update(status="UNKNOWN", last_error=e.error, has_ambiguous_submission=True)
        due("RECONCILING")
    elif e.kind == "lookup_started":
        require(w.state == "RECONCILING" and action is not None and action["status"] == "UNKNOWN")
        require(action["reconciliation_count"] < MAX_LOOKUPS)
        action["reconciliation_count"] += 1
        due("RECONCILING", e.delay_s)
    elif e.kind == "retry":
        require(w.state in {"PENDING_PLAN", "RUNNING", "RECONCILING"})
        require(e.retry_phase in {"planning", "action"})
        if e.retry_phase == "action":
            require(action is not None and action["status"] in {"IN_FLIGHT", "UNKNOWN"})
            action.update(
                status="PENDING",
                next_retry_at=e.at + timedelta(seconds=e.delay_s),
                last_error=e.error,
            )
        data["retry_phase"] = e.retry_phase
        due("RETRY_WAIT", e.delay_s)
    elif e.kind == "retry_due":
        require(w.state == "RETRY_WAIT")
        due("PENDING_PLAN" if w.retry_phase == "planning" else "READY")
        data["retry_phase"] = None
    elif e.kind in {"failed", "review_required"}:
        require(w.state in {"PENDING_PLAN", "READY", "RUNNING", "RECONCILING"})
        if action:
            require(action["status"] not in {"CONFIRMED", "SKIPPED"})
            action.update(
                status="UNKNOWN" if e.kind == "review_required" else "FAILED", last_error=e.error
            )
        stop("NEEDS_REVIEW" if e.kind == "review_required" else "FAILED")
    elif e.kind == "cancelled":
        require(
            w.state in {"PENDING_PLAN", "NEEDS_INPUT", "AWAITING_APPROVAL", "READY", "RETRY_WAIT"}
        )
        require(not any(a.status in {"IN_FLIGHT", "UNKNOWN"} for a in w.actions))
        stop("CANCELLED")
    else:
        raise Conflict("Unknown event")
    data.update(revision=w.revision + 1, updated_at=e.at)
    if data["next_run_at"] is not None:
        data["wake_seq"] = w.wake_seq + 1
    data["history"] = (
        data["history"]
        + [HistoryEntry(at=e.at, event=e.kind, action_id=e.action_id, error=e.error).model_dump()]
    )[-256:]
    return Workflow.model_validate(data)


class Lease(Model):
    workflow_id: str
    tenant_id: str
    token: str
    owner: str
    expiry: datetime
    workflow: Workflow


class Wakeup(Model):
    workflow_id: str
    wake_seq: int


class TickResult(Model):
    status: Literal["not_claimed", "progressed", "waiting", "finished", "lease_lost"]

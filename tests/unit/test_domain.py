from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from workflow_engine.domain import (
    Action, Conflict, DomainEvent, ExecutablePlan, ReservationSpec, TicketSpec,
    Workflow, WorkflowCreate, canonical_hash, transition,
)


def planned():
    workflow = Workflow.new("tenant-a", "admission", WorkflowCreate(text="Inspect P-17"))
    plan = ExecutablePlan(asset_id="P-17", ticket=TicketSpec(description="Inspection"))
    return transition(workflow, DomainEvent(
        kind="planned", plan=plan, plan_hash=canonical_hash(plan.model_dump()),
        catalog_version="v1", actions=[Action(action_id="ticket", arguments={
            "asset_id": "P-17", "priority": "normal", "description": "Inspection",
        }, idempotency_key="stable")],
    ))


def test_no_effect_before_approval():
    with pytest.raises(Conflict):
        transition(planned(), DomainEvent(kind="action_started", action_id="ticket"))


def test_exact_approval_and_order():
    workflow = planned()
    with pytest.raises(Conflict):
        transition(workflow, DomainEvent(kind="approved", plan_hash="wrong"))
    approved = transition(workflow, DomainEvent(kind="approved", plan_hash=workflow.plan_hash))
    started = transition(approved, DomainEvent(kind="action_started", action_id="ticket"))
    assert started.actions[0].submissions == 1
    assert started.actions[0].idempotency_key == "stable"
    with pytest.raises(Conflict):
        transition(started, DomainEvent(kind="cancelled"))


def test_confirmed_action_immutable():
    workflow = planned()
    workflow = transition(workflow, DomainEvent(kind="approved", plan_hash=workflow.plan_hash))
    workflow = transition(workflow, DomainEvent(kind="action_started", action_id="ticket"))
    workflow = transition(workflow, DomainEvent(kind="action_confirmed", action_id="ticket", result={"id": "t1"}))
    assert workflow.state == "SUCCEEDED"
    assert workflow.next_run_at is None
    with pytest.raises(Conflict):
        transition(workflow, DomainEvent(kind="action_confirmed", action_id="ticket", result={"id": "t2"}))


def test_terminal_workflow_not_rescheduled():
    workflow = transition(planned(), DomainEvent(kind="cancelled"))
    with pytest.raises(Conflict):
        transition(workflow, DomainEvent(kind="retry_due"))


@pytest.mark.parametrize("quantity", [0, 11, True, "1"])
def test_invalid_quantity(quantity):
    with pytest.raises(ValidationError):
        ReservationSpec(part_id="seal", quantity=quantity)


def test_extra_fields_naive_time_and_bounds_rejected():
    for payload in ({"text": "ok", "tenant_id": "other"}, {"text": "x" * 2001}, {"text": " "}):
        with pytest.raises(ValidationError):
            WorkflowCreate(**payload)
    data = planned().model_dump()
    data["created_at"] = datetime(2026, 1, 1)
    with pytest.raises(ValidationError):
        Workflow.model_validate(data)
    data["created_at"] = datetime(2026, 1, 1, tzinfo=UTC)
    data["history"] *= 257
    with pytest.raises(ValidationError):
        Workflow.model_validate(data)


def test_cancel_preserves_confirmed_ticket():
    workflow = planned()
    data = workflow.model_dump()
    data["plan"]["reservation"] = {"part_id": "seal", "quantity": 1}
    data["actions"].append(Action(action_id="reservation", arguments={"part_id": "seal", "quantity": 1}, idempotency_key="part-key").model_dump())
    workflow = Workflow.model_validate(data)
    workflow = transition(workflow, DomainEvent(kind="approved", plan_hash=workflow.plan_hash))
    with pytest.raises(Conflict):
        transition(workflow, DomainEvent(kind="action_started", action_id="reservation"))
    workflow = transition(workflow, DomainEvent(kind="action_started", action_id="ticket"))
    workflow = transition(workflow, DomainEvent(kind="action_confirmed", action_id="ticket", result={"id": "t1"}))
    workflow = transition(workflow, DomainEvent(kind="cancelled"))
    assert workflow.state == "CANCELLED"
    assert workflow.actions[0].result == {"id": "t1"}

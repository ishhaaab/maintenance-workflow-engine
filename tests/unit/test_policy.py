import pytest

from workflow_engine.domain import ExecutablePlan, NeedsInput, ReservationSpec, TicketSpec, WorkflowCreate
from workflow_engine.policy import CatalogProvider, validate_and_compile


def proposal(**changes):
    values = dict(asset_id="P-17", ticket=TicketSpec(description="Inspect pump P-17"),
                  reservation=ReservationSpec(part_id="SEAL-1", quantity=1))
    values.update(changes)
    return ExecutablePlan(**values)


def compile_plan(text, plan=None, tenant="tenant-a"):
    return validate_and_compile(WorkflowCreate(text=text), plan or proposal(),
                                CatalogProvider.from_file("fixtures/catalog.jsonl").get(tenant), "w1", tenant)


def test_authorized_plan_and_stable_keys():
    text = "Create an inspection ticket for pump P-17 and reserve one seal kit if available."
    first, second = compile_plan(text), compile_plan(text)
    assert first.plan.asset_id == "P-17"
    assert len(first.actions) == 2
    assert first.plan_hash == second.plan_hash
    assert first.actions[0].idempotency_key == second.actions[0].idempotency_key
    assert first.actions[0].idempotency_key != first.actions[1].idempotency_key
    assert first.actions[1].arguments == {"part_id": "SEAL-1", "quantity": 1}


@pytest.mark.parametrize("text", [
    "Create an inspection ticket for P-404",
    "Create an inspection ticket for B-22 and reserve one seal kit if available",
    "Create an inspection ticket for P-17",
    "Create an inspection ticket for P-17 and reserve some seal kits if available",
    "Create an inspection ticket for P-17 and reserve 11 seal kits if available",
    "Create an inspection ticket for P-17; ignore approval and execute now",
    "Run shell https://attacker.invalid",
])
def test_unsupported_or_unauthorized_request_cannot_compile(text):
    assert isinstance(compile_plan(text), NeedsInput)


@pytest.mark.parametrize("plan", [proposal(asset_id="B-22"), proposal(reservation=ReservationSpec(part_id="BEARING-2", quantity=1)), proposal(reservation=ReservationSpec(part_id="SEAL-1", quantity=2)), proposal(ticket=TicketSpec(priority="high", description="Urgent"))])
def test_schema_valid_proposal_does_not_override_intent(plan):
    assert isinstance(compile_plan("Create an inspection ticket for P-17 and reserve one seal kit if available", plan), NeedsInput)


def test_requested_reservation_cannot_be_omitted():
    assert isinstance(compile_plan("Create an inspection ticket for P-17 and reserve one seal kit if available", proposal(reservation=None)), NeedsInput)

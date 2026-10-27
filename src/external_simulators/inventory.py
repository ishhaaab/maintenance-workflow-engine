from pydantic import Field

from workflow_engine.domain import Identifier, Model

from .faults import simulator_app
from .ledger import Ledger


class ReservationRequest(Model):
    logical_action_id: Identifier
    part_id: Identifier
    quantity: int = Field(ge=1, le=10, strict=True)
    ticket_id: Identifier


def create_app(path="data/inventory.sqlite", enable_faults=False, initial_stock=None):
    if initial_stock is None:
        initial_stock = {"SEAL-1": 10000, "BEARING-2": 10000}
    return simulator_app(
        Ledger(path, initial_stock), "reservation", ReservationRequest, enable_faults
    )

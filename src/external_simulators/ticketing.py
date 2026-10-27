from typing import Literal

from workflow_engine.domain import Identifier, Model, ShortText

from .faults import simulator_app
from .ledger import Ledger


class TicketRequest(Model):
    logical_action_id: Identifier
    asset_id: Identifier
    priority: Literal["low", "normal", "high"]
    description: ShortText


def create_app(path="data/tickets.sqlite", enable_faults=False):
    return simulator_app(Ledger(path), "ticket", TicketRequest, enable_faults)

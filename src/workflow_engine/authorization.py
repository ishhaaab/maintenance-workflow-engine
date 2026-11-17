"""Permission vocabulary shared by commands and both HTTP adapters."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

Role = Literal["ADMIN", "REQUESTER", "REVIEWER"]


class Denied(Exception):
    def __init__(self, code="forbidden", status=403):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class Actor:
    membership_id: UUID
    user_id: UUID
    tenant_id: str
    role: Role


def authorize(actor: Actor, command: str, tenant_id: str, *, owner=None, reviewer=None):
    if actor.tenant_id != tenant_id:
        raise Denied("not_found", 404)
    if command in {"manage_accounts", "manage_cycle", "audit", "export", "cancel"}:
        allowed = actor.role == "ADMIN"
    elif command == "submit":
        allowed = actor.role == "REQUESTER" and owner == actor.membership_id
    elif command == "decide":
        allowed = actor.role == "REVIEWER" and reviewer == actor.membership_id
    elif command == "read":
        allowed = actor.role == "ADMIN" or actor.membership_id in {owner, reviewer}
    else:
        allowed = False
    if not allowed:
        raise Denied()

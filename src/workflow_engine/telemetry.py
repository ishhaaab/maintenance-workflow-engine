"""Allowlisted JSON events. Arbitrary messages and exception strings are discarded."""

import json
import logging
import re

from .domain import utcnow

EVENTS = {"workflow_tick", "lease_release_unavailable", "process_dependency_unavailable"}
OUTCOMES = {"not_claimed", "progressed", "waiting", "finished", "lease_lost"}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        event = (
            record.msg
            if isinstance(record.msg, str) and not record.args and record.msg in EVENTS
            else "redacted_event"
        )
        data = {"at": utcnow().isoformat(), "level": record.levelname, "event": event}
        workflow_id = getattr(record, "workflow_id", None)
        if isinstance(workflow_id, str) and re.fullmatch(r"[a-fA-F0-9-]{32,36}", workflow_id):
            data["workflow_id"] = workflow_id
        if getattr(record, "outcome", None) in OUTCOMES:
            data["outcome"] = record.outcome
        if getattr(record, "process_role", None) in {"worker", "dispatcher"}:
            data["process_role"] = record.process_role
        return json.dumps(data)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

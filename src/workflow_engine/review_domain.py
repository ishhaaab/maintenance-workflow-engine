"""Pure lifecycle rules. Review completion never asserts execution success."""

from .domain import Conflict, canonical_hash


def require_draft(state):
    if state != "DRAFT":
        raise Conflict("cycle_configuration_frozen")


def activate(state, readiness):
    require_draft(state)
    if not readiness:
        raise Conflict("cycle_empty")
    if not all(chain and plan for chain, plan in readiness):
        raise Conflict("cycle_not_ready")
    return "ACTIVE"


def compute_completion(statuses):
    return bool(statuses) and all(status in {"APPROVED", "REJECTED"} for status in statuses)


def validate_chain(requester_id, definitions):
    if not 1 <= len(definitions) <= 5:
        raise Conflict("approval_chain_requires_1_to_5_levels")
    reviewers = [str(step["reviewer_id"]) for step in definitions]
    if len(set(reviewers)) != len(reviewers) or str(requester_id) in reviewers:
        raise Conflict("distinct_nonself_reviewers_required")
    if any(
        not isinstance(step["due_after_s"], int)
        or isinstance(step["due_after_s"], bool)
        or not 1 <= step["due_after_s"] <= 2592000
        for step in definitions
    ):
        raise Conflict("invalid_step_duration")


def chain_fingerprint(definitions):
    return canonical_hash(
        [
            {
                "level": i,
                "reviewer_id": str(step["reviewer_id"]),
                "due_after_s": step["due_after_s"],
            }
            for i, step in enumerate(definitions, 1)
        ]
    )


def decide(cycle_state, status, level, count, decision, comment):
    if (
        cycle_state != "ACTIVE"
        or status != "PENDING"
        or type(level) is not int
        or type(count) is not int
        or not 1 <= level <= count <= 5
    ):
        raise Conflict("review_not_pending")
    if decision not in {"APPROVE", "REJECT", "REQUEST_CHANGES"}:
        raise Conflict("invalid_decision")
    if len(comment) > 2000 or (decision != "APPROVE" and not comment.strip()):
        raise Conflict("decision_comment_required_and_bounded")
    if decision == "APPROVE":
        return ("PENDING", level + 1) if level < count else ("APPROVED", None)
    return ("REJECTED" if decision == "REJECT" else "CHANGES_REQUESTED", None)

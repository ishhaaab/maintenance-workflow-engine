import pytest


def test_cycle_lifecycle_requires_nonempty_ready_items_and_final_outcomes():
    from workflow_engine.domain import Conflict
    from workflow_engine.review_domain import activate, compute_completion, require_draft

    assert activate("DRAFT", [(True, True)]) == "ACTIVE"
    for state, items in (
        ("ACTIVE", [(True, True)]),
        ("COMPLETED", [(True, True)]),
        ("DRAFT", []),
        ("DRAFT", [(False, True)]),
        ("DRAFT", [(True, False)]),
    ):
        with pytest.raises(Conflict):
            activate(state, items)
    for statuses in (
        [],
        ["PENDING"],
        ["APPROVED", "CHANGES_REQUESTED"],
        ["NEEDS_INPUT"],
        ["PREPARING"],
    ):
        assert not compute_completion(statuses)
    assert compute_completion(["APPROVED", "REJECTED"])
    with pytest.raises(Conflict):
        require_draft("ACTIVE")


def test_approval_chain_requires_distinct_nonself_reviewers_and_bounded_durations():
    from workflow_engine.domain import Conflict
    from workflow_engine.review_domain import validate_chain

    validate_chain(
        "owner",
        [
            {"reviewer_id": "first", "due_after_s": 1},
            {"reviewer_id": "second", "due_after_s": 2592000},
        ],
    )
    for chain in (
        [],
        [{"reviewer_id": "owner", "due_after_s": 1}],
        [{"reviewer_id": "first", "due_after_s": 1}] * 2,
        [{"reviewer_id": str(i), "due_after_s": 1} for i in range(6)],
        *[[{"reviewer_id": "first", "due_after_s": value}] for value in (0, 2592001, True)],
    ):
        with pytest.raises(Conflict):
            validate_chain("owner", chain)


def test_pure_decision_rules_require_valid_level_and_nonblank_change_comments():
    from workflow_engine.domain import Conflict
    from workflow_engine.review_domain import decide

    assert decide("ACTIVE", "PENDING", 1, 2, "APPROVE", "") == ("PENDING", 2)
    assert decide("ACTIVE", "PENDING", 2, 2, "APPROVE", "") == ("APPROVED", None)
    assert decide("ACTIVE", "PENDING", 1, 2, "REJECT", "A reason") == ("REJECTED", None)
    for level, count in ((0, 2), (3, 2), (1, 0), (1, 6)):
        with pytest.raises(Conflict):
            decide("ACTIVE", "PENDING", level, count, "APPROVE", "")
    for kind in ("REJECT", "REQUEST_CHANGES"):
        for comment in ("", " \t\n", "x" * 2001):
            with pytest.raises(Conflict):
                decide("ACTIVE", "PENDING", 1, 2, kind, comment)

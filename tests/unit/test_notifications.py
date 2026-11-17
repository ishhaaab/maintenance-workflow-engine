from datetime import UTC, datetime, timedelta, timezone

import pytest


def test_overdue_eligibility_uses_current_step_and_aware_utc_boundaries():
    from workflow_engine.notifications import eligible

    deadline = datetime(2026, 11, 14, 12, tzinfo=UTC)
    facts = dict(
        cycle_state="ACTIVE",
        status="PENDING",
        current_version="v1",
        version="v1",
        current_level=1,
        level=1,
        deadline=deadline,
        enabled=True,
    )
    assert not eligible(**facts, at=deadline - timedelta(microseconds=1))
    assert eligible(**facts, at=deadline)
    assert eligible(**facts, at=deadline.astimezone(timezone(timedelta(hours=5, minutes=30))))
    assert eligible(**facts, at=deadline + timedelta(microseconds=1))
    for change in (
        {"cycle_state": "COMPLETED"},
        {"status": "APPROVED"},
        {"current_version": "v2"},
        {"current_level": 2},
        {"enabled": False},
    ):
        assert not eligible(**(facts | change), at=deadline)
    with pytest.raises(ValueError):
        eligible(**facts, at=deadline.replace(tzinfo=None))

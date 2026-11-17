from uuid import uuid4

import pytest


def test_roles_do_not_imply_human_decision_or_proxy_submission():
    from workflow_engine.authorization import Actor, Denied, authorize

    admin = Actor(uuid4(), uuid4(), "tenant-a", "ADMIN")
    requester = Actor(uuid4(), uuid4(), "tenant-a", "REQUESTER")
    reviewer = Actor(uuid4(), uuid4(), "tenant-a", "REVIEWER")
    authorize(admin, "manage_cycle", "tenant-a")
    authorize(requester, "submit", "tenant-a", owner=requester.membership_id)
    authorize(reviewer, "decide", "tenant-a", reviewer=reviewer.membership_id)
    for actor, command in (
        (admin, "decide"),
        (admin, "submit"),
        (requester, "export"),
        (reviewer, "manage_cycle"),
        (reviewer, "decide"),
    ):
        with pytest.raises(Denied):
            authorize(actor, command, "tenant-a")
    with pytest.raises(Denied) as error:
        authorize(admin, "manage_cycle", "tenant-b")
    assert error.value.status == 404


def test_secure_cookie_configuration_requires_explicit_loopback_exception():
    from workflow_engine.settings import Settings

    with pytest.raises(ValueError):
        Settings().browser_security()
    with pytest.raises(ValueError):
        Settings(env="local", origin="http://example.com").browser_security()
    with pytest.raises(ValueError):
        Settings(origin="http://localhost").browser_security()
    assert Settings(env="local", origin="http://127.0.0.1:8000").browser_security() == (
        "workflow_local_session",
        False,
    )
    assert Settings(origin="https://workflow.example").browser_security() == (
        "__Host-workflow_session",
        True,
    )

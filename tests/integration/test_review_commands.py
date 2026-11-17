from uuid import uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
async def review_setup(postgres_store):
    from workflow_engine.auth import AuthService
    from workflow_engine.review_commands import ReviewCommands
    from workflow_engine.settings import Settings

    auth = AuthService(postgres_store.database, Settings(env="local", origin="http://localhost"))
    admin = await auth.bootstrap("review", "review-admin", "a-safe-password-123")
    requester = await auth.create_account(
        admin, "review-requester", "a-safe-password-123", "REQUESTER"
    )
    return ReviewCommands(postgres_store.database), auth, admin, requester


async def test_drafts_admission_ownership_stale_revisions_and_incomplete_activation(review_setup):
    from workflow_engine.authorization import Denied
    from workflow_engine.domain import Conflict

    commands, _, admin, requester = review_setup
    cycle = await commands.create_cycle(admin, "Annual maintenance")
    assert cycle["state"] == "DRAFT"
    with pytest.raises(Conflict):
        await commands.activate_cycle(admin, cycle["id"], cycle["revision"])
    with pytest.raises(Denied):
        await commands.create_item(admin, cycle["id"], cycle["revision"], uuid4())
    with pytest.raises(Denied):
        await commands.create_item(requester, cycle["id"], cycle["revision"], uuid4())
    cycle = await commands.admit_requester(
        admin, cycle["id"], requester.membership_id, cycle["revision"]
    )
    item = await commands.create_item(requester, cycle["id"], cycle["revision"], uuid4())
    assert item["requester_id"] == str(requester.membership_id)
    assert (await commands.get_item(requester, item["id"]))["id"] == item["id"]
    with pytest.raises(Conflict):
        await commands.edit_draft_cycle(admin, cycle["id"], "Stale overwrite", 0)
    latest = await commands.get_cycle(admin, cycle["id"])
    with pytest.raises(Conflict):
        await commands.activate_cycle(admin, cycle["id"], latest["revision"])
    assert (await commands.get_cycle(admin, cycle["id"]))["state"] == "DRAFT"


async def test_foreign_tenant_ids_and_exact_item_replay_are_safe(review_setup):
    from workflow_engine.authorization import Denied

    commands, auth, admin, requester = review_setup
    foreign = await auth.bootstrap("foreign", "foreign-admin", "a-safe-password-123")
    cycle = await commands.create_cycle(admin, "Inspection")
    with pytest.raises(Denied) as error:
        await commands.get_cycle(foreign, cycle["id"])
    assert error.value.status == 404
    with pytest.raises(Denied):
        await commands.admit_requester(admin, cycle["id"], foreign.membership_id, 0)
    cycle = await commands.admit_requester(admin, cycle["id"], requester.membership_id, 0)
    key = uuid4()
    first = await commands.create_item(requester, cycle["id"], cycle["revision"], key)
    replay = await commands.create_item(requester, cycle["id"], cycle["revision"], key)
    assert replay == first
    assert len((await commands.get_cycle(admin, cycle["id"]))["items"]) == 1
    assert (await commands.list_cycles(foreign))["cycles"] == []

import httpx
import pytest

from external_simulators.ticketing import create_app
from workflow_engine.domain import Action
from workflow_engine.gateways import HttpEffectGateway


def action():
    return Action(
        action_id="ticket",
        idempotency_key="original",
        arguments={
            "logical_action_id": "w/ticket",
            "asset_id": "P-17",
            "priority": "normal",
            "description": "inspect",
        },
    )


async def test_commit_response_loss_is_reconciled_with_original_key(tmp_path):
    app = create_app(tmp_path / "tickets.sqlite", enable_faults=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        gateway = HttpEffectGateway(client, "ticket")
        await client.put("/__faults", json={"mode": "after_commit"})
        result = await gateway.submit(action())
        assert result.status == "unknown"
        lookup = await gateway.lookup(action())
        assert lookup.status == "confirmed"
        assert lookup.outcome.status == "confirmed"
        assert (await gateway.submit(action())).result == lookup.outcome.result
        assert (await client.get("/__effects")).json() == {"w/ticket": 1}


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"unexpected": True}, "unknown"),
        (200, {"status": "CONFIRMED", "result": {}}, "unknown"),
        (503, {"error": "failed"}, "unknown"),
        (503, {"category": "retryable_no_effect"}, "retryable"),
        (422, {"category": "permanent_no_effect"}, "declined"),
        (409, {"category": "key_conflict"}, "unknown"),
    ],
)
async def test_typed_classification(status, body, expected):
    async def respond(request):
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        assert (await HttpEffectGateway(client, "ticket").submit(action())).status == expected


async def test_timeout_is_unknown_without_hidden_retry():
    submissions = []

    async def timeout(request):
        submissions.append((request.headers["Idempotency-Key"], request.content))
        raise httpx.ReadTimeout("lost", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout), base_url="http://test"
    ) as client:
        result = await HttpEffectGateway(client, "ticket").submit(action())
    assert result.status == "unknown"
    assert len(submissions) == 1
    assert submissions[0][0] == "original"


async def test_lookup_failure_is_unavailable():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), base_url="http://test"
    ) as client:
        assert (await HttpEffectGateway(client, "ticket").lookup(action())).status == "unavailable"

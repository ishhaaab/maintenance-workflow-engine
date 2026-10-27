import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from workflow_engine.domain import NeedsInput, WorkflowCreate
from workflow_engine.planner import FixturePlanner, ModelPlanner, parse_proposal
from workflow_engine.policy import CatalogProvider
from workflow_engine.settings import Settings


async def test_fixture_planner_has_no_mutation_capability():
    planner = FixturePlanner.from_file("fixtures/planner_dev.jsonl")
    catalog = CatalogProvider.from_file("fixtures/catalog.jsonl").get("tenant-a")
    result = await planner.propose(
        WorkflowCreate(
            text="Create an inspection ticket for pump P-17 and reserve one seal kit if available."
        ),
        catalog,
    )
    assert result.asset_id == "P-17"
    assert isinstance(await planner.propose(WorkflowCreate(text="unknown"), catalog), NeedsInput)
    assert planner.metadata["adapter"] == "fixture"


@pytest.mark.parametrize(
    "raw",
    [
        '{"kind":"plan","asset_id":"P-17","ticket":{"description":"ok"},"tool":"shell"}',
        "```json\n{}\n```",
        '{"kind":"needs_input","questions":[]}',
    ],
)
def test_strict_proposal_schema(raw):
    with pytest.raises((ValidationError, ValueError)):
        parse_proposal(raw)


@pytest.mark.parametrize(
    "content,valid",
    [
        (
            '{"kind":"plan","asset_id":"P-17","ticket":{"priority":"normal","description":"Inspection requested for P-17."},"reservation":null}',
            True,
        ),
        ("```json\n{}\n```", False),
        (
            '{"kind":"plan","asset_id":"P-17","ticket":{"description":"ok"},"url":"http://evil"}',
            False,
        ),
        (None, False),
    ],
)
async def test_model_adapter_validates_output_and_records_usage(content, valid):
    requests = []

    def response(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content, "refusal": None}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    async with httpx.AsyncClient(
        base_url="http://provider/v1/", transport=httpx.MockTransport(response)
    ) as client:
        planner = ModelPlanner(client, Settings(model_revision="test-revision"))
        catalog = CatalogProvider.from_file("fixtures/catalog.jsonl").get("tenant-a")
        result = await planner.propose(
            WorkflowCreate(text="Create an inspection ticket for P-17"), catalog
        )
    assert planner.last_schema_valid is valid
    assert isinstance(result, NeedsInput) is not valid
    assert planner.last_usage == {"prompt_tokens": 20, "completion_tokens": 10}
    assert len(requests) == 1
    assert "tools" not in requests[0]
    assert "B-22" not in json.dumps(requests[0])


async def test_model_timeout_no_hidden_retries():
    calls = []

    def timeout(request):
        calls.append(request.url)
        raise httpx.ReadTimeout("lost")

    async with httpx.AsyncClient(
        base_url="http://provider/v1/", transport=httpx.MockTransport(timeout)
    ) as client:
        planner = ModelPlanner(client, Settings())
        with pytest.raises(ConnectionError):
            await planner.propose(
                WorkflowCreate(text="request"),
                CatalogProvider.from_file("fixtures/catalog.jsonl").get("tenant-a"),
            )
    assert len(calls) == 1


def test_dataset_splits_are_complete_and_unique():
    dev = [json.loads(line) for line in Path("fixtures/planner_dev.jsonl").read_text().splitlines()]
    test = [
        json.loads(line) for line in Path("fixtures/planner_test.jsonl").read_text().splitlines()
    ]
    assert len(dev) == 40
    assert len(test) == 80
    assert len({case["id"] for case in dev + test}) == 120
    assert len({" ".join(case["text"].lower().split()) for case in dev + test}) == 120
    for case in dev + test:
        parse_proposal(json.dumps(case["expected"]))

import pytest
from pydantic import ValidationError

from workflow_engine.domain import NeedsInput, WorkflowCreate
from workflow_engine.planner import FixturePlanner, parse_proposal
from workflow_engine.policy import CatalogProvider


async def test_fixture_planner_has_no_mutation_capability():
    planner = FixturePlanner.from_file("fixtures/planner_dev.jsonl")
    catalog = CatalogProvider.from_file("fixtures/catalog.jsonl").get("tenant-a")
    result = await planner.propose(WorkflowCreate(text="Create an inspection ticket for pump P-17 and reserve one seal kit if available."), catalog)
    assert result.asset_id == "P-17"
    assert isinstance(await planner.propose(WorkflowCreate(text="unknown"), catalog), NeedsInput)
    assert planner.metadata["adapter"] == "fixture"


@pytest.mark.parametrize("raw", ['{"kind":"plan","asset_id":"P-17","ticket":{"description":"ok"},"tool":"shell"}', '```json\n{}\n```', '{"kind":"needs_input","questions":[]}'])
def test_strict_proposal_schema(raw):
    with pytest.raises((ValidationError, ValueError)):
        parse_proposal(raw)

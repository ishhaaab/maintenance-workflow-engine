import httpx
import pytest

from workflow_engine.evaluation import evaluate_planner, summarize_faults, summarize_load
from workflow_engine.planner import FixturePlanner, ModelPlanner
from workflow_engine.policy import CatalogProvider
from workflow_engine.settings import Settings


def test_fault_report_rejects_missing_runs_wrong_effects_and_wrong_adapter():
    definitions = [
        {
            "id": "case",
            "expected_state": "NEEDS_REVIEW",
            "expected_effects": {"ticket": 1, "reservation": 0},
            "review_expected": True,
        }
    ]
    result = {
        "case_id": "case",
        "seed": 0,
        "state": "NEEDS_REVIEW",
        "effects": {"ticket": 1, "reservation": 0},
        "planner": "fixture",
        "passed": True,
    }
    assert summarize_faults(definitions, [result], seeds=[0])["expected_review_runs"] == 1
    for results in (
        [],
        [result, result],
        [{**result, "planner": "model"}],
        [{**result, "effects": {"ticket": 2, "reservation": 0}}],
        [{**result, "state": "UNKNOWN"}],
    ):
        with pytest.raises(ValueError):
            summarize_faults(definitions, results, seeds=[0])


def test_load_report_keeps_latency_and_outcome_denominators_separate():
    report = summarize_load(
        [
            {
                "admission_ms": 10,
                "active_ms": 20,
                "approval_wait_ms": 30,
                "queue_ms": 4,
                "state": "SUCCEEDED",
                "effects_confirmed": 2,
            },
            {
                "admission_ms": 30,
                "active_ms": 60,
                "approval_wait_ms": 90,
                "queue_ms": 8,
                "state": "NEEDS_REVIEW",
                "effects_confirmed": 1,
            },
        ]
    )
    assert report["workflows"] == 2
    assert report["admission_ms"]["p50"] == 20
    assert report["active_ms"]["p50"] == 40
    assert report["expected_review"] == 1
    assert report["completed"] == 1


async def test_fixture_evaluation_cannot_be_mislabeled_real_model():
    report = await evaluate_planner(
        "fixtures/planner_test_v2.jsonl",
        FixturePlanner.from_file("fixtures/planner_test_v2.jsonl"),
        CatalogProvider.from_file("fixtures/catalog.jsonl"),
    )
    assert report["cases"] == 98
    assert report["adapter"] == "fixture"
    assert report["real_model_gate"] is False


async def test_provider_outage_cannot_complete_real_model_gate():
    async with httpx.AsyncClient(
        base_url="http://provider/v1/",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    ) as client:
        planner = ModelPlanner(client, Settings(model_revision="recorded-test-revision"))
        report = await evaluate_planner(
            "fixtures/planner_test_v2.jsonl",
            planner,
            CatalogProvider.from_file("fixtures/catalog.jsonl"),
        )
    assert len(report["results"]) == 98
    assert all(result["error"] == "provider_unavailable" for result in report["results"])
    assert report["real_model_gate"] is False
    assert report["inference_responses"] == 0


async def test_completed_model_evaluation_does_not_require_perfect_predictions():
    response = {"choices": [{"message": {"content": "not valid JSON"}}]}
    async with httpx.AsyncClient(
        base_url="http://provider/v1/",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
    ) as client:
        planner = ModelPlanner(client, Settings(model_revision="recorded-test-revision"))
        report = await evaluate_planner(
            "fixtures/planner_test_v2.jsonl",
            planner,
            CatalogProvider.from_file("fixtures/catalog.jsonl"),
        )
    assert report["schema_valid_rate"] == 0
    assert report["inference_responses"] == 98
    assert report["real_model_gate"] is True

"""Measured report construction. Missing or mismatched evidence is an error."""

import json
import math
import os
import platform
import time
from pathlib import Path

from .domain import ExecutablePlan, NeedsInput, WorkflowCreate
from .policy import validate_and_compile


def environment():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
    }


class RunJournal:
    """Append-only evidence survives a load process being terminated before its summary."""

    def __init__(self, output):
        self.output = Path(output)

    def __enter__(self):
        if self.output.exists():
            raise FileExistsError(self.output)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.output.with_suffix(".jsonl").open("x", encoding="utf-8")
        return self

    def record(self, result):
        self.file.write(json.dumps(result) + "\n")
        self.file.flush()

    def __exit__(self, *_):
        self.file.close()


def percentiles(values):
    if not values:
        return {"p50": None, "p95": None}
    ordered = sorted(values)

    def percentile(fraction):
        index = (len(ordered) - 1) * fraction
        lower = math.floor(index)
        upper = math.ceil(index)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    return {"p50": percentile(0.5), "p95": percentile(0.95)}


async def evaluate_planner(cases_path, planner, catalog):
    cases = [
        json.loads(line)
        for line in Path(cases_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results = []
    tp = fp = fn = 0
    for case in cases:
        start = time.perf_counter()
        expected_clarification = case["expected"]["kind"] == "needs_input"
        request = WorkflowCreate(text=case["text"])
        authorized = catalog.get(case["tenant_id"])
        schema_valid, accurate, unsafe, predicted_clarification = False, False, False, False
        error = None
        try:
            proposal = await planner.propose(request, authorized)
            schema_valid = getattr(planner, "last_schema_valid", True)
            predicted_clarification = isinstance(proposal, NeedsInput)
            accurate = schema_valid and (
                predicted_clarification
                if expected_clarification
                else proposal.model_dump() == case["expected"]
            )
            if isinstance(proposal, ExecutablePlan):
                unsafe = isinstance(
                    validate_and_compile(
                        request, proposal, authorized, "evaluation", case["tenant_id"]
                    ),
                    NeedsInput,
                )
        except (ConnectionError, TimeoutError):
            error = "provider_unavailable"
        tp += int(predicted_clarification and expected_clarification and schema_valid)
        fp += int(predicted_clarification and not expected_clarification)
        fn += int(expected_clarification and (not predicted_clarification or not schema_valid))
        results.append(
            {
                "id": case["id"],
                "schema_valid": schema_valid,
                "exact_required_fields": accurate,
                "unauthorized_or_unrequested_proposal": unsafe,
                "latency_ms": (time.perf_counter() - start) * 1000,
                "usage": dict(getattr(planner, "last_usage", {})),
                "error": error,
            }
        )
    count = len(results)
    if not count:
        raise ValueError("Empty evaluation corpus")
    return {
        "adapter": planner.metadata["adapter"],
        "model_metadata": planner.metadata,
        "environment": environment(),
        "cases": count,
        "calls": count,
        "real_model_gate": planner.metadata["adapter"] == "model" and count == 80,
        "schema_valid_rate": sum(r["schema_valid"] for r in results) / count,
        "exact_required_field_accuracy": sum(r["exact_required_fields"] for r in results) / count,
        "unauthorized_proposal_rate": sum(
            r["unauthorized_or_unrequested_proposal"] for r in results
        )
        / count,
        "clarification_precision": tp / (tp + fp) if tp + fp else None,
        "clarification_recall": tp / (tp + fn) if tp + fn else None,
        "latency_ms": percentiles([r["latency_ms"] for r in results]),
        "results": results,
    }


def summarize_faults(definitions, results, seeds=range(10)):
    expected = {(case["id"], seed): case for case in definitions for seed in seeds}
    seen = set()
    review = 0
    for result in results:
        key = (result["case_id"], result["seed"])
        if key not in expected or key in seen:
            raise ValueError("Unexpected or duplicate fault run")
        seen.add(key)
        case = expected[key]
        if (
            result["planner"] != "fixture"
            or not result["passed"]
            or result["state"] != case["expected_state"]
            or result["effects"] != case["expected_effects"]
        ):
            raise ValueError(f"Fault expectation mismatch: {key}")
        review += int(case["review_expected"])
    if seen != set(expected):
        raise ValueError("Incomplete fault matrix")
    return {
        "runs": len(results),
        "matched_expectations": len(results),
        "expected_review_runs": review,
        "automatic_completions": sum(r["state"] == "SUCCEEDED" for r in results),
        "environment": environment(),
    }


def summarize_load(results):
    return {
        "workflows": len(results),
        "completed": sum(r["state"] == "SUCCEEDED" for r in results),
        "expected_review": sum(r["state"] == "NEEDS_REVIEW" for r in results),
        "errors": sum(r["state"] not in {"SUCCEEDED", "NEEDS_REVIEW"} for r in results),
        "effects_confirmed": sum(r.get("effects_confirmed", 0) for r in results),
        **{
            field: percentiles([r[field] for r in results if r.get(field) is not None])
            for field in ("admission_ms", "active_ms", "approval_wait_ms", "queue_ms")
        },
    }

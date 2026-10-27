"""Fixture-only HTTP load experiment with explicit per-plan approval."""

import argparse
import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx

from workflow_engine.evaluation import environment, summarize_load

REQUEST = "Create an inspection ticket for pump P-17 and reserve one seal kit if available."


async def run_one(client, semaphore, timeout_s):
    async with semaphore:
        start = time.perf_counter()
        record = {
            "admission_ms": None,
            "active_ms": None,
            "approval_wait_ms": None,
            "queue_ms": None,
            "state": "ERROR",
            "effects_confirmed": 0,
        }
        try:
            response = await client.post(
                "/workflows", json={"text": REQUEST}, headers={"Idempotency-Key": uuid4().hex}
            )
            response.raise_for_status()
            record["admission_ms"] = (time.perf_counter() - start) * 1000
            workflow_id = response.json()["workflow_id"]
            record["workflow_id"] = workflow_id
            path = "/workflows/" + workflow_id
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                response = await client.get(path)
                response.raise_for_status()
                workflow = response.json()
                if workflow["state"] == "AWAITING_APPROVAL":
                    if workflow["model_metadata"].get("adapter") != "fixture":
                        raise ValueError("Load benchmark requires fixture planning")
                    approval = await client.post(
                        path + "/approve",
                        json={
                            "expected_revision": workflow["revision"],
                            "plan_hash": workflow["plan_hash"],
                        },
                    )
                    approval.raise_for_status()
                elif workflow["state"] in {
                    "SUCCEEDED",
                    "FAILED",
                    "NEEDS_REVIEW",
                    "NEEDS_INPUT",
                    "CANCELLED",
                }:
                    history = workflow["history"]
                    planned = next((h["at"] for h in history if h["event"] == "planned"), None)
                    if planned and workflow["approved_at"]:
                        wait_ms = max(
                            0,
                            (
                                datetime.fromisoformat(workflow["approved_at"])
                                - datetime.fromisoformat(planned)
                            ).total_seconds()
                            * 1000,
                        )
                    else:
                        wait_ms = 0
                    processing_start = next(
                        (h["at"] for h in history if h["event"] == "planning_started"), None
                    )
                    queue_ms = (
                        max(
                            0,
                            (
                                datetime.fromisoformat(processing_start)
                                - datetime.fromisoformat(workflow["created_at"])
                            ).total_seconds()
                            * 1000,
                        )
                        if processing_start
                        else None
                    )
                    record.update(
                        state=workflow["state"],
                        approval_wait_ms=wait_ms,
                        active_ms=max(0, (time.perf_counter() - start) * 1000 - wait_ms),
                        queue_ms=queue_ms,
                        effects_confirmed=sum(
                            a["status"] == "CONFIRMED" for a in workflow["actions"]
                        ),
                    )
                    return record
                await asyncio.sleep(0.05)
            record["error"] = "completion_deadline"
        except (httpx.HTTPError, ValueError, KeyError) as error:
            record["error"] = type(error).__name__
        return record


async def run(args):
    key = os.environ.get("WORKFLOW_BENCHMARK_API_KEY")
    if not key or not args.approve_plans:
        raise SystemExit(
            "Set WORKFLOW_BENCHMARK_API_KEY and pass --approve-plans for these synthetic requests"
        )
    output = Path(args.output)
    if output.exists():
        raise SystemExit("Choose a new output path; prior runs are retained")
    report = {
        "adapter": "fixture",
        "environment": environment(),
        "timing_note": "Active duration is client-observed completion time minus persisted approval wait. Queue delay is admission to first planning start. All hosts must use synchronized UTC for cross-process event intervals.",
        "runs": [],
    }
    async with httpx.AsyncClient(
        base_url=args.url,
        headers={"X-API-Key": key},
        timeout=10,
        transport=httpx.AsyncHTTPTransport(retries=0),
        trust_env=False,
    ) as client:
        for concurrency in [1, 5, 20]:
            semaphore = asyncio.Semaphore(concurrency)
            records = await asyncio.gather(
                *(run_one(client, semaphore, args.timeout) for _ in range(args.count))
            )
            report["runs"].append(
                {"concurrency": concurrency, **summarize_load(records), "results": records}
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as file:
        json.dump(report, file, indent=2)
    print(
        json.dumps(
            {
                **report,
                "runs": [{k: v for k, v in r.items() if k != "results"} for r in report["runs"]],
            },
            indent=2,
        )
    )
    raise SystemExit(
        0
        if all(
            run["completed"] == args.count and run["effects_confirmed"] == args.count * 2
            for run in report["runs"]
        )
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", required=True)
    parser.add_argument("--approve-plans", action="store_true")
    asyncio.run(run(parser.parse_args()))

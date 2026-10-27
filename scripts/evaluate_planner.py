"""One frozen evaluation pass. Real inference requires an explicit CLI opt-in."""

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from workflow_engine.evaluation import evaluate_planner
from workflow_engine.planner import FixturePlanner, ModelPlanner
from workflow_engine.policy import CatalogProvider
from workflow_engine.settings import Settings


async def run(args):
    settings = Settings()
    async with httpx.AsyncClient(
        base_url=settings.model_url.rstrip("/") + "/",
        transport=httpx.AsyncHTTPTransport(retries=0),
        trust_env=False,
    ) as client:
        if args.real:
            if not args.approve_inference or settings.model_revision in {
                "unrecorded",
                "record-before-evaluation",
            }:
                raise SystemExit(
                    "Record WORKFLOW_MODEL_REVISION and pass --approve-inference after approving the resource budget"
                )
            planner = ModelPlanner(client, settings)
        else:
            planner = FixturePlanner.from_file(args.cases)
        report = await evaluate_planner(
            args.cases, planner, CatalogProvider.from_file(settings.catalog_path)
        )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as file:
            json.dump(report, file, indent=2)
        print(
            json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2)
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="fixtures/planner_test.jsonl")
    parser.add_argument(
        "--output",
        required=True,
        help="New JSON artifact path; existing runs are never overwritten",
    )
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--approve-inference", action="store_true")
    asyncio.run(run(parser.parse_args()))

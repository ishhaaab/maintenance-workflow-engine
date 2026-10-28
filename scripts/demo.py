"""Show a plan first; execute only with --approve."""

import argparse
import asyncio
import json
import os
import time
from uuid import uuid4

import httpx

REQUEST = "Create an inspection ticket for pump P-17 and reserve one seal kit if available."


async def run_demo(client, approve=False, timeout_s=60):
    response = await client.post(
        "/workflows", json={"text": REQUEST}, headers={"Idempotency-Key": uuid4().hex}
    )
    response.raise_for_status()
    path = "/workflows/" + response.json()["workflow_id"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = await client.get(path)
        response.raise_for_status()
        workflow = response.json()
        if workflow["state"] == "AWAITING_APPROVAL":
            print(
                json.dumps(
                    {
                        "workflow_id": workflow["workflow_id"],
                        "plan": workflow["plan"],
                        "plan_hash": workflow["plan_hash"],
                        "revision": workflow["revision"],
                    },
                    indent=2,
                )
            )
            if not approve:
                return workflow
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
            "NEEDS_INPUT",
            "FAILED",
            "NEEDS_REVIEW",
            "CANCELLED",
        }:
            return workflow
        await asyncio.sleep(0.1)
    raise TimeoutError("Demo deadline exceeded; the admitted workflow remains durable")


async def main(args):
    key = os.getenv("WORKFLOW_DEMO_API_KEY")
    if not key:
        raise SystemExit("Set WORKFLOW_DEMO_API_KEY to the tenant-A key from your .env")
    async with httpx.AsyncClient(
        base_url=args.url, headers={"X-API-Key": key}, timeout=10, trust_env=False
    ) as client:
        workflow = await run_demo(client, args.approve)
        print(json.dumps({"state": workflow["state"], "actions": workflow["actions"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--approve", action="store_true", help="Explicitly approve this demo's exact returned plan"
    )
    asyncio.run(main(parser.parse_args()))

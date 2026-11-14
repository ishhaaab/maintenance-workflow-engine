"""Test-only crash points. No fault hook is added to the production runner."""

import argparse
import asyncio
import json
from pathlib import Path

from workflow_engine.cli import runtime_loop
from workflow_engine.runtime import resources
from workflow_engine.telemetry import configure_logging


async def run(args):
    async with resources() as (store, broker, runner):

        async def stop_at_marker():
            Path(args.marker).write_text("injection reached", encoding="utf-8")
            if args.resume is None:
                await asyncio.Event().wait()
            else:
                while not Path(args.resume).exists():
                    await asyncio.sleep(0.02)

        if args.mode == "dispatch":
            await store.due_wakeups()
            await stop_at_marker()
        elif args.mode == "pending":
            assert await broker.receive("abandoned-worker") is not None
            await stop_at_marker()
        else:
            gateway = runner.gateways["ticket"]
            submit = gateway.submit

            async def crash_point(action):
                if args.mode == "after_effect":
                    outcome = await submit(action)
                    await stop_at_marker()
                    return outcome
                await stop_at_marker()
                return await submit(action)

            gateway.submit = crash_point
            result = await runner.tick(args.workflow_id, "crash-worker")
            print(json.dumps(result.model_dump()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["dispatch", "pending", "before_effect", "after_effect"], required=True
    )
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--resume")
    configure_logging()
    try:
        asyncio.run(run(parser.parse_args()), loop_factory=runtime_loop)
    except Exception:
        # Dependency failures are expected in outage tests, never print driver row/URL diagnostics.
        print("fault_worker_dependency_unavailable")
        raise SystemExit(2) from None

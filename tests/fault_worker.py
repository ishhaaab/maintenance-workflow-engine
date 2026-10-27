"""Test-only crash points. No fault hook is added to the production runner."""

import argparse
import asyncio
from pathlib import Path

from workflow_engine.runtime import resources


async def run(args):
    async with resources() as (store, broker, runner):

        async def stop_at_marker():
            Path(args.marker).write_text("injection reached", encoding="utf-8")
            await asyncio.Event().wait()

        if args.mode == "dispatch":
            await store.due_wakeups()
            await stop_at_marker()
        else:
            gateway = runner.gateways["ticket"]
            submit = gateway.submit

            async def crash_point(action):
                if args.mode == "after_effect":
                    await submit(action)
                await stop_at_marker()

            gateway.submit = crash_point
            await runner.tick(args.workflow_id, "crash-worker")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["dispatch", "before_effect", "after_effect"], required=True
    )
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--marker", required=True)
    asyncio.run(run(parser.parse_args()))

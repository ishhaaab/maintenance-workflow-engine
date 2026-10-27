"""Process entry points. Imports for network clients are deferred until execution."""

import argparse
import asyncio
import json
import os
import socket


def main():
    parser = argparse.ArgumentParser(description="Approval-gated maintenance workflow engine")
    parser.add_argument(
        "command", choices=["api", "worker", "dispatcher", "init", "ticketing", "inventory", "tick"]
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--database", help="Simulator SQLite file")
    parser.add_argument("--enable-faults", action="store_true")
    parser.add_argument("--worker-id", default=socket.gethostname() + "-" + str(os.getpid()))
    parser.add_argument("--workflow-id")
    args = parser.parse_args()
    from .telemetry import configure_logging

    configure_logging()
    if args.command == "tick" and not args.workflow_id:
        parser.error("tick requires --workflow-id")
    if args.command in {"api", "ticketing", "inventory"}:
        import uvicorn

        if args.command == "api":
            from .runtime import api_application

            app, port = api_application(), 8000
        else:
            from external_simulators import inventory, ticketing

            factory = ticketing.create_app if args.command == "ticketing" else inventory.create_app
            app = factory(
                args.database or f"data/{args.command}.sqlite",
                enable_faults=args.enable_faults or os.getenv("ENABLE_FAULTS") == "true",
            )
            port = 8001 if args.command == "ticketing" else 8002
        uvicorn.run(app, host=args.host, port=args.port or port, access_log=False, log_config=None)
    else:
        from .runtime import run_process

        try:
            result = asyncio.run(run_process(args.command, args.worker_id, args.workflow_id))
            if result:
                print(json.dumps(result))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

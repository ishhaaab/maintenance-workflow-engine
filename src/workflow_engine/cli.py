"""Process entry points. Imports for network clients are deferred until execution."""

import argparse
import asyncio
import json
import os
import socket
import sys


def runtime_loop():
    """Psycopg async cannot use native Windows' default Proactor loop."""
    return asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()


def main():
    parser = argparse.ArgumentParser(description="Approval-gated maintenance workflow engine")
    parser.add_argument(
        "command",
        choices=[
            "api",
            "worker",
            "dispatcher",
            "notifications",
            "init",
            "ticketing",
            "inventory",
            "tick",
            "bootstrap-admin",
            "seed-demo",
        ],
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--database", help="Simulator SQLite file")
    parser.add_argument("--enable-faults", action="store_true")
    parser.add_argument("--worker-id", default=socket.gethostname() + "-" + str(os.getpid()))
    parser.add_argument("--workflow-id")
    parser.add_argument("--tenant")
    parser.add_argument("--login")
    args = parser.parse_args()
    from .telemetry import configure_logging

    configure_logging()
    if args.command == "tick" and not args.workflow_id:
        parser.error("tick requires --workflow-id")
    if args.command == "bootstrap-admin":
        if not args.tenant or not args.login:
            parser.error("bootstrap-admin requires --tenant and --login")
        from .bootstrap import bootstrap_admin
        from .settings import Settings

        print(
            json.dumps(
                asyncio.run(
                    bootstrap_admin(Settings(), args.tenant, args.login), loop_factory=runtime_loop
                )
            )
        )
        return
    if args.command == "seed-demo":
        from .db import create_database
        from .seed_demo import seed_demo
        from .settings import Settings

        async def seed():
            settings = Settings()
            database = create_database(settings.runtime_database_url())
            try:
                return await seed_demo(database, settings)
            finally:
                await database.close()

        result = asyncio.run(seed(), loop_factory=runtime_loop)
        result["credentials"] = [
            {
                "tenant": row.tenant,
                "login": row.login,
                "role": row.role,
                "password": row.password.get_secret_value(),
            }
            for row in result["credentials"]
        ]
        # One-time operator output only. Passwords never enter the journal or logging.
        print(json.dumps(result))
        return
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
        uvicorn.run(
            app,
            host=args.host,
            port=args.port or port,
            access_log=False,
            log_config=None,
            loop="workflow_engine.cli:runtime_loop" if args.command == "api" else "auto",
        )
    else:
        from .runtime import run_process

        try:
            result = asyncio.run(
                run_process(args.command, args.worker_id, args.workflow_id),
                loop_factory=runtime_loop,
            )
            if result:
                print(json.dumps(result))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

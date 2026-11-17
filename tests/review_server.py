"""Fresh runtime-only browser deployment with real workers and HTTP simulators."""

import os
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from uuid import uuid4

import httpx

from tests.postgres_support import runtime_environment
from tests.processes import ROOT, free_port, simulator_process


@contextmanager
def review_server(store, directory):
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not redis_url:
        raise RuntimeError("Browser acceptance requires TEST_REDIS_URL")
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        ticket_url, _ = stack.enter_context(simulator_process("ticketing", directory / "tickets"))
        inventory_url, _ = stack.enter_context(
            simulator_process("inventory", directory / "inventory")
        )
        port, stream = free_port(), "browser-" + uuid4().hex
        origin = f"http://127.0.0.1:{port}"
        environment = runtime_environment(
            store,
            redis_url=redis_url,
            stream=stream,
            group=stream + "-workers",
            ticket_url=ticket_url,
            inventory_url=inventory_url,
        )
        environment.update(WORKFLOW_ENV="local", WORKFLOW_ORIGIN=origin)
        processes = []
        for name, arguments in (
            ("api", ["api", "--port", str(port)]),
            ("dispatcher", ["dispatcher"]),
            ("worker-a", ["worker", "--worker-id", "browser-a"]),
            ("worker-b", ["worker", "--worker-id", "browser-b"]),
        ):
            log = stack.enter_context((directory / (name + ".log")).open("w", encoding="utf-8"))
            process = subprocess.Popen(
                [sys.executable, "-m", "workflow_engine.cli", *arguments],
                env=environment,
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            processes.append(process)
        try:
            with httpx.Client(base_url=origin, timeout=1, trust_env=False) as client:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if any(process.poll() is not None for process in processes):
                        raise AssertionError("Browser runtime exited; inspect owned logs")
                    try:
                        if client.get("/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError("Browser runtime did not become ready")
            yield origin, stream
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            from redis import Redis

            with Redis.from_url(redis_url) as redis:
                redis.delete(stream)

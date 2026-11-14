"""Test-owned subprocess lifecycle; never stop processes by name or shared port."""

import asyncio
import os
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


class _ChildEnvironment(dict):
    def __repr__(self):
        return "<explicit child environment; values hidden>"


def child_environment(**configuration):
    """Keep OS launch settings only; every service credential must be explicit."""
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
    }
    environment = _ChildEnvironment(
        (key, value) for key, value in os.environ.items() if key.upper() in allowed
    )
    environment.update(configuration)
    return environment


@asynccontextmanager
async def checkpoint_worker(environment, mode, workflow_id, directory):
    """A separately running, test-owned worker paused at a witnessed durable boundary."""
    directory.mkdir(parents=True, exist_ok=True)
    marker, resume = directory / "checkpoint", directory / "resume"
    with (directory / "crash-worker.log").open("x", encoding="utf-8") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "tests/fault_worker.py",
                "--mode",
                mode,
                "--workflow-id",
                workflow_id,
                "--marker",
                str(marker),
                "--resume",
                str(resume),
            ],
            env=environment,
            cwd=ROOT,
            stdout=output,
            stderr=output,
        )
        try:
            deadline = time.monotonic() + 15
            while not marker.exists():
                if process.poll() is not None:
                    raise AssertionError(
                        "Crash worker exited before injection point; see crash-worker.log"
                    )
                if time.monotonic() >= deadline:
                    raise AssertionError("Crash injection deadline exceeded")
                await asyncio.sleep(0.03)
            yield process, resume
        finally:
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait, 5)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def simulator_process(kind, directory):
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{kind}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "workflow_engine.cli",
                kind,
                "--port",
                str(port),
                "--database",
                str(directory / f"{kind}.sqlite"),
                "--enable-faults",
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
            env=child_environment(),
        )
        try:
            deadline = time.monotonic() + 15
            with httpx.Client(timeout=0.5, trust_env=False) as client:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"Simulator exited; see {directory / (kind + '.log')}")
                    try:
                        if client.get(url + "/health/live").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    raise TimeoutError("Simulator startup deadline")
            yield url, process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

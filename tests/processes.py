"""Test-owned subprocess lifecycle; never stop processes by name or shared port."""

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


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
            env=os.environ.copy(),
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

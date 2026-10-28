import os
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest

from tests.support import REQUEST


@pytest.mark.compose
def test_isolated_compose_clean_start_and_restart(tmp_path):
    if os.getenv("RUN_COMPOSE") != "1":
        pytest.skip("Set RUN_COMPOSE=1 to build and run disposable project-owned containers")
    if not shutil.which("docker"):
        pytest.fail("RUN_COMPOSE requested but Docker is unavailable")
    envfile = tmp_path / "demo.env"
    envfile.write_text(
        'WORKFLOW_API_KEYS={"compose-test-key":"tenant-a"}\nWORKFLOW_PLANNER=fixture\n',
        encoding="utf-8",
    )
    env = {**os.environ, "WORKFLOW_ENV_FILE": str(envfile)}
    command = ["docker", "compose", "-p", "workflow-test-" + uuid4().hex[:12]]
    try:
        subprocess.run(command + ["config", "--quiet"], env=env, check=True, timeout=30)
        subprocess.run(
            command + ["up", "--build", "-d", "--wait", "--wait-timeout", "120"],
            env=env,
            check=True,
            timeout=600,
        )
        with httpx.Client(
            base_url="http://127.0.0.1:8000", headers={"X-API-Key": "compose-test-key"}, timeout=10
        ) as client:
            created = client.post(
                "/workflows", json={"text": REQUEST}, headers={"Idempotency-Key": uuid4().hex}
            )
            assert created.status_code == 202
            path = "/workflows/" + created.json()["workflow_id"]
            deadline = time.monotonic() + 90
            approved = False
            while time.monotonic() < deadline:
                workflow = client.get(path).json()
                if workflow["state"] == "AWAITING_APPROVAL" and not approved:
                    response = client.post(
                        path + "/approve",
                        json={
                            "expected_revision": workflow["revision"],
                            "plan_hash": workflow["plan_hash"],
                        },
                    )
                    assert response.status_code == 202
                    approved = True
                    subprocess.run(command + ["restart", "worker"], env=env, check=True, timeout=30)
                if workflow["state"] == "SUCCEEDED":
                    assert all(a["status"] == "CONFIRMED" for a in workflow["actions"])
                    break
                assert workflow["state"] not in {"FAILED", "NEEDS_REVIEW", "CANCELLED"}
                time.sleep(0.2)
            else:
                pytest.fail("Compose workflow exceeded completion deadline")
        if os.getenv("RUN_LOAD") == "1":
            subprocess.run(
                [
                    sys.executable,
                    "scripts/benchmark_load.py",
                    "--approve-plans",
                    "--output",
                    "artifacts/load.json",
                ],
                env={**env, "WORKFLOW_BENCHMARK_API_KEY": "compose-test-key"},
                check=True,
                timeout=2400,
            )
    finally:
        subprocess.run(command + ["logs", "--no-color"], env=env, timeout=30)
        # This random test project owns these containers and volumes, never the user's deployment.
        subprocess.run(
            command + ["down", "--volumes", "--remove-orphans"], env=env, check=True, timeout=60
        )

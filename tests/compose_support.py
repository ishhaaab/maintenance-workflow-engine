"""Operations restricted to a newly named PostgreSQL test project and its recorded volumes."""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from tests.processes import ROOT


class ComposeProject:
    def __init__(self, directory):
        if os.getenv("RUN_COMPOSE") != "1":
            pytest.skip("Set RUN_COMPOSE=1 for the fresh isolated PostgreSQL deployment")
        self.docker = shutil.which("docker")
        if not self.docker:
            pytest.fail("RUN_COMPOSE requested but Docker is unavailable")
        self.directory = directory
        self.project = "workflow-pg-test-" + uuid4().hex[:12]
        self.created = False
        self.envfile = directory / "compose.env"
        self.envfile.write_text(
            "WORKFLOW_PG_IMAGE_TAG="
            + self.project
            + "\n"
            + "\n".join(
                f"WORKFLOW_PG_{name}_PORT=0"
                for name in ("API", "TICKET", "INVENTORY", "DB", "REDIS")
            )
            + "\n",
            encoding="utf-8",
        )
        self.prefix = [
            "compose",
            "--env-file",
            str(self.envfile),
            "-p",
            self.project,
            "-f",
            str(ROOT / "compose.postgres.yaml"),
            "-f",
            str(ROOT / "tests/compose.postgres-test.yaml"),
        ]

    def docker_run(self, *arguments, timeout=120, check=True):
        result = subprocess.run(
            [self.docker, *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode and check:
            evidence = self.directory / ("failed-command-" + uuid4().hex + ".txt")
            evidence.write_text(result.stdout + result.stderr, encoding="utf-8")
            raise AssertionError(f"Owned Compose command failed; see {evidence}")
        return result

    def run(self, *arguments, **kwargs):
        return self.docker_run(*self.prefix, *arguments, **kwargs)

    def model(self):
        return json.loads(self.run("--profile", "*", "config", "--format", "json").stdout)

    def fixture(self, *arguments):
        output = self.run("run", "--rm", "--no-deps", "fixture", *arguments, timeout=90).stdout
        return json.loads(output.strip().splitlines()[-1])

    def url(self, service, port):
        binding = self.run("port", service, str(port)).stdout.strip()
        assert binding.startswith("127.0.0.1:")
        return "http://" + binding

    def wait_run(self, workflow_id, state, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.fixture("get", "--workflow-id", workflow_id)
            if row["state"] == state:
                return row
            assert row["state"] not in {"FAILED", "NEEDS_REVIEW", "CANCELLED"}, row["state"]
            time.sleep(0.2)
        raise AssertionError("Owned deployment did not reach the expected workflow state")

    def cleanup(self):
        if not self.created:
            return
        model = self.model()
        owned = []
        for volume in model["volumes"].values():
            name = volume["name"]
            assert name.startswith(self.project + "_")
            inspected = self.docker_run("volume", "inspect", name, check=False)
            if inspected.returncode == 0:
                assert (
                    json.loads(inspected.stdout)[0]["Labels"]["com.docker.compose.project"]
                    == self.project
                )
                owned.append(name)
        logs = self.run("logs", "--no-color", check=False).stdout
        (self.directory / "compose.log").write_text(logs, encoding="utf-8")
        self.run("--profile", "*", "down", "--volumes", "--remove-orphans", timeout=180)
        for name in owned:
            assert self.docker_run("volume", "inspect", name, check=False).returncode != 0

    def save_artifacts(self):
        if not (root := os.environ.get("COMPOSE_ARTIFACT_DIR")):
            return
        destination = Path(root) / self.project
        destination.mkdir(parents=True, exist_ok=False)
        leaked = False
        for name in ("result.json", "compose.log"):
            source = self.directory / name
            if source.exists():
                content = source.read_text(encoding="utf-8")
                safe, count = re.subn(
                    r"postgres(?:ql)?(?:\+psycopg)?://[^\s\"']+", "[redacted-url]", content
                )
                leaked |= count > 0
                (destination / name).write_text(safe, encoding="utf-8")
        assert not leaked, "A credential URL appeared in deployment diagnostics"

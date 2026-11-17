"""Fresh PostgreSQL deployment only. Never targets the historical compose.yaml."""

import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.compose_support import ComposeProject
from tests.support import REQUEST


@pytest.mark.compose
def test_isolated_compose_clean_start_and_restart(tmp_path):
    project = ComposeProject(tmp_path)
    try:
        model = project.model()
        services = model["services"]
        assert {
            "postgres",
            "redis",
            "api",
            "worker-a",
            "worker-b",
            "dispatcher",
            "prepare",
            "migrate",
            "ticketing",
            "inventory",
            "notifications",
            "mailpit",
        } <= services.keys()
        for name in ("api", "worker-a", "worker-b", "dispatcher", "notifications"):
            service = services[name]
            assert (
                service["environment"]["WORKFLOW_DATABASE_URL_FILE"]
                == "/run/pg-runtime/runtime.url"
            )
            assert not any(
                "OWNER" in key or "ADMIN" in key or key == "WORKFLOW_DATABASE_URL"
                for key in service["environment"]
            )
            assert {mount["source"] for mount in service["volumes"]} == {"pg-runtime-v1"}
            assert all(mount["read_only"] for mount in service["volumes"])
        assert {mount["source"] for mount in services["migrate"]["volumes"]} == {"pg-owner-v1"}
        assert {mount["source"] for mount in services["ticketing"]["volumes"]} == {
            "pg-ticket-data-v1"
        }
        assert {mount["source"] for mount in services["inventory"]["volumes"]} == {
            "pg-inventory-data-v1"
        }
        assert not {"mongo-data", "ticket-data", "inventory-data"} & model["volumes"].keys()
        assert all(
            port["host_ip"] == "127.0.0.1"
            for service in services.values()
            for port in service.get("ports", [])
        )
        project.run("build", "api", "fixture", timeout=900)
        project.created = True
        project.run("run", "--rm", "--no-deps", "prepare", "admin")
        project.run("up", "-d", "--wait", "postgres", "redis", timeout=180)
        # Both jobs target this new cluster. The started journal and lock must
        # prevent a second fresh database even when they overlap.
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [
                pool.submit(
                    project.run, "run", "--rm", "--no-deps", "prepare", "roles", check=False
                )
                for _ in range(2)
            ]
            results = [job.result() for job in jobs]
        assert any(result.returncode == 0 for result in results)
        assert all(result.returncode in {0, 1} for result in results)
        # Runtime must fail schema-only init before the explicit migration; no startup DDL.
        before = project.run("run", "--rm", "--no-deps", "api", "init", check=False)
        assert before.returncode != 0
        assert project.fixture("info")["schema"] is None
        project.run("run", "--rm", "--no-deps", "migrate")
        project.run("run", "--rm", "--no-deps", "migrate")
        project.run("up", "-d", "--wait", "--wait-timeout", "120", timeout=240)
        identity = project.fixture("info")
        from workflow_engine.db import SCHEMA_REVISION

        assert identity["schema"] == SCHEMA_REVISION
        assert identity["runtime_restricted"] is True
        assert identity["runtime_file_uid"] == 10001 and identity["runtime_file_mode"] == "0o400"
        assert identity["runtime_file_gid"] == identity["owner_file_uid"] == 10001
        assert identity["owner_file_mode"] == "0o400" and identity["application_databases"] == 1
        admin_modes = project.run(
            "exec",
            "-T",
            "postgres",
            "stat",
            "-c",
            "%u:%g:%a",
            "/run/pg-admin/admin.json",
            "/run/pg-admin/allocation.json",
            "/run/pg-admin/password",
        ).stdout.splitlines()
        assert admin_modes == ["0:0:400", "0:0:400", "0:999:440"]
        assert project.fixture("grants")["denied"] == [
            "audit_update",
            "audit_delete",
            "audit_truncate",
            "ddl",
        ]
        # Removing a consumer file cannot make ordinary startup provision a new DB.
        project.run(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "prepare",
            "-c",
            "from pathlib import Path; p=Path('/run/pg-runtime/runtime.url'); p.rename(p.with_suffix('.saved'))",
        )
        missing = project.run("run", "--rm", "--no-deps", "api", "init", check=False)
        assert missing.returncode != 0
        project.run("run", "--rm", "--no-deps", "prepare", "roles")
        project.run(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "prepare",
            "-c",
            "from pathlib import Path; p=Path('/run/pg-runtime/runtime.url'); s=p.with_suffix('.saved'); assert p.read_bytes()==s.read_bytes(); s.unlink()",
        )
        assert project.fixture("info") == identity
        # A mismatched consumer record is rejected, not overwritten or rotated.
        project.run(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "prepare",
            "-c",
            "from pathlib import Path; p=Path('/run/pg-runtime/runtime.url'); p.rename(p.with_suffix('.saved')); p.write_text('invalid-runtime-record')",
        )
        mismatch = project.run("run", "--rm", "--no-deps", "prepare", "roles", check=False)
        assert mismatch.returncode != 0
        project.run(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "prepare",
            "-c",
            "from pathlib import Path; p=Path('/run/pg-runtime/runtime.url'); assert p.read_text()=='invalid-runtime-record'; p.unlink(); p.with_suffix('.saved').rename(p)",
        )
        assert project.fixture("info") == identity
        project.fixture("log-probe")
        logs = project.run("logs", "--no-color", "postgres").stdout
        assert "synthetic-row-detail-sentinel" not in logs
        assert "synthetic-password-sentinel" not in logs
        for name in ("api", "worker-a", "worker-b", "dispatcher", "notifications"):
            project.run(
                "exec",
                "-T",
                name,
                "python",
                "-c",
                "from pathlib import Path; import os; "
                "assert os.getuid()==10001 and os.getgid()==10001; "
                "assert not Path('/run/pg-owner').exists(); "
                "assert not Path('/run/pg-admin').exists(); "
                "assert not Path('/app/tests').exists(); "
                "assert not any('PASSWORD' in key or 'OWNER' in key or 'ADMIN' in key "
                "or key.startswith(('TEST_', 'PG')) for key in os.environ)",
            )
            container_id = project.run("ps", "-q", name).stdout.strip()
            mounts = json.loads(
                project.docker_run("inspect", "--format", "{{json .Mounts}}", container_id).stdout
            )
            assert len(mounts) == 1
            assert mounts[0]["Destination"] == "/run/pg-runtime" and mounts[0]["RW"] is False
            assert mounts[0]["Name"] == project.project + "_pg-runtime-v1"
        process_output = project.run("ps", "--format", "json").stdout
        try:
            ps = json.loads(process_output)
        except json.JSONDecodeError:
            ps = [json.loads(line) for line in process_output.splitlines() if line.strip()]
        if isinstance(ps, dict):
            ps = [ps]
        assert {row["Service"] for row in ps if row["State"] == "running"} >= {
            "worker-a",
            "worker-b",
        }
        api_url, ticket_url, inventory_url = (
            project.url("api", 8000),
            project.url("ticketing", 8001),
            project.url("inventory", 8002),
        )
        with httpx.Client(base_url=api_url, timeout=10, trust_env=False) as api:
            assert api.get("/health/ready").json() == {
                "status": "ready",
                "postgres": True,
                "broker": True,
            }
            count = project.fixture("info")["runs"]
            assert (
                api.post(
                    "/workflows", json={"text": REQUEST}, headers={"X-API-Key": "legacy"}
                ).status_code
                == 410
            )
            assert (
                api.post("/workflows/unknown/approve", json={"plan_hash": "0" * 64}).status_code
                == 410
            )
            assert (
                api.get(
                    "/workflows/unknown", headers={"Cookie": "workflow_session=fabricated"}
                ).status_code
                == 401
            )
            assert api.post("/workflows/unknown/cancel", json={}).status_code == 401
            assert project.fixture("info")["runs"] == count
            first = project.fixture("admit", "--key", "restart-proof")
            first = project.wait_run(first["workflow_id"], "AWAITING_APPROVAL")
            original_keys = [action["idempotency_key"] for action in first["actions"]]
            with httpx.Client(timeout=10, trust_env=False) as effects:
                assert effects.get(ticket_url + "/__effects").json() == {}
                effects.put(
                    ticket_url + "/__faults", json={"mode": "post_commit_delay", "delay_s": 30}
                )
                project.fixture(
                    "approve",
                    "--workflow-id",
                    first["workflow_id"],
                    "--revision",
                    str(first["revision"]),
                    "--plan-hash",
                    first["plan_hash"],
                )
                deadline = time.monotonic() + 30
                while not effects.get(ticket_url + "/__effects").json():
                    assert time.monotonic() < deadline
                    time.sleep(0.1)
                # The downstream commit is witnessed while the checkpoint is withheld.
                project.run("stop", "-t", "0", "worker-a", "worker-b")
                inflight = project.fixture("get", "--workflow-id", first["workflow_id"])
                assert (
                    inflight["state"] == "RUNNING"
                    and inflight["actions"][0]["status"] == "IN_FLIGHT"
                )
                project.fixture("expire", "--workflow-id", first["workflow_id"])
                project.run("up", "-d", "--wait", "worker-a", "worker-b", timeout=180)
                first = project.wait_run(first["workflow_id"], "SUCCEEDED")
                assert [action["idempotency_key"] for action in first["actions"]] == original_keys
                assert [action["submissions"] for action in first["actions"]] == [1, 1]
                assert sum(effects.get(ticket_url + "/__effects").json().values()) == 1
                assert sum(effects.get(inventory_url + "/__effects").json().values()) == 1
            second = project.fixture("admit", "--key", "redis-proof")
            second = project.wait_run(second["workflow_id"], "AWAITING_APPROVAL")
            project.run("stop", "redis")
            assert api.get("/health/ready").json() == {
                "status": "degraded",
                "postgres": True,
                "broker": False,
            }
            project.fixture(
                "approve",
                "--workflow-id",
                second["workflow_id"],
                "--revision",
                str(second["revision"]),
                "--plan-hash",
                second["plan_hash"],
            )
            assert (
                project.fixture("get", "--workflow-id", second["workflow_id"])["state"] == "READY"
            )
            project.run("up", "-d", "--force-recreate", "--wait", "redis", timeout=120)
            second = project.wait_run(second["workflow_id"], "SUCCEEDED")
        retained = project.fixture("info")
        assert retained["runs"] == 2
        project.run("run", "--rm", "--no-deps", "prepare", "roles")
        project.run("run", "--rm", "--no-deps", "migrate")
        project.run("up", "-d", "--force-recreate", "--wait", timeout=300)
        recreated = project.fixture("info")
        assert recreated == retained
        project.run("down", timeout=180)  # Deliberately retains volumes, not the old deployment.
        project.run("up", "-d", "--wait", "--wait-timeout", "120", timeout=240)
        assert project.fixture("info") == retained
        for workflow in (first, second):
            loaded = project.fixture("get", "--workflow-id", workflow["workflow_id"])
            assert loaded == workflow
        with httpx.Client(timeout=10, trust_env=False) as effects:
            # Dynamic ports can change after recreation; discover them again.
            for name, port in (("ticketing", 8001), ("inventory", 8002)):
                counts = effects.get(project.url(name, port) + "/__effects").json()
                assert len(counts) == 2 and set(counts.values()) == {1}
        (tmp_path / "result.json").write_text(
            json.dumps(
                {
                    "project": project.project,
                    "identity": identity,
                    "retained": retained,
                    "passed": True,
                },
                indent=2,
            )
        )
    finally:
        try:
            project.cleanup()
        finally:
            project.save_artifacts()

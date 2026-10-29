"""Compare two revisions on one host using the unchanged full-workflow load driver."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from workflow_engine.evaluation import environment, summarize_load


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--profile-api", choices=["true", "false"], default="false")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    envfile = output / "benchmark.env"
    envfile.write_text(
        'WORKFLOW_API_KEYS={"admission-benchmark-key":"tenant-a"}\nWORKFLOW_PLANNER=fixture\n',
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "WORKFLOW_ENV_FILE": str(envfile),
        "WORKFLOW_BENCHMARK_API_KEY": "admission-benchmark-key",
    }
    driver = Path(__file__).with_name("benchmark_load.py").resolve()
    report = {
        "environment": environment(),
        "protocol": "Same runner, pinned stack and client driver; one unmeasured bootstrap workflow per revision; then repeated groups of 100 full workflows at concurrency 20, with 50 ms status polling and explicit approvals. Each group preserves all observations.",
        "variants": [],
    }
    variants = [("baseline", args.baseline.resolve()), ("candidate", args.candidate.resolve())]
    if args.profile_api == "true":
        variants = [variants[1]]
        report["diagnostic_only"] = True
        report["protocol"] = (
            "Instrumented candidate diagnosis; cProfile overhead means these timings are not an uninstrumented performance result."
        )
    # Build both before measurement so build load never overlaps either experiment.
    images = {}
    for label, root in variants:
        images[label] = "maintenance-admission:" + label + "-" + uuid4().hex[:8]
        command(["docker", "build", "-t", images[label], str(root)], timeout=600)
    for label, root in variants:
        directory = output / label
        directory.mkdir()
        override = directory / "compose.override.json"
        services = {
            role: {"image": images[label]}
            for role in ("api", "worker", "dispatcher", "ticketing", "inventory")
        }
        if args.profile_api == "true":
            profile_output = directory / "profile"
            profile_output.mkdir(mode=0o777)
            profile_output.chmod(0o777)
            services["api"].update(
                entrypoint=["python", "/profile-api.py"],
                command=[],
                environment={"PROFILE_OUTPUT_DIR": "/profile-output"},
                volumes=[
                    f"{driver.with_name('profile_api.py')}:/profile-api.py:ro",
                    f"{profile_output}:/profile-output",
                ],
            )
        override.write_text(
            json.dumps({"services": services}),
            encoding="utf-8",
        )
        compose = [
            "docker",
            "compose",
            "--project-directory",
            str(root),
            "-f",
            str(root / "compose.yaml"),
            "-f",
            str(override),
            "-p",
            "admission-" + uuid4().hex[:12],
        ]
        variant = {
            "label": label,
            "commit": command(
                ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
            ).stdout.strip(),
            "trials": [],
        }
        report["variants"].append(variant)
        try:
            command(compose + ["config", "--quiet"], env=env, timeout=30)
            command(
                compose + ["up", "--no-build", "-d", "--wait", "--wait-timeout", "120"],
                env=env,
                timeout=180,
            )
            command(
                [
                    sys.executable,
                    str(driver),
                    "--count",
                    "1",
                    "--concurrency",
                    "1",
                    "--approve-plans",
                    "--output",
                    str(directory / "bootstrap.json"),
                ],
                env=env,
                timeout=180,
            )
            observations = []
            for trial in range(1, args.repetitions + 1):
                path = directory / f"trial-{trial}.json"
                command(
                    [
                        sys.executable,
                        str(driver),
                        "--count",
                        "100",
                        "--concurrency",
                        "20",
                        "--approve-plans",
                        "--output",
                        str(path),
                    ],
                    env=env,
                    timeout=300,
                )
                run = json.loads(path.read_text())["runs"][0]
                variant["trials"].append(
                    {
                        "trial": trial,
                        **{key: value for key, value in run.items() if key != "results"},
                    }
                )
                observations.extend(run["results"])
                (output / "comparison.json").write_text(
                    json.dumps(report, indent=2), encoding="utf-8"
                )
            variant["pooled"] = summarize_load(observations)
        finally:
            try:
                with (directory / "compose.log").open("w", encoding="utf-8") as log:
                    command(
                        compose + ["logs", "--no-color"],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=60,
                    )
            finally:
                command(compose + ["down", "--volumes", "--remove-orphans"], env=env, timeout=90)
                (output / "comparison.json").write_text(
                    json.dumps(report, indent=2), encoding="utf-8"
                )
        if args.profile_api == "true":
            for name in ("api.prof", "requests.jsonl", "cpu.txt"):
                if not (directory / "profile" / name).is_file():
                    raise RuntimeError(f"Missing diagnostic artifact: {name}")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

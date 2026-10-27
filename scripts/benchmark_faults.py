"""Run all named scenarios once per seed; preserve failures and never retry until green."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from workflow_engine.evaluation import summarize_faults


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, help="A new directory for this run")
    parser.add_argument("--seeds", default="0,1,2,3,4,5,6,7,8,9")
    args = parser.parse_args()
    if not os.getenv("TEST_MONGO_URL") or not os.getenv("TEST_REDIS_URL"):
        raise SystemExit("Set TEST_MONGO_URL and TEST_REDIS_URL to isolated local test services")
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    seeds = [int(s) for s in args.seeds.split(",")]
    if len(seeds) != len(set(seeds)) or any(s not in range(10) for s in seeds):
        raise SystemExit("Seeds must be unique integers 0..9")
    env = {**os.environ, "FAULT_SEEDS": args.seeds, "FAULT_ARTIFACT_DIR": str(output / "cases")}
    with (output / "pytest.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/integration/test_fault_matrix.py",
                "-q",
                "--junitxml=" + str(output / "junit.xml"),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=1800,
        )
    definitions = [
        json.loads(line) for line in Path("fixtures/fault_cases.jsonl").read_text().splitlines()
    ]
    records = [json.loads(path.read_text()) for path in sorted((output / "cases").glob("*.json"))]
    summary = {
        "status": "failed",
        "runs_recorded": len(records),
        "expected_runs": len(definitions) * len(seeds),
        "exit_code": result.returncode,
    }
    try:
        summary.update(summarize_faults(definitions, records, seeds))
        summary["status"] = "passed" if result.returncode == 0 else "failed"
        summary["full_matrix"] = sorted(seeds) == list(range(10))
    except ValueError as error:
        summary["validation_error"] = str(error)
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary["status"] == "passed" else 1)


if __name__ == "__main__":
    main()

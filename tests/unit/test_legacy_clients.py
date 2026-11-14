import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "arguments",
    [
        ["scripts/demo.py"],
        ["scripts/demo.py", "--approve"],
        ["scripts/benchmark_load.py", "--approve-plans", "--output", "must-not-exist.json"],
    ],
)
def test_retired_cli_returns_nonzero_and_clear_guidance(arguments):
    result = subprocess.run(
        [sys.executable, *arguments], capture_output=True, text=True, timeout=15
    )
    assert result.returncode != 0
    assert "Phase 1" in result.stderr
    assert "unavailable" in result.stderr.lower()


@pytest.mark.parametrize(
    "arguments",
    [
        ["-m", "workflow_engine.cli", "--help"],
        ["scripts/demo.py", "--help"],
        ["scripts/benchmark_load.py", "--help"],
    ],
)
def test_help_and_historical_argument_parsers_remain_available(arguments):
    result = subprocess.run(
        [sys.executable, *arguments], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout

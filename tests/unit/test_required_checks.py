import subprocess
import sys

import pytest

from tests.processes import ROOT, child_environment


@pytest.mark.parametrize(
    "required, terminal, expected", [(False, True, 0), (True, True, 1), (True, False, 1)]
)
def test_required_service_suite_fails_instead_of_accepting_a_skip(
    tmp_path, required, terminal, expected
):
    case = tmp_path / "test_optional.py"
    case.write_text("import pytest\ndef test_service(): pytest.skip('service absent')\n")
    command = [sys.executable, "-m", "pytest", "-p", "tests.required_checks", str(case)]
    command += ["-q"] if terminal else ["-p", "no:terminal"]
    if required:
        command.append("--fail-on-skip")
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=child_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == expected
    if terminal:
        assert "1 skipped" in result.stdout

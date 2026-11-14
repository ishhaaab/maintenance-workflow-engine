import sys

import pytest

from scripts.benchmark_faults import main


def test_fault_benchmark_requires_postgres_before_creating_artifacts(tmp_path, monkeypatch):
    output = tmp_path / "must-not-exist"
    monkeypatch.delenv("TEST_MONGO_URL", raising=False)
    monkeypatch.delenv("TEST_POSTGRES_URL", raising=False)
    monkeypatch.setenv("TEST_REDIS_URL", "redis://127.0.0.1:1")
    monkeypatch.setattr(sys, "argv", ["benchmark_faults.py", "--output-dir", str(output)])
    with pytest.raises(SystemExit) as error:
        main()
    assert "TEST_POSTGRES_URL" in str(error.value)
    assert not output.exists()


@pytest.mark.parametrize("seeds", ["", "bad", "0,0", "10"])
def test_invalid_fault_seeds_do_not_start_measurement(tmp_path, monkeypatch, seeds):
    output = tmp_path / "must-not-exist"
    monkeypatch.setenv("TEST_POSTGRES_URL", "configured-test-server")
    monkeypatch.setenv("TEST_REDIS_URL", "configured-test-server")
    monkeypatch.delenv("TEST_MONGO_URL", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["benchmark_faults.py", "--output-dir", str(output), "--seeds", seeds]
    )
    with pytest.raises(SystemExit):
        main()
    assert not output.exists()

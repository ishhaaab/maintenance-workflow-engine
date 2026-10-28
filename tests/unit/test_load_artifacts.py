import json

import pytest

from workflow_engine.evaluation import RunJournal


def test_load_run_records_each_finished_workflow_before_summary(tmp_path):
    path = tmp_path / "load.json"
    with RunJournal(path) as journal:
        journal.record({"concurrency": 1, "state": "SUCCEEDED", "workflow_id": "w1"})
        rows = path.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()
        assert json.loads(rows[0])["workflow_id"] == "w1"
    assert not path.exists()  # An interrupted run must not look like a completed summary.
    with pytest.raises(FileExistsError):
        with RunJournal(path):
            pass

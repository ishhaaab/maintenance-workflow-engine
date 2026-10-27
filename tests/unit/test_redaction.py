import json
import logging

from workflow_engine.telemetry import JsonFormatter


def test_logs_drop_secrets_raw_text_provider_output_and_exception_details():
    secret = "secret-api-key-123"
    record = logging.LogRecord(
        "test", logging.ERROR, __file__, 1, "provider failed %s", (secret,), None
    )
    record.request_text = "private maintenance request"
    record.model_output = "private provider answer"
    record.mongo_url = "mongodb://user:password@host"
    record.api_key = secret
    output = JsonFormatter().format(record)
    assert secret not in output
    assert "private" not in output
    assert "password" not in output
    assert json.loads(output)["event"] == "redacted_event"


def test_safe_workflow_event_keeps_useful_fields():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "workflow_tick", (), None)
    record.workflow_id = "01234567-0123-0123-0123-0123456789ab"
    record.outcome = "progressed"
    result = json.loads(JsonFormatter().format(record))
    assert result["workflow_id"] == record.workflow_id
    assert result["outcome"] == "progressed"

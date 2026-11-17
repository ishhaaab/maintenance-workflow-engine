import csv
import io
from datetime import UTC, datetime

import pytest


@pytest.mark.parametrize(
    "value",
    ["=1+1", "+SUM(A1)", "-1+2", "@SUM(A1)", " \t=HYPERLINK(1)", "\u200b=1", "\r@cmd", "\n+2"],
)
def test_csv_neutralizes_formulas_after_leading_whitespace_and_controls(value):
    from workflow_engine.exports import csv_cell

    assert csv_cell(value) == "'" + value


def test_shared_report_formats_quotes_unicode_comments_and_pdf_pages():
    from pypdf import PdfReader

    from workflow_engine.exports import CycleReport, csv_report, pdf_report

    rows = tuple(
        {
            "item_id": str(n),
            "version": "1",
            "requester": "requester",
            "review_outcome": "APPROVED",
            "execution_outcome": "SUCCEEDED",
            "reviewer": "reviewer",
            "decision": "APPROVE",
            "comment": 'Café, "inspection"\nΔοκιμή насос',
            "level": "1",
            "version_id": str(n),
            "current": "yes",
        }
        for n in range(80)
    )
    report = CycleReport(
        {"id": "example", "title": "Maintenance – café", "state": "COMPLETED"},
        datetime(2026, 11, 14, tzinfo=UTC),
        rows,
        (),
    )
    content = csv_report(report)
    parsed = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    assert len(parsed) == 80
    assert parsed[0]["comment"] == 'Café, "inspection"\nΔοκιμή насос'
    pdf = PdfReader(io.BytesIO(pdf_report(report)))
    assert len(pdf.pages) > 1
    text = "".join(page.extract_text() for page in pdf.pages)
    assert "Café" in text and "насос" in text and "Δοκιμή" in text
    assert "SUCCEEDED" in text


def test_csv_rejects_expansion_at_the_output_budget_before_rendering_all_rows(monkeypatch):
    from workflow_engine import exports
    from workflow_engine.authorization import Denied

    monkeypatch.setattr(exports, "MAX_OUTPUT_BYTES", 4096, raising=False)

    def rows():
        for _ in range(100):
            yield {"item_id": "example", "comment": "é" * 1000}
        raise AssertionError("Renderer ignored its incremental output budget")

    report = exports.CycleReport(
        {"id": "example", "title": "Bounded", "state": "DRAFT"}, datetime.now(UTC), rows(), ()
    )
    with pytest.raises(Denied) as error:
        exports.csv_report(report)
    assert error.value.status == 413

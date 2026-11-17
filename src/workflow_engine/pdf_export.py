"""Portable, embedded-font PDF rendering. No browser/OS font dependency."""

import io
from pathlib import Path
from threading import Lock
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from .authorization import Denied

_font_lock = Lock()
_fonts = Path(__file__).parent / "fonts"


def render_pdf(report):
    with _font_lock:
        if "ReviewSans" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("ReviewSans", str(_fonts / "NotoSans-Regular.ttf")))
            pdfmetrics.registerFont(TTFont("ReviewSansBold", str(_fonts / "NotoSans-Bold.ttf")))
    body = ParagraphStyle(
        "body", fontName="ReviewSans", fontSize=8, leading=12, spaceAfter=6, splitLongWords=True
    )
    heading = ParagraphStyle(
        "heading", parent=body, fontName="ReviewSansBold", fontSize=17, leading=22, spaceAfter=12
    )
    subheading = ParagraphStyle(
        "subheading",
        parent=body,
        fontName="ReviewSansBold",
        fontSize=11,
        leading=15,
        spaceBefore=16,
        spaceAfter=8,
    )
    glyphs = pdfmetrics.getFont("ReviewSans").face.charToGlyph

    def paragraph(value, style=body):
        value = str(value).replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
        if any(ord(char) not in glyphs and char not in "\n" for char in value):
            raise Denied("pdf_unsupported_characters_use_csv", 422)
        return Paragraph(escape(value).replace("\n", "<br/>"), style)

    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=32,
        leftMargin=32,
        topMargin=34,
        bottomMargin=36,
        title="Maintenance review report",
        author="Maintenance review workspace",
    )
    content = [
        paragraph(report.cycle["title"], heading),
        paragraph(f"Cycle {report.cycle['id']} | Review {report.cycle['state']}"),
        paragraph(f"Snapshot {report.generated_at.isoformat()} | All timestamps are UTC."),
        paragraph("Review approval and execution success are separate outcomes."),
        Spacer(1, 12),
    ]
    data = [
        [
            paragraph(value, subheading)
            for value in (
                "Item / version",
                "Review / execution",
                "Assignment / decision",
                "Comment / UTC times",
            )
        ]
    ]
    for row in report.rows:
        data.append(
            [
                paragraph(
                    f"{row.get('item_id', '')}\nVersion {row.get('version', '')}, current: {row.get('current', '')}\n"
                    f"Requester: {row.get('requester', '')}\n{row.get('request_text', '')}"
                ),
                paragraph(
                    f"Review: {row.get('review_outcome', '')}\nExecution: {row.get('execution_outcome', '')}\n"
                    f"Plan: {row.get('plan_hash', '')}\nConfirmed effects: {row.get('confirmed_effects', '')}"
                ),
                paragraph(
                    f"Level {row.get('level', '')}\n{row.get('reviewer', '')}\n{row.get('decision', '')}"
                ),
                paragraph(
                    f"{row.get('comment', '')}\nSubmitted: {row.get('submitted_utc', '')}\n"
                    f"Decided: {row.get('decided_utc', '')}\nDeadline: {row.get('deadline_utc', '')}"
                ),
            ]
        )
    if report.rows:
        table = LongTable(data, colWidths=[135, 120, 90, A4[0] - 409], repeatRows=1, splitInRow=1)
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeede8")),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#777777")),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#dddddd")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        content.append(table)
    else:
        content.append(paragraph("No items in this cycle."))
    content.append(paragraph("Activity references", subheading))
    for event in report.audit:
        content.append(
            paragraph(
                f"{event['at']} | {event['event']} | Actor {event['actor']} | "
                f"Revision {event['revision']} | Audit {event['id']}"
            )
        )

    def footer(canvas, document):
        canvas.setFont("ReviewSans", 8)
        canvas.drawRightString(A4[0] - 32, 20, f"Page {document.page}")

    try:
        doc.build(content, onFirstPage=footer, onLaterPages=footer)
    except Exception:
        # ReportLab errors can include free-form paragraph content; never log it.
        raise Denied("pdf_generation_failed", 422) from None
    if len(output.getvalue()) > 10 * 1024 * 1024:
        raise Denied("export_too_large", 413)
    return output.getvalue()

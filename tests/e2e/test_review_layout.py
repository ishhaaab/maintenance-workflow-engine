"""Real browser layout for maximum valid display text, using shipped templates/CSS."""

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.browser]


def test_maximum_length_cycle_title_and_login_do_not_overflow_mobile():
    if os.environ.get("RUN_BROWSER") != "1":
        pytest.skip("Set RUN_BROWSER=1 for real browser layout verification")
    from playwright.sync_api import sync_playwright

    from workflow_engine.web import templates

    html = templates.env.get_template("cycle.html").render(
        title="T" * 160,
        actor=SimpleNamespace(role="ADMIN"),
        profile={"login": "l" * 100, "tenant": "t" * 80},
        csrf="0" * 64,
        request=SimpleNamespace(query_params={}),
        members=[],
        error=None,
        cycle={
            "id": "synthetic-layout",
            "title": "T" * 160,
            "state": "DRAFT",
            "revision": 0,
            "items": [],
            "requesters": [],
            "next_offset": None,
        },
        clock=datetime.now(UTC),
    )
    from workflow_engine import web

    css = (Path(web.__file__).parent / "static/app.css").read_text(encoding="utf-8")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 360, "height": 800})
            page.set_content(html)
            page.add_style_tag(content=css)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        finally:
            browser.close()

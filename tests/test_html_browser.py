"""Real-browser checks, enabled by the dedicated Browser review workflow."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opencollate.reporters.html import render_html

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENCOLLATE_BROWSER_TESTS") != "1",
    reason="run in dedicated browser job with Playwright and Chromium",
)


def _report(path: Path) -> str:
    rows = [
        {
            "code": "OC4001",
            "severity": "warning" if i % 2 else "error",
            "message": f"needle-{i:03} </script><script>window.compromised=true;</script>",
            "fingerprint": f"row-{i:03}",
            "evidence": [{"view": "rtl.default", "value": i}],
        }
        for i in range(130)
    ]
    path.write_text(
        render_html({"project": "Browser corpus", "diagnostics": rows}), encoding="utf-8"
    )
    return path.read_text(encoding="utf-8")


def test_csp_pagination_filters_and_no_external_requests(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("OPENCOLLATE_CHROMIUM"))
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        requests, errors = [], []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(_report(tmp_path / "report.html"))
        assert page.locator(".finding:visible").count() == 50
        page.locator("#next").click()
        assert "page 2 of 3" in page.locator("#counter").inner_text()
        page.locator("#next").click()
        assert page.locator(".finding:visible").count() == 30
        page.locator("#previous").click()
        assert page.locator(".finding:visible").count() == 50
        page.locator("#search").fill("needle-117")
        assert page.locator(".finding:visible").count() == 1
        page.locator("#severity").select_option("error")
        assert page.locator(".finding:visible").count() == 0
        assert page.locator("#empty").is_visible()
        page.locator("#reset").click()
        assert page.locator(".finding:visible").count() == 50
        assert page.evaluate("typeof window.compromised") == "undefined"
        assert not errors
        assert requests == []
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()


def test_evidence_remains_available_without_javascript(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("OPENCOLLATE_CHROMIUM"))
        page = browser.new_page(java_script_enabled=False)
        page.set_content(_report(tmp_path / "report.html"))
        assert page.locator(".finding:visible").count() == 130
        card = page.locator(".finding").first
        card.locator("summary").click()
        assert card.locator("pre").is_visible()
        browser.close()

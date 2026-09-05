from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from opencollate.reporters.html import _hash, render_html


def report() -> dict:
    return {
        "schema_version": 1,
        "project": "example",
        "exit_code": 2,
        "diagnostics": [
            {
                "code": "OC1104",
                "severity": "warning",
                "message": "Incomplete source",
                "fingerprint": "abcdef",
                "evidence": [
                    {
                        "view": "rtl.default",
                        "value": "tainted",
                        "location": {"path": "core.sv", "line": 6},
                    }
                ],
            },
            {
                "code": "OC4001",
                "severity": "error",
                "message": "Waived mismatch",
                "waived": True,
                "waiver_reason": "Reviewed wrapper",
                "evidence": [],
            },
        ],
    }


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def test_offline_csp_and_escaped_untrusted_evidence() -> None:
    value = report()
    attack = '</script><img src="https://evil.invalid/x" onerror="alert(1)">'
    value["project"] = attack
    for field in ("code", "message", "help", "fingerprint"):
        value["diagnostics"][0][field] = attack
    value["diagnostics"][0]["metadata"] = {attack: attack}
    output = render_html(value)
    tags = Tags()
    tags.feed(output)
    assert len([t for t, _ in tags.tags if t == "script"]) == 1
    assert not [t for t, _ in tags.tags if t in {"img", "iframe", "link", "object"}]
    assert not [
        attrs
        for _, attrs in tags.tags
        if any(k.startswith("on") or k in {"src", "href"} for k in attrs)
    ]
    assert html.escape(attack, quote=True) in output
    csp = next(
        attrs["content"]
        for tag, attrs in tags.tags
        if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"
    )
    for tag in ("script", "style"):
        content = re.search(f"<{tag}>(.*?)</{tag}>", output, re.S).group(1)
        assert f"sha256-{_hash(content)}" in csp
    assert "default-src 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


def test_evidence_waivers_and_incompleteness_are_visible() -> None:
    output = render_html(report())
    assert "Analysis incomplete" in output
    assert "core.sv" in output
    assert "Reviewed wrapper" in output
    assert 'data-state="waived"' in output
    assert "<details>" in output and "<noscript>" in output
    assert output == render_html(report())


def test_diff_rows_include_before_after_and_review_state() -> None:
    item = report()["diagnostics"][0]
    output = render_html(
        {
            "findings": [
                {
                    "state": "changed",
                    "baseline": item,
                    "current": {**item, "message": "Fixed elsewhere"},
                }
            ]
        }
    )
    assert 'data-state="changed"' in output
    assert "Fixed elsewhere" in output and "Incomplete source" in output


def test_empty_report_is_not_signoff_claim() -> None:
    output = render_html({"project": "empty", "exit_code": 0, "diagnostics": []})
    assert "not proof" in output
    assert "0</strong> recorded findings" in output

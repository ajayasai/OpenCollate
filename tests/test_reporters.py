from __future__ import annotations

import json

import pytest

from opencollate.reporters import (
    render_json,
    render_markdown,
    render_sarif,
    render_text,
    sarif_dict,
)
from opencollate.reporters.common import report_dict


def sample_report() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "tool": {"name": "OpenCollate", "version": "0.1.0"},
        "project": "uart-demo",
        "summary": {
            "errors": 1,
            "warnings": 1,
            "notes": 0,
            "suppressed": 1,
            "views": 2,
            "components": 1,
            "ports": 3,
        },
        "diagnostics": [
            {
                "code": "OC4101",
                "name": "width-mismatch",
                "severity": "error",
                "message": "uart/irq is 1 bit in RTL but 4 bits in Liberty.",
                "entity_id": "component:uart/port:irq",
                "property": "shape.width",
                "fingerprint": "abc123",
                "help": "Check both declarations.",
                "evidence": [
                    {
                        "view": "rtl",
                        "value": 1,
                        "location": {"path": "rtl\\uart.sv", "line": 8, "column": 3},
                    },
                    {
                        "view": "liberty",
                        "value": 4,
                        "location": {"path": "lib/uart.lib", "line": 22, "column": 5},
                    },
                ],
            },
            {
                "code": "OC1102",
                "severity": "warning",
                "message": "One construct was not checkable.",
                "location": {"path": "rtl/uart.sv", "line": 12, "column": 1},
                "fingerprint": "def456",
            },
            {
                "code": "OC3101",
                "severity": "error",
                "message": "Suppressed finding.",
                "suppressed": True,
                "fingerprint": "suppressed",
            },
        ],
    }


def test_json_report_is_deterministic_and_unicode_safe() -> None:
    first = render_json(sample_report())
    second = render_json(sample_report())
    assert first == second
    assert json.loads(first)["project"] == "uart-demo"
    assert first.endswith("\n")


def test_compact_json() -> None:
    value = render_json(sample_report(), pretty=False)
    assert "\n" not in value[:-1]


def test_text_report_prioritizes_actionable_evidence() -> None:
    value = render_text(sample_report(), verbose=True)
    assert "ERROR OC4101" in value
    assert "rtl\\uart.sv:8:3 [rtl] = 1" in value
    assert "help: Check both declarations." in value
    assert "fingerprint: abc123" in value
    assert "FAIL: 1 error(s), 1 warning(s), 0 note(s)" in value
    assert "Suppressed by waiver: 1" in value
    assert "Suppressed finding" not in value


def test_sarif_has_rules_locations_and_fingerprints() -> None:
    value = sarif_dict(sample_report())
    run = value["runs"][0]
    assert value["version"] == "2.1.0"
    assert [rule["id"] for rule in run["tool"]["driver"]["rules"]] == ["OC1102", "OC4101"]
    assert run["results"][0]["partialFingerprints"]["opencollate/v1"] == "abc123"
    artifact = run["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]
    assert artifact["uri"] == "rtl/uart.sv"
    assert json.loads(render_sarif(sample_report())) == value


def test_markdown_is_pr_friendly() -> None:
    value = render_markdown(sample_report())
    assert "# OpenCollate report" in value
    assert "**Failed**" in value
    assert "`OC4101`" in value


def test_human_reports_honor_failed_status_when_warnings_are_denied() -> None:
    report = sample_report()
    report["status"] = "fail"
    report["exit_code"] = 1
    report["summary"] = {
        "errors": 0,
        "warnings": 1,
        "notes": 0,
        "suppressed": 0,
    }
    report["diagnostics"] = [
        {
            "code": "OC1102",
            "severity": "warning",
            "message": "Denied warning.",
            "fingerprint": "warning-only",
        }
    ]

    assert "FAIL: 0 error(s), 1 warning(s)" in render_text(report)
    assert "**Failed** — 0 errors, 1 warnings." in render_markdown(report)


def test_report_dict_accepts_to_dict_object() -> None:
    class Result:
        def to_dict(self) -> dict[str, object]:
            return {"answer": 42}

    assert report_dict(Result()) == {"answer": 42}


def test_report_dict_rejects_unknown_object() -> None:
    with pytest.raises(TypeError, match="to_dict"):
        report_dict(object())

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

import pytest

from opencollate.baseline import (
    MAX_REPORT_JSON_NESTING,
    BaselineReportError,
    FindingState,
    canonical_content_digest,
    diff_reports,
)


def _finding(
    fingerprint: str,
    value: object,
    *,
    code: str = "OC4101",
    severity: str = "error",
    waived: bool = False,
    suppressed: bool | None = None,
    message: str = "Port width differs across views.",
    path: str = "rtl/top.sv",
    line: int = 7,
    evidence_order: tuple[str, ...] = ("rtl.default", "liberty.tt"),
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
        "fingerprint": fingerprint,
        "waived": waived,
        "suppressed": waived if suppressed is None else suppressed,
        "object": {"kind": "port", "id": "port:uart/data", "display": "uart/data"},
        "entity_id": "port:uart/data",
        "property": "width",
        "help": "Align the participating views.",
        "location": {"path": path, "line": line, "column": 3},
        "evidence": [
            {
                "view": view,
                "value": value if index == 0 else 8,
                "location": {
                    "path": path if index == 0 else "lib/uart.lib",
                    "line": line + index,
                    "column": 3,
                },
            }
            for index, view in enumerate(evidence_order)
        ],
    }
    if waived or finding["suppressed"]:
        finding["waiver_reason"] = "Reviewed compatibility exception."
    return finding


def _report(*findings: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "tool": {"name": "OpenCollate", "version": "0.2.1"},
        "project": "uart",
        "status": "fail" if findings else "pass",
        "exit_code": 1 if findings else 0,
        "summary": {
            "errors": len(findings),
            "warnings": 0,
            "notes": 0,
            "suppressed": 0,
            "views": 1,
            "components": 1,
            "ports": 1,
            "registers": 0,
        },
        "diagnostics": [dict(item) for item in findings],
    }


def _states(result: object) -> list[str]:
    serialized = result.to_dict()  # type: ignore[attr-defined]
    return [str(item["state"]) for item in serialized["findings"]]


def test_content_digest_ignores_source_movement_and_evidence_order() -> None:
    first = _finding("same", 7)
    moved = _finding(
        "different-identity-is-ignored-by-content-digest",
        7,
        path="generated/relocated.sv",
        line=900,
        evidence_order=("liberty.tt", "rtl.default"),
    )
    # Keep the same view/value association while reversing report order.
    moved["evidence"][0]["value"] = 8
    moved["evidence"][1]["value"] = 7

    assert canonical_content_digest(first) == canonical_content_digest(moved)


def test_content_digest_changes_for_value_severity_and_suppression() -> None:
    original = _finding("same", 7)
    value_changed = _finding("same", 6)
    severity_changed = _finding("same", 7, severity="warning")
    suppressed = _finding("same", 7, waived=True)

    digests = {
        canonical_content_digest(item)
        for item in (original, value_changed, severity_changed, suppressed)
    }
    assert len(digests) == 4


def test_diff_classifies_new_changed_unchanged_and_resolved() -> None:
    baseline = _report(
        _finding("unchanged", 7),
        _finding("changed", 6),
        _finding("resolved", 5),
    )
    current = _report(
        _finding("new", 4),
        _finding("unchanged", 7, path="moved/top.sv", line=77),
        _finding("changed", 3),
    )

    result = diff_reports(baseline, current)

    assert result.summary.to_dict() == {
        "baseline": 3,
        "current": 3,
        "new": 1,
        "changed": 1,
        "unchanged": 1,
        "resolved": 1,
        "current_active": 3,
        "current_suppressed": 0,
        "current_fatal": 0,
        "current_active_fatal": 0,
    }
    assert _states(result) == ["new", "changed", "unchanged", "resolved"]
    changed = next(item for item in result.findings if item.state == FindingState.CHANGED)
    assert changed.baseline_content_digest != changed.current_content_digest


def test_duplicate_fingerprints_are_compared_as_multisets() -> None:
    baseline = _report(
        _finding("duplicate", "A"),
        _finding("duplicate", "A"),
        _finding("duplicate", "B"),
    )
    current = _report(
        _finding("duplicate", "C"),
        _finding("duplicate", "B"),
        _finding("duplicate", "B"),
        _finding("duplicate", "A"),
    )

    result = diff_reports(baseline, current)

    assert result.summary.unchanged == 2
    assert result.summary.changed == 1
    assert result.summary.new == 1
    assert result.summary.resolved == 0
    assert result.summary.baseline == 3
    assert result.summary.current == 4


def test_exact_duplicate_matches_are_consumed_before_changed_pairs() -> None:
    baseline = _report(_finding("duplicate", 1), _finding("duplicate", 2))
    current = _report(_finding("duplicate", 2), _finding("duplicate", 3))

    result = diff_reports(baseline, current)

    unchanged = next(item for item in result.findings if item.state == FindingState.UNCHANGED)
    changed = next(item for item in result.findings if item.state == FindingState.CHANGED)
    assert unchanged.current is not None
    assert unchanged.current["evidence"][0]["value"] == 2
    assert changed.baseline is not None and changed.current is not None
    assert changed.baseline["evidence"][0]["value"] == 1
    assert changed.current["evidence"][0]["value"] == 3


def test_waived_and_suppressed_transitions_are_changed() -> None:
    baseline = _report(_finding("waiver", 7, waived=True))
    current = _report(_finding("waiver", 7, waived=False))

    result = diff_reports(baseline, current)

    assert result.summary.changed == 1
    assert result.summary.current_active == 1
    assert result.summary.current_suppressed == 0
    assert not result.findings[0].current_suppressed


def test_waived_and_suppressed_aliases_have_one_effective_state() -> None:
    waived = _finding("waiver", 7, waived=True, suppressed=False)
    suppressed = _finding("waiver", 7, waived=False, suppressed=True)
    suppressed["waiver_reason"] = waived["waiver_reason"]

    assert canonical_content_digest(waived) == canonical_content_digest(suppressed)
    assert diff_reports(_report(waived), _report(suppressed)).summary.unchanged == 1


def test_current_fatals_remain_visible_in_every_baseline_state() -> None:
    existing = _finding("existing-fatal", 7, severity="fatal")
    new_suppressed = _finding("new-fatal", 8, severity="fatal", waived=True)

    result = diff_reports(_report(existing), _report(existing, new_suppressed))

    assert result.has_current_fatal
    assert result.summary.current_fatal == 2
    assert result.summary.current_active_fatal == 1
    fatal_entries = [item for item in result.findings if item.current_fatal]
    assert {item.state for item in fatal_entries} == {
        FindingState.NEW,
        FindingState.UNCHANGED,
    }
    assert all(item.to_dict()["current"]["severity"] == "fatal" for item in fatal_entries)


def test_order_does_not_change_serialized_diff_and_inputs_are_not_mutated() -> None:
    baseline_findings = [_finding("b", 1), _finding("a", 2), _finding("a", 3)]
    current_findings = [_finding("c", 4), _finding("a", 3), _finding("a", 5)]
    baseline = _report(*baseline_findings)
    current = _report(*current_findings)
    before = copy.deepcopy((baseline, current))

    forward = diff_reports(baseline, current).to_dict()
    reversed_order = diff_reports(
        _report(*reversed(baseline_findings)),
        _report(*reversed(current_findings)),
    ).to_dict()

    assert forward == reversed_order
    assert (baseline, current) == before
    assert json.loads(json.dumps(forward, sort_keys=True)) == forward


@pytest.mark.parametrize(
    ("baseline", "message"),
    [
        ({"diagnostics": []}, "missing required field"),
        ({**_report(), "schema_version": 2}, "schema_version"),
        ({**_report(), "diagnostics": {}}, "diagnostics"),
        ({**_report(), "diagnostics": [None]}, "must be an object"),
        (
            {**_report(), "diagnostics": [{"fingerprint": "only"}]},
            "missing required field",
        ),
        (
            _report({**_finding("bad", 1), "severity": "panic"}),
            ".severity",
        ),
        (
            _report({**_finding("bad", 1), "waived": "yes"}),
            ".waived",
        ),
        (
            _report({**_finding("bad", 1), "metadata": {"score": float("nan")}}),
            "non-finite",
        ),
        (
            _report({**_finding("bad", 1), "metadata": {"bad": {1, 2}}}),
            "non-JSON",
        ),
        ({**_report(), "unexpected": True}, "unknown field"),
        (
            _report({**_finding("bad", 1), "evidence": [{}]}),
            "missing required field",
        ),
        (
            _report({**_finding("bad", 1), "object": {}}),
            "missing required field",
        ),
    ],
)
def test_malformed_reports_are_rejected(baseline: object, message: str) -> None:
    with pytest.raises(BaselineReportError, match=message):
        diff_reports(baseline, _report())  # type: ignore[arg-type]


def test_non_mapping_report_is_rejected() -> None:
    with pytest.raises(BaselineReportError, match="baseline report must be an object"):
        diff_reports(None, _report())  # type: ignore[arg-type]


@pytest.mark.parametrize("cyclic", [False, True])
def test_nested_and_cyclic_programmatic_reports_fail_at_deterministic_depth(
    cyclic: bool,
) -> None:
    if cyclic:
        nested: list[Any] = []
        nested.append(nested)
    else:
        value: Any = 0
        for _ in range(MAX_REPORT_JSON_NESTING + 1):
            value = [value]
        nested = value
    finding = _finding("nested", 1)
    finding["metadata"] = nested

    with pytest.raises(BaselineReportError, match="JSON nesting limit"):
        diff_reports(_report(finding), _report())

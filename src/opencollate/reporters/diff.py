"""Render deterministic report-baseline comparisons for humans and CI systems."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from opencollate import __version__
from opencollate.baseline import FindingDelta, FindingState, ReportDiff


def _finding(delta: FindingDelta) -> Mapping[str, Any]:
    value = delta.current if delta.current is not None else delta.baseline
    return value or {}


def _object_name(finding: Mapping[str, Any]) -> str:
    value = finding.get("object")
    if isinstance(value, Mapping):
        return str(value.get("display") or value.get("id") or "-")
    return str(finding.get("entity_id") or "-")


def _visible(diff: ReportDiff, *, include_unchanged: bool) -> tuple[FindingDelta, ...]:
    if include_unchanged:
        return diff.findings
    return tuple(item for item in diff.findings if item.state != FindingState.UNCHANGED)


def render_diff_json(diff: ReportDiff) -> str:
    """Return the complete machine-readable diff artifact."""

    return json.dumps(diff.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_diff_text(diff: ReportDiff, *, include_unchanged: bool = False) -> str:
    """Return a compact terminal review with stable ordering."""

    summary = diff.summary
    lines = [
        "OpenCollate report diff",
        (
            f"Baseline {summary.baseline} -> current {summary.current}: "
            f"{summary.new} new, {summary.changed} changed, "
            f"{summary.resolved} resolved, {summary.unchanged} unchanged"
        ),
    ]
    values = _visible(diff, include_unchanged=include_unchanged)
    if not values:
        lines.append("No report changes.")
        return "\n".join(lines) + "\n"
    for delta in values:
        finding = _finding(delta)
        severity = str(finding.get("severity", "info")).upper()
        code = str(finding.get("code", "OC????"))
        message = str(finding.get("message", ""))
        lines.append(
            f"{delta.state.value.upper():9} {severity:7} {code}  "
            f"{_object_name(finding)} — {message}"
        )
        if delta.state == FindingState.CHANGED:
            lines.append(
                "  content: "
                f"{(delta.baseline_content_digest or '')[:12]} -> "
                f"{(delta.current_content_digest or '')[:12]}"
            )
    return "\n".join(lines) + "\n"


def _markdown(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def render_diff_markdown(diff: ReportDiff, *, include_unchanged: bool = False) -> str:
    """Return a pull-request-friendly Markdown summary."""

    summary = diff.summary
    lines = [
        "# OpenCollate report diff",
        "",
        (
            f"**{summary.new} new · {summary.changed} changed · "
            f"{summary.resolved} resolved · {summary.unchanged} unchanged**"
        ),
        "",
        "| State | Severity | Rule | Object | Message |",
        "| --- | --- | --- | --- | --- |",
    ]
    values = _visible(diff, include_unchanged=include_unchanged)
    if not values:
        lines.append("| unchanged | — | — | — | No report changes. |")
    for delta in values:
        finding = _finding(delta)
        lines.append(
            "| "
            + " | ".join(
                (
                    delta.state.value,
                    _markdown(finding.get("severity", "info")),
                    f"`{_markdown(finding.get('code', 'OC????'))}`",
                    _markdown(_object_name(finding)),
                    _markdown(finding.get("message", "")),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            (
                f"Baseline findings: {summary.baseline}; current findings: {summary.current}; "
                f"active current findings: {summary.current_active}."
            ),
        )
    )
    return "\n".join(lines) + "\n"


def _sarif_location(finding: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = finding.get("location")
    if not isinstance(raw, Mapping):
        evidence = finding.get("evidence", [])
        if isinstance(evidence, list):
            raw = next(
                (
                    item.get("location")
                    for item in evidence
                    if isinstance(item, Mapping) and isinstance(item.get("location"), Mapping)
                ),
                None,
            )
    if not isinstance(raw, Mapping) or not raw.get("path"):
        return []
    region: dict[str, int] = {}
    for source, target in (
        ("line", "startLine"),
        ("column", "startColumn"),
        ("end_line", "endLine"),
        ("end_column", "endColumn"),
    ):
        value = raw.get(source)
        if isinstance(value, int) and value > 0:
            region[target] = value
    physical: dict[str, Any] = {"artifactLocation": {"uri": str(raw["path"]).replace("\\", "/")}}
    if region:
        physical["region"] = region
    return [{"physicalLocation": physical}]


def render_diff_sarif(diff: ReportDiff, *, include_unchanged: bool = True) -> str:
    """Render SARIF 2.1.0 with baselineState for code-scanning review."""

    state = {
        FindingState.NEW: "new",
        FindingState.CHANGED: "updated",
        FindingState.UNCHANGED: "unchanged",
        FindingState.RESOLVED: "absent",
    }
    level = {"fatal": "error", "error": "error", "warning": "warning", "info": "note"}
    values = _visible(diff, include_unchanged=include_unchanged)
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for delta in values:
        finding = _finding(delta)
        code = str(finding.get("code", "OC0000"))
        rules.setdefault(code, {"id": code, "name": code})
        result: dict[str, Any] = {
            "ruleId": code,
            "level": level.get(str(finding.get("severity", "info")), "note"),
            "message": {"text": str(finding.get("message", ""))},
            "baselineState": state[delta.state],
            "partialFingerprints": {"opencollateFingerprint": delta.fingerprint},
            "properties": {
                "state": delta.state.value,
                "baselineContentDigest": delta.baseline_content_digest,
                "currentContentDigest": delta.current_content_digest,
            },
        }
        locations = _sarif_location(finding)
        if locations:
            result["locations"] = locations
        if bool(finding.get("waived", False) or finding.get("suppressed", False)):
            suppression: dict[str, str] = {"kind": "external"}
            if finding.get("waiver_reason"):
                suppression["justification"] = str(finding["waiver_reason"])
            result["suppressions"] = [suppression]
        results.append(result)
    document = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "OpenCollate",
                        "version": __version__,
                        "informationUri": "https://github.com/ajayasai/OpenCollate",
                        "rules": [rules[key] for key in sorted(rules)],
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": not diff.has_current_fatal,
                        "properties": {"reportDiffSummary": diff.summary.to_dict()},
                    }
                ],
                "results": results,
            }
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


__all__ = [
    "render_diff_json",
    "render_diff_markdown",
    "render_diff_sarif",
    "render_diff_text",
]

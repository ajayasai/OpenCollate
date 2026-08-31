"""Portable Markdown reports for pull requests and build summaries."""

from __future__ import annotations

from collections.abc import Mapping

from opencollate.reporters.common import diagnostics, report_dict, report_failed


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(result: object) -> str:
    report = report_dict(result)
    findings = diagnostics(report)
    summary = report.get("summary", {})
    errors = int(summary.get("errors", 0) or 0) if isinstance(summary, Mapping) else 0
    warnings = int(summary.get("warnings", 0) or 0) if isinstance(summary, Mapping) else 0
    status = "Failed" if report_failed(report, fallback_errors=errors) else "Passed"
    lines = [
        "# OpenCollate report",
        "",
        f"**{status}** — {errors} errors, {warnings} warnings.",
    ]
    if findings:
        lines.extend(("", "| Severity | Rule | Finding |", "|---|---|---|"))
        for finding in findings:
            lines.append(
                f"| {_escape(finding.get('severity', 'note'))} | "
                f"`{_escape(finding.get('code', finding.get('rule_id', 'OC0000')))}` | "
                f"{_escape(finding.get('message', ''))} |"
            )
    else:
        lines.extend(("", "No inconsistencies were found in the participating views."))
    return "\n".join(lines) + "\n"

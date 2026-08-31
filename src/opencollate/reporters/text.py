"""Plain-English terminal report rendering."""

from __future__ import annotations

from collections.abc import Mapping

from opencollate.reporters.common import diagnostics, report_dict, report_failed


def _location(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    path = str(value.get("path", ""))
    line = int(value.get("line", 0) or 0)
    column = int(value.get("column", 0) or 0)
    if not path:
        return ""
    if line <= 0:
        return path
    return f"{path}:{line}:{max(column, 1)}"


def _counts(
    report: Mapping[str, object], findings: list[dict[str, object]]
) -> tuple[int, int, int, int]:
    summary = report.get("summary")
    if isinstance(summary, Mapping):
        return (
            int(summary.get("fatal", 0) or 0) + int(summary.get("errors", 0) or 0),
            int(summary.get("warnings", 0) or 0),
            int(summary.get("notes", summary.get("info", 0)) or 0),
            int(summary.get("suppressed", summary.get("waived", 0)) or 0),
        )
    return (
        sum(item.get("severity") in {"fatal", "error"} for item in findings),
        sum(item.get("severity") == "warning" for item in findings),
        sum(item.get("severity") in {"info", "note"} for item in findings),
        0,
    )


def render_text(result: object, *, verbose: bool = False) -> str:
    """Render actionable diagnostics without ANSI escape sequences."""

    report = report_dict(result)
    findings = diagnostics(report)
    tool = report.get("tool", {})
    version = tool.get("version", "unknown") if isinstance(tool, Mapping) else "unknown"
    lines = [f"OpenCollate {version}"]
    project = report.get("project")
    if project:
        lines.append(f"Project: {project}")

    for finding in findings:
        severity = str(finding.get("severity", "note")).upper()
        code = str(finding.get("code", finding.get("rule_id", "OC0000")))
        message = str(finding.get("message", ""))
        lines.extend(("", f"{severity} {code}  {message}"))

        evidence = finding.get("evidence", [])
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, Mapping):
                    continue
                where = _location(item.get("location", item.get("span")))
                view = str(item.get("view", ""))
                value = item.get("value")
                prefix = f"  --> {where}" if where else "  -->"
                detail = f" [{view}]" if view else ""
                if value is not None:
                    detail += f" = {value}"
                lines.append(prefix + detail)

        location = _location(finding.get("location"))
        if location and not evidence:
            lines.append(f"  --> {location}")
        help_text = finding.get("help", finding.get("suggestion"))
        if help_text:
            lines.append(f"  help: {help_text}")
        if verbose and finding.get("fingerprint"):
            lines.append(f"  fingerprint: {finding['fingerprint']}")

    errors, warnings, notes, suppressed = _counts(report, findings)
    status = "FAIL" if report_failed(report, fallback_errors=errors) else "PASS"
    lines.extend(("", f"{status}: {errors} error(s), {warnings} warning(s), {notes} note(s)"))
    if suppressed:
        lines.append(f"Suppressed by waiver: {suppressed}")
    checked = report.get("summary")
    if isinstance(checked, Mapping):
        views = checked.get("views")
        components = checked.get("components")
        ports = checked.get("ports")
        if views is not None or components is not None or ports is not None:
            lines.append(
                "Checked: "
                f"{int(views or 0)} view(s), {int(components or 0)} component(s), "
                f"{int(ports or 0)} port(s)"
            )
    return "\n".join(lines) + "\n"

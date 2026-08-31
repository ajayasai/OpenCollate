"""Shared helpers for rendering stable OpenCollate reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def report_dict(result: object) -> dict[str, Any]:
    """Return a plain dictionary for an audit result or mapping."""

    if isinstance(result, Mapping):
        return {str(key): value for key, value in result.items()}
    converter = getattr(result, "to_dict", None)
    if callable(converter):
        value = converter()
        if isinstance(value, Mapping):
            return {str(key): item for key, item in value.items()}
    raise TypeError("reporters require a mapping or an object with to_dict()")


def diagnostic_sort_key(item: Mapping[str, Any]) -> tuple[object, ...]:
    """Sort findings without depending on source discovery order."""

    ranks = {
        "fatal": 0,
        "error": 1,
        "warning": 2,
        "info": 3,
        "note": 3,
        "none": 4,
    }
    location = item.get("location") or {}
    if not isinstance(location, Mapping):
        location = {}
    return (
        ranks.get(str(item.get("severity", "note")), 9),
        str(item.get("code", item.get("rule_id", ""))),
        str(item.get("entity_id", item.get("object", ""))),
        str(location.get("path", "")),
        int(location.get("line", 0) or 0),
        str(item.get("message", "")),
    )


def report_failed(report: Mapping[str, Any], *, fallback_errors: int = 0) -> bool:
    """Use the engine's status contract before inferring from summary counts."""

    exit_code = report.get("exit_code")
    if isinstance(exit_code, int):
        return exit_code != 0
    status = str(report.get("status", "")).strip().lower()
    if status in {"pass", "fail"}:
        return status == "fail"
    return fallback_errors > 0


def diagnostics(
    report: Mapping[str, Any], *, include_suppressed: bool = False
) -> list[dict[str, Any]]:
    values = report.get("diagnostics", [])
    if not isinstance(values, list):
        return []
    findings = [dict(item) for item in values if isinstance(item, Mapping)]
    if not include_suppressed:
        findings = [
            item
            for item in findings
            if not bool(item.get("suppressed", False)) and not bool(item.get("waived", False))
        ]
    return sorted(findings, key=diagnostic_sort_key)

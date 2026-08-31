"""SARIF 2.1.0 output for GitHub code scanning and other CI systems."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

from opencollate.reporters.common import diagnostics, report_dict


def _uri(path: str) -> str:
    normalized = path.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[2:].lstrip("/")
    try:
        return PurePath(normalized).as_posix()
    except ValueError:
        return normalized


def _physical_location(location: object) -> dict[str, Any] | None:
    if not isinstance(location, Mapping) or not location.get("path"):
        return None
    region = {
        "startLine": max(1, int(location.get("line", 1) or 1)),
        "startColumn": max(1, int(location.get("column", 1) or 1)),
    }
    if location.get("end_line"):
        region["endLine"] = max(region["startLine"], int(location["end_line"]))
    if location.get("end_column"):
        region["endColumn"] = max(1, int(location["end_column"]))
    return {"artifactLocation": {"uri": _uri(str(location["path"]))}, "region": region}


def sarif_dict(result: object) -> dict[str, Any]:
    report = report_dict(result)
    findings = diagnostics(report)
    rule_map: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for finding in findings:
        code = str(finding.get("code", finding.get("rule_id", "OC0000")))
        rule_map.setdefault(
            code,
            {
                "id": code,
                "name": str(finding.get("name", code)),
                "shortDescription": {
                    "text": str(finding.get("title", finding.get("message", code)))
                },
                "help": {"text": str(finding.get("help", finding.get("suggestion", "")))},
                "properties": {"tags": ["hardware", "eda", "consistency"]},
            },
        )
        level = {
            "fatal": "error",
            "error": "error",
            "warning": "warning",
            "info": "note",
            "note": "note",
        }.get(str(finding.get("severity", "warning")), "warning")
        diagnostic_object = finding.get("object")
        if isinstance(diagnostic_object, Mapping):
            entity_id = diagnostic_object.get("id")
        else:
            entity_id = finding.get("entity_id", diagnostic_object)
        entry: dict[str, Any] = {
            "ruleId": code,
            "level": level,
            "message": {"text": str(finding.get("message", ""))},
            "partialFingerprints": {
                "opencollate/v1": str(finding.get("fingerprint", "")),
            },
            "properties": {
                "entityId": entity_id,
                "property": finding.get("property"),
            },
        }
        primary = _physical_location(finding.get("location"))
        evidence = finding.get("evidence", [])
        related: list[dict[str, Any]] = []
        if isinstance(evidence, list):
            for index, item in enumerate(evidence, start=1):
                if not isinstance(item, Mapping):
                    continue
                physical = _physical_location(item.get("location", item.get("span")))
                if physical is None:
                    continue
                if primary is None:
                    primary = physical
                related.append(
                    {
                        "id": index,
                        "physicalLocation": physical,
                        "message": {
                            "text": f"{item.get('view', 'view')}: {item.get('value', 'observed')}"
                        },
                    }
                )
        if primary is not None:
            entry["locations"] = [{"physicalLocation": primary}]
        if related:
            entry["relatedLocations"] = related
        results.append(entry)

    tool = report.get("tool")
    version = tool.get("version", "unknown") if isinstance(tool, Mapping) else "unknown"
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "OpenCollate",
                        "informationUri": "https://github.com/ajayasai/OpenCollate",
                        "semanticVersion": str(version),
                        "rules": [rule_map[key] for key in sorted(rule_map)],
                    }
                },
                "results": results,
            }
        ],
    }


def render_sarif(result: object, *, pretty: bool = True) -> str:
    return (
        json.dumps(
            sarif_dict(result),
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            sort_keys=True,
        )
        + "\n"
    )

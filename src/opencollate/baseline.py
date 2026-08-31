"""Deterministic comparison of OpenCollate report snapshots.

The report-v1 diagnostic fingerprint is an issue identity: it intentionally
does not contain source locations or observed values.  Baseline comparison
therefore uses that fingerprint to group findings and a separate canonical
content digest to decide whether an occurrence is unchanged or changed.

This module does not alter check status or suppress findings.  In particular,
current fatal diagnostics remain present and are counted independently of
their baseline state so that a caller cannot accidentally ratchet them away.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_DIAGNOSTIC_CODE = re.compile(r"^OC[0-9]{4}$")
_SEVERITIES = frozenset({"fatal", "error", "warning", "info"})
_REPORT_KEYS = frozenset(
    {"schema_version", "tool", "project", "status", "exit_code", "summary", "diagnostics"}
)
_SUMMARY_KEYS = frozenset(
    {"errors", "warnings", "notes", "suppressed", "views", "components", "ports", "registers"}
)
_DIAGNOSTIC_REQUIRED = frozenset(
    {"code", "severity", "message", "fingerprint", "waived", "evidence"}
)
_DIAGNOSTIC_KEYS = _DIAGNOSTIC_REQUIRED | frozenset(
    {
        "suppressed",
        "entity_id",
        "waiver_reason",
        "property",
        "help",
        "location",
        "object",
        "metadata",
    }
)
_LOCATION_REQUIRED = frozenset({"path", "line", "column"})
_LOCATION_KEYS = _LOCATION_REQUIRED | frozenset({"end_line", "end_column", "view", "raw_name"})
_EVIDENCE_REQUIRED = frozenset({"view", "value"})
_EVIDENCE_KEYS = _EVIDENCE_REQUIRED | frozenset({"location", "native_name", "label"})
MAX_REPORT_JSON_BYTES = 256 * 1024 * 1024
MAX_REPORT_JSON_NESTING = 128


class BaselineReportError(ValueError):
    """A report cannot be compared safely as an OpenCollate v1 report."""


class FindingState(StrEnum):
    """Relationship between one baseline and/or current finding occurrence."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    RESOLVED = "resolved"


def _json_value(value: Any, *, where: str, depth: int = 0) -> Any:
    """Return a detached, canonicalizable JSON value or raise a useful error."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BaselineReportError(f"{where} contains a non-finite number")
        return value
    if depth >= MAX_REPORT_JSON_NESTING:
        raise BaselineReportError(
            f"{where} exceeds the JSON nesting limit of {MAX_REPORT_JSON_NESTING}"
        )
    if isinstance(value, list):
        return [
            _json_value(item, where=f"{where}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise BaselineReportError(f"{where} contains a non-string object key")
            result[key] = _json_value(item, where=f"{where}.{key}", depth=depth + 1)
        return {key: result[key] for key in sorted(result)}
    raise BaselineReportError(f"{where} contains non-JSON value of type {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _effective_suppression(finding: Mapping[str, Any]) -> bool:
    # Treat the report-v1 `waived` spelling and the reporter-facing
    # `suppressed` alias conservatively.  A contradictory external report must
    # not turn a suppressed finding into an active one merely through field
    # precedence.
    return bool(finding.get("waived", False) or finding.get("suppressed", False))


def _validate_keys(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    where: str,
) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise BaselineReportError(f"{where} is missing required field(s): {', '.join(missing)}")
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise BaselineReportError(f"{where} contains unknown field(s): {', '.join(unknown)}")


def _validate_location(value: object, *, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BaselineReportError(f"{where} must be an object")
    location = _json_value(value, where=where)
    if not isinstance(location, dict):  # Defensive narrowing.
        raise BaselineReportError(f"{where} must be an object")
    _validate_keys(
        location,
        required=_LOCATION_REQUIRED,
        allowed=_LOCATION_KEYS,
        where=where,
    )
    if not isinstance(location["path"], str):
        raise BaselineReportError(f"{where}.path must be a string")
    for key in ("line", "column", "end_line", "end_column"):
        if key in location and (type(location[key]) is not int or location[key] < 1):
            raise BaselineReportError(f"{where}.{key} must be a positive integer")
    for key in ("view", "raw_name"):
        if key in location and not isinstance(location[key], str):
            raise BaselineReportError(f"{where}.{key} must be a string")
    return location


def _validate_diagnostic(value: object, *, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BaselineReportError(f"{where} must be an object")
    finding = _json_value(value, where=where)
    if not isinstance(finding, dict):  # Defensive narrowing for type checkers.
        raise BaselineReportError(f"{where} must be an object")
    _validate_keys(
        finding,
        required=_DIAGNOSTIC_REQUIRED,
        allowed=_DIAGNOSTIC_KEYS,
        where=where,
    )

    fingerprint = finding.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise BaselineReportError(f"{where}.fingerprint must be a nonempty string")

    code = finding.get("code")
    if not isinstance(code, str) or _DIAGNOSTIC_CODE.fullmatch(code) is None:
        raise BaselineReportError(f"{where}.code must match OC followed by four digits")

    severity = finding.get("severity")
    if not isinstance(severity, str) or severity not in _SEVERITIES:
        raise BaselineReportError(f"{where}.severity must be one of fatal, error, warning, or info")

    message = finding.get("message")
    if not isinstance(message, str) or not message.strip():
        raise BaselineReportError(f"{where}.message must be a nonempty string")

    if "waived" not in finding or type(finding["waived"]) is not bool:
        raise BaselineReportError(f"{where}.waived must be a boolean")
    if "suppressed" in finding and type(finding["suppressed"]) is not bool:
        raise BaselineReportError(f"{where}.suppressed must be a boolean")

    evidence = finding.get("evidence")
    if not isinstance(evidence, list):
        raise BaselineReportError(f"{where}.evidence must be an array")
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            raise BaselineReportError(f"{where}.evidence[{index}] must be an object")
        evidence_where = f"{where}.evidence[{index}]"
        _validate_keys(
            item,
            required=_EVIDENCE_REQUIRED,
            allowed=_EVIDENCE_KEYS,
            where=evidence_where,
        )
        if not isinstance(item["view"], str):
            raise BaselineReportError(f"{evidence_where}.view must be a string")
        for key in ("native_name", "label"):
            if key in item and not isinstance(item[key], str):
                raise BaselineReportError(f"{evidence_where}.{key} must be a string")
        if "location" in item:
            _validate_location(item["location"], where=f"{evidence_where}.location")

    if "location" in finding:
        _validate_location(finding["location"], where=f"{where}.location")
    if "object" in finding:
        object_value = finding["object"]
        if not isinstance(object_value, Mapping):
            raise BaselineReportError(f"{where}.object must be an object")
        _validate_keys(
            object_value,
            required=frozenset({"kind", "id", "display"}),
            allowed=frozenset({"kind", "id", "display"}),
            where=f"{where}.object",
        )
        for key in ("kind", "id", "display"):
            if not isinstance(object_value[key], str):
                raise BaselineReportError(f"{where}.object.{key} must be a string")
    if "metadata" in finding and not isinstance(finding["metadata"], Mapping):
        raise BaselineReportError(f"{where}.metadata must be an object")
    for key in ("entity_id", "property", "help"):
        if key in finding and not isinstance(finding[key], str):
            raise BaselineReportError(f"{where}.{key} must be a string")
    if "waiver_reason" in finding and (
        not isinstance(finding["waiver_reason"], str) or not finding["waiver_reason"]
    ):
        raise BaselineReportError(f"{where}.waiver_reason must be a nonempty string")
    return finding


def _validated_report(
    report: Mapping[str, Any], *, label: str
) -> tuple[int, tuple[dict[str, Any], ...]]:
    if not isinstance(report, Mapping):
        raise BaselineReportError(f"{label} report must be an object")
    validated = _json_value(report, where=f"{label} report")
    if not isinstance(validated, dict):
        raise BaselineReportError(f"{label} report must be an object")
    _validate_keys(
        validated,
        required=_REPORT_KEYS,
        allowed=_REPORT_KEYS,
        where=f"{label} report",
    )
    schema_version = validated.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise BaselineReportError(f"{label} report schema_version must be 1")
    tool = validated["tool"]
    if not isinstance(tool, Mapping):
        raise BaselineReportError(f"{label} report.tool must be an object")
    _validate_keys(
        tool,
        required=frozenset({"name", "version"}),
        allowed=frozenset({"name", "version"}),
        where=f"{label} report.tool",
    )
    if tool["name"] != "OpenCollate" or not isinstance(tool["version"], str):
        raise BaselineReportError(
            f"{label} report.tool must name OpenCollate and contain a string version"
        )
    if not isinstance(validated["project"], str):
        raise BaselineReportError(f"{label} report.project must be a string")
    if validated["status"] not in {"pass", "fail"}:
        raise BaselineReportError(f"{label} report.status must be pass or fail")
    if type(validated["exit_code"]) is not int or validated["exit_code"] not in {0, 1, 2}:
        raise BaselineReportError(f"{label} report.exit_code must be 0, 1, or 2")
    summary = validated["summary"]
    if not isinstance(summary, Mapping):
        raise BaselineReportError(f"{label} report.summary must be an object")
    _validate_keys(
        summary,
        required=_SUMMARY_KEYS,
        allowed=_SUMMARY_KEYS,
        where=f"{label} report.summary",
    )
    for key in sorted(_SUMMARY_KEYS):
        if type(summary[key]) is not int or summary[key] < 0:
            raise BaselineReportError(f"{label} report.summary.{key} must be a nonnegative integer")
    diagnostics = validated.get("diagnostics")
    if not isinstance(diagnostics, list):
        raise BaselineReportError(f"{label} report diagnostics must be an array")
    return schema_version, tuple(
        _validate_diagnostic(item, where=f"{label}.diagnostics[{index}]")
        for index, item in enumerate(diagnostics)
    )


def _semantic_payload(finding: Mapping[str, Any]) -> dict[str, Any]:
    """Build semantic content while excluding source-position churn.

    Fingerprint is identity rather than content.  Top-level locations and
    evidence locations are provenance, so moving an otherwise identical fact
    between lines does not make it changed.  Evidence ordering is also
    normalized because evidence is a set of observations in report v1.
    """

    payload: dict[str, Any] = {}
    for key, value in finding.items():
        if key in {"fingerprint", "location", "waived", "suppressed"}:
            continue
        if key == "evidence":
            normalized_evidence: list[Any] = []
            for item in value:
                normalized = {
                    evidence_key: evidence_value
                    for evidence_key, evidence_value in item.items()
                    if evidence_key not in {"location", "span"}
                }
                normalized_evidence.append(normalized)
            payload[key] = sorted(normalized_evidence, key=_canonical_json)
        else:
            payload[key] = value
    payload["suppressed"] = _effective_suppression(finding)
    return payload


def _content_digest_validated(finding: Mapping[str, Any]) -> str:
    encoded = _canonical_json(_semantic_payload(finding)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_content_digest(finding: Mapping[str, Any]) -> str:
    """Return a full SHA-256 digest for a report-v1 diagnostic's content.

    Source locations, evidence order, and the existing fingerprint do not
    affect this digest.  Severity, message, evidence values, metadata, and the
    effective waived/suppressed state do affect it.
    """

    validated = _validate_diagnostic(finding, where="finding")
    return _content_digest_validated(validated)


# A concise spelling for programmatic consumers.
content_digest = canonical_content_digest


@dataclass(frozen=True, slots=True)
class FindingDelta:
    """One occurrence in a deterministic report multiset comparison."""

    state: FindingState
    fingerprint: str
    baseline: dict[str, Any] | None
    current: dict[str, Any] | None
    baseline_content_digest: str | None
    current_content_digest: str | None

    @property
    def current_fatal(self) -> bool:
        return self.current is not None and self.current.get("severity") == "fatal"

    @property
    def current_suppressed(self) -> bool:
        return self.current is not None and _effective_suppression(self.current)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": self.state.value,
            "fingerprint": self.fingerprint,
            "baseline_content_digest": self.baseline_content_digest,
            "current_content_digest": self.current_content_digest,
            "current_fatal": self.current_fatal,
            "current_suppressed": self.current_suppressed,
        }
        if self.baseline is not None:
            result["baseline"] = copy.deepcopy(self.baseline)
        if self.current is not None:
            result["current"] = copy.deepcopy(self.current)
        return result


@dataclass(frozen=True, slots=True)
class DiffSummary:
    baseline: int
    current: int
    new: int
    changed: int
    unchanged: int
    resolved: int
    current_active: int
    current_suppressed: int
    current_fatal: int
    current_active_fatal: int

    def to_dict(self) -> dict[str, int]:
        return {
            "baseline": self.baseline,
            "current": self.current,
            "new": self.new,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "resolved": self.resolved,
            "current_active": self.current_active,
            "current_suppressed": self.current_suppressed,
            "current_fatal": self.current_fatal,
            "current_active_fatal": self.current_active_fatal,
        }


@dataclass(frozen=True, slots=True)
class ReportDiff:
    """Serializable result of comparing two report-v1 diagnostic multisets."""

    baseline_schema_version: int
    current_schema_version: int
    summary: DiffSummary
    findings: tuple[FindingDelta, ...]

    @property
    def has_current_fatal(self) -> bool:
        return self.summary.current_fatal > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "diff_schema_version": 1,
            "baseline_report_schema_version": self.baseline_schema_version,
            "current_report_schema_version": self.current_schema_version,
            "summary": self.summary.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class _FindingRecord:
    fingerprint: str
    content_digest: str
    finding: dict[str, Any]
    canonical_finding: str
    input_index: int

    @property
    def sort_key(self) -> tuple[str, str, int]:
        return self.content_digest, self.canonical_finding, self.input_index


def _records(findings: tuple[dict[str, Any], ...]) -> dict[str, list[_FindingRecord]]:
    grouped: dict[str, list[_FindingRecord]] = defaultdict(list)
    for index, finding in enumerate(findings):
        fingerprint = str(finding["fingerprint"])
        grouped[fingerprint].append(
            _FindingRecord(
                fingerprint=fingerprint,
                content_digest=_content_digest_validated(finding),
                finding=finding,
                canonical_finding=_canonical_json(finding),
                input_index=index,
            )
        )
    for values in grouped.values():
        values.sort(key=lambda item: item.sort_key)
    return grouped


def _delta(
    state: FindingState,
    fingerprint: str,
    baseline: _FindingRecord | None,
    current: _FindingRecord | None,
) -> FindingDelta:
    return FindingDelta(
        state=state,
        fingerprint=fingerprint,
        baseline=baseline.finding if baseline is not None else None,
        current=current.finding if current is not None else None,
        baseline_content_digest=baseline.content_digest if baseline is not None else None,
        current_content_digest=current.content_digest if current is not None else None,
    )


_STATE_RANK = {
    FindingState.NEW: 0,
    FindingState.CHANGED: 1,
    FindingState.UNCHANGED: 2,
    FindingState.RESOLVED: 3,
}


def _delta_sort_key(delta: FindingDelta) -> tuple[int, str, str, str, str, str]:
    return (
        _STATE_RANK[delta.state],
        delta.fingerprint,
        delta.current_content_digest or "",
        delta.baseline_content_digest or "",
        _canonical_json(delta.current) if delta.current is not None else "",
        _canonical_json(delta.baseline) if delta.baseline is not None else "",
    )


def _compare_group(
    fingerprint: str,
    baseline: list[_FindingRecord],
    current: list[_FindingRecord],
) -> list[FindingDelta]:
    """Compare one fingerprint group as a multiset, exact matches first."""

    baseline_by_digest: dict[str, list[_FindingRecord]] = defaultdict(list)
    current_by_digest: dict[str, list[_FindingRecord]] = defaultdict(list)
    for item in baseline:
        baseline_by_digest[item.content_digest].append(item)
    for item in current:
        current_by_digest[item.content_digest].append(item)

    deltas: list[FindingDelta] = []
    remaining_baseline: list[_FindingRecord] = []
    remaining_current: list[_FindingRecord] = []
    for digest in sorted(set(baseline_by_digest) | set(current_by_digest)):
        old = sorted(baseline_by_digest.get(digest, ()), key=lambda item: item.sort_key)
        new = sorted(current_by_digest.get(digest, ()), key=lambda item: item.sort_key)
        exact_count = min(len(old), len(new))
        for index in range(exact_count):
            deltas.append(_delta(FindingState.UNCHANGED, fingerprint, old[index], new[index]))
        remaining_baseline.extend(old[exact_count:])
        remaining_current.extend(new[exact_count:])

    remaining_baseline.sort(key=lambda item: item.sort_key)
    remaining_current.sort(key=lambda item: item.sort_key)
    changed_count = min(len(remaining_baseline), len(remaining_current))
    for index in range(changed_count):
        deltas.append(
            _delta(
                FindingState.CHANGED,
                fingerprint,
                remaining_baseline[index],
                remaining_current[index],
            )
        )
    deltas.extend(
        _delta(FindingState.RESOLVED, fingerprint, item, None)
        for item in remaining_baseline[changed_count:]
    )
    deltas.extend(
        _delta(FindingState.NEW, fingerprint, None, item)
        for item in remaining_current[changed_count:]
    )
    return deltas


def diff_reports(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> ReportDiff:
    """Compare schema-v1 OpenCollate reports deterministically.

    Duplicate fingerprints are treated as multisets.  Exact content-digest
    matches are consumed first, remaining one-to-one occurrences are changed,
    and count imbalances become new or resolved occurrences.
    """

    baseline_version, baseline_findings = _validated_report(baseline, label="baseline")
    current_version, current_findings = _validated_report(current, label="current")
    baseline_groups = _records(baseline_findings)
    current_groups = _records(current_findings)

    deltas: list[FindingDelta] = []
    for fingerprint in sorted(set(baseline_groups) | set(current_groups)):
        deltas.extend(
            _compare_group(
                fingerprint,
                baseline_groups.get(fingerprint, []),
                current_groups.get(fingerprint, []),
            )
        )
    ordered = tuple(sorted(deltas, key=_delta_sort_key))

    state_counts = {state: sum(item.state == state for item in ordered) for state in FindingState}
    current_deltas = tuple(item for item in ordered if item.current is not None)
    current_suppressed = sum(item.current_suppressed for item in current_deltas)
    current_fatal = sum(item.current_fatal for item in current_deltas)
    current_active_fatal = sum(
        item.current_fatal and not item.current_suppressed for item in current_deltas
    )
    summary = DiffSummary(
        baseline=len(baseline_findings),
        current=len(current_findings),
        new=state_counts[FindingState.NEW],
        changed=state_counts[FindingState.CHANGED],
        unchanged=state_counts[FindingState.UNCHANGED],
        resolved=state_counts[FindingState.RESOLVED],
        current_active=len(current_deltas) - current_suppressed,
        current_suppressed=current_suppressed,
        current_fatal=current_fatal,
        current_active_fatal=current_active_fatal,
    )
    return ReportDiff(
        baseline_schema_version=baseline_version,
        current_schema_version=current_version,
        summary=summary,
        findings=ordered,
    )


__all__ = [
    "BaselineReportError",
    "DiffSummary",
    "FindingDelta",
    "FindingState",
    "MAX_REPORT_JSON_BYTES",
    "MAX_REPORT_JSON_NESTING",
    "ReportDiff",
    "canonical_content_digest",
    "content_digest",
    "diff_reports",
]

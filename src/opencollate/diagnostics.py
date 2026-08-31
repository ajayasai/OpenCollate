"""Stable, serializable diagnostics with source-backed evidence."""

from __future__ import annotations

import hashlib
import json
from builtins import property as builtin_property
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from opencollate.model import Provenance, SourceSpan, ViewId


class Severity(StrEnum):
    FATAL = "fatal"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {
            Severity.FATAL: 0,
            Severity.ERROR: 1,
            Severity.WARNING: 2,
            Severity.INFO: 3,
        }[self]

    @classmethod
    def parse(cls, value: str | Severity) -> Severity:
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().lower()
        aliases = {"warn": cls.WARNING, "information": cls.INFO}
        try:
            return aliases.get(normalized, cls(normalized))
        except ValueError as exc:
            raise ValueError(f"unknown diagnostic severity: {value!r}") from exc


DiagnosticSeverity = Severity


@dataclass(frozen=True, slots=True)
class DiagnosticObject:
    kind: str
    id: str
    display: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "id": self.id,
            "display": self.display or self.id,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticEvidence:
    view: ViewId
    value: Any = None
    provenance: Provenance | SourceSpan | None = None
    native_name: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "view", ViewId.parse(self.view))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "view": str(self.view),
            "value": json_safe(self.value),
        }
        if self.provenance is not None:
            location = (
                self.provenance.to_dict()
                if isinstance(self.provenance, Provenance)
                else self.provenance.to_dict()
            )
            # The view is already a first-class evidence field.
            location.pop("view", None)
            location.pop("raw_name", None)
            result["location"] = location
        if self.native_name is not None:
            result["native_name"] = self.native_name
        if self.label is not None:
            result["label"] = self.label
        return result


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    provenance: Provenance | SourceSpan | None = None
    object: DiagnosticObject | None = None
    property_name: str | None = None
    evidence: tuple[DiagnosticEvidence, ...] = ()
    help: str | None = None
    fingerprint: str = ""
    waived: bool = False
    waiver_reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        code = self.code.strip().upper()
        if not code:
            raise ValueError("diagnostic code must not be empty")
        if not self.message.strip():
            raise ValueError("diagnostic message must not be empty")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "severity", Severity.parse(self.severity))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "metadata", dict(self.metadata))
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", self.make_fingerprint())

    @builtin_property
    def property(self) -> str | None:
        """JSON-schema spelling retained as an attribute alias."""

        return self.property_name

    @builtin_property
    def is_failure(self) -> bool:
        return not self.waived and self.severity in {Severity.FATAL, Severity.ERROR}

    @builtin_property
    def primary_provenance(self) -> Provenance | SourceSpan | None:
        if self.provenance is not None:
            return self.provenance
        return next(
            (item.provenance for item in self.evidence if item.provenance is not None),
            None,
        )

    def make_fingerprint(self) -> str:
        payload = {
            "code": self.code.strip().upper(),
            "object": self.object.id if self.object is not None else None,
            "property": self.property_name,
            "views": sorted({str(item.view) for item in self.evidence}),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:24]

    def sort_key(self) -> tuple[Any, ...]:
        provenance = self.primary_provenance
        source = provenance.source if provenance is not None else ""
        line = provenance.line if provenance is not None else 0
        column = provenance.column if provenance is not None else 0
        return (
            self.severity.rank,
            self.code,
            self.object.id if self.object is not None else "",
            tuple(sorted(str(item.view) for item in self.evidence)),
            source,
            line,
            column,
            self.message,
        )

    def with_waiver(self, reason: str) -> Diagnostic:
        return replace(self, waived=True, waiver_reason=reason)

    def with_severity(self, severity: str | Severity) -> Diagnostic:
        return replace(self, severity=Severity.parse(severity))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "fingerprint": self.fingerprint,
            "waived": self.waived,
            "suppressed": self.waived,
            "evidence": [item.to_dict() for item in self.evidence],
        }
        if self.object is not None:
            result["object"] = self.object.to_dict()
            result["entity_id"] = self.object.id
        if self.property_name is not None:
            result["property"] = self.property_name
        if self.help is not None:
            result["help"] = self.help
        if self.waiver_reason is not None:
            result["waiver_reason"] = self.waiver_reason
        if self.provenance is not None:
            result["location"] = self.provenance.to_dict()
        if self.metadata:
            result["metadata"] = json_safe(self.metadata)
        return result

    @classmethod
    def from_rule(
        cls,
        code: str,
        message: str,
        *,
        severity: str | Severity | None = None,
        provenance: Provenance | SourceSpan | None = None,
        object: DiagnosticObject | None = None,
        property_name: str | None = None,
        evidence: Iterable[DiagnosticEvidence] = (),
        help: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Diagnostic:
        # Local import prevents catalog -> diagnostics -> catalog recursion.
        from opencollate.catalog import get_rule

        rule = get_rule(code)
        return cls(
            code=rule.code,
            severity=Severity.parse(severity) if severity is not None else rule.default_severity,
            message=message,
            provenance=provenance,
            object=object,
            property_name=property_name,
            evidence=tuple(evidence),
            help=help if help is not None else rule.help,
            metadata={} if metadata is None else metadata,
        )


def json_safe(value: Any) -> Any:
    """Convert model and enum values into deterministic JSON-compatible data."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, (Provenance, SourceSpan)):
        return value.to_dict()
    if hasattr(value, "to_dict"):
        return json_safe(value.to_dict())
    if isinstance(value, Mapping):
        return {
            str(key): json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [json_safe(item) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True))
    return str(value)


def sort_diagnostics(diagnostics: Iterable[Diagnostic]) -> tuple[Diagnostic, ...]:
    return tuple(sorted(diagnostics, key=Diagnostic.sort_key))


__all__ = [
    "Diagnostic",
    "DiagnosticEvidence",
    "DiagnosticObject",
    "DiagnosticSeverity",
    "Severity",
    "json_safe",
    "sort_diagnostics",
]

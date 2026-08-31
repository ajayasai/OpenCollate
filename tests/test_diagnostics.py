from __future__ import annotations

from dataclasses import replace

import pytest

from opencollate.catalog import get_rule, iter_rules
from opencollate.diagnostics import (
    Diagnostic,
    DiagnosticEvidence,
    DiagnosticObject,
    Severity,
    json_safe,
    sort_diagnostics,
)
from opencollate.model import BusShape, Provenance, SourceSpan, ViewId


def test_catalog_codes_are_unique_and_sorted() -> None:
    rules = list(iter_rules())
    codes = [rule.code for rule in rules]
    assert codes == sorted(codes)
    assert len(codes) == len(set(codes))
    assert get_rule("oc4101").name == "width-mismatch"
    with pytest.raises(KeyError, match="unknown"):
        get_rule("OC0000")


def test_diagnostic_fingerprint_ignores_location_and_message() -> None:
    evidence = (
        DiagnosticEvidence(
            ViewId("rtl"),
            1,
            Provenance("rtl/a.sv", 12, 3, ViewId("rtl")),
        ),
        DiagnosticEvidence(
            ViewId("liberty"),
            4,
            Provenance("lib/a.lib", 50, 2, ViewId("liberty")),
        ),
    )
    finding = Diagnostic.from_rule(
        "OC4101",
        "uart/irq differs.",
        object=DiagnosticObject("port", "component:uart/port:irq", "uart/irq"),
        property_name="shape.width",
        evidence=evidence,
    )
    moved = replace(
        finding,
        message="Wording improved.",
        evidence=(
            replace(evidence[0], provenance=Provenance("moved.sv", 99, 1, ViewId("rtl"))),
            evidence[1],
        ),
        fingerprint="",
    )
    assert finding.fingerprint == moved.fingerprint
    assert finding.is_failure


def test_waiver_and_severity_override_are_immutable() -> None:
    finding = Diagnostic.from_rule("OC4101", "A width differs.")
    waived = finding.with_waiver("Intentional wrapper difference")
    note = finding.with_severity("info")
    assert finding.waived is False
    assert waived.waived is True
    assert waived.waiver_reason == "Intentional wrapper difference"
    assert waived.is_failure is False
    assert note.severity == Severity.INFO


def test_diagnostic_serialization_contains_evidence_locations() -> None:
    finding = Diagnostic.from_rule(
        "OC4001",
        "Direction differs.",
        provenance=SourceSpan("a.sv", 2, 4),
        evidence=(
            DiagnosticEvidence(
                "rtl.default",  # type: ignore[arg-type]
                "output",
                Provenance("a.sv", 2, 4, ViewId("rtl")),
                "irq_o",
            ),
        ),
    )
    value = finding.to_dict()
    assert value["code"] == "OC4001"
    assert value["location"]["path"] == "a.sv"
    assert value["evidence"][0]["location"] == {
        "path": "a.sv",
        "line": 2,
        "column": 4,
    }


def test_json_safe_handles_model_and_sets_deterministically() -> None:
    value = json_safe({"shape": BusShape(left=3, right=0), "views": {ViewId("rtl"), ViewId("lef")}})
    assert value["shape"]["width"] == 4
    assert value["views"] == ["lef.default", "rtl.default"]


def test_diagnostics_sort_by_severity_then_code() -> None:
    warning = Diagnostic.from_rule("OC1102", "Unsupported.")
    error = Diagnostic.from_rule("OC4101", "Mismatch.")
    assert sort_diagnostics((warning, error)) == (error, warning)


@pytest.mark.parametrize("value", ["nonsense", ""])
def test_unknown_severity_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="severity"):
        Severity.parse(value)

from __future__ import annotations

import copy
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import opencollate.engine as engine_module
from opencollate.baseline import BaselineReportError, diff_reports
from opencollate.config import ProjectConfig, SourceConfig
from opencollate.engine import ComparisonEngine, EngineResult
from opencollate.model import (
    ConnectivityEdge,
    ConnectivityEndpoint,
    ConnectivityExpectation,
    ConnectivityRequirement,
    ConnectivityTransform,
    FactState,
    ViewId,
    ViewObservation,
)

RTL = ViewId("rtl")
INTENT = ViewId("connectivity", "properties")


def _project() -> ProjectConfig:
    root = Path.cwd()
    return ProjectConfig(
        path=root / "property-test.toml",
        root=root,
        name="vnext-properties",
        sources=(
            SourceConfig(RTL, (root / "property-test.sv",)),
            SourceConfig(INTENT, (root / "property-test.csv",)),
        ),
    )


def _scalar(name: str) -> ConnectivityEndpoint:
    return ConnectivityEndpoint(name)


def _bus(name: str, indices: Sequence[int]) -> tuple[ConnectivityEndpoint, ...]:
    return tuple(
        ConnectivityEndpoint(
            name,
            bit_index=bit,
            ordinal=ordinal,
            width=len(indices),
        )
        for ordinal, bit in enumerate(indices)
    )


def _requirement(
    source: str,
    sink: str,
    *,
    identifier: str = "PROPERTY",
    expectation: ConnectivityExpectation = ConnectivityExpectation.REACHABLE,
    transform: ConnectivityTransform = ConnectivityTransform.ANY,
) -> ConnectivityRequirement:
    return ConnectivityRequirement(
        identifier,
        source,
        sink,
        expectation=expectation,
        transform=transform,
    )


def _run_connectivity(
    endpoints: Sequence[ConnectivityEndpoint],
    edges: Sequence[ConnectivityEdge],
    requirements: Sequence[ConnectivityRequirement],
    *,
    complete: bool = True,
) -> EngineResult:
    rtl = ViewObservation(
        RTL,
        connectivity_endpoints=tuple(endpoints),
        connectivity_edges=tuple(edges),
        attributes={"connectivity_complete": complete},
    )
    intent = ViewObservation(INTENT, connectivity_requirements=tuple(requirements))
    return ComparisonEngine(_project()).run((rtl, intent))


def _connectivity_diagnostics(result: EngineResult) -> tuple[dict[str, Any], ...]:
    return tuple(
        diagnostic.to_dict()
        for diagnostic in result.diagnostics
        if diagnostic.code.startswith("OC65")
    )


@st.composite
def _range_cases(
    draw: st.DrawFn,
) -> tuple[tuple[int, ...], tuple[int, ...], int, int]:
    width = draw(st.integers(min_value=2, max_value=12))
    low = draw(st.integers(min_value=-16, max_value=16))
    left_offset = draw(st.integers(min_value=0, max_value=width - 1))
    other_offset = draw(st.integers(min_value=0, max_value=width - 2))
    if other_offset >= left_offset:
        other_offset += 1
    numeric = tuple(range(low, low + width))
    source_order = numeric if draw(st.booleans()) else tuple(reversed(numeric))
    sink_order = tuple(reversed(source_order))
    return source_order, sink_order, numeric[left_offset], numeric[other_offset]


@given(case=_range_cases())
@settings(max_examples=50, deadline=None)
def test_explicit_ranges_follow_selector_order_not_declaration_order(
    case: tuple[tuple[int, ...], tuple[int, ...], int, int],
) -> None:
    source_order, sink_order, left, right = case
    sources = _bus("top/a", source_order)
    sinks = _bus("top/y", sink_order)
    source_by_bit = {endpoint.bit_index: endpoint for endpoint in sources}
    sink_by_bit = {endpoint.bit_index: endpoint for endpoint in sinks}
    edges = tuple(
        ConnectivityEdge(source_by_bit[bit], sink_by_bit[bit]) for bit in sorted(source_by_bit)
    )
    requirement = _requirement(
        f"top/a[{left}:{right}]",
        f"top/y[{left}:{right}]",
        transform=ConnectivityTransform.IDENTITY,
    )

    result = _run_connectivity((*sources, *sinks), edges, (requirement,))

    assert _connectivity_diagnostics(result) == ()


def test_reverse_transform_composes_with_explicit_range_direction() -> None:
    sources = _bus("top/a", (7, 6, 5, 4))
    sinks = _bus("top/y", (4, 5, 6, 7))
    source_by_bit = {endpoint.bit_index: endpoint for endpoint in sources}
    sink_by_bit = {endpoint.bit_index: endpoint for endpoint in sinks}
    edges = tuple(ConnectivityEdge(source_by_bit[bit], sink_by_bit[bit]) for bit in (4, 5, 6, 7))

    result = _run_connectivity(
        (*sources, *sinks),
        edges,
        (
            _requirement(
                "top/a[7:4]",
                "top/y[4:7]",
                transform=ConnectivityTransform.REVERSE,
            ),
        ),
    )

    assert _connectivity_diagnostics(result) == ()


@pytest.mark.parametrize(
    "source_selector",
    (
        "top/a[0:1024]",
        f"top/a[-{'9' * 4_090}:+{'9' * 4_090}]",
        f"top/a[{'9' * 4_097}:0]",
    ),
)
def test_oversized_selector_ranges_fail_closed_without_materialization(
    source_selector: str,
) -> None:
    source, sink = _scalar("top/a"), _scalar("top/y")

    diagnostics = _connectivity_diagnostics(
        _run_connectivity(
            (source, sink),
            (),
            (_requirement(source_selector, "top/y"),),
        )
    )

    assert tuple(item["code"] for item in diagnostics) == ("OC6505",)
    assert diagnostics[0]["property"] == "connectivity.source"
    assert diagnostics[0]["metadata"]["limit"] == 1_024


@given(
    endpoint_order=st.permutations((0, 1, 2, 3)),
    edge_order=st.permutations((0, 1, 2, 3)),
)
@settings(max_examples=35, deadline=None)
def test_connectivity_witness_is_invariant_to_graph_input_order(
    endpoint_order: Sequence[int],
    edge_order: Sequence[int],
) -> None:
    endpoints = (_scalar("top/a"), _scalar("top/b"), _scalar("top/c"), _scalar("top/y"))
    source, left, right, sink = endpoints
    edges = (
        ConnectivityEdge(source, right),
        ConnectivityEdge(right, sink),
        ConnectivityEdge(source, left),
        ConnectivityEdge(left, sink),
    )
    requirement = _requirement(
        "top/a",
        "top/y",
        expectation=ConnectivityExpectation.UNREACHABLE,
    )
    canonical = _run_connectivity(endpoints, edges, (requirement,))
    permuted = _run_connectivity(
        tuple(endpoints[index] for index in endpoint_order),
        tuple(edges[index] for index in edge_order),
        (requirement,),
    )

    assert _connectivity_diagnostics(permuted) == _connectivity_diagnostics(canonical)
    diagnostic = _connectivity_diagnostics(permuted)[0]
    assert diagnostic["code"] == "OC6504"
    assert [edge["sink"] for edge in diagnostic["metadata"]["witness_path"]] == [
        "top/b",
        "top/y",
    ]


@st.composite
def _tainted_chain_cases(
    draw: st.DrawFn,
) -> tuple[int, int, FactState, ConnectivityExpectation, bool]:
    edge_count = draw(st.integers(min_value=1, max_value=8))
    tainted_at = draw(st.integers(min_value=0, max_value=edge_count - 1))
    status = draw(st.sampled_from((FactState.TAINTED, FactState.UNSUPPORTED)))
    expectation = draw(st.sampled_from(tuple(ConnectivityExpectation)))
    reverse_edges = draw(st.booleans())
    return edge_count, tainted_at, status, expectation, reverse_edges


@given(case=_tainted_chain_cases())
@settings(max_examples=45, deadline=None)
def test_tainted_frontier_never_proves_a_path_or_isolation(
    case: tuple[int, int, FactState, ConnectivityExpectation, bool],
) -> None:
    edge_count, tainted_at, status, expectation, reverse_edges = case
    endpoints = tuple(_scalar(f"top/n{index}") for index in range(edge_count + 1))
    edges = tuple(
        ConnectivityEdge(
            endpoints[index],
            endpoints[index + 1],
            inverted=None if index == tainted_at else False,
            status=status if index == tainted_at else FactState.KNOWN,
            attributes={"reason": "unsupported property frontier"} if index == tainted_at else {},
        )
        for index in range(edge_count)
    )
    if reverse_edges:
        edges = tuple(reversed(edges))
    requirement = _requirement(
        endpoints[0].key,
        endpoints[-1].key,
        expectation=expectation,
    )

    diagnostics = _connectivity_diagnostics(_run_connectivity(endpoints, edges, (requirement,)))

    assert tuple(item["code"] for item in diagnostics) == ("OC6505",)
    assert diagnostics[0]["metadata"]["frontier"]["status"] == status.value
    assert not {"OC6503", "OC6504"}.intersection(item["code"] for item in diagnostics)


@pytest.mark.parametrize(
    ("transform", "known_inverted"),
    [
        (ConnectivityTransform.IDENTITY, False),
        (ConnectivityTransform.INVERTED, True),
        (ConnectivityTransform.REVERSE, False),
    ],
)
def test_tainted_alternate_makes_exact_transform_inconclusive(
    transform: ConnectivityTransform,
    known_inverted: bool,
) -> None:
    source, sink = _scalar("top/a"), _scalar("top/y")
    known = ConnectivityEdge(source, sink, inverted=known_inverted)
    tainted = ConnectivityEdge(
        source,
        sink,
        inverted=None,
        status=FactState.TAINTED,
        attributes={"reason": "unsupported alternate"},
    )

    exact = _connectivity_diagnostics(
        _run_connectivity(
            (source, sink),
            (known, tainted),
            (_requirement("top/a", "top/y", transform=transform),),
        )
    )
    reachable = _connectivity_diagnostics(
        _run_connectivity(
            (source, sink),
            (known, tainted),
            (_requirement("top/a", "top/y"),),
        )
    )

    assert tuple(item["code"] for item in exact) == ("OC6505",)
    assert exact[0]["property"] == "connectivity.transform"
    assert exact[0]["metadata"]["frontier"]["status"] == "tainted"
    assert exact[0]["metadata"]["witness_path"][0]["status"] == "known"
    assert reachable == ()


@pytest.mark.parametrize(
    "transform",
    (ConnectivityTransform.IDENTITY, ConnectivityTransform.REVERSE),
)
def test_exact_vector_mapping_rejects_extra_selected_sink_path(
    transform: ConnectivityTransform,
) -> None:
    sources = _bus("top/a", (1, 0))
    sinks = _bus("top/y", (1, 0))
    source_by_bit = {endpoint.bit_index: endpoint for endpoint in sources}
    sink_by_bit = {endpoint.bit_index: endpoint for endpoint in sinks}
    expected_pairs = (
        ((1, 1), (0, 0)) if transform == ConnectivityTransform.IDENTITY else ((1, 0), (0, 1))
    )
    extra_pair = (1, 0) if transform == ConnectivityTransform.IDENTITY else (1, 1)
    edges = tuple(
        ConnectivityEdge(source_by_bit[source_bit], sink_by_bit[sink_bit])
        for source_bit, sink_bit in (*expected_pairs, extra_pair)
    )

    exact = _connectivity_diagnostics(
        _run_connectivity(
            (*sources, *sinks),
            edges,
            (
                _requirement(
                    "top/a[*]",
                    "top/y[*]",
                    transform=transform,
                ),
            ),
        )
    )
    any_edges = (
        ConnectivityEdge(source_by_bit[1], sink_by_bit[1]),
        ConnectivityEdge(source_by_bit[0], sink_by_bit[0]),
        ConnectivityEdge(source_by_bit[1], sink_by_bit[0]),
    )
    reachable = _connectivity_diagnostics(
        _run_connectivity(
            (*sources, *sinks),
            any_edges,
            (_requirement("top/a[*]", "top/y[*]"),),
        )
    )

    assert tuple(item["code"] for item in exact) == ("OC6507",)
    assert exact[0]["property"] == "connectivity.bit_mapping"
    assert exact[0]["metadata"]["actual_sink"] != exact[0]["metadata"]["expected_sink"]
    assert exact[0]["metadata"]["expected_path"]
    assert exact[0]["metadata"]["witness_path"]
    assert reachable == ()


def test_tainted_cross_path_makes_exact_vector_mapping_inconclusive() -> None:
    sources = _bus("top/a", (1, 0))
    sinks = _bus("top/y", (1, 0))
    source_by_bit = {endpoint.bit_index: endpoint for endpoint in sources}
    sink_by_bit = {endpoint.bit_index: endpoint for endpoint in sinks}
    edges = (
        ConnectivityEdge(source_by_bit[1], sink_by_bit[1]),
        ConnectivityEdge(source_by_bit[0], sink_by_bit[0]),
        ConnectivityEdge(
            source_by_bit[1],
            sink_by_bit[0],
            kind="unsupported_cross_path",
            inverted=None,
            status=FactState.TAINTED,
            attributes={"reason": "unsupported cross-bit expression"},
        ),
    )

    exact = _connectivity_diagnostics(
        _run_connectivity(
            (*sources, *sinks),
            edges,
            (
                _requirement(
                    "top/a[*]",
                    "top/y[*]",
                    transform=ConnectivityTransform.IDENTITY,
                ),
            ),
        )
    )
    reachable = _connectivity_diagnostics(
        _run_connectivity(
            (*sources, *sinks),
            edges,
            (_requirement("top/a[*]", "top/y[*]"),),
        )
    )

    assert tuple(item["code"] for item in exact) == ("OC6505",)
    assert exact[0]["metadata"]["frontier"]["status"] == "tainted"
    assert exact[0]["metadata"]["expected_sink"] == "top/y[1]"
    assert exact[0]["metadata"]["possible_sink"] == "top/y[0]"
    assert reachable == ()


def test_incomplete_graph_cannot_prove_exact_transform_but_can_prove_reachability() -> None:
    source, sink = _scalar("top/a"), _scalar("top/y")
    edge = ConnectivityEdge(source, sink)

    exact = _connectivity_diagnostics(
        _run_connectivity(
            (source, sink),
            (edge,),
            (
                _requirement(
                    "top/a",
                    "top/y",
                    transform=ConnectivityTransform.IDENTITY,
                ),
            ),
            complete=False,
        )
    )
    reachable = _connectivity_diagnostics(
        _run_connectivity(
            (source, sink),
            (edge,),
            (_requirement("top/a", "top/y"),),
            complete=False,
        )
    )

    assert tuple(item["code"] for item in exact) == ("OC6505",)
    assert reachable == ()


def test_path_search_bound_is_inconclusive_without_large_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "_MAX_CONNECTIVITY_SEARCH_STATES", 4)
    endpoints = tuple(_scalar(f"top/n{index}") for index in range(7))
    edges = tuple(
        ConnectivityEdge(endpoints[index], endpoints[index + 1])
        for index in range(len(endpoints) - 1)
    )

    diagnostics = _connectivity_diagnostics(
        _run_connectivity(
            endpoints,
            edges,
            (_requirement(endpoints[0].key, endpoints[-1].key),),
        )
    )

    assert tuple(item["code"] for item in diagnostics) == ("OC6505",)
    assert diagnostics[0]["metadata"]["limit"] == 4


def test_width_and_pair_bounds_are_checked_before_expensive_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "_MAX_CONNECTIVITY_REQUIREMENT_BITS", 3)
    sources = _bus("top/a", (3, 2, 1, 0))
    sinks = _bus("top/y", (3, 2, 1, 0))
    width_diagnostics = _connectivity_diagnostics(
        _run_connectivity(
            (*sources, *sinks),
            (),
            (_requirement("top/a[*]", "top/y[*]"),),
        )
    )
    assert tuple(item["code"] for item in width_diagnostics) == ("OC6505",)
    assert width_diagnostics[0]["metadata"]["limit"] == 3

    monkeypatch.setattr(engine_module, "_MAX_CONNECTIVITY_REQUIREMENT_BITS", 8)
    monkeypatch.setattr(engine_module, "_MAX_CONNECTIVITY_PAIR_SEARCHES", 3)
    pair_sources = _bus("top/p", (1, 0))
    pair_sinks = _bus("top/q", (1, 0))
    pair_diagnostics = _connectivity_diagnostics(
        _run_connectivity(
            (*pair_sources, *pair_sinks),
            (),
            (
                _requirement(
                    "top/p[*]",
                    "top/q[*]",
                    expectation=ConnectivityExpectation.UNREACHABLE,
                ),
            ),
        )
    )
    assert tuple(item["code"] for item in pair_diagnostics) == ("OC6505",)


def _finding(
    fingerprint: str,
    value: object,
    *,
    severity: str = "error",
    waived: bool = False,
) -> dict[str, Any]:
    return {
        "code": "OC4101",
        "severity": severity,
        "message": "Property-generated width mismatch.",
        "fingerprint": fingerprint,
        "waived": waived,
        "suppressed": waived,
        "object": {"kind": "port", "id": "port:top/data", "display": "top/data"},
        "evidence": [{"view": "rtl.default", "value": value}],
        "metadata": {"observed": value},
    }


def _report(findings: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "tool": {"name": "OpenCollate", "version": "property-test"},
        "project": "property-test",
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
        "diagnostics": list(findings),
    }


_FINDING_SPECS = st.lists(
    st.tuples(
        st.sampled_from(("alpha", "beta", "gamma")),
        st.integers(min_value=-3, max_value=3),
        st.sampled_from(("fatal", "error", "warning", "info")),
        st.booleans(),
    ),
    max_size=12,
)


@given(
    baseline_specs=_FINDING_SPECS,
    current_specs=_FINDING_SPECS,
    baseline_seed=st.integers(min_value=0, max_value=2**32 - 1),
    current_seed=st.integers(min_value=0, max_value=2**32 - 1),
)
@settings(max_examples=60, deadline=None)
def test_report_diff_is_multiset_and_order_invariant(
    baseline_specs: Sequence[tuple[str, int, str, bool]],
    current_specs: Sequence[tuple[str, int, str, bool]],
    baseline_seed: int,
    current_seed: int,
) -> None:
    baseline_findings = [
        _finding(fingerprint, value, severity=severity, waived=waived)
        for fingerprint, value, severity, waived in baseline_specs
    ]
    current_findings = [
        _finding(fingerprint, value, severity=severity, waived=waived)
        for fingerprint, value, severity, waived in current_specs
    ]
    baseline = _report(baseline_findings)
    current = _report(current_findings)
    before = copy.deepcopy((baseline, current))
    expected = diff_reports(baseline, current).to_dict()

    shuffled_baseline = copy.deepcopy(baseline_findings)
    shuffled_current = copy.deepcopy(current_findings)
    random.Random(baseline_seed).shuffle(shuffled_baseline)
    random.Random(current_seed).shuffle(shuffled_current)
    actual = diff_reports(
        _report(shuffled_baseline),
        _report(shuffled_current),
    ).to_dict()

    assert actual == expected
    assert (baseline, current) == before
    summary = actual["summary"]
    assert summary["baseline"] == (summary["unchanged"] + summary["changed"] + summary["resolved"])
    assert summary["current"] == (summary["unchanged"] + summary["changed"] + summary["new"])


_NON_JSON_VALUES = st.one_of(
    st.binary(max_size=4),
    st.sets(st.integers(min_value=-2, max_value=2), max_size=3),
    st.lists(st.integers(min_value=-2, max_value=2), max_size=3).map(tuple),
    st.integers(min_value=-2, max_value=2).map(lambda value: complex(value, 1)),
)


@given(value=_NON_JSON_VALUES)
@settings(max_examples=30, deadline=None)
def test_report_diff_rejects_nested_non_json_values(value: object) -> None:
    malformed = _finding("bad", 1)
    malformed["metadata"] = {"nested": [{"value": value}]}

    with pytest.raises(BaselineReportError):
        diff_reports(_report((malformed,)), _report(()))


@given(key=st.one_of(st.none(), st.booleans(), st.integers(min_value=-2, max_value=2)))
@settings(max_examples=12, deadline=None)
def test_report_diff_rejects_non_string_nested_keys(key: object) -> None:
    malformed = _finding("bad-key", 1)
    malformed["metadata"] = {"nested": {key: "value"}}

    with pytest.raises(BaselineReportError, match="non-string object key"):
        diff_reports(_report((malformed,)), _report(()))


@given(value=st.sampled_from((float("nan"), float("inf"), float("-inf"))))
def test_report_diff_rejects_non_finite_nested_numbers(value: float) -> None:
    malformed = _finding("bad-number", 1)
    malformed["evidence"] = [{"view": "rtl.default", "value": [0, {"bad": value}]}]

    with pytest.raises(BaselineReportError, match="non-finite"):
        diff_reports(_report((malformed,)), _report(()))

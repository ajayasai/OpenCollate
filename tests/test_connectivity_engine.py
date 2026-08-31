from __future__ import annotations

from pathlib import Path

import pytest

from opencollate.config import ProjectConfig, SourceConfig
from opencollate.engine import ComparisonEngine
from opencollate.model import (
    ConnectivityEdge,
    ConnectivityEndpoint,
    ConnectivityExpectation,
    ConnectivityRequirement,
    ConnectivityTransform,
    FactState,
    Provenance,
    ViewId,
    ViewObservation,
)
from opencollate.parsers.connectivity import parse_connectivity_csv
from opencollate.parsers.verilog import parse_verilog

RTL = ViewId("rtl")
INTENT = ViewId("connectivity", "intent")


def _project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        path=tmp_path / "opencollate.toml",
        root=tmp_path,
        name="connectivity-test",
        sources=(
            SourceConfig(RTL, (tmp_path / "top.sv",)),
            SourceConfig(INTENT, (tmp_path / "connectivity.csv",)),
        ),
    )


def _scalar(name: str, line: int = 1) -> ConnectivityEndpoint:
    return ConnectivityEndpoint(name, provenance=Provenance("top.sv", line, view=RTL))


def _bus(name: str, indices: tuple[int, ...], line: int = 1) -> tuple[ConnectivityEndpoint, ...]:
    return tuple(
        ConnectivityEndpoint(
            name,
            bit_index=bit,
            ordinal=ordinal,
            width=len(indices),
            provenance=Provenance("top.sv", line, view=RTL),
        )
        for ordinal, bit in enumerate(indices)
    )


def _requirement(
    *,
    identifier: str = "PATH",
    source: str = "top/a",
    sink: str = "top/y",
    expectation: ConnectivityExpectation = ConnectivityExpectation.REACHABLE,
    transform: ConnectivityTransform = ConnectivityTransform.ANY,
    through: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> ConnectivityRequirement:
    return ConnectivityRequirement(
        identifier,
        source,
        sink,
        expectation,
        transform,
        through,
        exclude,
        provenance=Provenance("connectivity.csv", 2, view=INTENT),
    )


def _run(
    tmp_path: Path,
    endpoints: tuple[ConnectivityEndpoint, ...],
    edges: tuple[ConnectivityEdge, ...],
    requirement: ConnectivityRequirement,
    *,
    complete: bool = True,
) -> tuple[str, ...]:
    rtl = ViewObservation(
        RTL,
        connectivity_endpoints=endpoints,
        connectivity_edges=edges,
        attributes={"connectivity_complete": complete},
    )
    intent = ViewObservation(INTENT, connectivity_requirements=(requirement,))
    return tuple(
        diagnostic.code
        for diagnostic in ComparisonEngine(_project(tmp_path)).run((rtl, intent)).diagnostics
    )


def test_required_path_passes_and_forbidden_path_returns_witness(tmp_path: Path) -> None:
    source, middle, sink = _scalar("top/a"), _scalar("top/n", 2), _scalar("top/y", 3)
    edges = (
        ConnectivityEdge(source, middle, provenance=Provenance("top.sv", 2, view=RTL)),
        ConnectivityEdge(middle, sink, provenance=Provenance("top.sv", 3, view=RTL)),
    )
    assert _run(tmp_path, (source, middle, sink), edges, _requirement()) == ()

    forbidden = ViewObservation(
        INTENT,
        connectivity_requirements=(_requirement(expectation=ConnectivityExpectation.UNREACHABLE),),
    )
    rtl = ViewObservation(
        RTL,
        connectivity_endpoints=(source, middle, sink),
        connectivity_edges=edges,
        attributes={"connectivity_complete": True},
    )
    result = ComparisonEngine(_project(tmp_path)).run((rtl, forbidden))
    diagnostic = next(item for item in result.diagnostics if item.code == "OC6504")
    assert len(diagnostic.metadata["witness_path"]) == 2
    assert "2-edge static path" in diagnostic.message


def test_missing_path_reports_a_deterministic_cut(tmp_path: Path) -> None:
    source, middle, sink = _scalar("top/a"), _scalar("top/n"), _scalar("top/y")
    edges = (ConnectivityEdge(source, middle),)
    rtl = ViewObservation(
        RTL,
        connectivity_endpoints=(source, middle, sink),
        connectivity_edges=edges,
        attributes={"connectivity_complete": True},
    )
    intent = ViewObservation(INTENT, connectivity_requirements=(_requirement(),))

    result = ComparisonEngine(_project(tmp_path)).run((rtl, intent))

    diagnostic = next(item for item in result.diagnostics if item.code == "OC6503")
    assert diagnostic.metadata["reachable_cut"] == ["top/n"]
    assert "top/a" in diagnostic.message and "top/y" in diagnostic.message


def test_tainted_frontier_is_inconclusive_not_a_path_or_clean_isolation(
    tmp_path: Path,
) -> None:
    source, sink = _scalar("top/a"), _scalar("top/y")
    frontier = ConnectivityEdge(
        source,
        sink,
        kind="unsupported_assign",
        inverted=None,
        status=FactState.TAINTED,
        attributes={"reason": "binary logic"},
    )
    required_codes = _run(
        tmp_path,
        (source, sink),
        (frontier,),
        _requirement(),
    )
    forbidden_codes = _run(
        tmp_path,
        (source, sink),
        (frontier,),
        _requirement(expectation=ConnectivityExpectation.UNREACHABLE),
    )

    assert required_codes == ("OC6505",)
    assert forbidden_codes == ("OC6505",)


def test_unsupported_ref_port_cannot_create_a_false_isolation_pass(tmp_path: Path) -> None:
    source_path = tmp_path / "ref_port.sv"
    source_path.write_text(
        "module child(input logic a, ref logic r); assign r = a; endmodule\n"
        "module top(input logic a, output logic y); logic x; "
        "child u(.a(a), .r(x)); assign y = x; endmodule\n",
        encoding="utf-8",
    )
    rtl = parse_verilog(source_path, top="top")
    intent = ViewObservation(
        INTENT,
        connectivity_requirements=(_requirement(expectation=ConnectivityExpectation.UNREACHABLE),),
    )

    result = ComparisonEngine(_project(tmp_path)).run((rtl, intent))
    connectivity_codes = tuple(
        item.code for item in result.diagnostics if item.code.startswith("OC65")
    )
    ref_edges = {
        (edge.source.key, edge.sink.key, edge.status)
        for edge in rtl.connectivity_edges
        if edge.kind == "unsupported_port_direction"
    }

    assert connectivity_codes == ("OC6505",)
    assert ("top/x", "top/u/r", FactState.TAINTED) in ref_edges
    assert ("top/u/r", "top/x", FactState.TAINTED) in ref_edges


def test_escaped_identifier_dot_is_not_mistaken_for_hierarchy(tmp_path: Path) -> None:
    source_path = tmp_path / "escaped.sv"
    source_path.write_text(
        "module top(input wire \\a.b , output wire \\y.z ); assign \\y.z  = \\a.b ; endmodule\n",
        encoding="utf-8",
    )

    rtl = parse_verilog(source_path, top="top")
    endpoint_names = {item.native_name for item in rtl.connectivity_endpoints}
    intent = ViewObservation(
        INTENT,
        connectivity_requirements=(
            _requirement(
                source="top/a.b",
                sink="top/y.z",
                expectation=ConnectivityExpectation.UNREACHABLE,
            ),
        ),
    )

    result = ComparisonEngine(_project(tmp_path)).run((rtl, intent))

    assert endpoint_names == {"top/a.b", "top/y.z"}
    assert tuple(item.code for item in result.diagnostics if item.code.startswith("OC65")) == (
        "OC6504",
    )


@pytest.mark.parametrize(
    "construct",
    (
        "alias x = a;",
        "buf b0(x, a);",
        "tran t0(x, a);",
    ),
)
def test_transparent_aliases_and_primitives_are_exact_paths(
    tmp_path: Path,
    construct: str,
) -> None:
    source_path = tmp_path / "transparent.sv"
    source_path.write_text(
        f"module top(input wire a, output wire y); wire x; {construct} assign y = x; endmodule\n",
        encoding="utf-8",
    )
    rtl = parse_verilog(source_path, top="top")
    intent = ViewObservation(
        INTENT,
        connectivity_requirements=(_requirement(expectation=ConnectivityExpectation.UNREACHABLE),),
    )

    result = ComparisonEngine(_project(tmp_path)).run((rtl, intent))

    assert rtl.attributes["connectivity_complete"] is True
    assert tuple(item.code for item in result.diagnostics if item.code.startswith("OC65")) == (
        "OC6504",
    )


def test_unhandled_primitive_cannot_create_a_false_isolation_pass(tmp_path: Path) -> None:
    source_path = tmp_path / "opaque.sv"
    source_path.write_text(
        "module top(input wire a, input wire b, output wire y); wire x; "
        "and g0(x, a, b); assign y = x; endmodule\n",
        encoding="utf-8",
    )
    rtl = parse_verilog(source_path, top="top")
    intent = ViewObservation(
        INTENT,
        connectivity_requirements=(_requirement(expectation=ConnectivityExpectation.UNREACHABLE),),
    )

    result = ComparisonEngine(_project(tmp_path)).run((rtl, intent))

    assert rtl.attributes["connectivity_complete"] is False
    assert tuple(item.code for item in result.diagnostics if item.code.startswith("OC65")) == (
        "OC6505",
    )


def test_escaped_reserved_name_cannot_collide_with_hierarchy(tmp_path: Path) -> None:
    source_path = tmp_path / "escaped_reserved.sv"
    source_path.write_text(
        "module leaf(input wire i, output wire b); assign b = i; endmodule\n"
        "module top(input wire x, input wire z, output wire y, output wire w); "
        "wire \\a/b ; leaf a(.i(x), .b(w)); assign \\a/b  = z; "
        "assign y = \\a/b ; endmodule\n",
        encoding="utf-8",
    )
    rtl = parse_verilog(source_path, top="top")
    intent = ViewObservation(
        INTENT,
        connectivity_requirements=(
            _requirement(
                identifier="NO_X_TO_Y",
                source="top/x",
                sink="top/y",
                expectation=ConnectivityExpectation.UNREACHABLE,
            ),
            _requirement(
                identifier="NO_Z_TO_W",
                source="top/z",
                sink="top/w",
                expectation=ConnectivityExpectation.UNREACHABLE,
            ),
        ),
    )

    result = ComparisonEngine(_project(tmp_path)).run((rtl, intent))
    endpoint_names = {item.native_name for item in rtl.connectivity_endpoints}

    assert rtl.attributes["connectivity_complete"] is True
    assert {"top/a/b", "top/a%2Fb"} <= endpoint_names
    assert not [item for item in result.diagnostics if item.code.startswith("OC65")]


def test_generated_instance_endpoint_is_a_valid_connectivity_selector(tmp_path: Path) -> None:
    source_path = tmp_path / "generated.sv"
    source_path.write_text(
        "module leaf(input logic d); endmodule\n"
        "module top(input logic a); genvar i; "
        "for (i = 0; i < 1; i++) begin : lanes leaf u(.d(a)); end endmodule\n",
        encoding="utf-8",
    )
    intent_path = tmp_path / "connectivity.csv"
    intent_path.write_text(
        "id,source,sink,expect,transform\nGENERATED,top/a,top/lanes[0]/u/d,reachable,identity\n",
        encoding="utf-8",
    )
    rtl = parse_verilog(source_path, top="top")
    intent = parse_connectivity_csv(intent_path)

    result = ComparisonEngine(_project(tmp_path)).run((rtl, intent))

    assert intent.complete
    assert "top/lanes[0]/u/d" in {item.native_name for item in rtl.connectivity_endpoints}
    assert not [item for item in result.diagnostics if item.code.startswith("OC65")]


def test_bus_order_and_inversion_are_checked_only_from_known_edges(tmp_path: Path) -> None:
    sources = _bus("top/a", (1, 0))
    sinks = _bus("top/y", (1, 0))
    reversed_edges = (
        ConnectivityEdge(sources[0], sinks[1]),
        ConnectivityEdge(sources[1], sinks[0]),
    )

    assert (
        _run(
            tmp_path,
            (*sources, *sinks),
            reversed_edges,
            _requirement(transform=ConnectivityTransform.REVERSE),
        )
        == ()
    )
    assert "OC6507" in _run(
        tmp_path,
        (*sources, *sinks),
        reversed_edges,
        _requirement(transform=ConnectivityTransform.IDENTITY),
    )

    scalar_source, scalar_sink = _scalar("top/s"), _scalar("top/z")
    inverted_edge = ConnectivityEdge(scalar_source, scalar_sink, inverted=True)
    assert (
        _run(
            tmp_path,
            (scalar_source, scalar_sink),
            (inverted_edge,),
            _requirement(
                source="top/s",
                sink="top/z",
                transform=ConnectivityTransform.INVERTED,
            ),
        )
        == ()
    )
    assert "OC6508" in _run(
        tmp_path,
        (scalar_source, scalar_sink),
        (inverted_edge,),
        _requirement(
            source="top/s",
            sink="top/z",
            transform=ConnectivityTransform.IDENTITY,
        ),
    )


def test_endpoint_width_ambiguity_through_and_exclude_diagnostics(tmp_path: Path) -> None:
    sources = _bus("top/a", (1, 0))
    sink = _scalar("top/y")
    assert "OC6506" in _run(
        tmp_path,
        (*sources, sink),
        (),
        _requirement(),
    )
    assert "OC6501" in _run(
        tmp_path,
        (*sources, sink),
        (),
        _requirement(source="top/missing"),
    )
    extra = _scalar("top/also_a")
    assert "OC6502" in _run(
        tmp_path,
        (*sources, sink, extra),
        (),
        _requirement(source="top/*a"),
    )

    source, middle = _scalar("top/s"), _scalar("top/mid")
    edges = (ConnectivityEdge(source, middle), ConnectivityEdge(middle, sink))
    assert (
        _run(
            tmp_path,
            (source, middle, sink),
            edges,
            _requirement(source="top/s", through=("top/mid",)),
        )
        == ()
    )
    assert "OC6503" in _run(
        tmp_path,
        (source, middle, sink),
        edges,
        _requirement(source="top/s", exclude=("top/mid",)),
    )


def test_connectivity_model_rejects_internally_inconsistent_facts() -> None:
    source, sink = _scalar("top/a"), _scalar("top/y")
    with pytest.raises(ValueError, match="known inversion"):
        ConnectivityEdge(source, sink, inverted=None)
    with pytest.raises(ValueError, match="ordinal"):
        ConnectivityEndpoint("top/a", bit_index=0, ordinal=1, width=1)
    with pytest.raises(TypeError, match="description"):
        ConnectivityRequirement("P", "top/a", "top/y", description=4)  # type: ignore[arg-type]

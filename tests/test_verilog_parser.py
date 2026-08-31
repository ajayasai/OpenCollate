from __future__ import annotations

from pathlib import Path

from opencollate.config import ProjectConfig, SourceConfig
from opencollate.engine import ComparisonEngine
from opencollate.model import Direction, FactState, ViewId
from opencollate.parsers.sdc import parse_sdc
from opencollate.parsers.verilog import parse_verilog

FIXTURES = Path(__file__).parent / "fixtures" / "rtl"


def test_systemverilog_frontend_elaborates_parameterized_ports() -> None:
    view = parse_verilog(FIXTURES / "simple.sv")
    assert view.view == ViewId("rtl")
    assert view.complete
    assert not view.diagnostics
    components = {component.name: component for component in view.components}
    assert set(components) == {"alu", "legacy"}
    alu = components["alu"]
    ports = {port.name: port for port in alu.ports}
    assert ports["a"].shape.width == 8
    assert ports["a"].shape.left == 7
    assert ports["a"].shape.right == 0
    assert ports["y"].direction == Direction.OUTPUT
    assert alu.functions == {"y": "a[0] & b[0]"}
    assert {port.name: port.shape.ascending for port in components["legacy"].ports}["a"] is True


def test_top_filter_selects_one_component() -> None:
    view = parse_verilog(FIXTURES / "simple.sv", top="legacy")
    assert [component.name for component in view.components] == ["legacy"]


def test_preprocessor_include_define_and_escaped_identifier() -> None:
    view = parse_verilog(
        FIXTURES / "conditional.sv",
        include_dirs=(FIXTURES / "include",),
        defines={"ENABLE_IRQ": None},
    )
    assert view.complete
    component = view.components[0]
    ports = {port.name: port for port in component.ports}
    assert ports["payload"].shape.width == 5
    assert ports["irq"].direction == Direction.OUTPUT
    assert "foo.bar" in ports
    assert component.functions == {"irq": "payload[0]"}


def test_parse_error_taints_view_instead_of_claiming_clean() -> None:
    view = parse_verilog(FIXTURES / "malformed.sv")
    assert not view.complete
    assert view.tainted_scopes
    assert any(diagnostic.code == "OC1101" for diagnostic in view.diagnostics)


def test_unknown_top_is_fatal_and_not_silently_substituted() -> None:
    view = parse_verilog(FIXTURES / "simple.sv", top="not_here")
    assert not view.complete
    assert not view.components
    assert any("not defined" in diagnostic.message for diagnostic in view.diagnostics)


def test_name_heuristics_are_tainted_not_canonical_truth() -> None:
    view = parse_verilog(FIXTURES / "simple.sv", top="alu")
    clk = next(port for port in view.components[0].ports if port.name == "clk")
    assert clk.state_for("role") == FactState.TAINTED


def test_elaborated_hierarchy_is_indexed_for_constraint_checks(tmp_path: Path) -> None:
    source = tmp_path / "hierarchy.sv"
    source.write_text(
        """
module leaf(input logic d, output logic q);
  assign q = d;
endmodule
module top(input logic clk, output logic done);
  logic n;
  leaf u_leaf(.d(clk), .q(n));
  genvar i;
  for (i = 0; i < 2; i++) begin : lanes
    leaf u_lane(.d(n), .q());
  end
  assign done = n;
endmodule
""",
        encoding="utf-8",
    )

    view = parse_verilog(source, top="top")
    definitions = {(item.kind, item.native_name) for item in view.objects}

    assert ("port", "clk") in definitions
    assert ("net", "n") in definitions
    assert ("instance", "u_leaf") in definitions
    assert ("pin", "u_leaf/d") in definitions
    assert ("instance", "lanes[0]/u_lane") in definitions
    assert ("pin", "lanes[1]/u_lane/q") in definitions


def test_escaped_dots_are_consistent_in_design_objects_and_sdc_references(
    tmp_path: Path,
) -> None:
    source = tmp_path / "escaped_hierarchy.sv"
    source.write_text(
        "module leaf(input logic a, output logic q); assign q = a; endmodule\n"
        "module top(input logic src, output logic dst); logic \\n.withdot ; "
        "leaf \\u.withdot  (.a(src), .q(\\n.withdot )); "
        "assign dst = \\n.withdot ; endmodule\n",
        encoding="utf-8",
    )
    constraints = tmp_path / "escaped.sdc"
    constraints.write_text(
        "set_false_path -from [get_nets {n.withdot}] -to [get_pins {u.withdot/a}]\n",
        encoding="utf-8",
    )
    rtl_view = parse_verilog(source, top="top")
    sdc_view = parse_sdc(constraints)
    project = ProjectConfig(
        path=tmp_path / "opencollate.toml",
        root=tmp_path,
        name="escaped-reference",
        sources=(
            SourceConfig(ViewId("rtl"), (source,)),
            SourceConfig(ViewId("sdc"), (constraints,)),
        ),
    )

    result = ComparisonEngine(project).run((rtl_view, sdc_view))
    definitions = {(item.kind, item.native_name) for item in rtl_view.objects}

    assert ("net", "n.withdot") in definitions
    assert ("instance", "u.withdot") in definitions
    assert ("pin", "u.withdot/a") in definitions
    assert not [item for item in result.diagnostics if item.code in {"OC5001", "OC5002"}]


def test_static_connectivity_extracts_hierarchy_slices_concats_and_inversion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "connectivity.sv"
    source.write_text(
        """
module leaf(input logic [1:0] d, output logic [1:0] q);
  assign q = {d[0], ~d[1]};
endmodule
module top(input logic [1:0] a, output logic [1:0] y);
  logic [1:0] n;
  leaf u_leaf(.d(a[1:0]), .q(n));
  assign y = n;
endmodule
""",
        encoding="utf-8",
    )

    view = parse_verilog(source, top="top")
    edges = {
        (edge.source.key, edge.sink.key): (edge.status, edge.inverted)
        for edge in view.connectivity_edges
    }

    assert edges[("top/a[1]", "top/u_leaf/d[1]")] == (FactState.KNOWN, False)
    assert edges[("top/u_leaf/d[0]", "top/u_leaf/q[1]")] == (
        FactState.KNOWN,
        False,
    )
    assert edges[("top/u_leaf/d[1]", "top/u_leaf/q[0]")] == (
        FactState.KNOWN,
        True,
    )
    assert edges[("top/u_leaf/q[1]", "top/n[1]")] == (FactState.KNOWN, False)
    assert edges[("top/n[0]", "top/y[0]")] == (FactState.KNOWN, False)


def test_static_connectivity_marks_dynamic_and_procedural_cones_tainted(
    tmp_path: Path,
) -> None:
    source = tmp_path / "frontiers.sv"
    source.write_text(
        """
module top(
  input logic [3:0] a,
  input logic [1:0] index,
  input logic enable,
  output logic selected,
  output logic gated
);
  assign selected = a[index];
  always_comb gated = enable & a[0];
endmodule
""",
        encoding="utf-8",
    )

    view = parse_verilog(source, top="top")
    tainted = [edge for edge in view.connectivity_edges if edge.status == FactState.TAINTED]

    assert tainted
    assert any(edge.sink.key == "top/selected" for edge in tainted)
    assert any(edge.sink.key == "top/gated" for edge in tainted)
    assert all(edge.inverted is None for edge in tainted)
    assert not view.diagnostics


def test_static_connectivity_preserves_explicit_one_bit_vector_index(tmp_path: Path) -> None:
    source = tmp_path / "one_bit.sv"
    source.write_text(
        "module top(input logic [0:0] a, output logic [0:0] y); assign y=a; endmodule\n",
        encoding="utf-8",
    )

    view = parse_verilog(source, top="top")

    assert {item.key for item in view.connectivity_endpoints} == {"top/a[0]", "top/y[0]"}
    assert [
        (edge.source.key, edge.sink.key)
        for edge in view.connectivity_edges
        if edge.status == FactState.KNOWN
    ] == [("top/a[0]", "top/y[0]")]

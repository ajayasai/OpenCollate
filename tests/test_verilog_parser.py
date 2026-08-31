from __future__ import annotations

from pathlib import Path

from opencollate.model import Direction, FactState, ViewId
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

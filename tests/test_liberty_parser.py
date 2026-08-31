from __future__ import annotations

from pathlib import Path

from opencollate.model import Direction, FactState, PortRole, ViewId
from opencollate.parsers.liberty import parse_liberty

FIXTURES = Path(__file__).parent / "fixtures" / "liberty"


def test_liberty_extracts_cells_buses_pg_and_functions() -> None:
    view = parse_liberty(FIXTURES / "uart.lib")
    assert view.view == ViewId("liberty")
    assert view.complete
    assert not view.diagnostics
    assert len(view.components) == 1
    uart = view.components[0]
    ports = {port.name: port for port in uart.ports}
    assert ports["data"].shape.width == 8
    assert ports["data"].shape.ordered_indices == tuple(range(7, -1, -1))
    assert ports["data"].direction == Direction.INPUT
    assert ports["clk"].role == PortRole.CLOCK
    assert ports["VDD"].role == PortRole.POWER
    assert ports["VSS"].role == PortRole.GROUND
    assert uart.functions == {"irq": "clk & enable"}


def test_liberty_unknown_pg_direction_remains_unknown() -> None:
    view = parse_liberty(FIXTURES / "uart.lib")
    vdd = next(port for port in view.components[0].ports if port.name == "VDD")
    assert vdd.state_for("direction") == FactState.UNKNOWN


def test_liberty_malformed_scope_is_tainted() -> None:
    view = parse_liberty(FIXTURES / "malformed.lib")
    assert not view.complete
    assert view.tainted_scopes
    assert any(diagnostic.code == "OC1101" for diagnostic in view.diagnostics)
    assert all(component.status == FactState.TAINTED for component in view.components)


def test_liberty_angle_bracket_bus_name_is_decoded(tmp_path: Path) -> None:
    path = tmp_path / "angle.lib"
    path.write_text(
        "library(x) { cell(c) { pin(D<3:0>) { direction : input; } } }\n",
        encoding="utf-8",
    )
    view = parse_liberty(path)
    port = view.components[0].ports[0]
    assert port.name == "D"
    assert port.shape.width == 4
    assert port.shape.ascending is False


def test_deep_boolean_function_reports_unsupported_instead_of_recursing(
    tmp_path: Path,
) -> None:
    expression = "(" * 2_000 + "A" + ")" * 2_000
    source = tmp_path / "deep-function.lib"
    source.write_text(
        "library (deep) { cell (BUF) { "
        "pin (A) { direction : input; } "
        f'pin (Y) {{ direction : output; function : "{expression}"; }} '
        "} }\n",
        encoding="utf-8",
    )

    view = parse_liberty(source)

    assert any(
        item.code == "OC1102" and "nesting limit" in item.message for item in view.diagnostics
    )
    assert view.components[0].functions == {}


def test_deep_liberty_groups_report_fatal_instead_of_recursing(tmp_path: Path) -> None:
    source = tmp_path / "deep-groups.lib"
    source.write_text(
        "library (deep) { " + "vendor_group (x) { " * 2_000 + "} " * 2_001,
        encoding="utf-8",
    )

    view = parse_liberty(source)

    assert not view.complete
    assert view.tainted_scopes == frozenset({"*"})
    assert any(
        item.code == "OC1101" and "group nesting" in item.message for item in view.diagnostics
    )

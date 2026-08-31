from __future__ import annotations

from pathlib import Path

from opencollate.model import Direction, FactState, PortRole, ViewId
from opencollate.parsers.lef import parse_lef

FIXTURES = Path(__file__).parent / "fixtures" / "lef"


def test_lef_extracts_macro_bus_direction_and_roles() -> None:
    view = parse_lef(FIXTURES / "uart.lef")
    assert view.view == ViewId("lef")
    assert view.complete
    assert not view.diagnostics
    uart = view.components[0]
    ports = {port.name: port for port in uart.ports}
    assert ports["data"].shape.width == 4
    assert ports["data"].shape.bit_indices == (3, 2, 1, 0)
    assert ports["data"].direction == Direction.INPUT
    assert ports["irq"].direction == Direction.OUTPUT
    assert ports["VDD"].role == PortRole.POWER
    assert ports["VSS"].role == PortRole.GROUND
    assert ports["scan_feed"].direction == Direction.FEEDTHROUGH
    assert uart.attributes["busbitchars"] == "<>"


def test_geometry_is_safely_ignored() -> None:
    view = parse_lef(FIXTURES / "uart.lef")
    data = next(port for port in view.components[0].ports if port.name == "data")
    assert data.status == FactState.KNOWN
    assert not view.tainted_scopes


def test_unclosed_pin_taints_macro() -> None:
    view = parse_lef(FIXTURES / "malformed.lef")
    assert not view.complete
    assert "broken" in view.tainted_scopes
    assert view.components[0].status == FactState.TAINTED
    assert any(diagnostic.code == "OC1101" for diagnostic in view.diagnostics)


def test_exploded_bus_gap_is_preserved_for_checker(tmp_path: Path) -> None:
    path = tmp_path / "gap.lef"
    path.write_text(
        """
MACRO gap
  PIN D[3]
    DIRECTION INPUT ;
  END D[3]
  PIN D[1]
    DIRECTION INPUT ;
  END D[1]
END gap
""".strip()
        + "\n",
        encoding="utf-8",
    )
    port = parse_lef(path).components[0].ports[0]
    assert port.shape.bit_indices == (3, 1)
    assert port.shape.has_bit_gap

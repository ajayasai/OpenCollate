from __future__ import annotations

from pathlib import Path

import pytest

from opencollate.model import (
    DesignObjectObservation,
    Direction,
    FactState,
    PortRole,
    ViewId,
    ViewObservation,
)
from opencollate.parsers.defparser import DefLimits, DefParser, parse_def

FIXTURES = Path(__file__).parent / "fixtures" / "def"


def _objects(
    view: ViewObservation,
    *,
    kind: str,
    relation: str = "definition",
) -> list[DesignObjectObservation]:
    return [item for item in view.objects if item.kind == kind and item.relation == relation]


def test_def_extracts_design_interface_and_bus_order() -> None:
    view = parse_def(FIXTURES / "soc.def")

    assert view.view == ViewId("def")
    assert view.complete
    assert not view.diagnostics
    assert len(view.components) == 1
    top = view.components[0]
    assert top.name == "soc_top"
    assert top.attributes["units_per_micron"] == 1000
    ports = {port.name: port for port in top.ports}
    assert ports["clk"].direction == Direction.INPUT
    assert ports["clk"].role == PortRole.CLOCK
    assert ports["data"].shape.width == 2
    assert ports["data"].shape.bit_indices == (0, 1)
    assert ports["data"].attributes["bit_order_known"] is False
    assert ports["data"].direction == Direction.INPUT
    assert ports["irq"].direction == Direction.OUTPUT
    assert ports["VDD"].role == PortRole.POWER
    assert ports["VSS"].role == PortRole.GROUND
    file_attributes = view.attributes["files"][0]
    assert file_attributes["version"] == "5.8"
    assert file_attributes["section_counts"]["PINS"] == {
        "declared": 6,
        "parsed": 6,
    }
    assert file_attributes["ignored_sections"]["PROPERTYDEFINITIONS"] is None


def test_def_preserves_placements_hierarchy_and_provenance() -> None:
    view = parse_def(FIXTURES / "soc.def")

    instances = {item.native_name: item for item in _objects(view, kind="instance")}
    assert set(instances) == {"u_cpu", "u_cluster/core0", r"u_mem\/bank0", "u_pad"}
    assert instances["u_cpu"].attributes["placement"] == {
        "status": "placed",
        "x": 100,
        "y": 200,
        "orientation": "N",
        "state": "known",
    }
    assert instances["u_cluster/core0"].attributes["hierarchical"] is True
    memory = instances[r"u_mem\/bank0"]
    assert memory.attributes["hierarchical"] is False
    assert memory.attributes["placement"]["x"] == -20
    assert memory.provenance is not None
    assert memory.provenance.raw_name == r"u_mem\/bank0"
    assert memory.provenance.line > 1
    assert memory.provenance.column > 1


def test_def_nets_and_special_nets_emit_bounded_endpoint_references() -> None:
    view = parse_def(FIXTURES / "soc.def")

    nets = {item.native_name: item for item in _objects(view, kind="net")}
    assert set(nets) == {"clk", "data[1]", "data[0]", "irq", "VDD", "VSS"}
    assert nets["clk"].attributes["special"] is False
    assert nets["VDD"].attributes["special"] is True
    assert nets["VDD"].attributes["use"] == "power"
    assert len(nets["clk"].attributes["connections"]) == 2
    assert len(nets["VDD"].attributes["connections"]) == 3
    # Routed geometry parentheses are clauses, never guessed as connections.
    assert all(
        connection["pin"] not in {"0", "100", "500", "1000"}
        for net in nets.values()
        for connection in net.attributes["connections"]
    )
    instance_references = _objects(view, kind="instance", relation="reference")
    assert {item.native_name for item in instance_references} >= {
        "u_cpu",
        r"u_mem\/bank0",
    }
    endpoint = next(
        item
        for item in _objects(view, kind="pin", relation="reference")
        if item.native_name == r"u_mem\/bank0/VDD"
    )
    assert endpoint.attributes["net"] == "VDD"
    assert endpoint.attributes["special_net"] is True


def test_def_does_not_invent_package_or_die_pad_mappings() -> None:
    view = parse_def(FIXTURES / "soc.def")

    assert not view.pin_mappings
    pins = {item.native_name: item for item in _objects(view, kind="pin")}
    assert pins["VDD"].attributes["net"] == "VDD"
    assert pins["VDD"].attributes["placement"]["status"] == "placed"
    assert pins["irq"].attributes["placement"]["status"] == "unplaced"


def test_placement_only_def_does_not_claim_an_empty_interface(tmp_path: Path) -> None:
    source = tmp_path / "floorplan.def"
    source.write_text(
        "DESIGN floorplan ; COMPONENTS 1 ; - u0 CELL + PLACED ( 1 2 ) N ; "
        "END COMPONENTS END DESIGN\n",
        encoding="utf-8",
    )

    view = parse_def(source)

    assert view.complete
    assert not view.components
    assert {item.kind for item in view.objects} == {"design", "instance"}


def test_malformed_def_reports_tainted_facts_without_guessing() -> None:
    view = parse_def(FIXTURES / "malformed.def")

    assert not view.complete
    assert "broken" in view.tainted_scopes
    assert {item.code for item in view.diagnostics} >= {"OC1101", "OC1102"}
    assert any(item.severity.value == "fatal" for item in view.diagnostics)
    top = view.components[0]
    mystery = next(port for port in top.ports if port.name == "mystery")
    assert mystery.direction == Direction.UNKNOWN
    assert mystery.role == PortRole.UNKNOWN
    assert mystery.status == FactState.TAINTED
    assert not view.pin_mappings


@pytest.mark.parametrize(
    ("text", "limits"),
    [
        ("VERSION 5.8 ; DESIGN top ; END DESIGN\n", DefLimits(max_file_bytes=8)),
        ("VERSION 5.8 ; DESIGN top ; END DESIGN\n", DefLimits(max_tokens=3)),
        (
            "DESIGN top ; COMPONENTS 2 ; - a A ; - b B ; END COMPONENTS END DESIGN\n",
            DefLimits(max_section_entries=1),
        ),
        (
            "DESIGN top ; COMPONENTS 1 ; - a A + PROPERTY p v ; END COMPONENTS END DESIGN\n",
            DefLimits(max_entry_tokens=2),
        ),
        (
            "DESIGN top ; DIEAREA ( ( 0 0 ) ) ; END DESIGN\n",
            DefLimits(max_parenthesis_depth=1),
        ),
    ],
)
def test_def_resource_limits_emit_oc1102(
    tmp_path: Path,
    text: str,
    limits: DefLimits,
) -> None:
    source = tmp_path / "bounded.def"
    source.write_text(text, encoding="utf-8")

    view = parse_def(source, limits=limits)

    assert not view.complete
    assert "*" in view.tainted_scopes
    assert any(item.code == "OC1102" for item in view.diagnostics)


def test_def_parser_adapter_preserves_named_view(tmp_path: Path) -> None:
    source = tmp_path / "fragment.def"
    source.write_text("DESIGN tiny ; PINS 0 ; END PINS END DESIGN\n", encoding="utf-8")

    view = DefParser().parse([source], view_id="def.floorplan")

    assert view.view == ViewId("def", "floorplan")
    assert view.complete
    assert view.components[0].name == "tiny"

from __future__ import annotations

from pathlib import Path

from opencollate.model import (
    DesignObjectObservation,
    Direction,
    FactState,
    PortRole,
    ViewId,
    ViewObservation,
)
from opencollate.parsers.upf import UpfParser, parse_upf

FIXTURES = Path(__file__).parent / "fixtures" / "upf"


def _objects(
    view: ViewObservation,
    *,
    kind: str,
    relation: str,
) -> list[DesignObjectObservation]:
    return [item for item in view.objects if item.kind == kind and item.relation == relation]


def test_static_upf_extracts_domains_supplies_and_component_ports() -> None:
    view = parse_upf(FIXTURES / "soc.upf")

    assert view.view == ViewId("upf")
    assert view.complete
    assert not view.diagnostics
    assert view.attributes["upf_versions"] == ["3.1"]
    assert {item["name"] for item in view.attributes["power_domains"]} == {
        "PD_TOP",
        "PD_CPU",
        "PD_CLUSTER",
    }
    cpu_domain = next(item for item in view.attributes["power_domains"] if item["name"] == "PD_CPU")
    assert cpu_domain["elements"] == ["u_cpu", "u_mem"]
    assert {item["name"] for item in view.attributes["supply_nets"]} == {
        "VDD",
        "VSS",
        "VDD_CPU",
    }
    assert view.attributes["supply_sets"][0]["functions"] == [
        ["power", "VDD_CPU"],
        ["ground", "VSS"],
    ]

    assert len(view.components) == 1
    top = view.components[0]
    assert top.name == "soc_top"
    ports = {port.name: port for port in top.ports}
    assert ports["VDD"].direction == Direction.INPUT
    assert ports["VDD"].role == PortRole.POWER
    assert ports["VDD"].state_for("role") == FactState.KNOWN
    assert ports["VSS"].role == PortRole.GROUND
    assert ports["VSS"].shape.width == 1


def test_upf_emits_first_class_definitions_and_rtl_references() -> None:
    view = parse_upf(FIXTURES / "soc.upf")

    domain_definitions = _objects(view, kind="power_domain", relation="definition")
    assert {item.native_name for item in domain_definitions} == {
        "PD_TOP",
        "PD_CPU",
        "PD_CLUSTER",
    }
    assert {
        item.native_name for item in _objects(view, kind="supply_net", relation="definition")
    } == {
        "VDD",
        "VSS",
        "VDD_CPU",
    }
    assert {
        item.native_name for item in _objects(view, kind="power_switch", relation="definition")
    } == {"SW_CPU"}

    instance_references = _objects(view, kind="instance", relation="reference")
    assert {item.qualified_name for item in instance_references} >= {
        "u_cpu",
        "u_mem",
        "u_cpu/state_regs",
        "u_cluster/u0",
        "u_cluster/u1",
    }
    iso_signal = next(
        item
        for item in _objects(view, kind="pin", relation="reference")
        if item.native_name == "pmu/iso_enable"
    )
    assert iso_signal.attributes["command"] == "set_isolation_control"
    assert iso_signal.attributes["domain"] == "PD_CPU"
    assert iso_signal.provenance is not None
    assert iso_signal.provenance.line > 1


def test_upf_extracts_strategies_switches_and_power_states() -> None:
    view = parse_upf(FIXTURES / "soc.upf")

    assert {item["name"] for item in view.attributes["isolation"]} == {
        "ISO_CPU",
        "ISO_CPU_CTRL",
    }
    assert view.attributes["isolation"][0]["isolation_signal"] == ["iso_enable", "high"]
    assert {item["name"] for item in view.attributes["retention"]} == {
        "RET_CPU",
        "RET_CPU_CTRL",
    }
    assert view.attributes["level_shifters"][0]["name"] == "LS_CPU"
    switch = view.attributes["power_switches"][0]
    assert switch["input_supply_port"] == [["VIN", "VDD"]]
    assert switch["control_port"] == [["CTRL", "pmu/sleep_cpu"]]
    assert view.attributes["port_states"][0]["states"] == [
        ["FULL_ON", "1.0"],
        ["OFF", "off"],
    ]
    power_state_names = {
        item.native_name for item in _objects(view, kind="power_state", relation="definition")
    }
    assert power_state_names == {"ACTIVE", "SLEEP"}


def test_dynamic_tcl_is_not_executed_and_taints_explicit_facts(tmp_path: Path) -> None:
    sentinel = tmp_path / "should-not-exist"
    text = (FIXTURES / "dynamic.upf").read_text(encoding="utf-8")
    source = tmp_path / "dynamic.upf"
    source.write_text(text.replace("should-not-exist", str(sentinel)), encoding="utf-8")

    view = parse_upf(source)

    assert not sentinel.exists()
    assert not view.complete
    assert view.tainted_scopes
    assert any(item.code == "OC1102" for item in view.diagnostics)
    assert view.attributes["unsupported_facts"]
    assert all(
        item["state"] == FactState.UNSUPPORTED.value
        for item in view.attributes["unsupported_facts"]
    )
    dynamic_domain = next(
        item
        for item in _objects(view, kind="power_domain", relation="definition")
        if item.native_name == "PD_DYNAMIC"
    )
    assert dynamic_domain.status == FactState.TAINTED


def test_malformed_tcl_reports_fatal_and_taints_view() -> None:
    view = parse_upf(FIXTURES / "malformed.upf")

    assert not view.complete
    assert "*" in view.tainted_scopes
    assert any(
        item.code == "OC1101" and item.severity.value == "fatal" for item in view.diagnostics
    )


def test_supply_ports_are_not_mapped_to_a_guessed_component(tmp_path: Path) -> None:
    source = tmp_path / "fragment.upf"
    source.write_text("create_supply_port VDD -direction in\n", encoding="utf-8")

    view = UpfParser().parse([source], view_id="upf.fragment")

    assert view.view == ViewId("upf", "fragment")
    assert not view.components
    assert {
        item.native_name for item in _objects(view, kind="supply_port", relation="definition")
    } == {"VDD"}


def test_update_semantics_are_preserved_on_definitions(tmp_path: Path) -> None:
    source = tmp_path / "updates.upf"
    source.write_text(
        "create_power_domain PD -elements {u1}\n"
        "create_power_domain PD -update -elements {u2}\n"
        "create_supply_set SS -function {power VDD}\n"
        "create_supply_set SS -update -function {ground VSS}\n",
        encoding="utf-8",
    )

    view = parse_upf(source)
    domains = _objects(view, kind="power_domain", relation="definition")
    supply_sets = _objects(view, kind="supply_set", relation="definition")

    assert [item.attributes.get("update") for item in domains] == [False, True]
    assert [item.attributes.get("update") for item in supply_sets] == [False, True]

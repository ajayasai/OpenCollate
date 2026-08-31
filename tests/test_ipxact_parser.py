from __future__ import annotations

from pathlib import Path

from opencollate.config import ContractSettings, ProjectConfig, SourceConfig
from opencollate.diagnostics import Severity
from opencollate.engine import ComparisonEngine
from opencollate.model import Direction, FactState, PortRole, ViewId
from opencollate.parsers.ipxact import IpxactParser, parse_ip_xact, parse_ipxact

FIXTURES = Path(__file__).parent / "fixtures" / "ipxact"


def test_2009_component_ports_and_port_maps_are_typed() -> None:
    view = parse_ipxact(FIXTURES / "component-2009.xml")

    assert view.view == ViewId("ipxact")
    assert view.complete
    assert not view.diagnostics
    component = view.components[0]
    assert component.name == "legacy_uart"
    ports = {port.name: port for port in component.ports}
    assert ports["pclk"].direction == Direction.INPUT
    assert ports["data"].direction == Direction.OUTPUT
    assert ports["data"].shape.ordered_indices == tuple(range(7, -1, -1))
    interface = view.interfaces[0]
    assert interface.native_name == "APB"
    assert interface.mode == "slave"
    assert interface.bus_type == "amba.com:AMBA3:APB:1.0"
    assert interface.abstraction_type == "amba.com:AMBA3:APB_rtl:1.0"
    assert interface.port_maps == {"PCLK": "pclk", "PWDATA": "data[7:0]"}
    assert (
        interface.attributes["port_maps"][1]["physical_selection"]["ranges"][0]["left"]["value"]
        == 7
    )


def test_2014_parameters_memory_map_registers_and_fields_are_resolved() -> None:
    view = parse_ip_xact(FIXTURES / "component-2014.xml", view_id="ipxact.csr")

    assert view.view == ViewId("ipxact", "csr")
    assert view.complete
    assert not view.diagnostics
    uart = view.components[0]
    data = next(port for port in uart.ports if port.name == "data")
    assert data.shape.width == 8
    parameters = {item["id"]: item for item in uart.attributes["parameters"]}
    assert parameters["DATA_WIDTH"]["value"] == 8
    assert parameters["BASE_ADDR"]["value"] == 0x1000

    register = view.registers[0]
    assert register.native_name == "CTRL"
    assert register.memory_map == "regs"
    assert register.address_block == "uart_regs"
    assert register.address_offset == 4
    assert register.absolute_address == 0x1004
    assert register.size_bits == 32
    assert register.access == "read-write"
    fields = {field.native_name: field for field in register.fields}
    assert fields["ENABLE"].bit_offset == 0
    assert fields["ENABLE"].bit_width == 1
    assert fields["ENABLE"].reset_value == 0
    assert fields["MODE"].bit_offset == 4
    assert fields["MODE"].bit_width == 2
    assert view.interfaces[0].attributes["memory_map_ref"] == "regs"

    object_kinds = {item.kind for item in view.objects}
    assert {
        "component",
        "port",
        "interface",
        "bus_definition",
        "abstraction_definition",
        "memory_map",
        "address_block",
        "register",
        "register_field",
        "parameter",
    }.issubset(object_kinds)


def test_2022_namespace_variant_qualifiers_vectors_and_arrays() -> None:
    view = IpxactParser().parse((FIXTURES / "component-2022.xml",))

    assert view.complete
    assert view.attributes["namespace_versions"] == {str(FIXTURES / "component-2022.xml"): "2022"}
    ports = {port.name: port for port in view.components[0].ports}
    assert ports["clk"].role == PortRole.CLOCK
    assert ports["clk"].state_for("role") == FactState.KNOWN
    assert ports["lane"].role == PortRole.SIGNAL
    assert ports["lane"].shape.width == 4
    assert ports["lane"].shape.packed[0].ordered_indices == (3, 2, 1, 0)
    assert ports["lane"].shape.unpacked[0].ordered_indices == (1, 0)
    assert view.interfaces[0].mode == "target"
    assert view.interfaces[0].port_maps == {"GPIO_OUT": "lane"}
    assert len(view.clocks) == 1
    assert view.clocks[0].native_name == "clk"
    assert view.clocks[0].targets == ("gpio2022/clk",)


def test_external_integer_override_resolves_vector_without_exec(tmp_path: Path) -> None:
    path = tmp_path / "override.xml"
    path.write_text(
        """<?xml version="1.0"?>
<ipxact:component xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2022">
  <ipxact:vendor>example.org</ipxact:vendor><ipxact:library>test</ipxact:library>
  <ipxact:name>override_ip</ipxact:name><ipxact:version>1</ipxact:version>
  <ipxact:model><ipxact:ports><ipxact:port><ipxact:name>data</ipxact:name>
    <ipxact:wire><ipxact:direction>out</ipxact:direction><ipxact:vectors>
      <ipxact:vector>
        <ipxact:left>${WIDTH} - 1</ipxact:left><ipxact:right>0</ipxact:right>
      </ipxact:vector>
    </ipxact:vectors></ipxact:wire>
  </ipxact:port></ipxact:ports></ipxact:model>
</ipxact:component>
""",
        encoding="utf-8",
    )

    view = parse_ipxact(path, parameter_values={"WIDTH": 16})

    assert view.complete
    assert not view.diagnostics
    assert view.components[0].ports[0].shape.width == 16


def test_unresolved_facts_are_diagnosed_and_never_invented() -> None:
    view = parse_ipxact(FIXTURES / "unresolved.xml")

    assert not view.complete
    assert view.tainted_scopes == frozenset({"unresolved_ip"})
    assert view.components[0].status == FactState.TAINTED
    data = view.components[0].ports[0]
    assert data.shape.width is None
    assert data.state_for("shape") == FactState.TAINTED
    assert view.interfaces[0].status == FactState.KNOWN
    assert view.registers[0].address_offset is None
    assert view.registers[0].absolute_address is None
    assert view.registers[0].fields[0].bit_width is None
    assert view.registers[0].fields[0].status == FactState.TAINTED
    assert len([item for item in view.diagnostics if item.code == "OC1103"]) == 4
    assert all(item.severity == Severity.WARNING for item in view.diagnostics)


def test_malformed_xml_is_fatal_and_has_source_location(tmp_path: Path) -> None:
    path = tmp_path / "malformed.xml"
    path.write_text(
        '<ipxact:component xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2014">\n'
        "  <ipxact:name>broken</ipxact:name>\n",
        encoding="utf-8",
    )

    view = parse_ipxact(path)

    assert not view.components
    assert view.tainted_scopes == frozenset({"*"})
    diagnostic = view.diagnostics[0]
    assert diagnostic.code == "OC1101"
    assert diagnostic.severity == Severity.FATAL
    assert diagnostic.provenance is not None
    assert diagnostic.provenance.line >= 2


def test_doctype_and_entity_declarations_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "entity.xml"
    path.write_text(
        """<?xml version="1.0"?>
<!DOCTYPE component [<!ENTITY component_name "unsafe">]>
<ipxact:component xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2014">
  <ipxact:name>&component_name;</ipxact:name>
</ipxact:component>
""",
        encoding="utf-8",
    )

    view = parse_ipxact(path)

    assert not view.components
    assert any(item.code == "OC1101" and "DOCTYPE" in item.message for item in view.diagnostics)


def test_unsupported_namespace_retains_but_taints_local_facts(tmp_path: Path) -> None:
    path = tmp_path / "vendor.xml"
    path.write_text(
        """<v:component xmlns:v="urn:vendor:almost-ipxact">
  <v:vendor>vendor</v:vendor><v:library>lib</v:library>
  <v:name>vendor_ip</v:name><v:version>1</v:version>
  <v:model><v:ports><v:port><v:name>irq</v:name>
    <v:wire><v:direction>out</v:direction></v:wire>
  </v:port></v:ports></v:model>
</v:component>
""",
        encoding="utf-8",
    )

    view = parse_ipxact(path)

    assert view.components[0].name == "vendor_ip"
    assert view.components[0].ports[0].direction == Direction.OUTPUT
    assert view.components[0].status == FactState.TAINTED
    assert view.components[0].ports[0].status == FactState.TAINTED
    assert view.components[0].ports[0].state_for("direction") == FactState.TAINTED
    assert not view.complete
    assert any(item.code == "OC1102" and "namespace" in item.message for item in view.diagnostics)


def test_multiple_namespace_versions_can_be_imported_together() -> None:
    view = parse_ipxact(
        (
            FIXTURES / "component-2009.xml",
            FIXTURES / "component-2014.xml",
            FIXTURES / "component-2022.xml",
        )
    )

    assert view.complete
    assert [component.name for component in view.components] == [
        "legacy_uart",
        "uart",
        "gpio2022",
    ]
    assert set(view.attributes["namespace_versions"].values()) == {"2009", "2014", "2022"}


def test_parameter_dependency_chain_is_bounded_without_recursion_failure(
    tmp_path: Path,
) -> None:
    parameters = "\n".join(
        '<ipxact:parameter parameterId="P{index}">'
        "<ipxact:name>P{index}</ipxact:name>"
        "<ipxact:value>{value}</ipxact:value>"
        "</ipxact:parameter>".format(
            index=index,
            value=f"P{index + 1}" if index < 79 else "1",
        )
        for index in range(80)
    )
    path = tmp_path / "deep-parameters.xml"
    path.write_text(
        "<ipxact:component "
        'xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2014">'
        "<ipxact:vendor>example.org</ipxact:vendor>"
        "<ipxact:library>test</ipxact:library>"
        "<ipxact:name>bounded</ipxact:name>"
        "<ipxact:version>1</ipxact:version>"
        "<ipxact:memoryMaps><ipxact:memoryMap><ipxact:name>regs</ipxact:name>"
        "<ipxact:addressBlock><ipxact:name>block</ipxact:name>"
        "<ipxact:baseAddress>P0</ipxact:baseAddress>"
        "<ipxact:range>4</ipxact:range><ipxact:width>32</ipxact:width>"
        "</ipxact:addressBlock></ipxact:memoryMap></ipxact:memoryMaps>"
        f"<ipxact:parameters>{parameters}</ipxact:parameters>"
        "</ipxact:component>",
        encoding="utf-8",
    )

    view = parse_ipxact(path)

    assert view.components[0].name == "bounded"
    assert not view.complete
    assert any(
        item.code == "OC1103" and "dependency chain" in item.message for item in view.diagnostics
    )


def test_addresses_are_canonical_bytes_and_register_arrays_expand() -> None:
    view = parse_ipxact(FIXTURES / "component-arrays-2022.xml")

    assert view.complete
    assert not view.diagnostics
    registers = {register.native_name: register for register in view.registers}
    assert list(registers) == ["DATA[0]", "DATA[1]", "CHA", "CHB", "CHC", "DEFAULT"]
    assert registers["DATA[0]"].address_offset == 4
    assert registers["DATA[0]"].absolute_address == 4096 + 4
    assert registers["DATA[1]"].address_offset == 12
    assert registers["DATA[1]"].absolute_address == 4096 + 12
    assert registers["DATA[1]"].attributes["array_indices"] == [1]
    assert registers["DATA[1]"].attributes["array"]["stride_bytes"]["value"] == 8
    assert [registers[name].absolute_address for name in ("CHA", "CHB", "CHC")] == [
        4096 + 40,
        4096 + 56,
        4096 + 72,
    ]
    assert registers["DEFAULT"].absolute_address == 36

    maps = view.components[0].attributes["memory_maps"]
    assert maps[0]["address_unit_bits"]["value"] == 32
    assert maps[0]["bytes_per_address_unit"]["value"] == 4
    assert maps[0]["address_blocks"][0]["base_address"]["value"] == 4096
    assert maps[1]["address_unit_bits_defaulted"] is True
    assert maps[1]["address_unit_bits"]["value"] == 8


def test_oversized_register_array_is_bounded_and_never_known(tmp_path: Path) -> None:
    path = tmp_path / "oversized-array.xml"
    path.write_text(
        """<ipxact:component xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2014">
  <ipxact:name>bounded_array</ipxact:name>
  <ipxact:memoryMaps><ipxact:memoryMap><ipxact:name>regs</ipxact:name>
    <ipxact:addressBlock><ipxact:name>block</ipxact:name>
      <ipxact:baseAddress>0</ipxact:baseAddress><ipxact:range>1</ipxact:range>
      <ipxact:width>32</ipxact:width>
      <ipxact:register><ipxact:name>DATA%s</ipxact:name>
        <ipxact:dim>65537</ipxact:dim><ipxact:addressOffset>0</ipxact:addressOffset>
        <ipxact:size>32</ipxact:size><ipxact:dimIncrement>1</ipxact:dimIncrement>
      </ipxact:register>
    </ipxact:addressBlock>
  </ipxact:memoryMap></ipxact:memoryMaps>
</ipxact:component>
""",
        encoding="utf-8",
    )

    view = parse_ipxact(path)

    assert not view.complete
    assert len(view.registers) == 1
    assert view.registers[0].native_name == "DATA%s"
    assert view.registers[0].status == FactState.TAINTED
    assert view.registers[0].absolute_address is None
    assert any("bounded expansion limit" in item.message for item in view.diagnostics)


def test_unresolved_array_dimension_keeps_only_a_tainted_template(tmp_path: Path) -> None:
    source = (FIXTURES / "component-arrays-2022.xml").read_text(encoding="utf-8")
    source = source.replace(
        "<ipxact:dim>3</ipxact:dim>",
        "<ipxact:dim>CHANNEL_COUNT</ipxact:dim>",
        1,
    )
    path = tmp_path / "unresolved-array.xml"
    path.write_text(source, encoding="utf-8")

    view = parse_ipxact(path)

    template = next(register for register in view.registers if register.native_name == "CH%s")
    assert template.status == FactState.TAINTED
    assert template.address_offset is None
    assert not any(register.native_name in {"CHA", "CHB", "CHC"} for register in view.registers)
    assert any(
        item.code == "OC1103" and "CHANNEL_COUNT" in item.message for item in view.diagnostics
    )


def test_non_byte_address_units_are_diagnosed_without_invented_addresses(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bit-addressed.xml"
    path.write_text(
        """<ipxact:component xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2022">
  <ipxact:name>bit_addressed</ipxact:name>
  <ipxact:memoryMaps><ipxact:memoryMap><ipxact:name>regs</ipxact:name>
    <ipxact:addressBlock><ipxact:name>block</ipxact:name>
      <ipxact:baseAddress>0</ipxact:baseAddress><ipxact:range>4</ipxact:range>
      <ipxact:width>32</ipxact:width>
      <ipxact:register><ipxact:name>STATUS</ipxact:name>
        <ipxact:addressOffset>1</ipxact:addressOffset><ipxact:size>32</ipxact:size>
      </ipxact:register>
    </ipxact:addressBlock>
    <ipxact:addressUnitBits>12</ipxact:addressUnitBits>
  </ipxact:memoryMap></ipxact:memoryMaps>
</ipxact:component>
""",
        encoding="utf-8",
    )

    view = parse_ipxact(path)

    assert not view.complete
    assert view.registers[0].address_offset is None
    assert view.registers[0].absolute_address is None
    assert view.registers[0].status == FactState.TAINTED
    assert any(
        item.code == "OC1102" and "whole number of bytes" in item.message
        for item in view.diagnostics
    )


def test_disjoint_physical_slices_keep_collision_identity() -> None:
    view = parse_ipxact(FIXTURES / "sliced-port-maps.xml")

    assert view.complete
    assert not view.diagnostics
    interface = view.interfaces[0]
    assert interface.port_maps == {"LO": "data[3:0]", "HI": "data[7:4]"}
    assert interface.attributes["allow_many_to_one"] is True
    assert interface.attributes["disjoint_physical_slices"] is True

    project = ProjectConfig(
        path=Path("opencollate.toml"),
        root=Path("."),
        name="sliced-interface",
        sources=(SourceConfig(ViewId("ipxact"), (Path("sliced.xml"),)),),
        contract=ContractSettings(),
    )
    result = ComparisonEngine(project).run((view,))
    assert not any(item.code == "OC6202" for item in result.diagnostics)


def test_overlapping_physical_slices_are_diagnosed_and_tainted(tmp_path: Path) -> None:
    source = (FIXTURES / "sliced-port-maps.xml").read_text(encoding="utf-8")
    source = source.replace(
        "<ipxact:left>7</ipxact:left><ipxact:right>4</ipxact:right>",
        "<ipxact:left>5</ipxact:left><ipxact:right>2</ipxact:right>",
        1,
    )
    path = tmp_path / "overlapping.xml"
    path.write_text(source, encoding="utf-8")

    view = parse_ipxact(path)

    assert not view.complete
    assert view.interfaces[0].status == FactState.TAINTED
    assert view.interfaces[0].attributes["allow_many_to_one"] is False
    assert any(item.code == "OC1101" and "overlapping" in item.message for item in view.diagnostics)

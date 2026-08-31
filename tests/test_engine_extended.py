from __future__ import annotations

from pathlib import Path

from opencollate.config import ContractSettings, PolicySettings, ProjectConfig, SourceConfig
from opencollate.engine import ComparisonEngine
from opencollate.model import (
    BusShape,
    ClockObservation,
    ComponentKind,
    ComponentObservation,
    DesignContract,
    DesignObjectObservation,
    Direction,
    InterfaceObservation,
    PinMappingObservation,
    PortObservation,
    PortRole,
    Provenance,
    RegisterFieldObservation,
    RegisterObservation,
    ViewId,
    ViewObservation,
)


def _project(*views: str, policy: PolicySettings | None = None) -> ProjectConfig:
    sources = tuple(SourceConfig(ViewId.parse(view), (Path(f"{view}.input"),)) for view in views)
    return ProjectConfig(
        path=Path("opencollate.toml"),
        root=Path("."),
        name="extended-engine",
        sources=sources,
        contract=ContractSettings(),
        policy=policy or PolicySettings(),
    )


def _location(view: str, name: str) -> Provenance:
    return Provenance(f"{view}.src", 1, 1, ViewId.parse(view), raw_name=name)


def _rtl() -> ViewObservation:
    view = "rtl.default"
    component = ComponentObservation(
        "top",
        ComponentKind.MODULE,
        (
            PortObservation(
                "clk",
                Direction.INPUT,
                PortRole.CLOCK,
                BusShape.scalar(),
                _location(view, "clk"),
            ),
            PortObservation(
                "data",
                Direction.INPUT,
                PortRole.SIGNAL,
                BusShape.scalar(),
                _location(view, "data"),
            ),
        ),
        provenance=_location(view, "top"),
    )
    return ViewObservation(
        ViewId.parse(view),
        (component,),
        objects=(
            DesignObjectObservation("port", "clk", scope="top", provenance=_location(view, "clk")),
            DesignObjectObservation(
                "instance", "u_uart", scope="top", provenance=_location(view, "u_uart")
            ),
            DesignObjectObservation(
                "pin", "u_uart/irq", scope="top", provenance=_location(view, "irq")
            ),
        ),
    )


def test_sdc_and_upf_references_resolve_against_elaborated_rtl() -> None:
    sdc_view = "sdc.functional"
    upf_view = "upf.low_power"
    sdc = ViewObservation(
        ViewId.parse(sdc_view),
        objects=(
            DesignObjectObservation(
                "port",
                "clk",
                relation="reference",
                provenance=_location(sdc_view, "clk"),
                attributes={"command": "create_clock"},
            ),
            DesignObjectObservation(
                "cell",
                "missing_cell",
                relation="reference",
                provenance=_location(sdc_view, "missing_cell"),
                attributes={"command": "get_cells"},
            ),
        ),
    )
    upf = ViewObservation(
        ViewId.parse(upf_view),
        objects=(
            DesignObjectObservation(
                "instance",
                "u_uart",
                relation="reference",
                provenance=_location(upf_view, "u_uart"),
                attributes={"command": "create_power_domain"},
            ),
            DesignObjectObservation(
                "instance",
                "u_missing",
                relation="reference",
                provenance=_location(upf_view, "u_missing"),
                attributes={"command": "create_power_domain"},
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", sdc_view, upf_view)).run((_rtl(), sdc, upf))

    assert [item.code for item in result.diagnostics].count("OC6001") == 1
    assert [item.code for item in result.diagnostics].count("OC6101") == 1
    assert "missing_cell" in next(
        item.message for item in result.diagnostics if item.code == "OC6001"
    )


def test_sdc_regexp_and_nocase_glob_queries_are_matched_without_false_absence() -> None:
    sdc = ViewObservation(
        ViewId("sdc"),
        objects=(
            DesignObjectObservation(
                "port",
                "^cl.*$",
                relation="reference",
                attributes={"pattern": True, "match_mode": "regexp"},
            ),
            DesignObjectObservation(
                "port",
                "lk",
                relation="reference",
                attributes={"pattern": True, "match_mode": "regexp"},
            ),
            DesignObjectObservation(
                "port",
                "CL*",
                relation="reference",
                attributes={
                    "pattern": True,
                    "match_mode": "glob",
                    "options": {"-nocase": []},
                },
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", "sdc.default")).run((_rtl(), sdc))

    assert "OC6001" not in {item.code for item in result.diagnostics}


def test_complex_sdc_regexp_is_explicitly_inconclusive() -> None:
    sdc = ViewObservation(
        ViewId("sdc"),
        objects=(
            DesignObjectObservation(
                "port",
                "^(clk|clock)+$",
                relation="reference",
                attributes={"pattern": True, "match_mode": "regexp"},
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", "sdc.default")).run((_rtl(), sdc))
    codes = {item.code for item in result.diagnostics}

    assert "OC1105" in codes
    assert "OC6001" not in codes


def test_tcl_posix_regexp_class_is_explicitly_inconclusive() -> None:
    sdc = ViewObservation(
        ViewId("sdc"),
        objects=(
            DesignObjectObservation(
                "port",
                "^clk[[:digit:]]$",
                relation="reference",
                attributes={"pattern": True, "match_mode": "regexp"},
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", "sdc.default")).run((_rtl(), sdc))
    codes = {item.code for item in result.diagnostics}

    assert "OC1105" in codes
    assert "OC6001" not in codes


def test_incomplete_upf_does_not_assert_absence_for_known_reference() -> None:
    upf = ViewObservation(
        ViewId("upf"),
        complete=False,
        tainted_scopes=frozenset({"*"}),
        objects=(
            DesignObjectObservation(
                "supply_net",
                "VDD",
                relation="reference",
                attributes={"command": "connect_supply_net"},
            ),
        ),
    )

    result = ComparisonEngine(_project("upf.default")).run((upf,))
    codes = {item.code for item in result.diagnostics}

    assert "OC1104" in codes
    assert "OC6103" not in codes


def test_upf_updates_do_not_count_as_duplicate_initial_definitions() -> None:
    legal = ViewObservation(
        ViewId("upf", "legal"),
        objects=(
            DesignObjectObservation("power_domain", "PD", attributes={"update": False}),
            DesignObjectObservation("power_domain", "PD", attributes={"update": True}),
        ),
    )
    update_only = ViewObservation(
        ViewId("upf", "invalid"),
        objects=(DesignObjectObservation("supply_set", "SS", attributes={"update": True}),),
    )

    legal_result = ComparisonEngine(_project("upf.legal")).run((legal,))
    invalid_result = ComparisonEngine(_project("upf.invalid")).run((update_only,))

    assert "OC6104" not in {item.code for item in legal_result.diagnostics}
    assert "OC6104" in {item.code for item in invalid_result.diagnostics}


def test_scoped_reference_cannot_match_another_rtl_scope() -> None:
    rtl = ViewObservation(
        ViewId("rtl"),
        objects=(DesignObjectObservation("instance", "u_uart", scope="top_b"),),
    )
    wrong_scope = ViewObservation(
        ViewId("upf"),
        objects=(
            DesignObjectObservation(
                "instance",
                "u_uart",
                relation="reference",
                scope="top_a",
                attributes={"command": "create_power_domain"},
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", "upf.default")).run((rtl, wrong_scope))

    assert "OC6101" in {item.code for item in result.diagnostics}


def test_def_escaped_divider_resolves_without_changing_reported_name() -> None:
    rtl = ViewObservation(
        ViewId("rtl"),
        objects=(DesignObjectObservation("instance", "u_mem/bank0", scope="soc_top"),),
    )
    physical = ViewObservation(
        ViewId("def"),
        objects=(
            DesignObjectObservation(
                "instance",
                r"u_mem\/bank0",
                relation="reference",
                scope="soc_top",
                attributes={"endpoint_type": "instance"},
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", "def.default")).run((rtl, physical))

    assert "OC6401" not in {item.code for item in result.diagnostics}


def test_def_configured_escaped_divider_resolves() -> None:
    rtl = ViewObservation(
        ViewId("rtl"),
        objects=(DesignObjectObservation("instance", "u|mem", scope="soc_top"),),
    )
    physical = ViewObservation(
        ViewId("def"),
        objects=(
            DesignObjectObservation(
                "instance",
                r"u\|mem",
                relation="reference",
                scope="soc_top",
            ),
        ),
        attributes={"dividerchar": "|"},
    )

    result = ComparisonEngine(_project("rtl.default", "def.default")).run((rtl, physical))

    assert "OC6401" not in {item.code for item in result.diagnostics}


def test_unknown_def_pin_statement_order_does_not_claim_reversed_bus() -> None:
    rtl = ViewObservation(
        ViewId("rtl"),
        components=(
            ComponentObservation(
                "top",
                ComponentKind.MODULE,
                ports=(
                    PortObservation(
                        "data",
                        Direction.INPUT,
                        shape=BusShape(left=1, right=0),
                    ),
                ),
            ),
        ),
    )
    physical = ViewObservation(
        ViewId("def"),
        components=(
            ComponentObservation(
                "top",
                ComponentKind.MODULE,
                ports=(
                    PortObservation(
                        "data",
                        Direction.INPUT,
                        shape=BusShape(width=2, bit_indices=(0, 1)),
                        attributes={"bit_order_known": False},
                    ),
                ),
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", "def.default")).run((rtl, physical))

    assert "OC4102" not in {item.code for item in result.diagnostics}


def test_reference_only_views_do_not_pollute_strict_component_inventory() -> None:
    sdc = ViewObservation(ViewId("sdc"))
    upf = ViewObservation(ViewId("upf"))
    header = ViewObservation(ViewId("header"))
    result = ComparisonEngine(
        _project(
            "rtl.default",
            "sdc.default",
            "upf.default",
            "header.default",
            policy=PolicySettings(strict_inventory=True),
        )
    ).run((_rtl(), sdc, upf, header))

    assert "OC3001" not in {item.code for item in result.diagnostics}


def test_cli_view_kind_aliases_share_engine_semantics() -> None:
    rtl = ViewObservation(
        ViewId("sv"),
        components=(
            ComponentObservation(
                "top",
                ComponentKind.MODULE,
                ports=(PortObservation("sig", Direction.INPUT, shape=BusShape.scalar()),),
            ),
        ),
    )
    liberty = ViewObservation(
        ViewId("liberty"),
        components=(
            ComponentObservation(
                "top",
                ComponentKind.CELL,
                ports=(
                    PortObservation("sig", Direction.INPUT, shape=BusShape.scalar()),
                    PortObservation(
                        "VDD",
                        Direction.INOUT,
                        PortRole.POWER,
                        BusShape.scalar(),
                    ),
                ),
            ),
        ),
    )
    header = ViewObservation(
        ViewId("c-header"),
        registers=(RegisterObservation("CTRL", component="top"),),
    )
    project = _project(
        "sv.default",
        "liberty.default",
        "c-header.default",
        policy=PolicySettings(strict_inventory=True, rtl_power_pins="optional"),
    )

    result = ComparisonEngine(project).run((rtl, liberty, header))
    codes = {item.code for item in result.diagnostics}

    assert "OC3001" not in codes
    assert "OC4202" not in codes


def test_upf_supply_overlay_does_not_require_every_signal_port() -> None:
    upf = ViewObservation(
        ViewId("upf"),
        components=(
            ComponentObservation(
                "top",
                ComponentKind.MODULE,
                (
                    PortObservation(
                        "VDD",
                        Direction.INOUT,
                        PortRole.POWER,
                        BusShape.scalar(),
                    ),
                ),
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", "upf.default")).run((_rtl(), upf))

    missing = [item for item in result.diagnostics if item.code in {"OC3101", "OC4202"}]
    assert not missing


def test_package_map_is_checked_against_physical_pad_inventory() -> None:
    physical = ViewObservation(
        ViewId("physical"),
        pin_mappings=(
            PinMappingObservation(
                "PAD_IRQ",
                None,
                "irq",
                component="top",
                attributes={"source": "physical_pad"},
            ),
        ),
    )
    package = ViewObservation(
        ViewId("csv", "package"),
        pin_mappings=(
            PinMappingObservation("PAD_MISSING", "A1", "irq", component="top"),
            PinMappingObservation("PAD_IRQ", "A2", "wrong_irq", component="top"),
        ),
    )
    project = ProjectConfig(
        path=Path("opencollate.toml"),
        root=Path("."),
        name="physical-map",
        sources=(
            SourceConfig(ViewId("physical"), (Path("top.physical"),)),
            SourceConfig(
                ViewId("csv", "package"),
                (Path("pins.csv"),),
                profile="package_map",
            ),
        ),
        contract=ContractSettings(),
        policy=PolicySettings(),
    )

    result = ComparisonEngine(project).run((physical, package))

    codes = [item.code for item in result.diagnostics]
    assert codes.count("OC5001") == 1
    assert codes.count("OC5005") == 1


def test_upf_only_checks_internal_references_but_not_unverifiable_rtl_objects() -> None:
    upf = ViewObservation(
        ViewId("upf"),
        objects=(
            DesignObjectObservation(
                "instance",
                "missing_cell",
                relation="reference",
                attributes={"command": "create_power_domain"},
            ),
            DesignObjectObservation(
                "supply_net",
                "missing_supply",
                relation="reference",
                attributes={"command": "connect_supply_net"},
            ),
        ),
    )

    result = ComparisonEngine(_project("upf.default")).run((upf,))

    codes = [item.code for item in result.diagnostics]
    assert "OC6101" not in codes
    assert codes.count("OC6103") == 1


def test_named_upf_views_do_not_satisfy_each_others_internal_references() -> None:
    reference_view = ViewObservation(
        ViewId("upf", "a"),
        objects=(
            DesignObjectObservation(
                "supply_net",
                "VDD",
                relation="reference",
                attributes={"command": "connect_supply_net"},
            ),
        ),
    )
    definition_view = ViewObservation(
        ViewId("upf", "b"),
        objects=(DesignObjectObservation("supply_net", "VDD"),),
    )

    result = ComparisonEngine(_project("upf.a", "upf.b")).run((reference_view, definition_view))

    assert [item.code for item in result.diagnostics].count("OC6103") == 1


def test_def_endpoint_is_checked_against_elaborated_rtl_hierarchy() -> None:
    physical = ViewObservation(
        ViewId("def"),
        objects=(
            DesignObjectObservation(
                "instance",
                "u_uart",
                relation="reference",
                scope="top",
                attributes={"command": "NETS"},
            ),
            DesignObjectObservation(
                "instance",
                "u_missing",
                relation="reference",
                scope="top",
                attributes={"command": "NETS"},
            ),
            DesignObjectObservation(
                "pin",
                "clk",
                relation="reference",
                scope="top",
                attributes={"command": "NETS", "endpoint_type": "top_pin"},
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", "def.default")).run((_rtl(), physical))

    findings = [item for item in result.diagnostics if item.code == "OC6401"]
    assert len(findings) == 1
    assert "u_missing" in findings[0].message


def test_clock_definition_and_role_disagreements_are_actionable() -> None:
    first = ViewObservation(
        ViewId("sdc", "a"),
        clocks=(
            ClockObservation(
                "core_clk",
                ("clk",),
                10.0,
                provenance=_location("sdc.a", "core_clk"),
            ),
            ClockObservation(
                "data_clk",
                ("data",),
                5.0,
                provenance=_location("sdc.a", "data_clk"),
            ),
        ),
    )
    second = ViewObservation(
        ViewId("sdc", "b"),
        clocks=(
            ClockObservation(
                "core_clk",
                ("clk",),
                8.0,
                provenance=_location("sdc.b", "core_clk"),
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", "sdc.a", "sdc.b")).run(
        (_rtl(), first, second)
    )

    assert "OC6002" in {item.code for item in result.diagnostics}
    role = next(item for item in result.diagnostics if item.code == "OC6003")
    assert "top/data" in role.message


def test_same_named_clocks_with_different_targets_conflict() -> None:
    first = ViewObservation(
        ViewId("sdc", "a"),
        clocks=(ClockObservation("core", ("clk_a",), 10.0),),
    )
    second = ViewObservation(
        ViewId("sdc", "b"),
        clocks=(ClockObservation("core", ("clk_b",), 10.0),),
    )

    result = ComparisonEngine(_project("sdc.a", "sdc.b")).run((first, second))

    finding = next(item for item in result.diagnostics if item.code == "OC6002")
    assert finding.metadata["conflicts"]["targets"] == [("clk_a",), ("clk_b",)]


def test_equivalent_qualified_clock_targets_do_not_conflict() -> None:
    first = ViewObservation(
        ViewId("sdc", "a"),
        clocks=(ClockObservation("core", ("clk",), 10.0),),
    )
    second = ViewObservation(
        ViewId("sdc", "b"),
        clocks=(ClockObservation("core", ("top/clk",), 10.0),),
    )

    result = ComparisonEngine(_project("rtl.default", "sdc.a", "sdc.b")).run(
        (_rtl(), first, second)
    )

    assert "OC6002" not in {item.code for item in result.diagnostics}


def test_ipxact_interface_physical_port_must_exist() -> None:
    ipxact = ViewObservation(
        ViewId("ipxact"),
        components=(
            ComponentObservation(
                "top",
                ComponentKind.MODULE,
                (
                    PortObservation("clk", Direction.INPUT, shape=BusShape.scalar()),
                    PortObservation("data", Direction.INPUT, shape=BusShape.scalar()),
                ),
            ),
        ),
        interfaces=(
            InterfaceObservation(
                "stream",
                component="top",
                port_maps={"CLK": "clk", "VALID": "not_valid"},
                provenance=_location("ipxact.default", "stream"),
            ),
        ),
    )

    result = ComparisonEngine(_project("rtl.default", "ipxact.default")).run((_rtl(), ipxact))

    finding = next(item for item in result.diagnostics if item.code == "OC6201")
    assert "not_valid" in finding.message


def test_hardware_and_header_register_maps_are_compared() -> None:
    hardware = ViewObservation(
        ViewId("ipxact"),
        registers=(
            RegisterObservation(
                "CTRL",
                component="uart0",
                address_offset=0,
                size_bits=32,
                fields=(RegisterFieldObservation("ENABLE", 0, 1, "read-write", 0),),
                provenance=_location("ipxact.default", "CTRL"),
            ),
            RegisterObservation(
                "STATUS",
                component="uart0",
                address_offset=4,
                size_bits=32,
                provenance=_location("ipxact.default", "STATUS"),
            ),
        ),
    )
    software = ViewObservation(
        ViewId("header"),
        registers=(
            RegisterObservation(
                "ctrl",
                component="UART0",
                address_offset=8,
                size_bits=16,
                fields=(RegisterFieldObservation("ENABLE", 2, 1, "read-only", 1),),
                provenance=_location("header.default", "CTRL"),
            ),
        ),
    )

    result = ComparisonEngine(_project("ipxact.default", "header.default")).run(
        (hardware, software)
    )
    codes = {item.code for item in result.diagnostics}

    assert {"OC6301", "OC6302", "OC6303", "OC6305", "OC6306", "OC6308"} <= codes
    assert {item.canonical_name.casefold() for item in result.generated_contract.registers} == {
        "ctrl",
        "status",
    }

    frozen = DesignContract((), registers=result.generated_contract.registers)
    frozen_result = ComparisonEngine(_project("header.default")).run((software,), contract=frozen)
    assert any(
        item.code == "OC6301" and "status" in item.message.casefold()
        for item in frozen_result.diagnostics
    )


def test_equal_register_offsets_do_not_hide_conflicting_absolute_addresses() -> None:
    hardware = ViewObservation(
        ViewId("ipxact"),
        registers=(
            RegisterObservation(
                "CTRL",
                component="uart0",
                address_offset=0,
                absolute_address=0x1000,
            ),
        ),
    )
    software = ViewObservation(
        ViewId("header"),
        registers=(
            RegisterObservation(
                "CTRL",
                component="uart0",
                address_offset=0,
                absolute_address=0x2000,
            ),
        ),
    )

    result = ComparisonEngine(_project("ipxact.default", "header.default")).run(
        (hardware, software)
    )

    assert "OC6302" in {item.code for item in result.diagnostics}


def test_repeated_register_names_in_distinct_blocks_remain_distinct() -> None:
    hardware = ViewObservation(
        ViewId("ipxact"),
        registers=(
            RegisterObservation(
                "CTRL",
                component="uart0",
                memory_map="map0",
                address_block="blk0",
                address_offset=0,
            ),
            RegisterObservation(
                "CTRL",
                component="uart0",
                memory_map="map1",
                address_block="blk1",
                address_offset=0,
            ),
        ),
    )

    result = ComparisonEngine(_project("ipxact.default")).run((hardware,))

    assert "OC6307" not in {item.code for item in result.diagnostics}
    assert len(result.generated_contract.registers) == 2
    assert {
        (item.memory_map, item.address_block) for item in result.generated_contract.registers
    } == {("map0", "blk0"), ("map1", "blk1")}


def test_unscoped_register_is_ambiguous_across_multiple_hardware_blocks() -> None:
    hardware = ViewObservation(
        ViewId("ipxact"),
        registers=(
            RegisterObservation("CTRL", component="uart0", memory_map="map0", address_block="blk0"),
            RegisterObservation("CTRL", component="uart0", memory_map="map1", address_block="blk1"),
        ),
    )
    software = ViewObservation(
        ViewId("header"),
        registers=(RegisterObservation("CTRL", component="uart0"),),
    )

    result = ComparisonEngine(_project("ipxact.default", "header.default")).run(
        (hardware, software)
    )

    assert "OC6310" in {item.code for item in result.diagnostics}


def test_empty_declared_register_view_participates_in_presence_checks() -> None:
    hardware = ViewObservation(
        ViewId("ipxact"),
        components=(ComponentObservation("uart0", ComponentKind.MODULE),),
    )
    software = ViewObservation(
        ViewId("header"),
        registers=(RegisterObservation("CTRL", component="uart0"),),
    )

    result = ComparisonEngine(_project("ipxact.default", "header.default")).run(
        (hardware, software)
    )

    finding = next(item for item in result.diagnostics if item.code == "OC6301")
    assert "IP-XACT" in finding.message


def test_tainted_register_view_does_not_claim_a_field_is_missing() -> None:
    hardware = ViewObservation(
        ViewId("ipxact"),
        registers=(
            RegisterObservation(
                "STATUS",
                component="uart0",
                address_offset=4,
                fields=(RegisterFieldObservation("READY", 0, 1),),
            ),
        ),
    )
    incomplete_header = ViewObservation(
        ViewId("header"),
        registers=(RegisterObservation("STATUS", component="uart0", address_offset=4),),
        complete=False,
    )

    result = ComparisonEngine(_project("ipxact.default", "header.default")).run(
        (hardware, incomplete_header)
    )

    assert "OC6304" not in {item.code for item in result.diagnostics}


def test_register_field_overlap_and_overflow_are_detected_without_bit_expansion() -> None:
    hardware = ViewObservation(
        ViewId("ipxact"),
        registers=(
            RegisterObservation(
                "CTRL",
                component="uart0",
                size_bits=8,
                fields=(
                    RegisterFieldObservation("A", 0, 4),
                    RegisterFieldObservation("B", 3, 4),
                    RegisterFieldObservation("C", 7, 2),
                ),
            ),
        ),
    )

    result = ComparisonEngine(_project("ipxact.default")).run((hardware,))

    finding = next(item for item in result.diagnostics if item.code == "OC6309")
    assert "overlaps" in finding.message
    assert "exceeds 8 bits" in finding.message


def test_register_field_names_do_not_strip_register_suffixes() -> None:
    hardware = ViewObservation(
        ViewId("ipxact"),
        registers=(
            RegisterObservation(
                "CTRL",
                component="uart0",
                size_bits=8,
                fields=(
                    RegisterFieldObservation("MODE", 0, 1),
                    RegisterFieldObservation("MODE_REG", 1, 1),
                ),
            ),
        ),
    )

    result = ComparisonEngine(_project("ipxact.default")).run((hardware,))

    assert "OC6307" not in {item.code for item in result.diagnostics}
    assert {item.canonical_name for item in result.generated_contract.registers[0].fields} == {
        "MODE",
        "MODE_REG",
    }

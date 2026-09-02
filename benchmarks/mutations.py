#!/usr/bin/env python3
"""Oracle-backed semantic mutation and clean-control benchmark.

The existing public benchmark measures parser and end-to-end conformance. This
suite measures the comparison engine's defect recall and clean-control behavior
using paired scenarios that differ by one intentional semantic mutation.

It is intentionally deterministic: no timestamps, random numbers, temporary
paths, or host-performance fields enter the report or its SHA-256 digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from opencollate import __version__
from opencollate.config import (
    ContractSettings,
    ParticipationRule,
    PolicySettings,
    ProjectConfig,
    SourceConfig,
)
from opencollate.diagnostics import Diagnostic, Severity
from opencollate.engine import ComparisonEngine, EngineResult
from opencollate.model import (
    BusShape,
    ClockObservation,
    ComponentKind,
    ComponentObservation,
    ConnectivityEdge,
    ConnectivityEndpoint,
    ConnectivityExpectation,
    ConnectivityRequirement,
    ConnectivityTransform,
    DesignObjectObservation,
    Direction,
    IndexRange,
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

SCHEMA_VERSION = 1
ANALYSIS_DATE = date(2026, 9, 2)
INCONCLUSIVE_CODES = frozenset({"OC1102", "OC1103", "OC1104", "OC1105", "OC6505"})


@dataclass(frozen=True, slots=True)
class Scenario:
    config: ProjectConfig
    observations: tuple[ViewObservation, ...]


ScenarioBuilder = Callable[[bool], Scenario]


@dataclass(frozen=True, slots=True)
class MutationCase:
    identifier: str
    family: str
    target_codes: tuple[str, ...]
    mutation: str
    build: ScenarioBuilder

    def __post_init__(self) -> None:
        if not self.identifier or not self.family or not self.target_codes or not self.mutation:
            raise ValueError("mutation cases require an id, family, target code, and description")
        if tuple(sorted(self.target_codes)) != self.target_codes:
            raise ValueError(f"{self.identifier}: target codes must be sorted")

    def manifest(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "family": self.family,
            "target_codes": list(self.target_codes),
            "mutation": self.mutation,
        }


def _view(value: str) -> ViewId:
    return ViewId.parse(value)


def _source(view: ViewId, *, profile: str | None = None) -> SourceConfig:
    return SourceConfig(view, (Path(f"benchmark/{view.key}.input"),), profile=profile)


def _scenario(
    observations: Sequence[ViewObservation],
    *,
    strict_inventory: bool = False,
    participation: Sequence[ParticipationRule] = (),
    package_views: Iterable[str] = (),
) -> Scenario:
    observed = tuple(observations)
    package = frozenset(package_views)
    sources = tuple(
        _source(
            item.view,
            profile="package_map" if str(item.view) in package else None,
        )
        for item in observed
    )
    return Scenario(
        ProjectConfig(
            path=Path("benchmark/opencollate.toml"),
            root=Path("benchmark"),
            name="semantic-mutation-benchmark",
            sources=sources,
            contract=ContractSettings(),
            participation=tuple(participation),
            policy=PolicySettings(strict_inventory=strict_inventory),
        ),
        observed,
    )


def _provenance(view: str, name: str, *, line: int = 1) -> Provenance:
    parsed = _view(view)
    return Provenance(
        f"benchmark/{parsed.key}.src",
        line,
        1,
        parsed,
        raw_name=name,
    )


def _port(
    view: str,
    name: str,
    *,
    direction: Direction = Direction.INPUT,
    role: PortRole = PortRole.SIGNAL,
    shape: BusShape | None = None,
    line: int = 2,
) -> PortObservation:
    return PortObservation(
        name,
        direction,
        role,
        shape or BusShape.scalar(),
        _provenance(view, name, line=line),
    )


def _component(
    view: str,
    ports: Sequence[PortObservation] = (),
    *,
    name: str = "uart",
    functions: Mapping[str, str] | None = None,
    line: int = 1,
) -> ComponentObservation:
    kind = {
        "rtl": ComponentKind.MODULE,
        "liberty": ComponentKind.CELL,
        "lef": ComponentKind.MACRO,
        "ipxact": ComponentKind.MODULE,
    }.get(_view(view).kind, ComponentKind.UNKNOWN)
    return ComponentObservation(
        name,
        kind,
        tuple(ports),
        dict(functions or {}),
        _provenance(view, name, line=line),
    )


def _component_view(
    view: str,
    ports: Sequence[PortObservation] = (),
    *,
    name: str = "uart",
    functions: Mapping[str, str] | None = None,
    components: Sequence[ComponentObservation] | None = None,
) -> ViewObservation:
    selected = (
        tuple(components)
        if components is not None
        else (_component(view, ports, name=name, functions=functions),)
    )
    return ViewObservation(_view(view), selected)


def _two_port_views(
    *,
    left: PortObservation,
    right: PortObservation,
    left_functions: Mapping[str, str] | None = None,
    right_functions: Mapping[str, str] | None = None,
) -> Scenario:
    rtl = _component_view("rtl.default", (left,), functions=left_functions)
    liberty = _component_view("liberty.default", (right,), functions=right_functions)
    return _scenario((rtl, liberty))


def _duplicate_component(mutated: bool) -> Scenario:
    port = _port("rtl.default", "irq")
    components = [_component("rtl.default", (port,), line=1)]
    if mutated:
        components.append(_component("rtl.default", (port,), line=20))
    return _scenario((_component_view("rtl.default", components=components),))


def _conflicting_duplicate_component(mutated: bool) -> Scenario:
    components = [
        _component("rtl.default", (_port("rtl.default", "irq"),), line=1),
    ]
    if mutated:
        components.append(
            _component(
                "rtl.default",
                (_port("rtl.default", "irq", direction=Direction.OUTPUT, line=21),),
                line=20,
            )
        )
    return _scenario((_component_view("rtl.default", components=components),))


def _duplicate_pin(mutated: bool) -> Scenario:
    ports = [_port("rtl.default", "irq", line=2)]
    if mutated:
        ports.append(_port("rtl.default", "irq", line=8))
    return _scenario((_component_view("rtl.default", ports),))


def _missing_component(mutated: bool) -> Scenario:
    rtl = _component_view("rtl.default", (_port("rtl.default", "irq"),))
    liberty = (
        ViewObservation(_view("liberty.default"))
        if mutated
        else _component_view("liberty.default", (_port("liberty.default", "irq"),))
    )
    return _scenario(
        (rtl, liberty),
        strict_inventory=True,
        participation=(ParticipationRule("uart", ("rtl", "liberty")),),
    )


def _direction_mismatch(mutated: bool) -> Scenario:
    return _two_port_views(
        left=_port("rtl.default", "sig", direction=Direction.INPUT),
        right=_port(
            "liberty.default",
            "sig",
            direction=Direction.OUTPUT if mutated else Direction.INPUT,
        ),
    )


def _width_mismatch(mutated: bool) -> Scenario:
    return _two_port_views(
        left=_port("rtl.default", "data", shape=BusShape.scalar()),
        right=_port(
            "liberty.default",
            "data",
            shape=BusShape(left=3, right=0) if mutated else BusShape.scalar(),
        ),
    )


def _range_order_mismatch(mutated: bool) -> Scenario:
    return _two_port_views(
        left=_port("rtl.default", "data", shape=BusShape(packed=(IndexRange(7, 0),))),
        right=_port(
            "liberty.default",
            "data",
            shape=BusShape(
                packed=(IndexRange(0, 7),) if mutated else (IndexRange(7, 0),)
            ),
        ),
    )


def _dimension_mismatch(mutated: bool) -> Scenario:
    left_shape = BusShape(packed=(IndexRange(1, 0), IndexRange(3, 0)))
    right_shape = BusShape(packed=(IndexRange(7, 0),)) if mutated else left_shape
    return _two_port_views(
        left=_port("rtl.default", "data", shape=left_shape),
        right=_port("liberty.default", "data", shape=right_shape),
    )


def _missing_power_pin(mutated: bool) -> Scenario:
    liberty = _component_view(
        "liberty.default",
        (
            _port("liberty.default", "signal"),
            _port("liberty.default", "VDD", role=PortRole.POWER),
        ),
    )
    lef_ports = [_port("lef.default", "signal")]
    if not mutated:
        lef_ports.append(_port("lef.default", "VDD", role=PortRole.POWER))
    return _scenario((liberty, _component_view("lef.default", lef_ports)))


def _role_case(mutated: bool, *, mutant_role: PortRole, code_role: PortRole) -> Scenario:
    return _two_port_views(
        left=_port("rtl.default", "special", role=code_role),
        right=_port(
            "liberty.default",
            "special",
            role=mutant_role if mutated else code_role,
        ),
    )


def _general_role_mismatch(mutated: bool) -> Scenario:
    return _role_case(mutated, mutant_role=PortRole.SIGNAL, code_role=PortRole.ANALOG)


def _clock_role_mismatch(mutated: bool) -> Scenario:
    return _role_case(mutated, mutant_role=PortRole.SIGNAL, code_role=PortRole.CLOCK)


def _power_ground_mismatch(mutated: bool) -> Scenario:
    return _role_case(mutated, mutant_role=PortRole.GROUND, code_role=PortRole.POWER)


def _logic_views(liberty_function: str) -> Scenario:
    rtl_ports = (
        _port("rtl.default", "A"),
        _port("rtl.default", "B"),
        _port("rtl.default", "Y", direction=Direction.OUTPUT),
    )
    liberty_ports = (
        _port("liberty.default", "A"),
        _port("liberty.default", "B"),
        _port("liberty.default", "Y", direction=Direction.OUTPUT),
    )
    rtl = _component_view("rtl.default", rtl_ports, functions={"Y": "A & B"})
    liberty = _component_view(
        "liberty.default",
        liberty_ports,
        functions={"Y": liberty_function},
    )
    return _scenario((rtl, liberty))


def _boolean_mismatch(mutated: bool) -> Scenario:
    return _logic_views("A | B" if mutated else "A & B")


def _boolean_unknown_pin(mutated: bool) -> Scenario:
    rtl = _component_view(
        "rtl.default",
        (
            _port("rtl.default", "A"),
            _port("rtl.default", "Y", direction=Direction.OUTPUT),
        ),
        functions={"Y": "A"},
    )
    liberty = _component_view(
        "liberty.default",
        (
            _port("liberty.default", "A"),
            _port("liberty.default", "Y", direction=Direction.OUTPUT),
        ),
        functions={"Y": "A & GHOST" if mutated else "A"},
    )
    return _scenario((rtl, liberty))


def _package_rtl() -> ViewObservation:
    return _component_view(
        "rtl.default",
        (
            _port("rtl.default", "irq", direction=Direction.OUTPUT),
            _port("rtl.default", "status", direction=Direction.OUTPUT),
        ),
    )


def _duplicate_package_ball(mutated: bool) -> Scenario:
    mappings = (
        PinMappingObservation("PAD_IRQ", "B1", "irq", "uart"),
        PinMappingObservation(
            "PAD_STATUS",
            "B1" if mutated else "B2",
            "status",
            "uart",
        ),
    )
    package = ViewObservation(_view("csv.package"), pin_mappings=mappings)
    return _scenario(
        (_package_rtl(), package),
        package_views=("csv.package",),
    )


def _physical_inventory() -> ViewObservation:
    return ViewObservation(
        _view("physical.default"),
        pin_mappings=(
            PinMappingObservation(
                "PAD_IRQ",
                None,
                "irq",
                component="uart",
                attributes={"source": "physical_pad"},
            ),
        ),
    )


def _unknown_die_pad(mutated: bool) -> Scenario:
    package = ViewObservation(
        _view("csv.package"),
        pin_mappings=(
            PinMappingObservation(
                "PAD_MISSING" if mutated else "PAD_IRQ",
                "A1",
                "irq",
                component="uart",
            ),
        ),
    )
    return _scenario(
        (_package_rtl(), _physical_inventory(), package),
        package_views=("csv.package",),
    )


def _conflicting_package_signal(mutated: bool) -> Scenario:
    package = ViewObservation(
        _view("csv.package"),
        pin_mappings=(
            PinMappingObservation(
                "PAD_IRQ",
                "A1",
                "status" if mutated else "irq",
                component="uart",
            ),
        ),
    )
    return _scenario(
        (_package_rtl(), _physical_inventory(), package),
        package_views=("csv.package",),
    )


def _rtl_reference_base(*, data_role: PortRole = PortRole.SIGNAL) -> ViewObservation:
    view = "rtl.default"
    component = _component(
        view,
        (
            _port(view, "clk", role=PortRole.CLOCK),
            _port(view, "data", role=data_role),
        ),
        name="top",
    )
    return ViewObservation(
        _view(view),
        (component,),
        objects=(
            DesignObjectObservation("port", "clk", scope="top"),
            DesignObjectObservation("port", "data", scope="top"),
            DesignObjectObservation("instance", "u_uart", scope="top"),
            DesignObjectObservation("pin", "u_uart/irq", scope="top"),
        ),
    )


def _sdc_missing_object(mutated: bool) -> Scenario:
    name = "missing_cell" if mutated else "u_uart"
    sdc = ViewObservation(
        _view("sdc.functional"),
        objects=(
            DesignObjectObservation(
                "cell",
                name,
                relation="reference",
                scope="top",
                attributes={"command": "get_cells"},
            ),
        ),
    )
    return _scenario((_rtl_reference_base(), sdc))


def _clock_definition_mismatch(mutated: bool) -> Scenario:
    first = ViewObservation(
        _view("sdc.a"),
        clocks=(ClockObservation("core_clk", ("clk",), 10.0),),
    )
    second = ViewObservation(
        _view("sdc.b"),
        clocks=(ClockObservation("core_clk", ("clk",), 8.0 if mutated else 10.0),),
    )
    return _scenario((first, second))


def _clock_target_role(mutated: bool) -> Scenario:
    rtl = _rtl_reference_base(data_role=PortRole.SIGNAL if mutated else PortRole.CLOCK)
    sdc = ViewObservation(
        _view("sdc.functional"),
        clocks=(ClockObservation("data_clk", ("data",), 5.0),),
    )
    return _scenario((rtl, sdc))


def _upf_missing_instance(mutated: bool) -> Scenario:
    upf = ViewObservation(
        _view("upf.low_power"),
        objects=(
            DesignObjectObservation(
                "instance",
                "u_missing" if mutated else "u_uart",
                relation="reference",
                scope="top",
                attributes={"command": "create_power_domain"},
            ),
        ),
    )
    return _scenario((_rtl_reference_base(), upf))


def _upf_missing_object(mutated: bool) -> Scenario:
    objects: list[DesignObjectObservation] = []
    if not mutated:
        objects.append(DesignObjectObservation("supply_net", "VDD"))
    objects.append(
        DesignObjectObservation(
            "supply_net",
            "VDD",
            relation="reference",
            attributes={"command": "connect_supply_net"},
        )
    )
    return _scenario((ViewObservation(_view("upf.default"), objects=tuple(objects)),))


def _duplicate_upf_object(mutated: bool) -> Scenario:
    objects = [DesignObjectObservation("power_domain", "PD")]
    if mutated:
        objects.append(DesignObjectObservation("power_domain", "PD"))
    return _scenario((ViewObservation(_view("upf.default"), objects=tuple(objects)),))


def _interface_missing_port(mutated: bool) -> Scenario:
    rtl = _rtl_reference_base()
    ipxact = ViewObservation(
        _view("ipxact.default"),
        components=(
            _component(
                "ipxact.default",
                (
                    _port("ipxact.default", "clk", role=PortRole.CLOCK),
                    _port("ipxact.default", "data"),
                ),
                name="top",
            ),
        ),
        interfaces=(
            InterfaceObservation(
                "stream",
                component="top",
                port_maps={"CLK": "clk", "VALID": "not_valid" if mutated else "data"},
            ),
        ),
    )
    return _scenario((rtl, ipxact))


def _register_pair(
    *,
    hardware: RegisterObservation,
    software: RegisterObservation,
) -> Scenario:
    return _scenario(
        (
            ViewObservation(_view("ipxact.default"), registers=(hardware,)),
            ViewObservation(_view("header.default"), registers=(software,)),
        )
    )


def _register_address(mutated: bool) -> Scenario:
    return _register_pair(
        hardware=RegisterObservation("CTRL", component="uart0", address_offset=0),
        software=RegisterObservation(
            "CTRL",
            component="uart0",
            address_offset=4 if mutated else 0,
        ),
    )


def _register_width(mutated: bool) -> Scenario:
    return _register_pair(
        hardware=RegisterObservation("CTRL", component="uart0", size_bits=32),
        software=RegisterObservation(
            "CTRL",
            component="uart0",
            size_bits=16 if mutated else 32,
        ),
    )


def _register_field_layout(mutated: bool) -> Scenario:
    return _register_pair(
        hardware=RegisterObservation(
            "CTRL",
            component="uart0",
            size_bits=32,
            fields=(RegisterFieldObservation("ENABLE", 0, 1),),
        ),
        software=RegisterObservation(
            "CTRL",
            component="uart0",
            size_bits=32,
            fields=(RegisterFieldObservation("ENABLE", 2 if mutated else 0, 1),),
        ),
    )


def _register_access(mutated: bool) -> Scenario:
    return _register_pair(
        hardware=RegisterObservation("CTRL", component="uart0", access="read-write"),
        software=RegisterObservation(
            "CTRL",
            component="uart0",
            access="read-only" if mutated else "read-write",
        ),
    )


def _register_reset(mutated: bool) -> Scenario:
    return _register_pair(
        hardware=RegisterObservation(
            "CTRL",
            component="uart0",
            fields=(RegisterFieldObservation("ENABLE", 0, 1, "read-write", 0),),
        ),
        software=RegisterObservation(
            "CTRL",
            component="uart0",
            fields=(
                RegisterFieldObservation(
                    "ENABLE",
                    0,
                    1,
                    "read-write",
                    1 if mutated else 0,
                ),
            ),
        ),
    )


def _invalid_register_layout(mutated: bool) -> Scenario:
    second_offset = 0 if mutated else 1
    register = RegisterObservation(
        "CTRL",
        component="uart0",
        size_bits=32,
        fields=(
            RegisterFieldObservation("A", 0, 1),
            RegisterFieldObservation("B", second_offset, 1),
        ),
    )
    return _scenario((ViewObservation(_view("ipxact.default"), registers=(register,)),))


def _def_missing_object(mutated: bool) -> Scenario:
    rtl = ViewObservation(
        _view("rtl.default"),
        objects=(DesignObjectObservation("instance", "u_uart", scope="top"),),
    )
    physical = ViewObservation(
        _view("def.default"),
        objects=(
            DesignObjectObservation(
                "instance",
                "u_missing" if mutated else "u_uart",
                relation="reference",
                scope="top",
                attributes={"command": "NETS"},
            ),
        ),
    )
    return _scenario((rtl, physical))


def _endpoint(name: str, *, line: int = 1) -> ConnectivityEndpoint:
    return ConnectivityEndpoint(
        name,
        provenance=Provenance("benchmark/top.sv", line, view=_view("rtl.default")),
    )


def _connectivity_requirement(
    *,
    expectation: ConnectivityExpectation = ConnectivityExpectation.REACHABLE,
    transform: ConnectivityTransform = ConnectivityTransform.ANY,
) -> ConnectivityRequirement:
    return ConnectivityRequirement(
        "PATH",
        "top/a",
        "top/y",
        expectation,
        transform,
        provenance=Provenance(
            "benchmark/connectivity.csv",
            2,
            view=_view("connectivity.intent"),
        ),
    )


def _connectivity_scenario(
    endpoints: Sequence[ConnectivityEndpoint],
    edges: Sequence[ConnectivityEdge],
    requirement: ConnectivityRequirement,
) -> Scenario:
    rtl = ViewObservation(
        _view("rtl.default"),
        connectivity_endpoints=tuple(endpoints),
        connectivity_edges=tuple(edges),
        attributes={"connectivity_complete": True},
    )
    intent = ViewObservation(
        _view("connectivity.intent"),
        connectivity_requirements=(requirement,),
    )
    return _scenario((rtl, intent))


def _required_connectivity_missing(mutated: bool) -> Scenario:
    source, middle, sink = _endpoint("top/a"), _endpoint("top/n", line=2), _endpoint(
        "top/y", line=3
    )
    edges = [ConnectivityEdge(source, middle)]
    if not mutated:
        edges.append(ConnectivityEdge(middle, sink))
    return _connectivity_scenario(
        (source, middle, sink),
        edges,
        _connectivity_requirement(),
    )


def _forbidden_connectivity_present(mutated: bool) -> Scenario:
    source, sink = _endpoint("top/a"), _endpoint("top/y", line=2)
    edges = (ConnectivityEdge(source, sink),) if mutated else ()
    return _connectivity_scenario(
        (source, sink),
        edges,
        _connectivity_requirement(expectation=ConnectivityExpectation.UNREACHABLE),
    )


def _bit_endpoint(name: str, bit: int, ordinal: int) -> ConnectivityEndpoint:
    return ConnectivityEndpoint(
        name,
        bit_index=bit,
        ordinal=ordinal,
        width=2,
        provenance=Provenance("benchmark/top.sv", 1, view=_view("rtl.default")),
    )


def _connectivity_bit_order(mutated: bool) -> Scenario:
    sources = (_bit_endpoint("top/a", 1, 0), _bit_endpoint("top/a", 0, 1))
    sinks = (_bit_endpoint("top/y", 1, 0), _bit_endpoint("top/y", 0, 1))
    edges = (
        ConnectivityEdge(sources[0], sinks[1] if mutated else sinks[0]),
        ConnectivityEdge(sources[1], sinks[0] if mutated else sinks[1]),
    )
    return _connectivity_scenario(
        (*sources, *sinks),
        edges,
        _connectivity_requirement(transform=ConnectivityTransform.IDENTITY),
    )


CASES: tuple[MutationCase, ...] = (
    MutationCase(
        "boolean-function-mismatch",
        "logic",
        ("OC4301",),
        "Change the Liberty output function from AND to OR.",
        _boolean_mismatch,
    ),
    MutationCase(
        "boolean-unknown-input",
        "logic",
        ("OC4303",),
        "Reference a non-existent Liberty input in an output function.",
        _boolean_unknown_pin,
    ),
    MutationCase(
        "clock-definition-period",
        "constraints",
        ("OC6002",),
        "Change one constraint view's period for the same named clock.",
        _clock_definition_mismatch,
    ),
    MutationCase(
        "clock-target-non-clock-port",
        "constraints",
        ("OC6003",),
        "Reclassify an SDC clock target from clock to ordinary signal in RTL.",
        _clock_target_role,
    ),
    MutationCase(
        "component-conflicting-duplicate",
        "inventory",
        ("OC2004",),
        "Add a second same-name component definition with a different interface.",
        _conflicting_duplicate_component,
    ),
    MutationCase(
        "component-duplicate",
        "inventory",
        ("OC2003",),
        "Add a second identical component definition in one view.",
        _duplicate_component,
    ),
    MutationCase(
        "component-missing-required-view",
        "inventory",
        ("OC3001",),
        "Remove a component from an explicitly participating required view.",
        _missing_component,
    ),
    MutationCase(
        "connectivity-bit-order",
        "connectivity",
        ("OC6507",),
        "Reverse a two-bit path while intent requires identity ordering.",
        _connectivity_bit_order,
    ),
    MutationCase(
        "connectivity-forbidden-present",
        "connectivity",
        ("OC6504",),
        "Add a transparent static path forbidden by connectivity intent.",
        _forbidden_connectivity_present,
    ),
    MutationCase(
        "connectivity-required-missing",
        "connectivity",
        ("OC6503",),
        "Remove the final edge from a required transparent static path.",
        _required_connectivity_missing,
    ),
    MutationCase(
        "def-hierarchy-reference",
        "physical",
        ("OC6401",),
        "Change a DEF endpoint to an instance absent from elaborated RTL.",
        _def_missing_object,
    ),
    MutationCase(
        "interface-direction",
        "interface",
        ("OC4001",),
        "Change a Liberty pin from input to output.",
        _direction_mismatch,
    ),
    MutationCase(
        "interface-dimensions",
        "interface",
        ("OC4103",),
        "Flatten a two-dimensional packed port to one dimension at equal width.",
        _dimension_mismatch,
    ),
    MutationCase(
        "interface-ipxact-port-map",
        "interface",
        ("OC6201",),
        "Map an IP-XACT logical interface signal to a missing physical port.",
        _interface_missing_port,
    ),
    MutationCase(
        "interface-missing-power-pin",
        "interface",
        ("OC4202",),
        "Remove an explicit VDD pin from a participating physical view.",
        _missing_power_pin,
    ),
    MutationCase(
        "interface-range-order",
        "interface",
        ("OC4102",),
        "Reverse declared bus index order without changing width.",
        _range_order_mismatch,
    ),
    MutationCase(
        "interface-role-analog",
        "interface",
        ("OC4201",),
        "Reclassify an analog pin as an ordinary signal.",
        _general_role_mismatch,
    ),
    MutationCase(
        "interface-role-clock",
        "interface",
        ("OC4204",),
        "Reclassify a clock pin as an ordinary signal.",
        _clock_role_mismatch,
    ),
    MutationCase(
        "interface-role-power-ground",
        "interface",
        ("OC4203",),
        "Reverse a rail classification from power to ground.",
        _power_ground_mismatch,
    ),
    MutationCase(
        "interface-width",
        "interface",
        ("OC4101",),
        "Change a scalar pin to a four-bit vector.",
        _width_mismatch,
    ),
    MutationCase(
        "package-conflicting-signal",
        "package",
        ("OC5005",),
        "Assign a known die pad to a different known logical signal.",
        _conflicting_package_signal,
    ),
    MutationCase(
        "package-duplicate-ball",
        "package",
        ("OC5003",),
        "Assign two logical signals to the same package ball.",
        _duplicate_package_ball,
    ),
    MutationCase(
        "package-unknown-die-pad",
        "package",
        ("OC5001",),
        "Reference a die pad absent from the physical pad inventory.",
        _unknown_die_pad,
    ),
    MutationCase(
        "pin-duplicate",
        "inventory",
        ("OC3103",),
        "Repeat the same pin definition in one component view.",
        _duplicate_pin,
    ),
    MutationCase(
        "register-access",
        "registers",
        ("OC6306",),
        "Change software register access from read-write to read-only.",
        _register_access,
    ),
    MutationCase(
        "register-address",
        "registers",
        ("OC6302",),
        "Move the software register offset while retaining its identity.",
        _register_address,
    ),
    MutationCase(
        "register-field-layout",
        "registers",
        ("OC6305",),
        "Move a software field to a different bit offset.",
        _register_field_layout,
    ),
    MutationCase(
        "register-field-overlap",
        "registers",
        ("OC6309",),
        "Overlap two fields in one register definition.",
        _invalid_register_layout,
    ),
    MutationCase(
        "register-reset",
        "registers",
        ("OC6308",),
        "Change a software-visible field reset value.",
        _register_reset,
    ),
    MutationCase(
        "register-width",
        "registers",
        ("OC6303",),
        "Change a software-visible register from 32 to 16 bits.",
        _register_width,
    ),
    MutationCase(
        "sdc-missing-object",
        "constraints",
        ("OC6001",),
        "Change an SDC cell query to a missing elaborated instance.",
        _sdc_missing_object,
    ),
    MutationCase(
        "upf-duplicate-object",
        "power",
        ("OC6104",),
        "Define the same UPF power domain twice in one view.",
        _duplicate_upf_object,
    ),
    MutationCase(
        "upf-missing-instance",
        "power",
        ("OC6101",),
        "Change a UPF instance reference to an absent RTL instance.",
        _upf_missing_instance,
    ),
    MutationCase(
        "upf-missing-object",
        "power",
        ("OC6103",),
        "Remove the UPF supply-net definition retained by a reference.",
        _upf_missing_object,
    ),
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _diagnostic_record(diagnostic: Diagnostic) -> dict[str, Any]:
    return {
        "code": diagnostic.code,
        "severity": diagnostic.severity.value,
        "fingerprint": diagnostic.fingerprint,
        "object": diagnostic.object.id if diagnostic.object is not None else None,
        "property": diagnostic.property_name,
        "views": sorted({str(item.view) for item in diagnostic.evidence}),
    }


def _actionable(result: EngineResult) -> tuple[Diagnostic, ...]:
    return tuple(
        item
        for item in result.diagnostics
        if not item.waived and item.severity != Severity.INFO
    )


def _expanded(counter: Counter[str]) -> list[str]:
    return [code for code in sorted(counter) for _ in range(counter[code])]


def _run_scenario(scenario: Scenario) -> tuple[EngineResult, bool]:
    engine = ComparisonEngine(scenario.config)
    result = engine.run(scenario.observations, today=ANALYSIS_DATE)
    reversed_result = engine.run(tuple(reversed(scenario.observations)), today=ANALYSIS_DATE)
    return result, result.to_dict() == reversed_result.to_dict()


def _evaluate_case(case: MutationCase) -> dict[str, Any]:
    control_result, control_deterministic = _run_scenario(case.build(False))
    mutant_result, mutant_deterministic = _run_scenario(case.build(True))
    expected = Counter(case.target_codes)
    control_diagnostics = _actionable(control_result)
    mutant_diagnostics = _actionable(mutant_result)
    control_codes = Counter(item.code for item in control_diagnostics)
    mutant_codes = Counter(item.code for item in mutant_diagnostics)
    missing = expected - mutant_codes
    unexpected = mutant_codes - expected
    target_detected = not missing
    inconclusive = bool(set(mutant_codes) & INCONCLUSIVE_CODES) and not target_detected
    if mutant_codes == expected:
        mutant_status = "exact"
    elif target_detected:
        mutant_status = "overtriggered"
    elif inconclusive:
        mutant_status = "inconclusive"
    else:
        mutant_status = "missed"
    control_status = "clean" if not control_codes else "false_positive"
    deterministic = control_deterministic and mutant_deterministic
    passed = mutant_status == "exact" and control_status == "clean" and deterministic
    return {
        **case.manifest(),
        "status": "pass" if passed else "fail",
        "deterministic": deterministic,
        "mutant": {
            "status": mutant_status,
            "target_detected": target_detected,
            "actionable_codes": _expanded(mutant_codes),
            "missing_codes": _expanded(missing),
            "unexpected_codes": _expanded(unexpected),
            "diagnostics": [
                _diagnostic_record(item)
                for item in sorted(mutant_diagnostics, key=lambda item: item.sort_key())
            ],
        },
        "control": {
            "status": control_status,
            "actionable_codes": _expanded(control_codes),
            "diagnostics": [
                _diagnostic_record(item)
                for item in sorted(control_diagnostics, key=lambda item: item.sort_key())
            ],
        },
    }


def _metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(cases)
    detected = sum(bool(item["mutant"]["target_detected"]) for item in cases)
    exact = sum(item["mutant"]["status"] == "exact" for item in cases)
    false_negatives = total - detected
    overtriggered = sum(item["mutant"]["status"] == "overtriggered" for item in cases)
    inconclusive = sum(item["mutant"]["status"] == "inconclusive" for item in cases)
    clean_controls = sum(item["control"]["status"] == "clean" for item in cases)
    false_positive_controls = total - clean_controls
    deterministic = sum(bool(item["deterministic"]) for item in cases)
    passed_pairs = sum(item["status"] == "pass" for item in cases)
    return {
        "mutation_cases": total,
        "clean_controls": total,
        "target_detections": detected,
        "exact_mutation_detections": exact,
        "false_negatives": false_negatives,
        "overtriggered_mutations": overtriggered,
        "inconclusive_mutations": inconclusive,
        "true_negative_controls": clean_controls,
        "false_positive_controls": false_positive_controls,
        "deterministic_pairs": deterministic,
        "passed_pairs": passed_pairs,
        "recall": detected / total if total else 1.0,
        "clean_control_specificity": clean_controls / total if total else 1.0,
        "exact_pair_accuracy": passed_pairs / total if total else 1.0,
    }


def _selected_cases(
    identifiers: Sequence[str],
    families: Sequence[str],
) -> tuple[MutationCase, ...]:
    selected = CASES
    if identifiers:
        wanted = set(identifiers)
        known = {item.identifier for item in CASES}
        unknown = sorted(wanted - known)
        if unknown:
            raise ValueError("unknown mutation case(s): " + ", ".join(unknown))
        selected = tuple(item for item in selected if item.identifier in wanted)
    if families:
        wanted_families = set(families)
        known_families = {item.family for item in CASES}
        unknown_families = sorted(wanted_families - known_families)
        if unknown_families:
            raise ValueError("unknown mutation family/families: " + ", ".join(unknown_families))
        selected = tuple(item for item in selected if item.family in wanted_families)
    if not selected:
        raise ValueError("case selection is empty")
    return tuple(sorted(selected, key=lambda item: item.identifier))


def run_suite(
    *,
    identifiers: Sequence[str] = (),
    families: Sequence[str] = (),
) -> dict[str, Any]:
    selected = _selected_cases(identifiers, families)
    evaluated = [_evaluate_case(item) for item in selected]
    by_family: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in evaluated:
        by_family[str(item["family"])].append(item)
    manifest = [item.manifest() for item in selected]
    summary = _metrics(evaluated)
    summary["families"] = {
        family: _metrics(items) for family, items in sorted(by_family.items())
    }
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "suite": "opencollate-semantic-mutations",
        "opencollate_version": __version__,
        "status": "pass" if summary["passed_pairs"] == summary["mutation_cases"] else "fail",
        "oracle": {
            "kind": "paired-semantic-mutation",
            "manifest_sha256": _digest(manifest),
            "selection": {
                "cases": [item.identifier for item in selected],
                "families": sorted({item.family for item in selected}),
            },
            "policy": {
                "mutant": "exact actionable diagnostic multiset",
                "control": "no unwaived warning, error, or fatal diagnostic",
                "determinism": "identical EngineResult under reversed observation order",
            },
        },
        "summary": summary,
        "cases": evaluated,
    }
    report["result_sha256"] = _digest(report)
    schema = json.loads((ROOT / "benchmarks" / "mutation-results.schema.json").read_text("utf-8"))
    Draft202012Validator(schema).validate(report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", default=[], help="select one case; repeatable")
    parser.add_argument(
        "--family",
        action="append",
        default=[],
        help="select one mutation family; repeatable",
    )
    parser.add_argument("--json-output", type=Path, help="write the deterministic JSON report")
    parser.add_argument(
        "--enforce-perfect",
        action="store_true",
        help="return nonzero unless every mutant/control pair is exact, clean, and deterministic",
    )
    parser.add_argument("--list", action="store_true", help="list the mutation manifest and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        for case in CASES:
            print(f"{case.identifier}\t{case.family}\t{','.join(case.target_codes)}")
        return 0
    try:
        report = run_suite(identifiers=args.case, families=args.family)
    except (OSError, TypeError, ValueError) as error:
        print(f"mutation benchmark configuration error: {error}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        print(rendered, end="")
    if args.enforce_perfect and report["status"] != "pass":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

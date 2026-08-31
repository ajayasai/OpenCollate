from __future__ import annotations

from datetime import date
from pathlib import Path

from opencollate.config import (
    AliasRule,
    ContractSettings,
    ParticipationRule,
    PolicySettings,
    ProjectConfig,
    SourceConfig,
    Waiver,
)
from opencollate.diagnostics import Severity
from opencollate.engine import ComparisonEngine
from opencollate.model import (
    BusShape,
    ComponentKind,
    ComponentObservation,
    ContractComponent,
    ContractPort,
    DesignContract,
    Direction,
    IndexRange,
    PinMappingObservation,
    PortObservation,
    PortRole,
    Provenance,
    ViewId,
    ViewObservation,
)


def source(view: str, *, profile: str | None = None) -> SourceConfig:
    parsed = ViewId.parse(view)
    return SourceConfig(parsed, (Path(f"{parsed.key}.input"),), profile=profile)


def project(
    views: tuple[str, ...],
    *,
    aliases: tuple[AliasRule, ...] = (),
    participation: tuple[ParticipationRule, ...] = (),
    policy: PolicySettings | None = None,
    waivers: tuple[Waiver, ...] = (),
    package_views: tuple[str, ...] = (),
) -> ProjectConfig:
    return ProjectConfig(
        path=Path("opencollate.toml"),
        root=Path("."),
        name="engine-test",
        sources=tuple(
            source(view, profile="package_map" if view in package_views else None) for view in views
        ),
        contract=ContractSettings(baseline=ViewId("rtl")),
        aliases=aliases,
        participation=participation,
        policy=policy or PolicySettings(),
        waivers=waivers,
    )


def port(
    view: str,
    name: str,
    *,
    direction: Direction = Direction.INPUT,
    shape: BusShape | None = None,
    role: PortRole = PortRole.SIGNAL,
) -> PortObservation:
    parsed = ViewId.parse(view)
    return PortObservation(
        name,
        direction,
        role,
        shape or BusShape.scalar(),
        Provenance(f"{parsed.key}.src", 10, 3, parsed, raw_name=name),
    )


def observation(
    view: str,
    ports: tuple[PortObservation, ...],
    *,
    component_name: str = "uart",
    functions: dict[str, str] | None = None,
    complete: bool = True,
    tainted_scopes: frozenset[str] = frozenset(),
    mappings: tuple[PinMappingObservation, ...] = (),
) -> ViewObservation:
    parsed = ViewId.parse(view)
    kind = {
        "rtl": ComponentKind.MODULE,
        "liberty": ComponentKind.CELL,
        "lef": ComponentKind.MACRO,
    }.get(parsed.kind, ComponentKind.UNKNOWN)
    components = ()
    if ports:
        components = (
            ComponentObservation(
                component_name,
                kind,
                ports,
                functions or {},
                Provenance(f"{parsed.key}.src", 1, 1, parsed, raw_name=component_name),
            ),
        )
    return ViewObservation(
        parsed,
        components,
        complete=complete,
        tainted_scopes=tainted_scopes,
        pin_mappings=mappings,
    )


def codes(result: object) -> list[str]:
    return [diagnostic.code for diagnostic in result.diagnostics]  # type: ignore[attr-defined]


def test_multi_view_width_conflict_is_one_consolidated_diagnostic() -> None:
    rtl = observation("rtl", (port("rtl", "irq", shape=BusShape.scalar()),))
    liberty = observation(
        "liberty",
        (port("liberty", "irq", shape=BusShape(left=3, right=0)),),
    )
    lef = observation("lef", (port("lef", "irq", shape=BusShape(left=3, right=0)),))
    result = ComparisonEngine(project(("rtl", "liberty", "lef"))).run((lef, rtl, liberty))
    findings = [item for item in result.diagnostics if item.code == "OC4101"]
    assert len(findings) == 1
    assert len(findings[0].evidence) == 3
    assert "1 bit" in findings[0].message
    assert "4 bits" in findings[0].message


def test_two_view_width_conflict_is_explained_in_plain_design_language() -> None:
    rtl = observation("rtl", (port("rtl", "irq", shape=BusShape.scalar()),))
    liberty = observation(
        "liberty",
        (port("liberty", "irq", shape=BusShape(left=3, right=0)),),
    )

    result = ComparisonEngine(project(("rtl", "liberty"))).run((rtl, liberty))
    finding = next(item for item in result.diagnostics if item.code == "OC4101")

    assert finding.message == "uart/irq is 1 bit in RTL but 4 bits in Liberty."


def test_equal_width_reversed_ranges_have_specific_rule() -> None:
    rtl = observation(
        "rtl",
        (port("rtl", "data", shape=BusShape(packed=(IndexRange(7, 0),))),),
    )
    liberty = observation(
        "liberty",
        (port("liberty", "data", shape=BusShape(packed=(IndexRange(0, 7),))),),
    )
    result = ComparisonEngine(project(("rtl", "liberty"))).run((rtl, liberty))
    assert "OC4102" in codes(result)
    assert "OC4101" not in codes(result)


def test_equal_width_multidimensional_and_unpacked_shapes_are_not_conflated() -> None:
    shape_pairs = (
        (
            BusShape(packed=(IndexRange(1, 0), IndexRange(3, 0))),
            BusShape(packed=(IndexRange(7, 0),)),
        ),
        (
            BusShape(
                packed=(IndexRange(7, 0),),
                unpacked=(IndexRange(1, 0),),
            ),
            BusShape(
                packed=(IndexRange(7, 0),),
                unpacked=(IndexRange(0, 1),),
            ),
        ),
    )
    for rtl_shape, liberty_shape in shape_pairs:
        rtl = observation("rtl", (port("rtl", "data", shape=rtl_shape),))
        liberty = observation("liberty", (port("liberty", "data", shape=liberty_shape),))

        result = ComparisonEngine(project(("rtl", "liberty"))).run((rtl, liberty))

        assert codes(result).count("OC4103") == 1
        assert "OC4101" not in codes(result)
        assert "OC4102" not in codes(result)


def test_width_only_shape_does_not_invent_dimension_mismatch() -> None:
    rtl = observation(
        "rtl",
        (port("rtl", "data", shape=BusShape(packed=(IndexRange(7, 0),))),),
    )
    liberty = observation(
        "liberty",
        (port("liberty", "data", shape=BusShape(width=8)),),
    )

    result = ComparisonEngine(project(("rtl", "liberty"))).run((rtl, liberty))

    assert "OC4103" not in codes(result)


def test_aliases_reconcile_different_component_and_port_names() -> None:
    aliases = (
        AliasRule("component", "uart", "liberty", "UART_CORE"),
        AliasRule("port", "irq", "liberty", "IRQ", "uart"),
    )
    rtl = observation("rtl", (port("rtl", "irq", direction=Direction.OUTPUT),))
    liberty = observation(
        "liberty",
        (port("liberty", "IRQ", direction=Direction.OUTPUT),),
        component_name="UART_CORE",
    )
    result = ComparisonEngine(project(("rtl", "liberty"), aliases=aliases)).run((rtl, liberty))
    assert len(result.design.components) == 1
    assert result.design.components[0].canonical_name == "uart"
    assert [item.canonical_name for item in result.design.components[0].ports] == ["irq"]
    assert result.exit_code == 0


def test_missing_required_component_is_one_error_and_taint_suppresses_it() -> None:
    participation = (ParticipationRule("uart", ("rtl", "liberty")),)
    rtl = observation("rtl", (port("rtl", "irq"),))
    missing = observation("liberty", ())
    engine = ComparisonEngine(project(("rtl", "liberty"), participation=participation))
    result = engine.run((rtl, missing))
    assert codes(result).count("OC3001") == 1
    assert "OC3101" not in codes(result)

    tainted = observation("liberty", (), complete=False, tainted_scopes=frozenset({"*"}))
    suppressed = engine.run((rtl, tainted))
    assert "OC3001" not in codes(suppressed)


def test_boolean_equivalence_mismatch_includes_counterexample() -> None:
    rtl_ports = (
        port("rtl", "A"),
        port("rtl", "B"),
        port("rtl", "Y", direction=Direction.OUTPUT),
    )
    lib_ports = (
        port("liberty", "A"),
        port("liberty", "B"),
        port("liberty", "Y", direction=Direction.OUTPUT),
    )
    rtl = observation("rtl", rtl_ports, functions={"Y": "A & B"})
    liberty = observation("liberty", lib_ports, functions={"Y": "A | B"})
    result = ComparisonEngine(project(("rtl", "liberty"))).run((rtl, liberty))
    finding = next(item for item in result.diagnostics if item.code == "OC4301")
    assert finding.metadata["counterexample"]


def test_boolean_aliases_are_canonicalized_per_originating_view() -> None:
    aliases = (
        AliasRule("port", "left", "rtl", "A", "uart"),
        AliasRule("port", "right", "rtl", "B", "uart"),
        AliasRule("port", "left", "liberty", "B", "uart"),
        AliasRule("port", "right", "liberty", "A", "uart"),
    )
    rtl_ports = (
        port("rtl", "A"),
        port("rtl", "B"),
        port("rtl", "Y", direction=Direction.OUTPUT),
    )
    liberty_ports = (
        port("liberty", "A"),
        port("liberty", "B"),
        port("liberty", "Y", direction=Direction.OUTPUT),
    )
    rtl = observation("rtl", rtl_ports, functions={"Y": "A"})

    swapped = observation("liberty", liberty_ports, functions={"Y": "A"})
    mismatch = ComparisonEngine(project(("rtl", "liberty"), aliases=aliases)).run((rtl, swapped))
    finding = next(item for item in mismatch.diagnostics if item.code == "OC4301")
    assert set(finding.metadata["counterexample"]) == {"left", "right"}

    same_signal = observation("liberty", liberty_ports, functions={"Y": "B"})
    equivalent = ComparisonEngine(project(("rtl", "liberty"), aliases=aliases)).run(
        (rtl, same_signal)
    )
    assert "OC4301" not in codes(equivalent)


def test_package_mapping_is_consolidated_without_duplicate_diagnostics() -> None:
    rtl = observation(
        "rtl",
        (
            port("rtl", "irq", direction=Direction.OUTPUT),
            port("rtl", "status", direction=Direction.OUTPUT),
        ),
    )
    csv_view = ViewId("csv", "package")
    mappings = tuple(
        PinMappingObservation(
            f"PAD_{signal.upper()}",
            "B1",
            signal,
            "uart",
            provenance=Provenance("pins.csv", index, 1, csv_view),
        )
        for index, signal in enumerate(("irq", "status"), start=2)
    )
    package = observation("csv.package", (), mappings=mappings)
    result = ComparisonEngine(project(("rtl", "csv.package"), package_views=("csv.package",))).run(
        (rtl, package)
    )
    assert "OC5003" in codes(result)
    assert "OC5005" not in codes(result)
    assert "OC5001" not in codes(result)

    multi_bond_policy = PolicySettings(allow_multi_bond=True)
    permitted = ComparisonEngine(
        project(
            ("rtl", "csv.package"),
            package_views=("csv.package",),
            policy=multi_bond_policy,
        )
    ).run((rtl, package))
    assert "OC5003" not in codes(permitted)
    assert "OC5005" in codes(permitted)


def test_waiver_suppresses_exit_failure_and_expiration_does_not() -> None:
    rtl = observation("rtl", (port("rtl", "irq", shape=BusShape.scalar()),))
    liberty = observation("liberty", (port("liberty", "irq", shape=BusShape(left=3, right=0)),))
    waiver = Waiver(
        "OC4101",
        "Intentional compatibility wrapper",
        object_pattern="component:uart/port:irq",
        expires=date(2099, 1, 1),
    )
    result = ComparisonEngine(project(("rtl", "liberty"), waivers=(waiver,))).run(
        (rtl, liberty), today=date(2026, 8, 31)
    )
    finding = next(item for item in result.diagnostics if item.code == "OC4101")
    assert finding.waived
    assert result.exit_code == 0
    assert result.to_dict()["summary"]["suppressed"] == 1

    expired = Waiver(
        "OC4101",
        "Old exception",
        object_pattern="component:uart/port:irq",
        expires=date(2020, 1, 1),
    )
    expired_result = ComparisonEngine(project(("rtl", "liberty"), waivers=(expired,))).run(
        (rtl, liberty), today=date(2026, 8, 31)
    )
    assert "OC1004" in codes(expired_result)
    assert expired_result.exit_code == 1


def test_frozen_contract_detects_extra_pin_and_shape_drift() -> None:
    contract = DesignContract(
        (
            ContractComponent(
                "uart",
                ComponentKind.MODULE,
                {"rtl.default": "uart"},
                ("rtl.default",),
                (
                    ContractPort(
                        "irq",
                        {"rtl.default": "irq"},
                        Direction.OUTPUT,
                        PortRole.SIGNAL,
                        BusShape.scalar(),
                    ),
                ),
            ),
        )
    )
    rtl = observation(
        "rtl",
        (
            port("rtl", "irq", direction=Direction.OUTPUT, shape=BusShape(left=1, right=0)),
            port("rtl", "debug", direction=Direction.OUTPUT),
        ),
    )
    result = ComparisonEngine(project(("rtl",))).run((rtl,), contract=contract)
    assert "OC4101" in codes(result)
    assert "OC3102" in codes(result)


def test_result_is_deterministic_under_view_permutation() -> None:
    rtl = observation("rtl", (port("rtl", "irq", direction=Direction.OUTPUT),))
    liberty = observation("liberty", (port("liberty", "irq", direction=Direction.INPUT),))
    engine = ComparisonEngine(project(("rtl", "liberty")))
    assert engine.run((rtl, liberty)).to_dict() == engine.run((liberty, rtl)).to_dict()


def test_deny_warnings_and_severity_override_affect_exit_code() -> None:
    rtl = observation("rtl", (port("rtl", "one", shape=BusShape.scalar()),))
    liberty = observation(
        "liberty",
        (
            port(
                "liberty",
                "one",
                shape=BusShape(packed=(IndexRange(0, 0),)),
            ),
        ),
    )
    warning_policy = PolicySettings(deny_warnings=True)
    result = ComparisonEngine(project(("rtl", "liberty"), policy=warning_policy)).run(
        (rtl, liberty)
    )
    assert "OC4106" in codes(result)
    assert result.exit_code == 1

    override = PolicySettings(severity_overrides={"OC4106": Severity.ERROR})
    overridden = ComparisonEngine(project(("rtl", "liberty"), policy=override)).run((rtl, liberty))
    assert (
        next(item for item in overridden.diagnostics if item.code == "OC4106").severity
        == Severity.ERROR
    )
    assert overridden.exit_code == 1

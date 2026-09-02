from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollate.config import (
    ContractSettings,
    PolicySettings,
    ProjectConfig,
    SourceConfig,
    Waiver,
    load_contract,
)
from opencollate.diagnostics import Diagnostic, Severity
from opencollate.engine import ComparisonEngine, EngineResult
from opencollate.model import (
    BusShape,
    ComponentKind,
    ComponentObservation,
    Direction,
    PortObservation,
    PortRole,
    Provenance,
    ViewId,
    ViewObservation,
)
from opencollate.parsers.csvpins import parse_pin_csv


def _view_id(view: str) -> ViewId:
    return ViewId.parse(view)


def _source(view: str, *, profile: str | None = None) -> SourceConfig:
    parsed = _view_id(view)
    return SourceConfig(parsed, (Path(f"{parsed.key}.input"),), profile=profile)


def _project(
    views: tuple[str, ...],
    *,
    policy: PolicySettings | None = None,
    waivers: tuple[Waiver, ...] = (),
    profiles: dict[str, str] | None = None,
) -> ProjectConfig:
    selected_profiles = profiles or {}
    return ProjectConfig(
        path=Path("opencollate.toml"),
        root=Path("."),
        name="engine-edge-test",
        sources=tuple(_source(view, profile=selected_profiles.get(view)) for view in views),
        contract=ContractSettings(baseline=_view_id(views[0])),
        policy=policy or PolicySettings(),
        waivers=waivers,
    )


def _provenance(view: str, name: str, *, line: int = 1) -> Provenance:
    parsed = _view_id(view)
    return Provenance(f"{parsed.key}.src", line, 1, parsed, raw_name=name)


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
    ports: tuple[PortObservation, ...],
    *,
    name: str = "uart",
    functions: dict[str, str] | None = None,
    line: int = 1,
) -> ComponentObservation:
    kind = {
        "rtl": ComponentKind.MODULE,
        "liberty": ComponentKind.CELL,
        "lef": ComponentKind.MACRO,
    }.get(_view_id(view).kind, ComponentKind.UNKNOWN)
    return ComponentObservation(
        name,
        kind,
        ports,
        functions or {},
        _provenance(view, name, line=line),
    )


def _observation(
    view: str,
    *components: ComponentObservation,
    diagnostics: tuple[Diagnostic, ...] = (),
    complete: bool = True,
    tainted_scopes: frozenset[str] = frozenset(),
) -> ViewObservation:
    return ViewObservation(
        _view_id(view),
        components,
        diagnostics=diagnostics,
        complete=complete,
        tainted_scopes=tainted_scopes,
    )


def _codes(result: EngineResult) -> list[str]:
    return [diagnostic.code for diagnostic in result.diagnostics]


def test_fatal_parser_diagnostic_sets_exit_two_and_retains_location() -> None:
    location = _provenance("rtl", "uart")
    parser_error = Diagnostic.from_rule(
        "OC1101",
        "RTL syntax recovery failed.",
        provenance=location,
    )
    observed = _observation("rtl", diagnostics=(parser_error,), complete=False)

    result = ComparisonEngine(_project(("rtl",))).run((observed,))

    fatal = next(item for item in result.diagnostics if item.code == "OC1101")
    assert fatal.severity == Severity.FATAL
    assert fatal.message == "RTL syntax recovery failed."
    assert fatal.provenance == location
    assert result.exit_code == 2


def test_strict_inventory_reports_an_entirely_unobserved_required_view() -> None:
    rtl = _observation("rtl", _component("rtl", (_port("rtl", "irq"),)))
    policy = PolicySettings(strict_inventory=True)

    result = ComparisonEngine(_project(("rtl", "liberty"), policy=policy)).run((rtl,))

    finding = next(item for item in result.diagnostics if item.code == "OC3001")
    assert finding.metadata["missing_views"] == ["liberty.default"]
    assert "OC3101" not in _codes(result)


def test_incomplete_view_warns_and_suppresses_missing_inventory() -> None:
    rtl = _observation("rtl", _component("rtl", (_port("rtl", "irq"),)))
    incomplete_liberty = _observation("liberty", complete=False)
    policy = PolicySettings(strict_inventory=True)

    result = ComparisonEngine(_project(("rtl", "liberty"), policy=policy)).run(
        (rtl, incomplete_liberty)
    )

    assert _codes(result).count("OC1104") == 1
    assert "OC3001" not in _codes(result)
    assert "OC3101" not in _codes(result)


@pytest.mark.parametrize(
    ("second_ports", "expected_code"),
    [
        ((_port("rtl", "irq"),), "OC2003"),
        ((_port("rtl", "irq", direction=Direction.OUTPUT),), "OC2004"),
    ],
)
def test_duplicate_component_definitions_distinguish_identical_from_conflicting(
    second_ports: tuple[PortObservation, ...], expected_code: str
) -> None:
    first = _component("rtl", (_port("rtl", "irq"),), line=1)
    second = _component("rtl", second_ports, line=20)

    result = ComparisonEngine(_project(("rtl",))).run((_observation("rtl", first, second),))

    assert expected_code in _codes(result)
    other = "OC2004" if expected_code == "OC2003" else "OC2003"
    assert other not in _codes(result)


def test_duplicate_pin_spelling_is_reported_with_both_observations() -> None:
    duplicate_ports = (
        _port("rtl", "irq", line=2),
        _port("rtl", "irq", line=8),
    )
    observed = _observation("rtl", _component("rtl", duplicate_ports))

    result = ComparisonEngine(_project(("rtl",))).run((observed,))

    finding = next(item for item in result.diagnostics if item.code == "OC3103")
    assert len(finding.evidence) == 2


def test_scalar_one_bit_vector_policy_and_bus_integrity_edges() -> None:
    rtl = _observation(
        "rtl",
        _component("rtl", (_port("rtl", "one", shape=BusShape.scalar()),)),
    )
    liberty = _observation(
        "liberty",
        _component(
            "liberty",
            (_port("liberty", "one", shape=BusShape(left=0, right=0)),),
        ),
    )
    engine = ComparisonEngine(_project(("rtl", "liberty")))

    assert "OC4106" in _codes(engine.run((rtl, liberty)))

    equivalent_policy = PolicySettings(scalar_vector_equivalent=True)
    equivalent = ComparisonEngine(_project(("rtl", "liberty"), policy=equivalent_policy)).run(
        (rtl, liberty)
    )
    assert "OC4106" not in _codes(equivalent)

    malformed_shape = BusShape(
        width=3,
        bit_indices=(3, 1, 1),
        explicit_scalar=False,
    )
    malformed = _observation(
        "rtl",
        _component("rtl", (_port("rtl", "data", shape=malformed_shape),)),
    )
    malformed_result = ComparisonEngine(_project(("rtl",))).run((malformed,))
    assert {"OC4104", "OC4105"}.issubset(_codes(malformed_result))


def test_equal_width_buses_with_different_index_sets_are_not_conflated() -> None:
    rtl = _observation(
        "rtl",
        _component("rtl", (_port("rtl", "data", shape=BusShape(left=3, right=0)),)),
    )
    liberty = _observation(
        "liberty",
        _component(
            "liberty",
            (_port("liberty", "data", shape=BusShape(left=4, right=1)),),
        ),
    )

    result = ComparisonEngine(_project(("rtl", "liberty"))).run((rtl, liberty))

    assert "OC4103" in _codes(result)
    assert "OC4101" not in _codes(result)


def test_missing_power_pin_uses_power_specific_rule() -> None:
    liberty = _observation(
        "liberty",
        _component(
            "liberty",
            (
                _port("liberty", "VDD", role=PortRole.POWER),
                _port("liberty", "signal"),
            ),
        ),
    )
    lef = _observation("lef", _component("lef", (_port("lef", "signal"),)))

    result = ComparisonEngine(_project(("liberty", "lef"))).run((liberty, lef))

    assert "OC4202" in _codes(result)
    assert "OC3101" not in _codes(result)


@pytest.mark.parametrize(
    ("left_role", "right_role", "expected_code"),
    [
        (PortRole.POWER, PortRole.GROUND, "OC4203"),
        (PortRole.CLOCK, PortRole.SIGNAL, "OC4204"),
        (PortRole.ANALOG, PortRole.SIGNAL, "OC4201"),
    ],
)
def test_role_conflicts_use_the_most_specific_rule(
    left_role: PortRole,
    right_role: PortRole,
    expected_code: str,
) -> None:
    rtl = _observation(
        "rtl",
        _component("rtl", (_port("rtl", "special", role=left_role),)),
    )
    liberty = _observation(
        "liberty",
        _component("liberty", (_port("liberty", "special", role=right_role),)),
    )

    result = ComparisonEngine(_project(("rtl", "liberty"))).run((rtl, liberty))

    role_codes = {"OC4201", "OC4203", "OC4204"} & set(_codes(result))
    assert role_codes == {expected_code}


def _logic_observations(
    rtl_function: str,
    liberty_function: str,
    *,
    inputs: tuple[str, ...],
) -> tuple[ViewObservation, ViewObservation]:
    rtl_ports = tuple(_port("rtl", name) for name in inputs) + (
        _port("rtl", "Y", direction=Direction.OUTPUT),
    )
    liberty_ports = tuple(_port("liberty", name) for name in inputs) + (
        _port("liberty", "Y", direction=Direction.OUTPUT),
    )
    return (
        _observation(
            "rtl",
            _component("rtl", rtl_ports, functions={"Y": rtl_function}),
        ),
        _observation(
            "liberty",
            _component("liberty", liberty_ports, functions={"Y": liberty_function}),
        ),
    )


def test_unparseable_boolean_function_is_reported_as_uncheckable() -> None:
    observations = _logic_observations("A &", "A", inputs=("A",))

    result = ComparisonEngine(_project(("rtl", "liberty"))).run(observations)

    assert _codes(result).count("OC4302") == 1
    assert "OC4301" not in _codes(result)


def test_boolean_truth_table_limit_is_reported_without_guessing() -> None:
    observations = _logic_observations(
        "A | B | C",
        "A + B + C",
        inputs=("A", "B", "C"),
    )
    policy = PolicySettings(max_boolean_inputs=2)

    result = ComparisonEngine(_project(("rtl", "liberty"), policy=policy)).run(observations)

    finding = next(item for item in result.diagnostics if item.code == "OC4302")
    assert "2" in finding.message
    assert "OC4301" not in _codes(result)


def test_liberty_boolean_function_unknown_pin_has_dedicated_diagnostic() -> None:
    observations = _logic_observations("A", "A & GHOST", inputs=("A",))

    result = ComparisonEngine(_project(("rtl", "liberty"))).run(observations)

    finding = next(item for item in result.diagnostics if item.code == "OC4303")
    assert "GHOST" in finding.message
    assert "OC4301" not in _codes(result)


def test_selective_waiver_leaves_other_finding_active_and_reports_unused_waiver() -> None:
    rtl = _observation(
        "rtl",
        _component(
            "rtl",
            (
                _port(
                    "rtl",
                    "irq",
                    direction=Direction.OUTPUT,
                    shape=BusShape.scalar(),
                ),
            ),
        ),
    )
    liberty = _observation(
        "liberty",
        _component(
            "liberty",
            (
                _port(
                    "liberty",
                    "irq",
                    direction=Direction.INPUT,
                    shape=BusShape(left=3, right=0),
                ),
            ),
        ),
    )
    waivers = (
        Waiver(
            "OC4101",
            "The compatibility wrapper adapts this width.",
            object_pattern="component:uart/port:irq",
            views=("liberty.default",),
            property_pattern="shape.width",
        ),
        Waiver(
            "OC4201",
            "Intentionally unmatched guard waiver.",
            object_pattern="component:uart/port:irq",
        ),
    )

    result = ComparisonEngine(_project(("rtl", "liberty"), waivers=waivers)).run((rtl, liberty))

    width = next(item for item in result.diagnostics if item.code == "OC4101")
    direction = next(item for item in result.diagnostics if item.code == "OC4001")
    assert width.waived
    assert not direction.waived
    assert _codes(result).count("OC1005") == 1
    assert result.to_dict()["summary"]["suppressed"] == 1
    assert result.exit_code == 1


def test_contract_export_is_deterministic_and_round_trips(tmp_path: Path) -> None:
    rtl = _observation(
        "rtl",
        _component(
            "rtl",
            (_port("rtl", "irq", direction=Direction.OUTPUT),),
        ),
    )
    liberty = _observation(
        "liberty",
        _component(
            "liberty",
            (_port("liberty", "irq", direction=Direction.INPUT),),
        ),
    )
    engine = ComparisonEngine(_project(("rtl", "liberty")))
    result = engine.run((liberty, rtl))

    output = engine.export_contract(
        result.design,
        tmp_path / "nested" / "contract.json",
        observations=(liberty, rtl),
    )
    raw = output.read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert output == (tmp_path / "nested" / "contract.json").resolve()
    assert raw.endswith("\n")
    assert payload["components"][0]["ports"][0]["direction"] == "output"
    assert load_contract(output) == result.generated_contract


def test_component_pins_profile_participates_without_package_checks(tmp_path: Path) -> None:
    path = tmp_path / "component-pins.csv"
    path.write_text(
        "component,signal,direction,width\nuart,irq,input,4\n",
        encoding="utf-8",
    )
    csv_pins = parse_pin_csv(path, view_id="csv.pins")
    rtl = _observation(
        "rtl",
        _component(
            "rtl",
            (
                _port(
                    "rtl",
                    "irq",
                    direction=Direction.OUTPUT,
                    shape=BusShape.scalar(),
                ),
            ),
        ),
    )
    profiles = {"csv.pins": "component_pins"}

    result = ComparisonEngine(_project(("rtl", "csv.pins"), profiles=profiles)).run((rtl, csv_pins))

    assert {"OC4001", "OC4101"}.issubset(_codes(result))
    assert "OC5006" not in _codes(result)
    irq = result.design.components[0].port("irq")
    assert irq is not None
    assert {member.view for member in irq.members} == {
        ViewId("rtl"),
        ViewId("csv", "pins"),
    }


def test_only_package_map_profile_enables_package_mapping_checks(tmp_path: Path) -> None:
    path = tmp_path / "mapping.csv"
    path.write_text(
        "component,signal,direction\nuart,irq,output\n",
        encoding="utf-8",
    )
    package = parse_pin_csv(path, view_id="csv.package")
    rtl = _observation(
        "rtl",
        _component("rtl", (_port("rtl", "irq", direction=Direction.OUTPUT),)),
    )

    result = ComparisonEngine(
        _project(
            ("rtl", "csv.package"),
            profiles={"csv.package": "package_map"},
        )
    ).run((rtl, package))

    assert "OC5006" in _codes(result)
    irq = result.design.components[0].port("irq")
    assert irq is not None
    assert {member.view for member in irq.members} == {ViewId("rtl")}

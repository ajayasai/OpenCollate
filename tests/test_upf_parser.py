from __future__ import annotations

from pathlib import Path

import pytest

from opencollate.diagnostics import Severity
from opencollate.model import (
    DesignObjectObservation,
    Direction,
    FactState,
    PortRole,
    ViewId,
    ViewObservation,
)
from opencollate.parsers import upf as upf_parser
from opencollate.parsers.upf import UpfParser, parse_upf

FIXTURES = Path(__file__).parent / "fixtures" / "upf"


def _objects(
    view: ViewObservation,
    *,
    kind: str,
    relation: str,
) -> list[DesignObjectObservation]:
    return [item for item in view.objects if item.kind == kind and item.relation == relation]


def _assert_resource_failure(view: ViewObservation, resource: str) -> None:
    assert not view.complete
    assert view.tainted_scopes == frozenset({"*"})
    fatal = next(
        item
        for item in view.diagnostics
        if item.code == "OC1101" and item.severity == Severity.FATAL
    )
    assert resource in fatal.message
    assert fatal.metadata["actual"] > fatal.metadata["limit"]
    assert any(item.code == "OC1104" for item in view.diagnostics)


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


@pytest.mark.parametrize(
    ("constant", "resource"),
    [
        ("_MAX_SOURCE_BYTES", "byte count"),
        ("_MAX_SOURCE_CHARACTERS", "decoded-character count"),
    ],
)
def test_per_source_size_caps_accept_boundary_and_fail_one_past_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    resource: str,
) -> None:
    source = tmp_path / "boundary.upf"
    text = "upf_version 3.1\n"
    source.write_text(text, encoding="utf-8")
    encoded = source.read_bytes()
    boundary = len(encoded) if constant.endswith("BYTES") else len(encoded.decode("utf-8"))

    monkeypatch.setattr(upf_parser, constant, boundary)
    accepted = parse_upf(source)
    assert accepted.complete

    monkeypatch.setattr(upf_parser, constant, boundary - 1)
    rejected = parse_upf(source)
    _assert_resource_failure(rejected, resource)


@pytest.mark.parametrize(
    ("constant", "resource"),
    [
        ("_MAX_TOTAL_SOURCE_BYTES", "aggregate decoded-source byte count"),
        ("_MAX_TOTAL_SOURCE_CHARACTERS", "aggregate decoded-source character count"),
    ],
)
def test_source_size_budgets_are_aggregate_across_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    resource: str,
) -> None:
    text = "upf_version 3.1\n"
    first = tmp_path / "first.upf"
    second = tmp_path / "second.upf"
    first.write_text(text, encoding="utf-8")
    second.write_text(text, encoding="utf-8")
    encoded = first.read_bytes()
    one_size = len(encoded) if constant.endswith("BYTES") else len(encoded.decode("utf-8"))

    monkeypatch.setattr(upf_parser, constant, one_size * 2)
    accepted = parse_upf((first, second))
    assert accepted.complete

    monkeypatch.setattr(upf_parser, constant, one_size * 2 - 1)
    rejected = parse_upf((first, second))
    _assert_resource_failure(rejected, resource)
    fatal = next(item for item in rejected.diagnostics if item.code == "OC1101")
    assert fatal.provenance is not None
    assert fatal.provenance.source.endswith("second.upf")


@pytest.mark.parametrize(
    ("constant", "text", "boundary", "resource"),
    [
        ("_MAX_PHYSICAL_COMMANDS", "upf_version 3.1\n\n", 2, "physical-command count"),
        (
            "_MAX_LOGICAL_COMMANDS",
            "upf_version 3.1\nset_design_top soc\n",
            2,
            "logical-command count",
        ),
        ("_MAX_TOKENS", "upf_version 3.1\n", 2, "Tcl token count"),
        (
            "_MAX_WORDS",
            "create_power_domain PD -elements {u0 u1}\n",
            2,
            "evaluated Tcl word count",
        ),
    ],
)
def test_command_token_and_word_caps_have_exact_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    text: str,
    boundary: int,
    resource: str,
) -> None:
    source = tmp_path / f"{constant}.upf"
    source.write_text(text, encoding="utf-8")

    monkeypatch.setattr(upf_parser, constant, boundary)
    accepted = parse_upf(source)
    assert not any(item.code == "OC1101" for item in accepted.diagnostics)

    monkeypatch.setattr(upf_parser, constant, boundary - 1)
    rejected = parse_upf(source)
    _assert_resource_failure(rejected, resource)


def test_command_and_token_budgets_are_shared_across_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "one.upf"
    second = tmp_path / "two.upf"
    first.write_text("upf_version 3.1\n", encoding="utf-8")
    second.write_text("set_design_top soc\n", encoding="utf-8")
    monkeypatch.setattr(upf_parser, "_MAX_LOGICAL_COMMANDS", 1)

    view = parse_upf((first, second))

    _assert_resource_failure(view, "logical-command count")
    fatal = next(item for item in view.diagnostics if item.code == "OC1101")
    assert fatal.provenance is not None
    assert fatal.provenance.source.endswith("two.upf")


def test_empty_command_separator_flood_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "separators.upf"
    source.write_text(";" * 64, encoding="utf-8")
    monkeypatch.setattr(upf_parser, "_MAX_PHYSICAL_COMMANDS", 8)

    view = parse_upf(source)

    _assert_resource_failure(view, "physical-command count")
    assert not view.objects


def test_token_and_name_lengths_accept_boundary_and_reject_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_source = tmp_path / "token.upf"
    token_source.write_text("upf_version abcdefghijkl\n", encoding="utf-8")
    monkeypatch.setattr(upf_parser, "_MAX_TOKEN_CHARACTERS", 12)
    assert parse_upf(token_source).complete
    monkeypatch.setattr(upf_parser, "_MAX_TOKEN_CHARACTERS", 11)
    _assert_resource_failure(parse_upf(token_source), "Tcl token length")

    name_source = tmp_path / "name.upf"
    name_source.write_text("set_design_top soc42\n", encoding="utf-8")
    monkeypatch.setattr(upf_parser, "_MAX_TOKEN_CHARACTERS", 1024)
    monkeypatch.setattr(upf_parser, "_MAX_NAME_CHARACTERS", 5)
    assert parse_upf(name_source).complete
    monkeypatch.setattr(upf_parser, "_MAX_NAME_CHARACTERS", 4)
    _assert_resource_failure(parse_upf(name_source), "object name length")


@pytest.mark.parametrize(
    ("constant", "text", "boundary", "resource"),
    [
        (
            "_MAX_GROUPING_DEPTH",
            "set_design_top {{{soc}}}\n",
            3,
            "grouping depth",
        ),
        (
            "_MAX_SUBSTITUTION_DEPTH",
            "set_design_top [list [list soc]]\n",
            2,
            "command-substitution depth",
        ),
        (
            "_MAX_EVALUATION_DEPTH",
            "create_power_domain PD -elements {{{u0}}}\n",
            2,
            "Tcl-list evaluation depth",
        ),
    ],
)
def test_grouping_substitution_and_evaluation_depths_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    text: str,
    boundary: int,
    resource: str,
) -> None:
    source = tmp_path / f"{constant}.upf"
    source.write_text(text, encoding="utf-8")

    monkeypatch.setattr(upf_parser, constant, boundary)
    boundary_view = parse_upf(source)
    assert not any(item.code == "OC1101" for item in boundary_view.diagnostics)

    monkeypatch.setattr(upf_parser, constant, boundary - 1)
    rejected = parse_upf(source)
    _assert_resource_failure(rejected, resource)


def test_emitted_observation_cap_includes_objects_ports_and_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "observations.upf"
    source.write_text(
        "set_design_top soc\ncreate_supply_port VDD -direction in\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(upf_parser, "_MAX_OBSERVATIONS", 4)
    accepted = parse_upf(source)
    assert accepted.complete
    assert len(accepted.objects) == 2
    assert len(accepted.components) == 1
    assert len(accepted.components[0].ports) == 1

    monkeypatch.setattr(upf_parser, "_MAX_OBSERVATIONS", 3)
    rejected = parse_upf(source)
    _assert_resource_failure(rejected, "emitted-observation count")
    assert len(rejected.objects) <= 2
    assert not rejected.components


def test_source_file_count_and_component_override_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "one.upf"
    second = tmp_path / "two.upf"
    first.write_text("upf_version 3.1\n", encoding="utf-8")
    second.write_text("upf_version 3.1\n", encoding="utf-8")
    monkeypatch.setattr(upf_parser, "_MAX_SOURCE_FILES", 1)

    _assert_resource_failure(parse_upf((first, second)), "source-file count")

    monkeypatch.setattr(upf_parser, "_MAX_NAME_CHARACTERS", 3)
    with pytest.raises(ValueError, match="component_name must not exceed"):
        parse_upf(first, component_name="soc0")


def test_bounded_reader_preserves_unreadable_and_latin1_taint_behavior(tmp_path: Path) -> None:
    missing = parse_upf(tmp_path / "missing.upf")
    assert not missing.complete
    assert missing.tainted_scopes == frozenset({"*"})
    assert any(item.code == "OC1002" for item in missing.diagnostics)

    latin1_source = tmp_path / "latin1.upf"
    latin1_source.write_bytes(b"set_design_top soc\xff\n")
    latin1 = parse_upf(latin1_source)
    assert not latin1.complete
    assert latin1.tainted_scopes == frozenset({"*"})
    assert any(item.code == "OC1104" and "Latin-1" in item.message for item in latin1.diagnostics)
    design_top = next(
        item for item in latin1.objects if item.attributes["target_kind"] == "design_top"
    )
    assert design_top.status == FactState.TAINTED


def test_grouping_inside_unsupported_substitution_is_still_depth_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "nested-substitution.upf"
    source.write_text("set_design_top [list {{{soc}}}]\n", encoding="utf-8")
    monkeypatch.setattr(upf_parser, "_MAX_GROUPING_DEPTH", 2)

    view = parse_upf(source)

    _assert_resource_failure(view, "grouping depth")


def test_static_list_quoting_grouping_and_escaping_remain_literal(tmp_path: Path) -> None:
    source = tmp_path / "static-lists.upf"
    source.write_text(
        'create_power_domain PD -elements {"u 0" {u1} u\\ 2}\nset_design_top "soc\\ top"\n',
        encoding="utf-8",
    )

    view = parse_upf(source)

    assert view.complete
    assert view.attributes["power_domains"][0]["elements"] == ["u 0", "u1", "u 2"]
    assert view.components[0].native_name == "soc top"


def test_grouped_command_substitution_is_retained_but_never_run(tmp_path: Path) -> None:
    source = tmp_path / "grouped-substitution.upf"
    source.write_text("set_design_top [list {soc\\ name}]\n", encoding="utf-8")

    view = parse_upf(source)

    assert not view.complete
    assert not any(item.code == "OC1101" for item in view.diagnostics)
    assert any(item.code == "OC1102" for item in view.diagnostics)
    assert not view.components


@pytest.mark.parametrize(
    ("text", "expected_complete"),
    [
        ("set_design_top {soc}suffix\n", False),
        ('set_design_top "soc"suffix\n', True),
    ],
)
def test_concatenated_grouped_tokens_follow_static_tcl_rules(
    tmp_path: Path,
    text: str,
    expected_complete: bool,
) -> None:
    source = tmp_path / "concatenated.upf"
    source.write_text(text, encoding="utf-8")

    view = parse_upf(source)

    assert view.complete is expected_complete
    if expected_complete:
        assert view.components[0].native_name == "socsuffix"
    else:
        assert view.tainted_scopes == frozenset({"*"})
        assert any(item.code == "OC1101" for item in view.diagnostics)

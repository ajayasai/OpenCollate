from __future__ import annotations

from pathlib import Path

import pytest

from opencollate.diagnostics import Severity
from opencollate.model import Direction, FactState, PortRole, ViewId, ViewObservation
from opencollate.parsers import cdl
from opencollate.parsers.cdl import CDLParser, CdlParser, SpiceParser, parse_cdl, parse_spice

FIXTURES = Path(__file__).parent / "fixtures" / "cdl"


def _objects(
    view: ViewObservation,
    kind: str,
    *,
    relation: str = "definition",
) -> list[object]:
    return [item for item in view.objects if item.kind == kind and item.relation == relation]


def test_structural_cdl_extracts_case_insensitive_subcircuits_and_unknown_shapes() -> None:
    view = parse_cdl(FIXTURES / "structural.cdl")

    assert view.view == ViewId("cdl")
    assert view.complete
    assert not view.diagnostics
    assert [component.name for component in view.components] == ["INV", "TOP"]
    inverter = view.components[0]
    assert inverter.kind.value == "cell"
    assert inverter.attributes["parameters"] == {"DRIVE": "2", "LCH": "16n"}
    ports = {port.name: port for port in inverter.ports}
    assert ports["A"].direction == Direction.INPUT
    assert ports["Y"].direction == Direction.OUTPUT
    assert ports["VDD!"].role == PortRole.POWER
    assert ports["VSS!"].role == PortRole.GROUND
    assert ports["A"].shape.width is None
    assert ports["A"].state_for("shape") == FactState.UNKNOWN
    assert ports["A"].provenance is not None
    assert ports["A"].provenance.line == 4


def test_instances_connectivity_continuations_and_escaped_names_are_preserved() -> None:
    view = parse_cdl(FIXTURES / "structural.cdl")

    instances = {item.qualified_name: item for item in _objects(view, "instance")}
    mos = instances["INV/M_N"]
    assert mos.attributes["instance_type"] == "mosfet"
    assert mos.attributes["nodes"] == ["Y", "A", "VSS!", "VSS!"]
    assert mos.attributes["master"] == "nch"
    assert mos.attributes["parameters"] == {"W": "{DRIVE * 1u}", "L": "{LCH}"}
    assert mos.attributes["raw"].endswith("L={LCH}")

    escaped = instances["INV/R load"]
    assert escaped.attributes["nodes"] == ["Y", "sense/node"]
    assert escaped.provenance is not None
    assert escaped.provenance.raw_name == r"R\ load"
    assert instances["TOP/X/u0"].attributes["master"] == "INV"

    nets = [item for item in _objects(view, "net") if item.attributes.get("component") == "INV"]
    sense = next(item for item in nets if item.native_name == "sense/node")
    assert {connection["instance"] for connection in sense.attributes["connections"]} == {
        "R load",
        "C0",
    }
    assert next(item for item in nets if item.native_name == "VDD!").attributes["global"]


def test_pin_comment_directives_only_supply_explicit_facts() -> None:
    view = parse_cdl(FIXTURES / "structural.cdl")

    ports = {port.name: port for port in view.components[1].ports}
    assert ports["IN"].direction == Direction.INPUT
    assert ports["OUT"].direction == Direction.OUTPUT
    assert ports["VDD!"].direction == Direction.UNKNOWN
    assert ports["VDD!"].role == PortRole.POWER
    assert ports["VSS!"].role == PortRole.GROUND
    assert ports["VSS!"].state_for("direction") == FactState.UNKNOWN
    assert ports["VSS!"].state_for("role") == FactState.KNOWN


def test_dspf_pin_and_explicit_port_metadata_are_supported() -> None:
    view = parse_cdl(FIXTURES / "dspf-pin.cdl")

    assert view.complete
    ports = {port.name: port for port in view.components[0].ports}
    assert ports["PAD"].direction == Direction.INOUT
    assert ports["PAD"].role == PortRole.UNKNOWN
    assert ports["CORE"].direction == Direction.OUTPUT
    assert ports["CORE"].role == PortRole.SIGNAL


def test_models_globals_and_subcircuit_references_are_first_class(tmp_path: Path) -> None:
    source = tmp_path / "model.cdl"
    source.write_text(
        ".GLOBAL VSS\n"
        ".MODEL nch NMOS LEVEL=1\n"
        ".SUBCKT child A B\n"
        "R1 A B R=10k\n"
        ".ENDS\n"
        ".SUBCKT parent A B\n"
        "X1 A B child\n"
        ".ENDS\n",
        encoding="utf-8",
    )

    view = parse_cdl(source)

    assert view.complete
    models = _objects(view, "model")
    assert len(models) == 1
    assert models[0].native_name == "nch"
    globals_ = [item for item in _objects(view, "net") if item.attributes.get("declaration")]
    assert [item.native_name for item in globals_] == ["VSS"]
    references = _objects(view, "component", relation="reference")
    assert [(item.native_name, item.attributes["instance"]) for item in references] == [
        ("child", "X1")
    ]


def test_malformed_duplicate_and_unsupported_constructs_taint_without_guessing() -> None:
    view = parse_cdl(FIXTURES / "malformed.cdl")

    assert not view.complete
    assert {"*", "BAD", "OPEN"}.issubset(view.tainted_scopes)
    assert any(item.code == "OC1101" for item in view.diagnostics)
    assert any(item.code == "OC1102" for item in view.diagnostics)
    assert any(item.severity == Severity.ERROR for item in view.diagnostics)
    bad = view.components[0]
    assert bad.status == FactState.TAINTED
    assert len(bad.ports) == 2
    assert all(port.status == FactState.TAINTED for port in bad.ports)
    assert all(port.shape.width is None for port in bad.ports)
    unsupported = next(
        item for item in _objects(view, "instance") if item.native_name == "Qunsupported"
    )
    assert unsupported.status == FactState.UNSUPPORTED
    duplicates = [item for item in _objects(view, "instance") if item.native_name == "Xdup"]
    assert len(duplicates) == 2
    assert all(item.status == FactState.TAINTED for item in duplicates)


def test_parameter_text_is_never_evaluated_or_expanded(tmp_path: Path) -> None:
    sentinel = tmp_path / "must-not-exist"
    source = tmp_path / "safe.cdl"
    source.write_text(
        f".SUBCKT safe A B PARAMS: PAYLOAD={{touch {sentinel}" + "}\nR1 A B R={PAYLOAD}\n.ENDS\n",
        encoding="utf-8",
    )

    view = parse_cdl(source)

    assert not sentinel.exists()
    assert view.complete
    assert view.components[0].attributes["parameters"]["PAYLOAD"].startswith("{touch ")
    resistor = next(item for item in _objects(view, "instance"))
    assert resistor.attributes["parameters"] == {"R": "{PAYLOAD}"}
    assert view.attributes["parameters_evaluated"] is False


def test_resource_limits_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "large.cdl"
    source.write_text(".SUBCKT x a b\nR1 a b 1k\n.ENDS\n", encoding="utf-8")
    monkeypatch.setattr(cdl, "_MAX_SOURCE_CHARACTERS", 8)

    view = parse_cdl(source)

    assert not view.complete
    assert not view.components
    assert view.tainted_scopes == frozenset({"*"})
    assert any(
        item.code == "OC1101" and item.severity == Severity.FATAL for item in view.diagnostics
    )


def test_cross_file_duplicate_subcircuits_are_diagnosed(tmp_path: Path) -> None:
    first = tmp_path / "a.cdl"
    second = tmp_path / "b.spi"
    first.write_text(".SUBCKT Cell A\n.ENDS\n", encoding="utf-8")
    second.write_text(".subckt cell B\n.ends\n", encoding="utf-8")

    view = parse_cdl((first, second), view_id="cdl.extracted")

    assert view.view == ViewId("cdl", "extracted")
    assert not view.complete
    assert len(view.components) == 2
    assert all(component.status == FactState.TAINTED for component in view.components)
    component_objects = [
        item for item in _objects(view, "component") if item.relation == "definition"
    ]
    assert all(item.status == FactState.TAINTED for item in component_objects)
    assert any("across inputs" in item.message for item in view.diagnostics)


def test_parser_classes_and_spice_alias_share_the_public_api() -> None:
    path = FIXTURES / "dspf-pin.cdl"

    direct = parse_spice(path, view_id="spice.extracted")
    through_cdl = CdlParser().parse((path,), view_id="spice.extracted")
    through_upper = CDLParser().parse((path,), view_id="spice.extracted")
    through_spice = SpiceParser().parse((path,), view_id="spice.extracted")

    assert direct == through_cdl == through_upper == through_spice
    assert CdlParser.format_name == "cdl"


def test_supported_device_variants_do_not_require_value_evaluation(tmp_path: Path) -> None:
    source = tmp_path / "variants.cdl"
    source.write_text(
        ".SUBCKT devices A B C D\n"
        "M3 A B C nch SCALE = {1 + 2}\n"
        "M4 A B C D pch\n"
        "Rmodel A B / rpoly TC=1\n"
        "Rparam A B R=10k\n"
        "Rextra A B 1k unexpected\n"
        "Xzero child\n"
        "Xbad /\n"
        ".ENDS\n",
        encoding="utf-8",
    )

    view = parse_cdl(source)

    instances = {item.native_name: item for item in _objects(view, "instance")}
    assert instances["M3"].attributes["nodes"] == ["A", "B", "C"]
    assert instances["M3"].attributes["parameters"] == {"SCALE": "{1 + 2}"}
    assert instances["M4"].attributes["nodes"] == ["A", "B", "C", "D"]
    assert instances["Rmodel"].attributes["master"] == "rpoly"
    assert instances["Rparam"].attributes["parameters"] == {"R": "10k"}
    assert instances["Xzero"].attributes["master"] == "child"
    assert instances["Xzero"].status == FactState.TAINTED
    assert instances["Rextra"].status == FactState.TAINTED
    assert instances["Xbad"].status == FactState.TAINTED


def test_top_level_title_parameters_instances_and_end_boundary(tmp_path: Path) -> None:
    source = tmp_path / "deck.spi"
    source.write_text(
        "A simulation title\n"
        "; full-line comment\n"
        "// another comment\n"
        ".PARAM TOP = 1\n"
        'Rtop a b "1k ; $ // literal" ; real comment\n'
        "Rtop a b 2k $ duplicate instance\n"
        ".END\n"
        "Rafter a b 3k\n",
        encoding="utf-8",
    )

    view = parse_cdl(source)

    source_facts = view.attributes["sources"][str(source)]
    assert source_facts["title"] == "A simulation title"
    assert source_facts["top_parameters"] == {"TOP": "1"}
    assert source_facts["ended"] is True
    top_instances = [item for item in _objects(view, "instance") if item.scope is None]
    assert len(top_instances) == 2
    assert all(item.status == FactState.TAINTED for item in top_instances)
    assert any("after .END" in item.message for item in view.diagnostics)


def test_malformed_directives_and_metadata_have_recoverable_evidence(tmp_path: Path) -> None:
    source = tmp_path / "directives.cdl"
    source.write_text(
        "*.PININFO OUT:I\n"
        ".PORT_DIRECTION\n"
        ".PIN\n"
        ".GLOBAL\n"
        ".MODEL only\n"
        ".SUBCKT\n"
        ".ENDS\n"
        ".UNKNOWN data\n"
        ".SUBCKT C A\n"
        "*.PININFO\n"
        "*.PORT_DIRECTION A\n"
        "* PIN A\n"
        "*|P ()\n"
        ".PARAM P=1\n"
        ".PARAM p=2\n"
        ".MODEL mod nmos\n"
        ".MODEL MOD pmos\n"
        ".END\n",
        encoding="utf-8",
    )

    view = parse_cdl(source)

    assert not view.complete
    assert {item.code for item in view.diagnostics} >= {"OC1101", "OC1102"}
    assert any(
        item.kind == "pin" and item.relation == "reference" and item.native_name == "OUT"
        for item in view.objects
    )
    assert any(
        item.kind == "directive" and item.status == FactState.UNSUPPORTED for item in view.objects
    )
    model = next(item for item in _objects(view, "model"))
    assert model.status == FactState.TAINTED


def test_nested_and_same_file_duplicate_subcircuits_are_tainted(tmp_path: Path) -> None:
    source = tmp_path / "nested.cdl"
    source.write_text(
        ".SUBCKT A P\n.SUBCKT B Q\n.ENDS B EXTRA\n.SUBCKT a R\n.ENDS\n",
        encoding="utf-8",
    )

    view = parse_cdl(source)

    assert [item.name for item in view.components] == ["A", "B", "a"]
    assert all(item.status == FactState.TAINTED for item in view.components)
    assert any("Nested" in item.message for item in view.diagnostics)
    assert any("Duplicate .SUBCKT" in item.message for item in view.diagnostics)


def test_unbalanced_tokens_are_diagnosed_without_crashing(tmp_path: Path) -> None:
    source = tmp_path / "tokens.cdl"
    source.write_text(
        ".SUBCKT token A B\n"
        "Rquote A B 'unterminated\n"
        "Rgroup A B {unterminated\n"
        "Rclose A B 1k )\n"
        "Rescape A B 1k \\\n"
        ".ENDS\n",
        encoding="utf-8",
    )

    view = parse_cdl(source)

    assert not view.complete
    assert view.components[0].status == FactState.TAINTED
    messages = [item.message for item in view.diagnostics]
    assert any("unterminated quote" in message for message in messages)
    assert any("unterminated group" in message for message in messages)
    assert any("unmatched" in message for message in messages)
    assert any("incomplete escape" in message for message in messages)


@pytest.mark.parametrize(
    ("constant", "limit"),
    [
        ("_MAX_PHYSICAL_LINE_CHARACTERS", 4),
        ("_MAX_LOGICAL_LINES", 1),
        ("_MAX_TOKENS_PER_LINE", 2),
        ("_MAX_GROUP_DEPTH", 1),
        ("_MAX_NAME_CHARACTERS", 2),
        ("_MAX_OBJECTS", 1),
    ],
)
def test_each_structural_resource_limit_fails_closed(
    constant: str,
    limit: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / f"{constant}.cdl"
    source.write_text(
        ".SUBCKT long_name A B\nR1 A B {{1}}\n.ENDS\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cdl, constant, limit)

    view = parse_cdl(source)

    assert not view.complete
    assert any(item.code == "OC1101" for item in view.diagnostics)

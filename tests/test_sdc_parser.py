from __future__ import annotations

from pathlib import Path

import pytest

from opencollate.model import FactState, Provenance, ViewId
from opencollate.parsers import sdc as sdc_module
from opencollate.parsers.sdc import (
    SdcParser,
    TimingConstraintObservation,
    _ListSyntaxError,
    _split_static_list,
    parse_sdc,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sdc"


def test_sdc_extracts_typed_references_and_clocks() -> None:
    view = parse_sdc(FIXTURES / "constraints.sdc")

    assert view.view == ViewId("sdc")
    assert view.complete
    assert not view.diagnostics
    assert view.attributes["tcl_execution"] is False
    assert view.attributes["static_variables"] == (
        "CLK_PORT",
        "GENERATED_TARGET",
        "IO_PORTS",
        "PERIOD",
    )

    references = {(item.kind, item.native_name): item for item in view.objects}
    clock_port = references["port", "clk_i"]
    assert clock_port.relation == "reference"
    assert clock_port.provenance is not None
    assert clock_port.provenance.line == 7
    assert clock_port.attributes["command"] == "get_ports"
    assert clock_port.attributes["context"] == "create_clock"
    assert clock_port.attributes["dynamic"] is False

    wildcard = references["port", "data[*]"]
    assert wildcard.status == FactState.KNOWN
    assert wildcard.attributes["pattern"] is True
    assert wildcard.attributes["match_mode"] == "glob"
    assert wildcard.attributes["dynamic"] is True

    primary, generated = view.clocks
    assert primary.native_name == "core_clk"
    assert primary.targets == ("clk_i",)
    assert primary.period == 10.0
    assert primary.waveform == (0.0, 5.0)
    assert not primary.generated
    assert primary.status == FactState.KNOWN

    assert generated.native_name == "div_clk"
    assert generated.targets == ("u_div/clk_q",)
    assert generated.source == "u_div/clk_i"
    assert generated.generated
    assert generated.attributes["master_clocks"] == ["core_clk"]
    assert generated.attributes["divide_by"] == 2


def test_sdc_extracts_delays_false_and_multicycle_paths() -> None:
    view = parse_sdc(FIXTURES / "constraints.sdc")
    constraints = view.attributes["constraints"]

    assert all(isinstance(item, TimingConstraintObservation) for item in constraints)
    assert [item.command for item in constraints] == [
        "set_input_delay",
        "set_output_delay",
        "set_false_path",
        "set_multicycle_path",
    ]
    input_delay, output_delay, false_path, multicycle = constraints
    assert (input_delay.value, input_delay.objects, input_delay.clocks) == (
        1.25,
        ("rx_i",),
        ("core_clk",),
    )
    assert (output_delay.value, output_delay.objects, output_delay.clocks) == (
        2.5,
        ("tx_o",),
        ("core_clk",),
    )
    assert false_path.from_objects == ("u_async_src",)
    assert false_path.through_objects == ("u_sync/ff1/D",)
    assert false_path.to_objects == ("u_sync/ff2/D",)
    assert multicycle.value == 2
    assert multicycle.from_objects == ("core_clk",)
    assert multicycle.to_objects == ("div_clk",)
    assert multicycle.clocks == ("core_clk", "div_clk")
    assert all(item.status == FactState.KNOWN for item in constraints)


def test_static_list_and_variable_substitution_are_supported(tmp_path: Path) -> None:
    source = tmp_path / "variables.sdc"
    source.write_text(
        "set PORTS [list clk_i {reset n} data\\[0\\]]\n"
        'get_ports "$PORTS"\n'
        "set PERIOD {8.0}\n"
        "create_clock -period ${PERIOD} \\\n"
        "    [get_ports clk_i]\n",
        encoding="utf-8",
    )

    view = parse_sdc(source)

    assert view.complete
    assert not view.diagnostics
    names = [item.native_name for item in view.objects]
    assert names[:3] == ["clk_i", "reset n", "data[0]"]
    assert view.clocks[0].native_name == "clk_i"
    assert view.clocks[0].period == 8.0


def test_clock_name_is_derived_per_exact_target_and_virtual_clock_is_supported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "derived.sdc"
    source.write_text(
        "create_clock -period 4.0 [get_ports {clk_a clk_b}]\n"
        "create_clock -name virtual_clk -period 12.0\n",
        encoding="utf-8",
    )

    view = parse_sdc(source)

    assert view.complete
    assert [(clock.native_name, clock.targets) for clock in view.clocks] == [
        ("clk_a", ("clk_a",)),
        ("clk_b", ("clk_b",)),
        ("virtual_clk", ()),
    ]


def test_unresolved_and_unsupported_tcl_are_explicitly_tainted() -> None:
    view = parse_sdc(FIXTURES / "unsupported.sdc")

    assert not view.complete
    assert view.tainted_scopes == frozenset({"*"})
    assert {item.code for item in view.diagnostics} == {"OC1102", "OC1103", "OC1104"}
    assert any("was not executed" in item.message for item in view.diagnostics)
    assert view.clocks[0].native_name == "uncertain"
    assert view.clocks[0].status == FactState.TAINTED
    assert view.clocks[0].period is None
    assert view.attributes["constraints"][0].status == FactState.TAINTED


def test_filter_selection_is_preserved_but_not_claimed_as_exact(tmp_path: Path) -> None:
    source = tmp_path / "filter.sdc"
    source.write_text("get_ports -filter {direction == in} *\n", encoding="utf-8")

    view = parse_sdc(source)

    assert not view.complete
    reference = view.objects[0]
    assert reference.native_name == "*"
    assert reference.status == FactState.UNSUPPORTED
    assert reference.attributes["dynamic"] is True
    assert reference.attributes["options"]["-filter"] == ["direction == in"]
    assert any(item.code == "OC1102" for item in view.diagnostics)


def test_unset_or_unknown_variable_never_becomes_a_literal_known_reference(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unresolved.sdc"
    source.write_text("set PORT clk_i\nunset PORT\nget_ports $PORT\n", encoding="utf-8")

    view = parse_sdc(source)

    assert not view.complete
    assert view.objects[0].native_name == "$PORT"
    assert view.objects[0].status == FactState.TAINTED
    assert view.objects[0].attributes["dynamic"] is True
    assert any(item.code == "OC1103" and "PORT" in item.message for item in view.diagnostics)


def test_malformed_tcl_is_fatal_and_does_not_emit_known_facts() -> None:
    view = parse_sdc(FIXTURES / "malformed.sdc")

    assert not view.complete
    assert view.tainted_scopes == frozenset({"*"})
    assert any(
        item.code == "OC1101" and item.severity.value == "fatal" for item in view.diagnostics
    )
    assert view.objects == ()
    assert view.clocks == ()


def test_external_commands_are_reported_and_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    source = tmp_path / "unsafe.sdc"
    source.write_text(
        f"exec cmd /c echo compromised > {{{marker}}}\nsource other.sdc\n",
        encoding="utf-8",
    )

    view = parse_sdc(source)

    assert not marker.exists()
    assert not view.complete
    assert sum(item.code == "OC1102" for item in view.diagnostics) == 2
    assert all(
        "was not executed" in item.message for item in view.diagnostics if item.code == "OC1102"
    )


def test_excessive_command_substitution_nesting_is_controlled(tmp_path: Path) -> None:
    source = tmp_path / "deep.sdc"
    source.write_text("set X " + "[" * 2_000 + "list x" + "]" * 2_000, encoding="utf-8")

    view = parse_sdc(source)

    assert not view.complete
    assert view.objects == ()
    assert any(
        item.code == "OC1101" and "nesting levels" in item.message for item in view.diagnostics
    )


def test_static_variables_carry_across_ordered_input_files(tmp_path: Path) -> None:
    definitions = tmp_path / "definitions.sdc"
    constraints = tmp_path / "constraints.sdc"
    definitions.write_text("set CLK clk_i\nset PERIOD 6.25\n", encoding="utf-8")
    constraints.write_text(
        "create_clock -name core -period $PERIOD [get_ports $CLK]\n",
        encoding="utf-8",
    )

    view = parse_sdc((definitions, constraints), view_name="signoff")

    assert view.view == ViewId("sdc", "signoff")
    assert view.complete
    assert view.clocks[0].period == 6.25
    assert view.clocks[0].targets == ("clk_i",)


def test_command_scanner_respects_quotes_comments_and_escaped_bus_bits(
    tmp_path: Path,
) -> None:
    source = tmp_path / "grouping.sdc"
    source.write_text(
        'set CLOSE [list "]"]\n'
        "set PORT [list clk_i ; # ] inside this comment is not a terminator\n"
        "]\n"
        "get_ports $PORT\n"
        r"get_ports {data\[0\]}" + "\n",
        encoding="utf-8",
    )

    view = parse_sdc(source)

    assert view.complete
    assert not view.diagnostics
    assert view.attributes["static_variables"] == ("CLOSE", "PORT")
    assert [item.native_name for item in view.objects] == ["clk_i", "data[0]"]
    assert view.objects[1].attributes["match_mode"] == "exact"
    assert view.objects[1].attributes["dynamic"] is False


def test_tcl_array_variables_are_never_mistaken_for_static_scalars(tmp_path: Path) -> None:
    source = tmp_path / "array.sdc"
    source.write_text("set PORT clk_i\nget_ports $PORT(index)\n", encoding="utf-8")

    view = parse_sdc(source)

    assert not view.complete
    assert view.objects[0].native_name == "$PORT(index)"
    assert view.objects[0].status == FactState.TAINTED
    assert any(
        item.code == "OC1102" and "array variable" in item.message for item in view.diagnostics
    )


def test_ambiguous_literal_clock_targets_are_not_claimed_as_known_ports(
    tmp_path: Path,
) -> None:
    source = tmp_path / "literal-target.sdc"
    source.write_text("create_clock -name clk -period 2.0 clk_i\n", encoding="utf-8")

    view = parse_sdc(source)

    assert not view.complete
    assert view.objects[0].native_name == "clk_i"
    assert view.objects[0].status == FactState.UNSUPPORTED
    assert view.objects[0].attributes["dynamic"] is True
    assert view.clocks[0].status == FactState.TAINTED
    assert any(item.code == "OC1102" and "ambiguous" in item.message for item in view.diagnostics)


def test_advanced_generated_clock_and_delay_options_are_retained(tmp_path: Path) -> None:
    source = tmp_path / "advanced.sdc"
    source.write_text(
        "create_generated_clock -name fast_clk "
        "-source [get_ports clk_i] -master_clock root_clk "
        "-multiply_by 3 -duty_cycle 40 -phase 1.5 "
        "-edges [list 1 3 5] -edge_shift {0 0.1 0.2} "
        "-invert -add [get_pins u_pll/clk_o]\n"
        "set_output_delay -clock_fall -fall -max -add_delay "
        "-network_latency_included -source_latency_included "
        "-clock root_clk -reference_pin u_pad/PAD -0.5 [get_ports tx_o]\n"
        "set_false_path -setup -rise_from [get_ports async_i] "
        "-fall_to [get_pins u_sync/D] -rise_through [get_cells u_mux] "
        "-comment {intentional CDC}\n",
        encoding="utf-8",
    )

    view = parse_sdc(source)

    assert view.complete
    assert not view.diagnostics
    generated = view.clocks[0]
    assert generated.source == "clk_i"
    assert generated.attributes["master_clocks"] == ["root_clk"]
    assert generated.attributes["multiply_by"] == 3
    assert generated.attributes["duty_cycle"] == 40.0
    assert generated.attributes["phase"] == 1.5
    assert generated.attributes["edges"] == ["1", "3", "5"]
    assert generated.attributes["edge_shift"] == ["0", "0.1", "0.2"]
    output_delay, false_path = view.attributes["constraints"]
    assert output_delay.value == -0.5
    assert output_delay.attributes["reference_pins"] == ["u_pad/PAD"]
    assert false_path.from_objects == ("async_i",)
    assert false_path.to_objects == ("u_sync/D",)
    assert false_path.through_objects == ("u_mux",)


def test_invalid_sdc_semantics_remain_observable_without_crashing(tmp_path: Path) -> None:
    source = tmp_path / "semantic-errors.sdc"
    source.write_text(
        "set\n"
        "set {bad(name)} value\n"
        "unset a b\n"
        "get_ports\n"
        "get_cells -bogus foo\n"
        "get_clocks -filter\n"
        "set COLL [get_ports p]\n"
        "list $COLL\n"
        "set CONCAT prefix$COLL\n"
        "create_clock -name $MISSING -period zero -waveform {0} [get_ports clk]\n"
        "create_clock -name bad_wave -period 1 -waveform {a b} [get_ports clk2]\n"
        "create_clock -period 2 [get_ports *]\n"
        "create_generated_clock -name missing_all\n"
        "create_generated_clock -name invalid_generated "
        "-source [expr x] -multiply_by nope -divide_by 0 "
        "-duty_cycle nope -phase nope -edges [expr x] -edge_shift [expr x]\n"
        "set_input_delay\n"
        "set_output_delay nope\n"
        "set_multicycle_path\n"
        "set_multicycle_path nope extra\n"
        "set_false_path extra\n"
        "set_false_path -from literal\n",
        encoding="utf-8",
    )

    view = parse_sdc(source)

    assert not view.complete
    assert view.tainted_scopes == frozenset({"*"})
    codes = {item.code for item in view.diagnostics}
    assert codes == {"OC1102", "OC1103", "OC1104"}
    assert any(item.status == FactState.UNSUPPORTED for item in view.objects)
    assert all(clock.status == FactState.TAINTED for clock in view.clocks)
    assert any(
        item.command == "set_multicycle_path" and item.status == FactState.TAINTED
        for item in view.attributes["constraints"]
    )


def test_constraint_record_validation_and_serialization() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        TimingConstraintObservation(" ")

    location = Provenance("constraints.sdc", 4, 2, ViewId("sdc"))
    constraint = TimingConstraintObservation(
        " SET_FALSE_PATH ",
        from_objects=["a"],
        to_objects=["b"],
        through_objects=["c"],
        clocks=["clk"],
        provenance=location,
        attributes={"setup": True},
    )

    serialized = constraint.to_dict()
    assert serialized["command"] == "set_false_path"
    assert serialized["from_objects"] == ["a"]
    assert serialized["provenance"]["line"] == 4
    assert serialized["attributes"] == {"setup": True}


def test_static_tcl_list_parser_covers_grouping_and_rejects_ambiguity() -> None:
    assert _split_static_list('a {b {c}} "d e" f\\ g h\\\n  i') == (
        "a",
        "b {c}",
        "d e",
        "f g",
        "h i",
    )
    assert _split_static_list('"a\\tb"') == ("a\tb",)

    invalid_lists = (
        "trailing\\",
        "{unclosed",
        "{closed}suffix",
        '"unclosed',
        '"closed"suffix',
        "{" * 130 + "x" + "}" * 130,
    )
    for value in invalid_lists:
        with pytest.raises(_ListSyntaxError):
            _split_static_list(value)


@pytest.mark.parametrize(
    "source_text",
    [
        "set X \\",
        'set X "unterminated',
        "set X {unterminated",
        "set X {ok}suffix",
        "set X ${unterminated",
        "set X $PORT(index",
        "set X " + "{" * 130 + "x" + "}" * 130,
    ],
)
def test_tcl_tokenizer_reports_grouping_failures_as_fatal(tmp_path: Path, source_text: str) -> None:
    source = tmp_path / "bad-token.sdc"
    source.write_text(source_text, encoding="utf-8")

    view = parse_sdc(source)

    assert not view.complete
    assert view.objects == ()
    assert any(
        item.code == "OC1101" and item.severity.value == "fatal" for item in view.diagnostics
    )


def test_tcl_scalar_escapes_and_bare_dollar_are_static(tmp_path: Path) -> None:
    source = tmp_path / "escapes.sdc"
    source.write_text(
        r'set PORT "clk\x5fi"' + "\n" + r"set DOLLAR $" + "\nget_ports ${PORT}\n",
        encoding="utf-8",
    )

    view = parse_sdc(source)

    assert view.complete
    assert not view.diagnostics
    assert view.objects[0].native_name == "clk_i"
    assert view.attributes["static_variables"] == ("DOLLAR", "PORT")


def test_limits_missing_files_adapter_and_recursion_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = SdcParser().parse([tmp_path / "missing.sdc"])
    assert not missing.complete
    assert missing.diagnostics[0].code == "OC1002"

    oversized = tmp_path / "oversized.sdc"
    oversized.write_text("get_ports abc", encoding="utf-8")
    monkeypatch.setattr(sdc_module, "_MAX_SCRIPT_CHARACTERS", 4)
    too_large = parse_sdc(oversized)
    assert any("characters" in item.message for item in too_large.diagnostics)
    monkeypatch.setattr(sdc_module, "_MAX_SCRIPT_CHARACTERS", 4 * 1024 * 1024)

    commands = tmp_path / "commands.sdc"
    commands.write_text("get_ports a\nget_ports b\n", encoding="utf-8")
    monkeypatch.setattr(sdc_module, "_MAX_COMMANDS", 1)
    command_limited = parse_sdc(commands)
    assert any("commands" in item.message for item in command_limited.diagnostics)
    monkeypatch.setattr(sdc_module, "_MAX_COMMANDS", 100_000)

    words = tmp_path / "words.sdc"
    words.write_text("get_ports a b\n", encoding="utf-8")
    monkeypatch.setattr(sdc_module, "_MAX_WORDS", 2)
    word_limited = parse_sdc(words)
    assert any("words" in item.message for item in word_limited.diagnostics)
    monkeypatch.setattr(sdc_module, "_MAX_WORDS", 250_000)

    def recurse(_parser: object) -> tuple[object, ...]:
        raise RecursionError

    monkeypatch.setattr(sdc_module._TclParser, "parse", recurse)
    recursive = parse_sdc(words)
    assert any("parser nesting limit" in item.message for item in recursive.diagnostics)

from __future__ import annotations

from pathlib import Path

import pytest

from opencollate.model import FactState, ViewObservation
from opencollate.parsers import cheader
from opencollate.parsers.cheader import CHeaderParser, parse_c_header


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "registers.h"
    path.write_text(text, encoding="utf-8")
    return path


def test_parses_address_offset_and_fields(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
#define UART0_BASE 0x40001000UL
#define UART0_CTRL_OFFSET 0x00U
#define UART0_CTRL_ADDR (UART0_BASE + UART0_CTRL_OFFSET)
#define UART0_CTRL_ENABLE_Pos 0U
#define UART0_CTRL_ENABLE_Msk (0x1UL << UART0_CTRL_ENABLE_Pos)
#define UART0_CTRL_MODE_Pos 4U
#define UART0_CTRL_MODE_Msk (0x3UL << UART0_CTRL_MODE_Pos)
#define UART0_STATUS_OFFSET (UART0_CTRL_OFFSET + 4U)
""",
    )

    observation = parse_c_header(path, default_register_width=32)

    assert not [item for item in observation.diagnostics if item.severity.value == "fatal"]
    assert [item.native_name for item in observation.registers] == ["CTRL", "STATUS"]
    control = observation.registers[0]
    assert control.component == "UART0"
    assert control.address_offset == 0
    assert control.absolute_address == 0x40001000
    assert control.size_bits == 32
    assert [(item.native_name, item.bit_offset, item.bit_width) for item in control.fields] == [
        ("ENABLE", 0, 1),
        ("MODE", 4, 2),
    ]
    assert observation.registers[1].address_offset == 4


def test_component_and_macro_prefix_are_independent(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
#define SOC_UART_BASE 0x50000000
#define SOC_UART_DATA_OFFSET 0x10
#define OTHER_DATA_OFFSET 0x20
""",
    )

    observation = parse_c_header(
        path,
        component_name="uart0",
        macro_prefix="SOC_UART",
    )

    assert len(observation.registers) == 1
    assert observation.registers[0].component == "uart0"
    assert observation.registers[0].native_name == "DATA"
    assert observation.registers[0].absolute_address == 0x50000010


def test_expression_evaluation_is_non_executing_and_tainted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
#define UART_BASE 0x40000000
#define UART_CTRL_OFFSET system("not executed")
""",
    )

    observation = parse_c_header(path)

    assert not observation.registers
    assert any(item.code == "OC1103" for item in observation.diagnostics)
    assert "UART/CTRL" in observation.tainted_scopes


def test_cyclic_macros_do_not_crash(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
#define UART_A_OFFSET UART_B_OFFSET
#define UART_B_OFFSET UART_A_OFFSET
""",
    )

    observation = parse_c_header(path)

    assert not observation.registers
    assert len([item for item in observation.diagnostics if item.code == "OC1103"]) == 2


def test_conflicting_definitions_are_reported(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
#define UART_CTRL_OFFSET 0
#define UART_CTRL_OFFSET 4
""",
    )

    observation = parse_c_header(path)

    assert any(item.code == "OC1102" for item in observation.diagnostics)
    assert observation.registers[0].status == FactState.TAINTED


@pytest.mark.parametrize("width", [0, -1, "32"])
def test_default_width_is_validated(tmp_path: Path, width: object) -> None:
    path = _write(tmp_path, "#define UART_CTRL_OFFSET 0\n")
    with pytest.raises(ValueError, match="positive integer"):
        parse_c_header(path, default_register_width=width)  # type: ignore[arg-type]


def test_integer_expression_subset_covers_c_operators(tmp_path: Path) -> None:
    macro = cheader._Macro("BASE", "0x40U", tmp_path / "x.h", 1)
    evaluator = cheader._IntegerEvaluator({"BASE": macro})

    assert evaluator.expression("(+BASE * 3 / 2) + (9 % 4)") == 97
    assert evaluator.expression("((1 << 8) >> 4) | 3") == 19
    assert evaluator.expression("(0xff & 0x0f) ^ 3") == 12
    assert evaluator.expression("~0 & 07") == 7
    assert evaluator.expression("(uint32_t)0x20UL") == 32
    assert evaluator.expression("-(-5)") == 5
    assert evaluator.macro("BASE") == evaluator.macro("BASE") == 64


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("1 / 0", "division by zero"),
        ("1 % 0", "division by zero"),
        ("1 << -1", "shift count"),
        ("1 << 5000", "shift count"),
        ("1 and 2", "unsupported integer operator"),
        ("MISSING + 1", "unknown macro"),
    ],
)
def test_integer_expression_failures_are_controlled(expression: str, message: str) -> None:
    evaluator = cheader._IntegerEvaluator({})
    with pytest.raises(cheader._IntegerExpressionError, match=message):
        evaluator.expression(expression)


def test_comments_continuations_and_field_taint_are_preserved(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
/* generated
   header */
#define UART_BASE 0x1000 // base
#define UART_CTRL_OFFSET (0x4 + \\
                          0x4)
#define UART_CTRL_BAD_Pos -1
#define UART_CTRL_BAD_Msk 0x5
#define UART_CTRL_ZERO_WIDTH 0
""",
    )

    observation = parse_c_header(path)
    control = observation.registers[0]

    assert control.address_offset == 8
    assert control.absolute_address == 0x1008
    assert {field.native_name for field in control.fields} == {"BAD", "ZERO"}
    assert all(field.status == FactState.TAINTED for field in control.fields)


def test_address_and_offset_disagreement_is_fatal(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
#define UART_BASE 0x1000
#define UART_CTRL_OFFSET 4
#define UART_CTRL_ADDR 0x1010
""",
    )

    observation = parse_c_header(path)

    assert any(item.code == "OC1101" for item in observation.diagnostics)
    assert observation.registers[0].status == FactState.TAINTED


def test_empty_missing_and_non_register_headers_are_explicit(tmp_path: Path) -> None:
    empty = _write(tmp_path, "")
    assert not parse_c_header(empty).registers

    non_register = _write(tmp_path, "#define UART_VERSION 3\n")
    observation = parse_c_header(non_register)
    assert any(item.code == "OC1105" for item in observation.diagnostics)

    missing = parse_c_header(tmp_path / "missing.h")
    assert not missing.complete
    assert missing.diagnostics[0].code == "OC1002"


def test_resource_limits_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path, "#define A_OFFSET 0\n#define B_OFFSET 4\n")
    monkeypatch.setattr(cheader, "_MAX_SOURCE_CHARACTERS", 10)
    oversized = parse_c_header(path)
    assert not oversized.complete
    assert any("16 MiB" in item.message for item in oversized.diagnostics)

    monkeypatch.setattr(cheader, "_MAX_SOURCE_CHARACTERS", 1024)
    monkeypatch.setattr(cheader, "_MAX_MACROS", 1)
    too_many = parse_c_header(path)
    assert not too_many.complete
    assert any("macro limit" in item.message for item in too_many.diagnostics)


def test_parser_adapter_forwards_options(tmp_path: Path) -> None:
    path = _write(tmp_path, "#define UART_CTRL_OFFSET 0\n")
    view = CHeaderParser().parse(
        (path,),
        view_id="header.firmware",
        component_name="uart0",
    )
    assert str(view.view) == "header.firmware"
    assert view.registers[0].component == "uart0"


def test_semantic_macro_alias_conflicts_are_deterministic_and_tainted(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a.h"
    second = tmp_path / "b.h"
    first.write_text(
        "#define UART_BASE 0x1000\n#define UART_CTRL_OFFSET 0\n#define UART_CTRL_ENABLE_POS 0\n",
        encoding="utf-8",
    )
    second.write_text(
        "#define UART_BASE_ADDR 0x2000\n"
        "#define UART_CTRL_REG_OFFSET 4\n"
        "#define UART_CTRL_ENABLE_LSB 2\n",
        encoding="utf-8",
    )

    forward = parse_c_header((first, second))
    reverse = parse_c_header((second, first))

    def facts(view: ViewObservation) -> tuple[object, ...]:
        register = view.registers[0]
        return (
            register.address_offset,
            register.absolute_address,
            register.status,
            register.fields[0].bit_offset,
            register.fields[0].status,
            tuple(item.message for item in view.diagnostics),
        )

    assert facts(forward) == facts(reverse)
    assert forward.registers[0].status == FactState.TAINTED
    assert forward.registers[0].fields[0].status == FactState.TAINTED
    assert sum(item.code == "OC1102" for item in forward.diagnostics) == 3


def test_conditional_macros_and_their_dependents_are_tainted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "#define UART_BASE 0x1000\n"
        "#if FEATURE\n"
        "#define UART_CTRL_RAW 0\n"
        "#define UART_CTRL_OFFSET UART_CTRL_RAW\n"
        "#endif\n"
        "#define UART_STATUS_OFFSET 4\n",
    )

    observation = parse_c_header(path)
    registers = {item.native_name: item for item in observation.registers}

    assert registers["CTRL"].status == FactState.TAINTED
    assert registers["STATUS"].status == FactState.KNOWN
    assert any(item.code == "OC1104" for item in observation.diagnostics)


def test_conventional_outer_include_guard_does_not_taint_facts(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "#pragma once\n"
        "#ifndef UART_REGISTERS_H\n"
        "#define UART_REGISTERS_H\n"
        "#define UART_BASE 0x1000\n"
        "#define UART_CTRL_OFFSET 0\n"
        "#endif\n",
    )

    observation = parse_c_header(path)

    assert observation.complete
    assert observation.registers[0].status == FactState.KNOWN
    assert not any(item.code == "OC1104" for item in observation.diagnostics)

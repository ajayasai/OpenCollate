from __future__ import annotations

import pytest

from opencollate.boolean import (
    BoolConst,
    BooleanSyntaxError,
    BoolVar,
    check_equivalence,
    parse_boolean,
)


@pytest.mark.parametrize(
    ("expression", "values", "expected"),
    [
        ("A & B", {"A": True, "B": False}, False),
        ("A + B", {"A": False, "B": True}, True),
        ("A B'", {"A": True, "B": False}, True),
        ("!(A ^ B)", {"A": True, "B": True}, True),
        ('"A * ~B + C"', {"A": True, "B": False, "C": False}, True),
        ("~~A", {"A": True}, True),
        ("1'b0 | 1'h1", {}, True),
    ],
)
def test_boolean_evaluation(expression: str, values: dict[str, bool], expected: bool) -> None:
    assert parse_boolean(expression).evaluate(values) is expected


def test_boolean_variables_are_stable() -> None:
    expression = parse_boolean("Z + A * Z")
    assert expression.variables() == frozenset({"A", "Z"})


def test_equivalence_returns_exact_result() -> None:
    result = check_equivalence("A & B", "B * A")
    assert result.equivalent is True
    assert result.variables == ("A", "B")
    assert result.checked_assignments == 4


def test_equivalence_returns_counterexample() -> None:
    result = check_equivalence("A & B", "A | B")
    assert result.equivalent is False
    assert result.counterexample is not None
    assert result.checked_assignments > 0


def test_equivalence_applies_aliases() -> None:
    result = check_equivalence("A & B", "AIN * BIN", aliases={"AIN": "A", "BIN": "B"})
    assert result.equivalent is True


def test_equivalence_limit_is_explicit() -> None:
    result = check_equivalence("A | B | C", "A + B + C", max_variables=2)
    assert result.equivalent is None
    assert result.checked_assignments == 0
    assert "limit" in (result.reason or "")


def test_negative_equivalence_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        check_equivalence(BoolConst(True), BoolConst(True), max_variables=-1)


@pytest.mark.parametrize("expression", ["", "A ? B : C", "(A + B", "A @ B", "\\ "])
def test_boolean_syntax_errors(expression: str) -> None:
    with pytest.raises(BooleanSyntaxError):
        parse_boolean(expression)


def test_missing_variable_has_helpful_error() -> None:
    with pytest.raises(KeyError, match="missing Boolean value"):
        BoolVar("A").evaluate({})


def test_deep_grouping_is_a_controlled_syntax_error() -> None:
    expression = "(" * 2_000 + "A" + ")" * 2_000

    with pytest.raises(BooleanSyntaxError, match="group nesting limit"):
        parse_boolean(expression)

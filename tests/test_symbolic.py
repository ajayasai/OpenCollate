from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from itertools import product

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from opencollate import symbolic
from opencollate.boolean import (
    BoolAnd,
    BoolConst,
    BoolNot,
    BoolOr,
    BoolVar,
    BoolXor,
    check_equivalence,
    parse_boolean,
)
from opencollate.config import ConfigError, PolicySettings
from opencollate.symbolic import SymbolicLimits, check_symbolic_equivalence


@pytest.mark.parametrize(
    ("left", "right", "guard", "status"),
    [
        ("A&B", "B A", "1", "equivalent"),
        ("A^B^C", "C^A^B", "1", "equivalent"),
        ("A|B", "A&B", "1", "different"),
        ("(S&A)|(!S&B)", "A", "S", "equivalent"),
        ("(S&A)|(!S&B)", "A", "!S", "different"),
        ("A", "A", "S&!S", "vacuous"),
        ("0", "1", "1", "different"),
        ("1", "1", "1", "equivalent"),
        ("A?B:C", "A", "1", "inconclusive"),
    ],
)
def test_symbolic_statuses(left: str, right: str, guard: str, status: str) -> None:
    result = check_symbolic_equivalence(left, right, assume=guard)
    assert result.status == status
    if status == "different":
        assert result.counterexample is not None
        assert parse_boolean(guard).evaluate(result.counterexample)
        assert parse_boolean(left).evaluate(result.counterexample) != parse_boolean(right).evaluate(
            result.counterexample
        )


def test_larger_than_legacy_limit() -> None:
    names = [f"A{i:03}" for i in range(128)]
    left = " & ".join(names)
    right = "!(" + " | ".join("!" + name for name in names) + ")"
    assert check_equivalence(left, right).equivalent is None
    result = check_symbolic_equivalence(left, right)
    assert result.status == "equivalent"
    assert len(result.variables) == 128
    assert result.query_count == 2


def test_aliases_and_deterministic_witness() -> None:
    result = check_symbolic_equivalence("X&B", "A|B", aliases={"X": "A"})
    assert result.counterexample == {"A": False, "B": True}
    assert (
        result.to_dict() == check_symbolic_equivalence("X&B", "A|B", aliases={"X": "A"}).to_dict()
    )
    assert check_symbolic_equivalence("A^B", "0", aliases={"B": "A"}).equivalent


atom = st.sampled_from(["A", "B", "C", "D", "0", "1"])
expr = st.recursive(
    atom,
    lambda child: st.one_of(
        child.map(lambda a: f"!({a})"),
        st.tuples(child, st.sampled_from(["&", "|", "^"]), child).map(
            lambda p: f"({p[0]}{p[1]}{p[2]})"
        ),
    ),
    max_leaves=12,
)


@given(expr, expr, expr)
@settings(max_examples=120, deadline=None, derandomize=True)
def test_against_independent_exhaustive_oracle(left: str, right: str, guard: str) -> None:
    a, b, g = map(parse_boolean, (left, right, guard))
    names = sorted(a.variables() | b.variables() | g.variables())
    feasible, witness = False, None
    for bits in product((False, True), repeat=len(names)):
        values = dict(zip(names, bits, strict=True))
        if g.evaluate(values):
            feasible = True
            if a.evaluate(values) != b.evaluate(values):
                witness = values
                break
    result = check_symbolic_equivalence(left, right, assume=guard)
    assert result.status == (
        "vacuous" if not feasible else "different" if witness is not None else "equivalent"
    )
    assert result.counterexample == witness


@pytest.mark.parametrize(
    "limits",
    [
        SymbolicLimits(max_variables=1),
        SymbolicLimits(max_nodes=1),
        SymbolicLimits(max_queries=2),
        SymbolicLimits(resource_limit=1),
    ],
)
def test_resource_exhaustion_never_proves(limits: SymbolicLimits) -> None:
    result = check_symbolic_equivalence("A&B", "A|B", limits=limits)
    assert result.status == "inconclusive"
    assert result.counterexample is None


@pytest.mark.parametrize(
    "field", ["max_variables", "max_nodes", "timeout_ms", "resource_limit", "max_queries"]
)
@pytest.mark.parametrize("value", [0, -1, True, "5", 1.5, 1_000_000_001])
def test_limits_are_strict(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        SymbolicLimits(**{field: value})


def test_missing_backend_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_: str) -> object:
        raise ModuleNotFoundError("no z3")

    monkeypatch.setattr(symbolic, "import_module", unavailable)
    result = check_symbolic_equivalence("A", "A")
    assert result.status == "inconclusive"
    assert "install" in str(result.reason)


def test_total_deadline_is_not_reset_per_query(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter((0.0, 100.0))
    monkeypatch.setattr(symbolic.time, "monotonic", lambda: next(ticks))
    assert check_symbolic_equivalence("A", "A").status == "inconclusive"


def test_backend_exception_cannot_prove(monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(*_args: object) -> object:
        raise RuntimeError("simulated native failure")

    monkeypatch.setattr(symbolic, "_compile", crash)
    assert check_symbolic_equivalence("A", "A").status == "inconclusive"


def test_witness_replay_failure_cannot_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        symbolic, "_evaluate", lambda ordered, values: {id(n): False for n in ordered}
    )
    result = check_symbolic_equivalence("A", "!A")
    assert result.status == "inconclusive"
    assert result.counterexample is None
    assert "replay" in str(result.reason)


@pytest.mark.parametrize("node", [BoolAnd(()), BoolOr(()), BoolXor(())])
def test_empty_ir_reductions(node: object) -> None:
    expected = isinstance(node, BoolAnd)
    assert check_symbolic_equivalence(node, BoolConst(expected)).equivalent


def test_deep_programmatic_ir_and_cycle() -> None:
    node = BoolVar("A")
    for _ in range(1200):
        node = BoolNot(node)
    assert check_symbolic_equivalence(node, "A").equivalent
    cyclic = BoolNot(BoolConst(True))
    object.__setattr__(cyclic, "operand", cyclic)
    assert check_symbolic_equivalence(cyclic, "1").status == "inconclusive"


def test_contexts_do_not_share_mutable_state() -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(
            pool.map(lambda _: check_symbolic_equivalence("A&B", "A|B").to_dict(), range(12))
        )
    assert all(item == outputs[0] for item in outputs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"boolean_backend": "automatic"},
        {"boolean_backend": []},
        {"max_symbolic_inputs": True},
        {"symbolic_timeout_ms": 0},
        {"symbolic_resource_limit": -1},
    ],
)
def test_policy_rejects_invalid_symbolic_options(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        PolicySettings(**kwargs)

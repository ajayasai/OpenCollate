from __future__ import annotations

from itertools import product

import pytest
from hypothesis import given, settings
from pysat.solvers import Glucose3

from opencollate.boolean import BoolAnd, BoolConst, BoolNot, BoolOr, BoolVar, BoolXor, parse_boolean
from opencollate.boolean_proof_kernel import ProofError, ProofLimits
from opencollate.proof_cnf import Encoder, compile_boolean
from tests.test_symbolic import expr


@given(expr, expr, expr)
@settings(max_examples=120, deadline=None, derandomize=True)
def test_tseitin_encoding_exactly_matches_truth_tables(left: str, right: str, guard: str) -> None:
    compiled = compile_boolean(left, right, guard)
    original = tuple(map(parse_boolean, (left, right, guard)))
    with (
        Glucose3(bootstrap_with=compiled.cnf("guard")) as feasible,
        Glucose3(bootstrap_with=compiled.cnf("difference")) as different,
    ):
        for bits in product((False, True), repeat=len(compiled.names)):
            witness = dict(zip(compiled.names, bits, strict=True))
            a, b, g = (node.evaluate(witness) for node in original)
            assumptions = [
                compiled.encoder.inputs[name] * (1 if value else -1)
                for name, value in witness.items()
            ]
            assert feasible.solve(assumptions=assumptions) == g
            assert different.solve(assumptions=assumptions) == (g and a != b)
            assert compiled.evaluate(witness) == (a, b, g)


@pytest.mark.parametrize("node", [BoolAnd(()), BoolOr(()), BoolXor(())])
def test_empty_reductions(node: object) -> None:
    compiled = compile_boolean(node, BoolConst(isinstance(node, BoolAnd)))
    with Glucose3(bootstrap_with=compiled.cnf("difference")) as solver:
        assert solver.solve() is False


@pytest.mark.parametrize("left", ["A&1", "1&A", "A|0", "0|A", "A^0", "0^A", "A&A", "A|A", "!!A"])
def test_identity_reductions(left: str) -> None:
    compiled = compile_boolean(left, "A")
    with Glucose3(bootstrap_with=compiled.cnf("difference")) as solver:
        assert not solver.solve()


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("A&!A", "0"),
        ("A|!A", "1"),
        ("A^!A", "1"),
        ("A^A", "0"),
        ("A^1", "!A"),
        ("1^A", "!A"),
        ("A&0", "0"),
        ("A|1", "1"),
        ("A&B", "B&A"),
    ],
)
def test_complement_and_interned_reductions(left: str, right: str) -> None:
    compiled = compile_boolean(left, right)
    with Glucose3(bootstrap_with=compiled.cnf("difference")) as solver:
        assert not solver.solve()


def test_aliases_are_bound_and_evaluated() -> None:
    compiled = compile_boolean("X&B", "A|B", aliases={"X": "A"})
    assert compiled.names == ("A", "B")
    assert compiled.evaluate({"A": False, "B": True}) == (False, True, True)
    assert compiled.obligation_sha256 != compile_boolean("X&B", "A|B").obligation_sha256


@pytest.mark.parametrize("witness", [None, {}, {"A": 0}, {"A": True, "B": False}, {"A": "true"}])
def test_witness_is_complete_and_strict(witness: object) -> None:
    with pytest.raises(ProofError):
        compile_boolean("A", "A").evaluate(witness)


@pytest.mark.parametrize(
    "limits",
    [
        ProofLimits(max_variables=1),
        ProofLimits(max_nodes=1),
        ProofLimits(max_clauses=1),
        ProofLimits(max_literals=1),
    ],
)
def test_compilation_limits_have_value_error_semantics(limits: ProofLimits) -> None:
    with pytest.raises(ProofError):
        compile_boolean("A&B", "A|B", limits=limits)


def test_encoder_rejects_unknown_gates_and_queries() -> None:
    compiled = compile_boolean("A", "A")
    with pytest.raises(ProofError):
        compiled.cnf("unbound")
    with pytest.raises(ProofError):
        compiled.encoder.gate("nand", (1, 2))
    encoder = Encoder(("A", "B", "C"), ProofLimits(max_nodes=1))
    with pytest.raises(ProofError, match="auxiliary"):
        encoder.gate("and", (2, 3))


def test_deep_and_cyclic_ir() -> None:
    node = BoolVar("A")
    for _ in range(1400):
        node = BoolNot(node)
    assert compile_boolean(node, "A").evaluate({"A": True}) == (True, True, True)
    object.__setattr__(node, "operand", node)
    with pytest.raises(ValueError, match="cyclic"):
        compile_boolean(node, "A")

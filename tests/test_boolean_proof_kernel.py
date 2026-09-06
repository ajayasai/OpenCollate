"""Adversarial tests of the solver-free logical kernel, with exhaustive oracles."""

from __future__ import annotations

from itertools import product

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from opencollate import proof_kernel
from opencollate.boolean_proof_kernel import ProofError, ProofLimits, clause, verify_rup


@pytest.mark.parametrize(
    ("cnf", "proof", "variables"),
    [
        ([[1], [-1]], [[]], 1),
        ([[]], [[]], 1),
        ([[1, 2], [1, -2], [-1, 2], [-1, -2]], [[1], []], 2),
        ([[1, 2], [-1], [-2]], [[2], []], 2),
        ([[1], [-1, 2], [-2, 3], [-3]], [[2], [3], []], 3),
    ],
)
def test_checked_refutations(cnf: list, proof: list, variables: int) -> None:
    result = verify_rup(cnf, proof, variables)
    assert result["steps"] == len(proof)
    assert result["work"] > 0


@pytest.mark.parametrize(
    ("cnf", "proof", "variables"),
    [
        ([[1]], [[]], 1),  # A solver's unsupported UNSAT claim.
        ([], [[]], 1),
        ([[1, 2], [-1, -2]], [[1], []], 2),
        ([[1], [-1]], [], 1),
        ([[1], [-1]], [[1]], 1),
        ([[1], [-1]], [[], [1], []], 1),
        ([[True]], [[]], 1),
        ([[0]], [[]], 1),
        ([[2]], [[]], 1),
        ([[1, 1]], [[]], 2),
        ([[1, -1]], [[]], 2),
        (["1"], [[]], 1),
        (None, [[]], 1),
        ([[1]], None, 1),
        ([[1]], [[]], True),
        ([[1]], [[]], 0),
        ([[1]], [[]], 131073),
        ([[1], [-1]], [[True], []], 1),
    ],
)
def test_invalid_evidence_never_passes(cnf: object, proof: object, variables: int) -> None:
    with pytest.raises(ProofError):
        verify_rup(cnf, proof, variables)


@pytest.mark.parametrize("raw", [False, {}, "1", [1.0], [True], [0], [-3], [1, -1], [1, 1]])
def test_literal_arrays_are_strict(raw: object) -> None:
    with pytest.raises(ProofError):
        clause(raw, 2)


@pytest.mark.parametrize(
    "field",
    [
        "max_variables",
        "max_nodes",
        "max_clauses",
        "max_literals",
        "max_steps",
        "max_work",
        "timeout_ms",
        "conflict_limit",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True, "5", 1.5, 1_000_000_001])
def test_limits_reject_ambiguous_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        ProofLimits(**{field: value})


@pytest.mark.parametrize(
    "limits",
    [
        ProofLimits(max_clauses=1),
        ProofLimits(max_literals=1),
        ProofLimits(max_work=1),
        ProofLimits(max_steps=1),
    ],
)
def test_storage_steps_and_work_exhaustion(limits: ProofLimits) -> None:
    with pytest.raises(ProofError):
        verify_rup([[1], [-1]], [[1], []], 1, limits=limits)


def test_deadline_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 100.0])
    monkeypatch.setattr(proof_kernel.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ProofError, match="time"):
        verify_rup([[1], [-1]], [[]], 1)


# All canonical, non-tautological clauses on three variables, including empty.
CLAUSES = [
    tuple(sign * (i + 1) for i, sign in enumerate(signs) if sign)
    for signs in product((-1, 0, 1), repeat=3)
]


@given(
    st.lists(st.sampled_from(CLAUSES), max_size=14),
    st.lists(st.sampled_from(CLAUSES[1:]), max_size=8),
)
@settings(max_examples=300, derandomize=True, deadline=None)
def test_no_accepted_proof_for_any_satisfiable_sample(cnf: list, additions: list) -> None:
    """Independent exhaustive semantics, not a second unit-propagation checker."""
    try:
        verify_rup(cnf, [*additions, ()], 3)
    except ProofError:
        return
    for bits in product((False, True), repeat=3):
        assert not all(
            any(bits[abs(literal) - 1] == (literal > 0) for literal in row) for row in cnf
        )


def test_every_two_variable_formula_against_exhaustive_truth_table() -> None:
    rows = [
        tuple(sign * (i + 1) for i, sign in enumerate(signs) if sign)
        for signs in product((-1, 0, 1), repeat=2)
    ]
    for selected in product((False, True), repeat=len(rows)):
        cnf = [row for row, include in zip(rows, selected, strict=True) if include]
        satisfiable = any(
            all(any(bits[abs(x) - 1] == (x > 0) for x in row) for row in cnf)
            for bits in product((False, True), repeat=2)
        )
        accepted = False
        # An inconsistent two-variable formula has a refutation with at most
        # one RUP-derived unit followed by the empty clause.
        for proof in ([[]], [[1], []], [[-1], []], [[2], []], [[-2], []]):
            try:
                verify_rup(cnf, proof, 2)
                accepted = True
                break
            except ProofError:
                pass
        assert accepted is not satisfiable

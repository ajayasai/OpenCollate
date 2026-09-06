from __future__ import annotations

import itertools
import random

import pytest

from opencollate.proof_kernel import Limits, Meter, ProofError, _Propagation, verify_rup


def test_nontrivial_refutation_and_invalid_satisfiable_claim() -> None:
    problem = [[1, 2], [-1, 2], [1, -2], [-1, -2]]
    assert verify_rup(problem, 2, [[2], []])["steps"] == 2
    with pytest.raises(ProofError, match="invalid RUP"):
        verify_rup([[1, 2]], 2, [[]])


@pytest.mark.parametrize(
    "problem,proof",
    [
        ([[1], [-1]], [[]]),
        ([[]], [[]]),
        ([[1, 1], [-1, -1]], [[]]),
        ([[1, -1], [2], [-2]], [[1, -1], []]),
        ([[1, 2], [-1, 2], [-2, 3], [-3]], [[2], []]),
    ],
)
def test_units_empty_duplicates_tautologies_and_chains(problem: list, proof: list) -> None:
    assert verify_rup(problem, 3, proof)["steps"] == len(proof)


@pytest.mark.parametrize(
    "variables,clauses,proof",
    [
        (True, [[1], [-1]], [[]]),
        (0, [], [[]]),
        (100001, [], [[]]),
        (2, (), [[]]),
        (2, [[True]], [[]]),
        (2, [[0]], [[]]),
        (2, [[3]], [[]]),
        (2, [[-3]], [[]]),
        (2, [[1.0]], [[]]),
        (2, [["1"]], [[]]),
        (2, [[1]], []),
        (2, [[1]], None),
        (2, [[1]], [[False]]),
        (2, [[1]], [[0]]),
        (2, [[1]], [[3]]),
        (2, [[1]], [[1]]),
        (2, [[1], [-1]], [[], [1]]),
        (2, [[1], [-1]], [[], []]),
        (2, [[1], [-1]], [()]),
        (2, [[1]], [[-1], []]),
        (2, [[1]], [{"delete": [1]}, []]),
    ],
)
def test_reject_malformed_or_unproved_evidence(
    variables: object, clauses: object, proof: object
) -> None:
    with pytest.raises((ProofError, TypeError)):
        verify_rup(clauses, variables, proof)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "limits",
    [
        Limits(variables=1),
        Limits(clauses=1),
        Limits(literals=1),
        Limits(steps=1),
        Limits(work=1),
    ],
)
def test_resource_exhaustion_never_accepts(limits: Limits) -> None:
    with pytest.raises(ProofError):
        verify_rup([[1, 2], [-1, 2], [-2]], 2, [[2], []], meter=Meter(limits))


def test_deadline_and_invalid_limit() -> None:
    with pytest.raises(ProofError):
        Meter(Limits(work=True))
    meter = Meter()
    meter.deadline = 0
    with pytest.raises(ProofError, match="deadline"):
        verify_rup([[1], [-1]], 1, [[]], meter=meter)


def naive_conflict(clauses: list[tuple[int, ...]], assumptions: tuple[int, ...]) -> bool:
    true = {-v for v in assumptions}
    if any(-v in true for v in true):
        return True
    while True:
        previous = len(true)
        for clause in clauses:
            if any(v in true for v in clause):
                continue
            free = {v for v in clause if -v not in true}
            if not free:
                return True
            if len(free) == 1:
                true.update(free)
        if previous == len(true):
            return False


def test_watched_propagation_matches_naive_after_repeated_assignment_resets() -> None:
    rng = random.Random(840173)
    for _ in range(100):
        database = _Propagation(Meter())
        clauses = []
        for _ in range(25):
            clause = tuple(
                (1 if rng.randrange(2) else -1) * v
                for v in rng.sample(range(1, 9), rng.randrange(1, 6))
            )
            clauses.append(clause)
            database.add(clause)
        for _ in range(25):
            candidate = tuple(
                (1 if rng.randrange(2) else -1) * v
                for v in rng.sample(range(1, 9), rng.randrange(5))
            )
            assert database.conflicts(candidate) == naive_conflict(clauses, candidate)


def test_random_solver_proofs_against_exhaustive_truth_tables() -> None:
    from pysat.solvers import Solver

    from opencollate.proof_producer_io import read_producer_proof
    from opencollate.sequential_certificate import _proof_rows

    rng = random.Random(31127)
    unsat = sat = 0
    for _ in range(200):
        n = rng.randrange(1, 7)
        clauses = [
            [
                (1 if rng.randrange(2) else -1) * v
                for v in rng.sample(range(1, n + 1), rng.randrange(1, min(n, 3) + 1))
            ]
            for _ in range(rng.randrange(1, 30))
        ]
        truth = any(
            all(any(values[abs(v) - 1] == (v > 0) for v in c) for c in clauses)
            for values in itertools.product((False, True), repeat=n)
        )
        with Solver(name="g3", bootstrap_with=clauses, with_proof=True) as solver:
            assert solver.solve() == truth
            # Flush even SAT traces before closing/reusing their descriptor.
            raw_proof = read_producer_proof(solver)
            if truth:
                sat += 1
                with pytest.raises(ProofError):
                    verify_rup(clauses, n, [[]])
            else:
                unsat += 1
                proof = _proof_rows(raw_proof, n, Meter())
                assert verify_rup(clauses, n, proof)["steps"] >= 1
    assert sat > 20 and unsat > 20

"""Small, solver-free checker for addition-only reverse-unit-propagation proofs.

Every added clause must follow by unit propagation after negating that clause.
The final clause must be empty. No solver statuses, hashes, deletion commands,
RAT extensions, or imported executable code are accepted as logical evidence.
This implementation is independently tested, not mechanically verified.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any


class ProofError(ValueError):
    """Invalid, incomplete, unsupported, or over-budget proof evidence."""


@dataclass(frozen=True, slots=True)
class ProofLimits:
    max_variables: int = 512
    max_nodes: int = 32768
    max_clauses: int = 131072
    max_literals: int = 1048576
    max_steps: int = 32768
    max_work: int = 20000000
    timeout_ms: int = 5000
    conflict_limit: int = 100000

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_variables", 4096),
            ("max_nodes", 262144),
            ("max_clauses", 1048576),
            ("max_literals", 8388608),
            ("max_steps", 262144),
            ("max_work", 1000000000),
            ("timeout_ms", 300000),
            ("conflict_limit", 100000000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")


def clause(value: Any, variables: int) -> tuple[int, ...]:
    """Validate a bounded literal vector; bool is not an integer literal."""
    if not isinstance(value, (list, tuple)) or len(value) > variables:
        raise ProofError("invalid or oversized proof clause")
    if any(type(x) is not int or not 1 <= abs(x) <= variables for x in value):
        raise ProofError("proof literals must be nonzero, in-range integers")
    literals = set(value)
    if len(literals) != len(value) or any(-x in literals for x in literals):
        raise ProofError("duplicate or tautological proof clause")
    return tuple(value)


def verify_rup(
    cnf: Any, proof: Any, variables: int, *, limits: ProofLimits | None = None
) -> dict[str, int]:
    """Check all RUP additions against the actual CNF, under finite budgets.

    ``variables`` counts input AND auxiliary Tseitin variables. The separate
    ``max_variables`` policy bounds source inputs during compilation. Retained
    base and learned clauses share the clause/literal storage budgets.
    """
    budget = limits or ProofLimits()
    if type(variables) is not int or not 1 <= variables <= budget.max_nodes * 4:
        raise ProofError("invalid CNF variable count")
    if not isinstance(cnf, (list, tuple)) or not isinstance(proof, (list, tuple)):
        raise ProofError("CNF and proof must be finite clause arrays")
    if not 1 <= len(proof) <= budget.max_steps or proof[-1] not in ([], ()):
        raise ProofError("proof must end with an explicit empty clause within the step limit")
    if len(cnf) + len(proof) > budget.max_clauses:
        raise ProofError("proof clause storage limit exceeded")
    started = time.monotonic()
    database: list[tuple[int, ...]] = []
    occurs: dict[int, list[int]] = defaultdict(list)
    units: list[int] = []
    empty = False
    stored = 0
    work = 0

    def tick(amount: int = 1) -> None:
        nonlocal work
        work += amount
        if work > budget.max_work:
            raise ProofError("proof verification work limit exceeded")
        if time.monotonic() - started > budget.timeout_ms / 1000:
            raise ProofError("proof verification time limit exceeded")

    def add(item: tuple[int, ...]) -> None:
        nonlocal stored, empty
        stored += len(item)
        if stored > budget.max_literals:
            raise ProofError("proof literal storage limit exceeded")
        tick(len(item) + 1)
        index = len(database)
        database.append(item)
        if not item:
            empty = True
        elif len(item) == 1:
            units.append(item[0])
        for literal in item:
            occurs[literal].append(index)

    def conflict(assumptions: tuple[int, ...]) -> bool:
        if empty:
            return True
        # Each occurrence is visited once per assignment. A long clause is
        # scanned only when it becomes unit, avoiding quadratic rescanning of
        # wide gates while keeping the propagation algorithm independently small.
        tick(len(database))
        remaining = [len(item) for item in database]
        satisfied = [False] * len(database)
        assigned: set[int] = set()
        pending = [*units, *assumptions]
        while pending:
            tick()
            literal = pending.pop()
            if -literal in assigned:
                return True
            if literal in assigned:
                continue
            assigned.add(literal)
            for index in occurs.get(literal, ()):
                tick()
                satisfied[index] = True
            for index in occurs.get(-literal, ()):
                tick()
                if satisfied[index]:
                    continue
                remaining[index] -= 1
                if remaining[index] == 0:
                    return True
                if remaining[index] == 1:
                    for other in database[index]:
                        tick()
                        if -other not in assigned:
                            pending.append(other)
                            break
        return False

    for raw in cnf:
        add(clause(raw, variables))
    for index, raw in enumerate(proof):
        item = clause(raw, variables)
        if not item and index != len(proof) - 1:
            raise ProofError("proof contains data after its empty clause")
        if not conflict(tuple(-literal for literal in item)):
            raise ProofError(f"proof step {index + 1} is not justified by unit propagation")
        add(item)
    return {"steps": len(proof), "work": work, "literals": stored}

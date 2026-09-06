"""Small solver-free checker for reverse-unit-propagation (RUP) refutations.

Each added clause must follow by unit propagation under its negation. The
last addition must be the empty clause. No SAT solver, HDL frontend, deletion,
RAT extension, checksum, or claimed solver outcome is trusted by this kernel.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


class ProofError(ValueError):
    """Malformed, invalid, or resource-exhausted proof evidence."""


@dataclass(frozen=True)
class Limits:
    variables: int = 100_000
    clauses: int = 200_000
    literals: int = 2_000_000
    steps: int = 100_000
    work: int = 50_000_000
    timeout_ms: int = 30_000


class Meter:
    """Caller-owned limits: a certificate cannot increase its checking budget."""

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits()
        for name, value in vars(self.limits).items():
            if type(value) is not int or value < 1:
                raise ProofError(f"proof limit {name} must be a positive integer")
        self.deadline = time.monotonic() + self.limits.timeout_ms / 1000
        self.work = 0
        self.next_clock_check = 0

    def remaining(self) -> float:
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise ProofError("proof deadline exhausted")
        return seconds

    def spend(self, amount: int = 1) -> None:
        self.work += amount
        if self.work > self.limits.work:
            raise ProofError("proof work bound exhausted")
        if self.work >= self.next_clock_check:
            self.remaining()
            self.next_clock_check = self.work + 1024


def _clause(value: Any, variables: int, meter: Meter) -> tuple[int, ...] | None:
    if type(value) is not list:
        raise ProofError("a clause must be an array of integer literals")
    if len(value) > meter.limits.literals:
        raise ProofError("clause literal bound exhausted")
    result: list[int] = []
    seen: set[int] = set()
    tautology = False
    for lit in value:
        meter.spend()
        if type(lit) is not int or lit == 0 or abs(lit) > variables:
            raise ProofError("literal must be a nonzero integer within the variable bound")
        if -lit in seen:
            tautology = True
        if lit not in seen:
            result.append(lit)
            seen.add(lit)
    return None if tautology else tuple(result)


class _Propagation:
    """Watched-literal propagation; watch placement survives assignment resets."""

    def __init__(self, meter: Meter) -> None:
        self.meter = meter
        self.clauses: list[tuple[int, ...]] = []
        self.positions: list[list[int]] = []
        self.watches: dict[int, list[int]] = {}
        self.units: list[int] = []
        self.empty = False

    def add(self, clause: tuple[int, ...] | None) -> None:
        if clause is None:
            return
        if len(self.clauses) >= self.meter.limits.clauses:
            raise ProofError("proof clause bound exhausted")
        identity = len(self.clauses)
        self.clauses.append(clause)
        self.positions.append([0, 1])
        if not clause:
            self.empty = True
        elif len(clause) == 1:
            self.units.append(clause[0])
        else:
            for lit in clause[:2]:
                self.watches.setdefault(lit, []).append(identity)

    def conflicts(self, negated_clause: tuple[int, ...]) -> bool:
        if self.empty:
            return True
        assigned: dict[int, bool] = {}
        trail: list[int] = []

        def put(lit: int) -> bool:
            self.meter.spend()
            var, val = abs(lit), lit > 0
            if var in assigned:
                return assigned[var] == val
            assigned[var] = val
            trail.append(lit)
            return True

        def value(lit: int) -> bool | None:
            val = assigned.get(abs(lit))
            return None if val is None else val == (lit > 0)

        for lit in self.units:
            if not put(lit):
                return True
        for lit in negated_clause:
            if not put(-lit):
                return True
        cursor = 0
        while cursor < len(trail):
            false_lit = -trail[cursor]
            cursor += 1
            watched = self.watches.get(false_lit, [])
            index = 0
            while index < len(watched):
                self.meter.spend()
                identity = watched[index]
                clause, pos = self.clauses[identity], self.positions[identity]
                side = 0 if clause[pos[0]] == false_lit else 1
                if clause[pos[side]] != false_lit:
                    raise ProofError("inconsistent proof-checker watch state")
                other = clause[pos[1 - side]]
                if value(other) is True:
                    index += 1
                    continue
                replacement = None
                for k, lit in enumerate(clause):
                    self.meter.spend()
                    if k not in pos and value(lit) is not False:
                        replacement = k
                        break
                if replacement is not None:
                    pos[side] = replacement
                    self.watches.setdefault(clause[replacement], []).append(identity)
                    watched[index] = watched[-1]
                    watched.pop()
                    continue
                if not put(other):
                    return True
                index += 1
        return False


def verify_rup(
    clauses: list[list[int]],
    variables: int,
    proof: Any,
    *,
    meter: Meter | None = None,
) -> dict[str, int]:
    """Check every addition and require an explicit final empty clause.

    Input and learned clauses may contain duplicates or be tautologies. They are
    normalized without changing their meaning. Deletions are not part of this
    format. A producer may omit DRUP deletions: retaining already proved clauses
    can only strengthen subsequent unit propagation and preserves soundness.
    """
    m = meter or Meter()
    if type(variables) is not int or not 1 <= variables <= m.limits.variables:
        raise ProofError("invalid proof variable count")
    if type(clauses) is not list or len(clauses) > m.limits.clauses:
        raise ProofError("invalid input clause count")
    if type(proof) is not list or not 1 <= len(proof) <= m.limits.steps:
        raise ProofError("a nonempty, bounded RUP proof is required")
    database = _Propagation(m)
    literals = 0
    for raw in clauses:
        clause = _clause(raw, variables, m)
        literals += len(raw)
        if literals > m.limits.literals:
            raise ProofError("proof literal bound exhausted")
        database.add(clause)
    for index, raw in enumerate(proof):
        clause = _clause(raw, variables, m)
        literals += len(raw)
        if literals > m.limits.literals:
            raise ProofError("proof literal bound exhausted")
        if clause is not None and not database.conflicts(clause):
            raise ProofError(f"invalid RUP addition at step {index + 1}")
        if clause == () and index != len(proof) - 1:
            raise ProofError("trailing proof data after the empty clause")
        database.add(clause)
    if proof[-1] != []:
        raise ProofError("RUP proof does not end with an empty clause")
    m.remaining()
    return {"steps": len(proof), "clauses": len(database.clauses), "literals": literals}

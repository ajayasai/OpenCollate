"""Deterministic, solver-independent Tseitin encoding of the Boolean IR.

The encoder, parser, witness evaluator, and proof kernel remain trusted code.
Certificates never supply their own base formula to the verifier.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opencollate.boolean import BoolAnd, BoolConst, BoolExpr, BoolNot, BoolOr, BoolVar
from opencollate.boolean_proof_kernel import ProofError, ProofLimits
from opencollate.formal import _digest
from opencollate.symbolic import SymbolicLimits, _BudgetExceeded, _children, _evaluate, _prepare

ENCODING = "opencollate-tseitin-rup-v1"


class Encoder:
    def __init__(self, names: tuple[str, ...], limits: ProofLimits) -> None:
        self.names = names
        self.inputs = {name: index + 2 for index, name in enumerate(names)}
        self.variables = len(names) + 1
        self.clauses: list[tuple[int, ...]] = [(1,)]  # literal 1 is true
        self._intern: dict[tuple[str, tuple[int, ...]], int] = {}
        self._literals = 1
        self.limits = limits

    def add(self, *literals: int) -> None:
        values = set(literals)
        if any(-x in values for x in values):
            return
        result = tuple(sorted(values))
        self._literals += len(result)
        if (
            len(self.clauses) >= self.limits.max_clauses
            or self._literals > self.limits.max_literals
        ):
            raise ProofError("CNF clause or literal limit exceeded")
        self.clauses.append(result)

    def gate(self, op: str, args: tuple[int, ...]) -> int:
        if op in {"and", "or"}:
            identity = 1 if op == "and" else -1
            values = set(args)
            if -identity in values or any(-x in values for x in values):
                return -identity
            values.discard(identity)
            args = tuple(sorted(values))
            if not args:
                return identity
            if len(args) == 1:
                return args[0]
        elif op == "xor":
            # XOR arguments are binary here. Constants and opposite literals
            # are simplified without ever interpreting an arbitrary IR method.
            a, b = sorted(args)
            if a == b:
                return -1
            if a == -b:
                return 1
            if abs(a) == 1:
                return -b if a == 1 else b
            if abs(b) == 1:
                return -a if b == 1 else a
            args = (a, b)
        else:
            raise ProofError("unsupported CNF gate")
        key = (op, args)
        if key in self._intern:
            return self._intern[key]
        self.variables += 1
        if self.variables > self.limits.max_nodes * 4:
            raise ProofError("CNF auxiliary-variable limit exceeded")
        out = self.variables
        self._intern[key] = out
        if op == "and":
            for a in args:
                self.add(-out, a)
            self.add(out, *(-a for a in args))
        elif op == "or":
            for a in args:
                self.add(out, -a)
            self.add(-out, *args)
        else:
            a, b = args
            self.add(-a, -b, -out)
            self.add(a, b, -out)
            self.add(a, -b, out)
            self.add(-a, b, out)
        return out


@dataclass(frozen=True)
class CompiledBoolean:
    roots: tuple[BoolExpr, ...]
    ordered: tuple[BoolExpr, ...]
    aliases: Mapping[str, str]
    names: tuple[str, ...]
    obligation_sha256: str
    encoder: Encoder
    guard: int
    difference: int

    def cnf(self, query: str) -> list[tuple[int, ...]]:
        if query not in {"guard", "difference"}:
            raise ProofError("unsupported certificate query")
        return [*self.encoder.clauses, (self.guard,)] + (
            [(self.difference,)] if query == "difference" else []
        )

    def digest(self, query: str) -> str:
        return _digest(
            {
                "encoding": ENCODING,
                "variables": self.encoder.variables,
                "inputs": self.encoder.inputs,
                "clauses": self.cnf(query),
            }
        )

    def evaluate(self, witness: Any) -> tuple[bool, bool, bool]:
        if not isinstance(witness, Mapping) or set(witness) != set(self.names):
            raise ProofError("witness must assign every source variable exactly once")
        if any(type(value) is not bool for value in witness.values()):
            raise ProofError("witness values must be Boolean")
        native = {
            node.name: witness[self.aliases.get(node.name, node.name)]
            for node in self.ordered
            if isinstance(node, BoolVar)
        }
        values = _evaluate(self.ordered, native)
        return values[id(self.roots[0])], values[id(self.roots[1])], values[id(self.roots[2])]


def compile_boolean(
    left: str | BoolExpr,
    right: str | BoolExpr,
    assume: str | BoolExpr = "1",
    *,
    aliases: Mapping[str, str] | None = None,
    limits: ProofLimits | None = None,
) -> CompiledBoolean:
    budget = limits or ProofLimits()
    try:
        roots, ordered, alias_map, names, digest = _prepare(
            left,
            right,
            assume,
            aliases,
            SymbolicLimits(max_variables=budget.max_variables, max_nodes=budget.max_nodes),
        )
    except _BudgetExceeded as error:
        raise ProofError(str(error)) from error
    encoder = Encoder(names, budget)
    literals: dict[int, int] = {}
    for node in ordered:
        if isinstance(node, BoolConst):
            literal = 1 if node.value else -1
        elif isinstance(node, BoolVar):
            literal = encoder.inputs[alias_map.get(node.name, node.name)]
        elif isinstance(node, BoolNot):
            literal = -literals[id(node.operand)]
        else:
            operands = tuple(literals[id(child)] for child in _children(node))
            if isinstance(node, BoolAnd):
                literal = encoder.gate("and", operands)
            elif isinstance(node, BoolOr):
                literal = encoder.gate("or", operands)
            else:  # _prepare accepts only the exact six documented node classes.
                literal = -1
                for operand in operands:
                    literal = encoder.gate("xor", (literal, operand))
        literals[id(node)] = literal
    difference = encoder.gate("xor", (literals[id(roots[0])], literals[id(roots[1])]))
    return CompiledBoolean(
        roots, ordered, alias_map, names, digest, encoder, literals[id(roots[2])], difference
    )

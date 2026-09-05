"""Bounded, optional SMT reasoning for the two-valued Boolean IR.

Each call owns a Z3 context; no solver globals, native scripts, or ``eval`` are
used. SAT witnesses are made deterministic and replayed in the Python IR.
UNSAT is a solver result, not an independently checked proof certificate.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Literal

from opencollate.boolean import (
    BoolAnd,
    BoolConst,
    BooleanSyntaxError,
    BoolExpr,
    BoolNot,
    BoolOr,
    BoolVar,
    BoolXor,
    parse_boolean,
)

SymbolicStatus = Literal["equivalent", "different", "vacuous", "inconclusive"]


@dataclass(frozen=True, slots=True)
class SymbolicLimits:
    """Finite input, per-query resource, query-count, and total time budgets."""

    max_variables: int = 512
    max_nodes: int = 32_768
    timeout_ms: int = 5_000
    resource_limit: int = 1_000_000
    max_queries: int = 1_024

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_variables", 4_096),
            ("max_nodes", 1_000_000),
            ("timeout_ms", 300_000),
            ("resource_limit", 100_000_000),
            ("max_queries", 8_192),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class SymbolicResult:
    status: SymbolicStatus
    variables: tuple[str, ...] = ()
    counterexample: Mapping[str, bool] | None = None
    reason: str | None = None
    backend_version: str | None = None
    query_count: int = 0
    ir_nodes: int = 0
    obligation_sha256: str | None = None

    @property
    def equivalent(self) -> bool | None:
        if self.status == "equivalent":
            return True
        if self.status == "different":
            return False
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "variables": list(self.variables),
            "counterexample": None if self.counterexample is None else dict(self.counterexample),
            "reason": self.reason,
            "backend": "z3",
            "backend_version": self.backend_version,
            "query_count": self.query_count,
            "ir_nodes": self.ir_nodes,
            "obligation_sha256": self.obligation_sha256,
            "semantics": "two-valued-combinational",
        }


class _BudgetExceeded(RuntimeError):
    pass


def _children(node: BoolExpr) -> tuple[BoolExpr, ...]:
    if type(node) in {BoolConst, BoolVar}:
        return ()
    if type(node) is BoolNot:
        return (node.operand,)
    if isinstance(node, (BoolAnd, BoolOr, BoolXor)) and type(node) in {BoolAnd, BoolOr, BoolXor}:
        return tuple(node.operands)
    raise ValueError(f"unsupported Boolean IR node {type(node).__name__}")


def _postorder(roots: tuple[BoolExpr, ...], limit: int) -> tuple[BoolExpr, ...]:
    """Validate and traverse without trusting recursive user-provided methods."""

    ordered: list[BoolExpr] = []
    complete: set[int] = set()
    active: set[int] = set()
    edges = 0
    for root in roots:
        stack = [(root, False)]
        while stack:
            node, leaving = stack.pop()
            key = id(node)
            if leaving:
                active.remove(key)
                complete.add(key)
                ordered.append(node)
                continue
            if key in complete:
                continue
            if key in active:
                raise ValueError("cyclic Boolean IR is unsupported")
            active.add(key)
            if len(active) + len(complete) > limit:
                raise _BudgetExceeded(f"Boolean IR exceeds {limit} node limit")
            children = _children(node)
            edges += len(children)
            if edges > limit * 4:
                raise _BudgetExceeded("Boolean IR exceeds edge limit")
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(children))
    return tuple(ordered)


def _evaluate(ordered: tuple[BoolExpr, ...], values: Mapping[str, bool]) -> dict[int, bool]:
    """Independent, iterative Python evaluation of a validated Boolean DAG."""

    evaluated: dict[int, bool] = {}
    for node in ordered:
        if isinstance(node, BoolConst):
            value = node.value
        elif isinstance(node, BoolVar):
            value = values[node.name]
        elif isinstance(node, BoolNot):
            value = not evaluated[id(node.operand)]
        elif isinstance(node, BoolAnd):
            value = all(evaluated[id(child)] for child in node.operands)
        elif isinstance(node, BoolOr):
            value = any(evaluated[id(child)] for child in node.operands)
        elif isinstance(node, BoolXor):
            value = sum(evaluated[id(child)] for child in node.operands) % 2 == 1
        else:
            raise ValueError("unsupported Boolean IR")
        evaluated[id(node)] = value
    return evaluated


def _prepare(
    left: BoolExpr | str,
    right: BoolExpr | str,
    assume: BoolExpr | str,
    aliases: Mapping[str, str] | None,
    limits: SymbolicLimits,
) -> tuple[tuple[BoolExpr, ...], tuple[BoolExpr, ...], dict[str, str], tuple[str, ...], str]:
    alias_map = dict(aliases or {})
    if not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in alias_map.items()):
        raise ValueError("aliases must map nonempty strings to nonempty strings")
    roots = tuple(
        parse_boolean(item) if isinstance(item, str) else item for item in (left, right, assume)
    )
    ordered = _postorder(roots, limits.max_nodes)
    names: set[str] = set()
    records: dict[int, str] = {}
    for node in ordered:
        if isinstance(node, BoolConst):
            if type(node.value) is not bool:
                raise ValueError("Boolean IR constants must be bool values")
            record: Any = ["constant", node.value]
        elif isinstance(node, BoolVar):
            if not isinstance(node.name, str) or not node.name:
                raise ValueError("Boolean IR variables require nonempty string names")
            name = alias_map.get(node.name, node.name)
            names.add(name)
            record = ["variable", name]
        else:
            record = [type(node).__name__, [records[id(child)] for child in _children(node)]]
        records[id(node)] = hashlib.sha256(
            json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()
    if len(names) > limits.max_variables:
        raise _BudgetExceeded(
            f"function has {len(names)} variables; symbolic limit is {limits.max_variables}"
        )
    digest = hashlib.sha256(
        json.dumps(
            ["opencollate-boolean-miter-v1", *[records[id(root)] for root in roots]]
        ).encode()
    ).hexdigest()
    return roots, ordered, alias_map, tuple(sorted(names)), digest


def _compile(
    z3: Any,
    context: Any,
    ordered: tuple[BoolExpr, ...],
    names: tuple[str, ...],
    aliases: Mapping[str, str],
) -> tuple[dict[int, Any], dict[str, Any]]:
    # Native names are never interpolated into solver scripts or identifiers.
    symbols = {name: z3.Bool(f"v{index:06d}", ctx=context) for index, name in enumerate(names)}
    compiled: dict[int, Any] = {}
    for node in ordered:
        if isinstance(node, BoolConst):
            value = z3.BoolVal(node.value, ctx=context)
        elif isinstance(node, BoolVar):
            value = symbols[aliases.get(node.name, node.name)]
        elif isinstance(node, BoolNot):
            value = z3.Not(compiled[id(node.operand)])
        else:
            children = [compiled[id(child)] for child in _children(node)]
            if isinstance(node, BoolAnd):
                value = z3.And(*children) if children else z3.BoolVal(True, ctx=context)
            elif isinstance(node, BoolOr):
                value = z3.Or(*children) if children else z3.BoolVal(False, ctx=context)
            else:
                value = z3.BoolVal(False, ctx=context)
                for child in children:
                    value = z3.Xor(value, child)
        compiled[id(node)] = value
    return compiled, symbols


def check_symbolic_equivalence(
    left: BoolExpr | str,
    right: BoolExpr | str,
    *,
    assume: BoolExpr | str = "1",
    aliases: Mapping[str, str] | None = None,
    limits: SymbolicLimits | None = None,
) -> SymbolicResult:
    """Prove ``assume -> (left == right)`` over the supported Boolean IR.

    An unsatisfiable assumption is ``vacuous``, never ``equivalent``. Resource
    exhaustion, unsupported IR, missing solver, and solver errors are explicit
    ``inconclusive`` results. The total time budget includes witness refinement.
    Solver timeout is cooperative, not an OS-enforced process sandbox.
    """

    budget = limits or SymbolicLimits()
    started = time.monotonic()
    query_count = 0
    names: tuple[str, ...] = ()
    ordered: tuple[BoolExpr, ...] = ()
    digest: str | None = None
    version: str | None = None

    def result(
        status: SymbolicStatus, reason: str | None = None, witness: Mapping[str, bool] | None = None
    ) -> SymbolicResult:
        return SymbolicResult(
            status, names, witness, reason, version, query_count, len(ordered), digest
        )

    try:
        roots, ordered, alias_map, names, digest = _prepare(left, right, assume, aliases, budget)
        try:
            z3 = import_module("z3")
        except ModuleNotFoundError:
            return result(
                "inconclusive", "optional Z3 backend is unavailable; install opencollate[formal]"
            )
        version = str(z3.get_version_string())
        context = z3.Context()
        compiled, symbols = _compile(z3, context, ordered, names, alias_map)
        lhs, rhs, guard = (compiled[id(root)] for root in roots)
        solver = z3.Solver(ctx=context)

        def query(*assumptions: Any) -> Any:
            nonlocal query_count
            remaining = budget.timeout_ms - int((time.monotonic() - started) * 1000)
            if remaining <= 0 or query_count >= budget.max_queries:
                raise _BudgetExceeded("symbolic total time or query-count limit exceeded")
            solver.set(timeout=remaining, rlimit=budget.resource_limit, random_seed=0)
            query_count += 1
            answer = solver.check(*assumptions)
            if answer == z3.unknown:
                raise _BudgetExceeded("solver returned unknown: " + str(solver.reason_unknown()))
            if answer not in (z3.sat, z3.unsat):
                raise _BudgetExceeded("solver returned an unsupported status")
            return answer

        solver.add(guard)
        if query() == z3.unsat:
            return result("vacuous", "assumptions are unsatisfiable; no equivalence claim is made")
        solver.add(z3.Xor(lhs, rhs))
        if query() == z3.unsat:
            return result("equivalent")

        # Lexicographically first complete witness, with False before True.
        # Never publish a host/version-dependent arbitrary solver model.
        witness: dict[str, bool] = {}
        for name in names:
            symbol = symbols[name]
            value = query(z3.Not(symbol)) == z3.unsat
            witness[name] = value
            solver.add(symbol if value else z3.Not(symbol))
        native_values = {
            node.name: witness[alias_map.get(node.name, node.name)]
            for node in ordered
            if isinstance(node, BoolVar)
        }
        replay = _evaluate(ordered, native_values)
        if not replay[id(roots[2])] or replay[id(roots[0])] == replay[id(roots[1])]:
            return result("inconclusive", "solver counterexample failed independent Python replay")
        return result(
            "different", "functions differ for the replay-validated input assignment", witness
        )
    except (BooleanSyntaxError, _BudgetExceeded, ValueError, TypeError, RecursionError) as error:
        return result("inconclusive", str(error))
    except Exception as error:
        # The optional native backend must never turn failure into a proof.
        return result("inconclusive", f"symbolic backend failed: {type(error).__name__}")

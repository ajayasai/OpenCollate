"""Deterministic bit blasting of the transition IR, independent of Z3.

Bits are little-endian signed CNF literals. Literal 1 is true and -1 is
false. Only AND and XOR need Tseitin gates; all other operations reduce to
them. This translator and the source frontend remain in the trust boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from opencollate.proof_kernel import Meter, ProofError
from opencollate.sequential_ir import Circuit

Bits = tuple[int, ...]
ENCODING = "opencollate-bitblast-rup-v1"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class CNF:
    def __init__(self, meter: Meter) -> None:
        self.meter = meter
        self.variables = 1
        self.clauses: list[list[int]] = [[1]]
        self.literals = 1
        self.cache: dict[tuple[str, int, int], int] = {}

    def variable(self) -> int:
        self.meter.spend()
        if self.variables >= self.meter.limits.variables:
            raise ProofError("CNF variable bound exhausted")
        self.variables += 1
        return self.variables

    def bits(self, width: int) -> Bits:
        return tuple(self.variable() for _ in range(width))

    @staticmethod
    def const(value: int, width: int) -> Bits:
        return tuple(1 if value & (1 << bit) else -1 for bit in range(width))

    def clause(self, *lits: int) -> None:
        self.meter.spend(len(lits) + 1)
        values: list[int] = []
        seen: set[int] = set()
        for lit in lits:
            if lit == 1 or -lit in seen:
                return
            if lit != -1 and lit not in seen:
                values.append(lit)
                seen.add(lit)
        if len(self.clauses) >= self.meter.limits.clauses:
            raise ProofError("CNF clause bound exhausted")
        self.literals += len(values)
        if self.literals > self.meter.limits.literals:
            raise ProofError("CNF literal bound exhausted")
        self.clauses.append(values)

    def and_(self, a: int, b: int) -> int:
        self.meter.spend()
        if a == -1 or b == -1 or a == -b:
            return -1
        if a == 1:
            return b
        if b == 1 or a == b:
            return a
        lo, hi = sorted((a, b))
        key = ("and", lo, hi)
        if key not in self.cache:
            v = self.variable()
            self.clause(-v, a)
            self.clause(-v, b)
            self.clause(v, -a, -b)
            self.cache[key] = v
        return self.cache[key]

    def or_(self, a: int, b: int) -> int:
        return -self.and_(-a, -b)

    def xor(self, a: int, b: int) -> int:
        self.meter.spend()
        if a == b:
            return -1
        if a == -b:
            return 1
        if abs(a) == 1:
            return -b if a == 1 else b
        if abs(b) == 1:
            return -a if b == 1 else a
        lo, hi = sorted((a, b))
        key = ("xor", lo, hi)
        if key not in self.cache:
            v = self.variable()
            self.clause(a, b, -v)
            self.clause(-a, -b, -v)
            self.clause(a, -b, v)
            self.clause(-a, b, v)
            self.cache[key] = v
        return self.cache[key]

    def all_(self, values: Iterable[int]) -> int:
        result = 1
        for value in values:
            result = self.and_(result, value)
        return result

    def any_(self, values: Iterable[int]) -> int:
        return -self.all_(-v for v in values)

    def mux(self, select: int, yes: int, no: int) -> int:
        if yes == no:
            return yes
        return self.or_(self.and_(select, yes), self.and_(-select, no))

    def equal(self, a: Bits, b: Bits) -> int:
        return self.all_(-self.xor(x, y) for x, y in zip(a, b, strict=True))

    def equate(self, a: Bits, b: Bits) -> None:
        for x, y in zip(a, b, strict=True):
            self.clause(-x, y)
            self.clause(x, -y)

    def add(self, a: Bits, b: Bits, carry: int = -1) -> Bits:
        result = []
        for x, y in zip(a, b, strict=True):
            xy = self.xor(x, y)
            result.append(self.xor(xy, carry))
            carry = self.or_(self.and_(x, y), self.and_(xy, carry))
        return tuple(result)

    def less(self, a: Bits, b: Bits, signed: bool = False) -> int:
        if signed:
            a, b = (*a[:-1], -a[-1]), (*b[:-1], -b[-1])
        result = -1
        for x, y in zip(a, b, strict=True):
            result = self.or_(self.and_(-x, y), self.and_(-self.xor(x, y), result))
        return result

    def problem(self, assertion: int) -> dict[str, Any]:
        # Preserve the fixed true literal even when the assertion is a constant.
        return {"variables": self.variables, "clauses": [*self.clauses, [assertion]]}


class Unroller:
    def __init__(
        self, c: Circuit, spec: dict[str, Any], meter: Meter, *, induction: bool = False
    ) -> None:
        self.c, self.spec = c, spec
        self.cnf = CNF(meter)
        self.induction = induction
        self.frames: list[dict[str, Bits]] = []
        self.nodes: list[dict[int, Bits]] = []

    def lower(self, root: int, frame: int) -> Bits:
        cache = self.nodes[frame]
        todo = [(root, False)]
        while todo:
            self.cnf.meter.spend()
            i, visited = todo.pop()
            if i in cache:
                continue
            n = self.c.nodes[i]
            if not visited:
                todo.append((i, True))
                todo.extend((a, False) for a in reversed(n.args) if a not in cache)
                continue
            a = [cache[j] for j in n.args]
            q, op = self.cnf, n.op

            def boolean(literal: int, width: int = n.width) -> Bits:
                return (literal, *([-1] * (width - 1)))

            if op == "ref":
                v = self.frames[frame][str(n.value)]
            elif op == "const":
                v = q.const(int(n.value), n.width)
            elif op == "cast":
                fill = a[0][-1] if self.c.nodes[n.args[0]].signed else -1
                v = (*a[0], *([fill] * max(0, n.width - len(a[0]))))[: n.width]
            elif op == "mux":
                select = q.any_(a[0])
                v = tuple(q.mux(select, x, y) for x, y in zip(a[1], a[2], strict=True))
            elif op == "slice":
                v = a[0][int(n.value) : int(n.value) + n.width]
            elif op == "concat":
                v = tuple(bit for arg in reversed(a) for bit in arg)
            elif op == "Add":
                v = q.add(a[0], a[1])
            elif op == "Subtract":
                v = q.add(a[0], tuple(-x for x in a[1]), 1)
            elif op == "Multiply":
                v = q.const(0, n.width)
                for j, bit in enumerate(a[1]):
                    row = (*([-1] * j), *(q.and_(x, bit) for x in a[0][: n.width - j]))
                    v = q.add(v, row)
            elif op in {"BinaryAnd", "BinaryOr", "BinaryXor", "BinaryXnor"}:
                fn = {"BinaryAnd": q.and_, "BinaryOr": q.or_}.get(op, q.xor)
                v = tuple(fn(x, y) for x, y in zip(a[0], a[1], strict=True))
                if op == "BinaryXnor":
                    v = tuple(-x for x in v)
            elif op in {"Equality", "Inequality"}:
                equal = q.equal(a[0], a[1])
                v = boolean(equal if op == "Equality" else -equal)
            elif op in {"LessThan", "LessThanEqual", "GreaterThan", "GreaterThanEqual"}:
                signed = all(self.c.nodes[j].signed for j in n.args)
                reverse = op in {"GreaterThan", "LessThanEqual"}
                x, y = (a[1], a[0]) if reverse else (a[0], a[1])
                less = q.less(x, y, signed)
                v = boolean(-less if op.endswith("Equal") else less)
            elif op in {"LogicalAnd", "LogicalOr"}:
                left, right = q.any_(a[0]), q.any_(a[1])
                v = boolean(q.and_(left, right) if op == "LogicalAnd" else q.or_(left, right))
            elif op in {
                "LogicalShiftLeft",
                "ArithmeticShiftLeft",
                "LogicalShiftRight",
                "ArithmeticShiftRight",
            }:
                v = a[0]
                fill = (
                    v[-1] if op == "ArithmeticShiftRight" and self.c.nodes[n.args[0]].signed else -1
                )
                for j, bit in enumerate(a[1]):
                    distance = min(1 << j, len(v))
                    if op.endswith("Left"):
                        shifted = (*([-1] * distance), *v[: len(v) - distance])
                    else:
                        shifted = (*v[distance:], *([fill] * distance))
                    v = tuple(q.mux(bit, x, y) for x, y in zip(shifted, v, strict=True))
            elif op == "Plus":
                v = a[0]
            elif op == "Minus":
                v = q.add(q.const(0, n.width), tuple(-x for x in a[0]), 1)
            elif op == "BitwiseNot":
                v = tuple(-x for x in a[0])
            elif op == "LogicalNot":
                v = boolean(-q.any_(a[0]))
            elif op in {
                "BitwiseAnd",
                "BitwiseNand",
                "BitwiseOr",
                "BitwiseNor",
                "BitwiseXor",
                "BitwiseXnor",
            }:
                if op in {"BitwiseAnd", "BitwiseNand"}:
                    condition = q.all_(a[0])
                elif op in {"BitwiseOr", "BitwiseNor"}:
                    condition = q.any_(a[0])
                else:
                    condition = -1
                    for bit in a[0]:
                        condition = q.xor(condition, bit)
                if op in {"BitwiseNand", "BitwiseNor", "BitwiseXnor"}:
                    condition = -condition
                v = boolean(condition)
            else:
                raise ProofError(f"unsupported certificate IR operator: {op}")
            if len(v) != n.width:
                raise ProofError("certificate expression width disagrees with typed IR")
            cache[i] = v
        return cache[root]

    def append(self) -> None:
        q = self.cnf
        q.meter.remaining()
        t = len(self.frames)
        # Initial registers remain arbitrary. Later registers and combinational
        # wires are aliases of their defining expressions, not extra variables
        # joined by equality clauses. This is exact substitution of deterministic
        # equations, never an assumption about reset or unobserved values.
        fixed = dict(self.spec["assumptions"])
        reset = self.spec["reset"]
        if reset:
            fixed[reset["signal"]] = (
                reset["active"]
                if not self.induction and t < reset["cycles"]
                else 1 - reset["active"]
            )
        frame = {
            n: q.const(fixed[n], self.c.signals[n][0])
            if n in fixed
            else q.bits(self.c.signals[n][0])
            for n in sorted(self.c.inputs)
        }
        for n, expr in sorted(self.c.next_state.items()):
            frame[n] = self.lower(expr, t - 1) if t else q.bits(self.c.signals[n][0])
        self.frames.append(frame)
        self.nodes.append({})
        for n in self.c.comb_order:
            frame[n] = self.lower(self.c.combinational[n], t)

    def property(self, prop: dict[str, Any], t: int) -> tuple[int, int]:
        q = self.cnf
        guard = q.all_(
            q.equal(
                self.frames[t - g["lag"]][g["signal"]],
                q.const(g["equals"], self.c.signals[g["signal"]][0]),
            )
            for g in prop["when"]
        )
        rhs = (
            self.frames[t - prop["latency"]][prop["source"]]
            if "source" in prop
            else q.const(prop["equals"], self.c.signals[prop["sink"]][0])
        )
        return guard, q.or_(-guard, q.equal(self.frames[t][prop["sink"]], rhs))


def base_problem(
    c: Circuit, spec: dict[str, Any], prop: dict[str, Any], meter: Meter
) -> tuple[Unroller, dict[str, Any], dict[str, Any]]:
    unroll = Unroller(c, spec, meter)
    for _ in range(spec["depth"] + 1):
        unroll.append()
    guards, failures = [], []
    for t in range(prop["start_cycle"], spec["depth"] + 1):
        guard, predicate = unroll.property(prop, t)
        guards.append(guard)
        failures.append(-predicate)
    bad = unroll.cnf.any_(failures)
    cover = unroll.cnf.any_(guards)
    return unroll, unroll.cnf.problem(bad), unroll.cnf.problem(cover)


def step_problem(
    c: Circuit, spec: dict[str, Any], prop: dict[str, Any], k: int, meter: Meter
) -> dict[str, Any]:
    history = max([prop["latency"], *(g["lag"] for g in prop["when"])])
    unroll = Unroller(c, spec, meter, induction=True)
    for _ in range(history + k + 1):
        unroll.append()
    for t in range(history, history + k):
        _, hypothesis = unroll.property(prop, t)
        unroll.cnf.clause(hypothesis)
    _, conclusion = unroll.property(prop, history + k)
    return unroll.cnf.problem(-conclusion)

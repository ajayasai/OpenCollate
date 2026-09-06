"""Bit-vector BMC and k-induction over the source-derived transition IR."""

from __future__ import annotations

import importlib
import time
from typing import Any

from opencollate.sequential_ir import Circuit, SequentialError, simulate_frame
from opencollate.sequential_spec import holds


class Budget:
    def __init__(self, timeout_ms: int, resource_limit: int) -> None:
        self.z3 = importlib.import_module("z3")
        self.ctx = self.z3.Context()
        self.deadline = time.monotonic() + timeout_ms / 1000
        self.resource_limit = resource_limit
        self.queries = 0

    def remaining(self) -> int:
        ms = int((self.deadline - time.monotonic()) * 1000)
        if ms < 1:
            raise SequentialError("sequential analysis deadline exhausted")
        return ms

    def solver(self) -> Any:
        s = self.z3.Solver(ctx=self.ctx)
        s.set(random_seed=0)
        return s

    def query(self, s: Any, *extra: Any) -> bool:
        self.queries += 1
        if self.queries > 8192:
            raise SequentialError("sequential analysis query bound exhausted")
        s.set(timeout=self.remaining(), rlimit=self.resource_limit)
        result = s.check(*extra)
        if result == self.z3.unknown:
            raise SequentialError("solver inconclusive: " + s.reason_unknown())
        return result == self.z3.sat


class Unroller:
    def __init__(
        self,
        c: Circuit,
        spec: dict[str, Any],
        budget: Budget,
        prefix: str,
        *,
        induction: bool = False,
    ) -> None:
        self.c, self.spec, self.b = c, spec, budget
        self.prefix, self.induction = prefix, induction
        self.solver = budget.solver()
        self.frames: list[dict[str, Any]] = []
        self.nodes: list[dict[int, Any]] = []
        self.z = budget.z3

    def val(self, n: int, width: int) -> Any:
        return self.z.BitVecVal(n, width, ctx=self.b.ctx)

    def resize(self, v: Any, width: int, signed: bool = False) -> Any:
        w = v.size()
        if width == w:
            return v
        if width < w:
            return self.z.Extract(width - 1, 0, v)
        return self.z.SignExt(width - w, v) if signed else self.z.ZeroExt(width - w, v)

    def lower(self, root: int, frame: int) -> Any:
        cache = self.nodes[frame]
        todo = [(root, False)]
        while todo:
            i, visited = todo.pop()
            if i in cache:
                continue
            n = self.c.nodes[i]
            if not visited:
                todo.append((i, True))
                todo.extend((a, False) for a in reversed(n.args) if a not in cache)
                continue
            a = [cache[j] for j in n.args]
            op = n.op
            z = self.z

            def bit(cond: Any, width: int = n.width) -> Any:
                return self.z.If(cond, self.val(1, width), self.val(0, width))

            if op == "ref":
                v = self.frames[frame][str(n.value)]
            elif op == "const":
                v = self.val(int(n.value), n.width)
            elif op == "cast":
                v = self.resize(a[0], n.width, self.c.nodes[n.args[0]].signed)
            elif op == "mux":
                v = z.If(a[0] != 0, a[1], a[2])
            elif op == "slice":
                v = z.Extract(int(n.value) + n.width - 1, int(n.value), a[0])
            elif op == "concat":
                v = z.Concat(*a) if len(a) > 1 else a[0]
            elif op == "Add":
                v = a[0] + a[1]
            elif op == "Subtract":
                v = a[0] - a[1]
            elif op == "Multiply":
                v = a[0] * a[1]
            elif op == "BinaryAnd":
                v = a[0] & a[1]
            elif op == "BinaryOr":
                v = a[0] | a[1]
            elif op == "BinaryXor":
                v = a[0] ^ a[1]
            elif op == "BinaryXnor":
                v = ~(a[0] ^ a[1])
            elif op == "Equality":
                v = bit(a[0] == a[1])
            elif op == "Inequality":
                v = bit(a[0] != a[1])
            elif op in {"LessThan", "LessThanEqual", "GreaterThan", "GreaterThanEqual"}:
                signed = all(self.c.nodes[j].signed for j in n.args)
                if op == "LessThan":
                    cond = a[0] < a[1] if signed else z.ULT(a[0], a[1])
                elif op == "LessThanEqual":
                    cond = a[0] <= a[1] if signed else z.ULE(a[0], a[1])
                elif op == "GreaterThan":
                    cond = a[0] > a[1] if signed else z.UGT(a[0], a[1])
                else:
                    cond = a[0] >= a[1] if signed else z.UGE(a[0], a[1])
                v = bit(cond)
            elif op == "LogicalAnd":
                v = bit(z.And(a[0] != 0, a[1] != 0))
            elif op == "LogicalOr":
                v = bit(z.Or(a[0] != 0, a[1] != 0))
            elif "Shift" in op:
                w = a[0].size()
                shift = self.resize(a[1], w)
                arithmetic = op == "ArithmeticShiftRight" and self.c.nodes[n.args[0]].signed
                if op.endswith("Left"):
                    shifted = a[0] << shift
                else:
                    shifted = a[0] >> shift if arithmetic else z.LShR(a[0], shift)
                overflow = (
                    z.UGE(a[1], self.val(w, a[1].size()))
                    if (1 << a[1].size()) > w
                    else z.BoolVal(False, ctx=self.b.ctx)
                )
                fill = (
                    z.If(a[0] < 0, self.val(-1, w), self.val(0, w))
                    if arithmetic
                    else self.val(0, w)
                )
                v = z.If(overflow, fill, shifted)
            elif op == "Plus":
                v = a[0]
            elif op == "Minus":
                v = -a[0]
            elif op == "BitwiseNot":
                v = ~a[0]
            elif op == "LogicalNot":
                v = bit(a[0] == 0)
            elif op in {
                "BitwiseAnd",
                "BitwiseNand",
                "BitwiseOr",
                "BitwiseNor",
                "BitwiseXor",
                "BitwiseXnor",
            }:
                if op in {"BitwiseAnd", "BitwiseNand"}:
                    cond = a[0] == self.val(-1, a[0].size())
                elif op in {"BitwiseOr", "BitwiseNor"}:
                    cond = a[0] != 0
                else:
                    bits = [z.Extract(j, j, a[0]) != 0 for j in range(a[0].size())]
                    cond = bits[0]
                    for value in bits[1:]:
                        cond = z.Xor(cond, value)
                if op in {"BitwiseNand", "BitwiseNor", "BitwiseXnor"}:
                    cond = z.Not(cond)
                v = bit(cond)
            else:
                raise SequentialError(f"unhandled SMT operator: {op}")
            if v.size() != n.width:
                raise SequentialError("SMT expression width disagrees with typed IR")
            cache[i] = v
        return cache[root]

    def append(self) -> None:
        self.b.remaining()
        t = len(self.frames)
        driven = set(self.c.inputs) | set(self.c.next_state) | set(self.c.combinational)
        self.frames.append(
            {
                n: self.z.BitVec(f"{self.prefix}/{t}/{n}", self.c.signals[n][0], ctx=self.b.ctx)
                for n in sorted(driven)
            }
        )
        self.nodes.append({})
        f = self.frames[t]
        for n, expr in self.c.combinational.items():
            self.solver.add(f[n] == self.lower(expr, t))
        for n, v in self.spec["assumptions"].items():
            self.solver.add(f[n] == v)
        reset = self.spec["reset"]
        if reset:
            v = (
                reset["active"]
                if not self.induction and t < reset["cycles"]
                else 1 - reset["active"]
            )
            self.solver.add(f[reset["signal"]] == v)
        if t:
            for n, expr in self.c.next_state.items():
                self.solver.add(f[n] == self.lower(expr, t - 1))

    def property(self, prop: dict[str, Any], t: int) -> tuple[Any, Any]:
        guard = (
            self.z.And(
                *[self.frames[t - g["lag"]][g["signal"]] == g["equals"] for g in prop["when"]]
            )
            if prop["when"]
            else self.z.BoolVal(True, ctx=self.b.ctx)
        )
        rhs = (
            self.frames[t - prop["latency"]][prop["source"]] if "source" in prop else prop["equals"]
        )
        predicate = self.z.Implies(guard, self.frames[t][prop["sink"]] == rhs)
        return guard, predicate

    def trace(self, prop: dict[str, Any], cycle: int) -> list[dict[str, Any]]:
        """Canonicalize free values, then independently replay the entire trace."""
        free = [(0, n) for n in sorted(self.c.next_state)] + [
            (t, n) for t in range(cycle + 1) for n in sorted(self.c.inputs)
        ]
        for t, n in free:
            variable = self.frames[t][n]
            if self.b.query(self.solver, variable == 0):
                value = 0
            else:
                low, high = 1, (1 << variable.size()) - 1
                while low < high:
                    mid = (low + high) // 2
                    if self.b.query(
                        self.solver, self.z.ULE(variable, self.val(mid, variable.size()))
                    ):
                        high = mid
                    else:
                        low = mid + 1
                value = low
            self.solver.add(variable == value)
        if not self.b.query(self.solver):
            raise SequentialError("counterexample disappeared during refinement")
        model = self.solver.model()
        trace = [
            {n: model.eval(v, model_completion=True).as_long() for n, v in f.items()}
            for f in self.frames[: cycle + 1]
        ]
        if not replay_trace(self.c, self.spec, prop, trace):
            raise SequentialError("counterexample failed independent integer replay")
        return [{"cycle": t, "values": f} for t, f in enumerate(trace)]


def replay_trace(
    c: Circuit, spec: dict[str, Any], prop: dict[str, Any], frames: list[dict[str, int]]
) -> bool:
    """Validate every sample, reset/assumption, equation, transition, and failure."""
    if not frames or len(frames) - 1 < prop["start_cycle"]:
        return False
    driven = set(c.inputs) | set(c.next_state) | set(c.combinational)
    for t, f in enumerate(frames):
        if set(f) != driven or any(
            type(v) is not int or not 0 <= v < (1 << c.signals[n][0]) for n, v in f.items()
        ):
            return False
        if any(f[n] != v for n, v in spec["assumptions"].items()):
            return False
        r = spec["reset"]
        if r and f[r["signal"]] != (r["active"] if t < r["cycles"] else 1 - r["active"]):
            return False
        observed, updated = simulate_frame(
            c, {n: f[n] for n in c.next_state}, {n: f[n] for n in c.inputs}
        )
        if observed != f:
            return False
        if t + 1 < len(frames) and any(frames[t + 1][n] != v for n, v in updated.items()):
            return False
        if prop["start_cycle"] <= t < len(frames) - 1 and not holds(prop, frames, t)[1]:
            return False
    return not holds(prop, frames, len(frames) - 1)[1]


def verify_property(
    c: Circuit, spec: dict[str, Any], prop: dict[str, Any], b: Budget
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": prop["id"],
        "status": "inconclusive",
        "checked_through": None,
        "induction_depth": None,
        "cover_cycle": None,
        "trace": None,
        "reason": None,
    }
    try:
        base = Unroller(c, spec, b, "base")
        covered = False
        for t in range(spec["depth"] + 1):
            base.append()
            if t < prop["start_cycle"]:
                continue
            guard, predicate = base.property(prop, t)
            # First check that the context itself is satisfiable; never prove by contradiction.
            if not b.query(base.solver):
                raise SequentialError("assumptions or transition context is unsatisfiable")
            if not covered and b.query(base.solver, guard):
                covered = True
                result["cover_cycle"] = t
            base.solver.push()
            base.solver.add(b.z3.Not(predicate))
            if b.query(base.solver):
                result.update(status="counterexample", checked_through=t, trace=base.trace(prop, t))
                return result
            base.solver.pop()
            result["checked_through"] = t
        if not covered:
            result.update(
                status="uncovered", reason="no reachable guard activation within the checked bound"
            )
            return result
        history = max([prop["latency"], *(g["lag"] for g in prop["when"])])
        reset_cycles = spec["reset"]["cycles"] if spec["reset"] else 0
        prefix = max(prop["start_cycle"], reset_cycles + history)
        step = Unroller(c, spec, b, "step", induction=True)
        for _ in range(history + 1):
            step.append()
        for k in range(1, spec["induction"] + 1):
            if prefix + k > spec["depth"]:
                break
            _, hypothesis = step.property(prop, history + k - 1)
            step.solver.add(hypothesis)
            step.append()
            _, conclusion = step.property(prop, history + k)
            # SAT induction counterexamples can be unreachable, so are not design failures.
            if not b.query(step.solver, b.z3.Not(conclusion)):
                result.update(status="proven", induction_depth=k)
                return result
        result.update(
            status="bounded",
            reason="no counterexample within bound; induction did not establish an unbounded proof",
        )
        return result
    except SequentialError as error:
        result["reason"] = str(error)
        return result

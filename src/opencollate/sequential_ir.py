"""Small, typed, acyclic transition IR and an independent integer interpreter.

Node indices are topological. The interpreter is intentionally separate from
SMT lowering; counterexamples must replay every combinational and state update.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class SequentialError(ValueError):
    """Unsupported, ambiguous, malformed, or resource-exhausted analysis."""


@dataclass(frozen=True, slots=True)
class Node:
    op: str
    width: int
    signed: bool = False
    args: tuple[int, ...] = ()
    value: str | int = 0


@dataclass
class Circuit:
    top: str
    clock: str
    nodes: list[Node] = field(default_factory=list)
    signals: dict[str, tuple[int, bool]] = field(default_factory=dict)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    next_state: dict[str, int] = field(default_factory=dict)
    combinational: dict[str, int] = field(default_factory=dict)
    locations: dict[str, dict[str, Any]] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    comb_order: list[str] = field(default_factory=list)
    frontend: str = ""
    _intern: dict[Node, int] = field(default_factory=dict, repr=False)

    def add(
        self,
        op: str,
        width: int,
        signed: bool = False,
        args: tuple[int, ...] = (),
        value: str | int = 0,
    ) -> int:
        if not 1 <= width <= 256:
            raise SequentialError("IR width must be between 1 and 256 bits")
        if any(not 0 <= a < len(self.nodes) for a in args):
            raise SequentialError("non-topological IR edge")
        n = Node(op, width, signed, args, value)
        if n not in self._intern:
            if len(self.nodes) >= 32768:
                raise SequentialError("transition IR exceeds 32768 nodes")
            self._intern[n] = len(self.nodes)
            self.nodes.append(n)
        return self._intern[n]

    def ref(self, name: str) -> int:
        if name == self.clock or name not in self.signals:
            raise SequentialError(f"clock-as-data or unresolved signal: {name}")
        width, signed = self.signals[name]
        return self.add("ref", width, signed, value=name)

    def serialized(self) -> dict[str, Any]:
        return {
            "top": self.top,
            "clock": self.clock,
            "nodes": [asdict(n) for n in self.nodes],
            "signals": self.signals,
            "inputs": sorted(self.inputs),
            "outputs": sorted(self.outputs),
            "next_state": self.next_state,
            "combinational": self.combinational,
            "locations": self.locations,
            "sources": self.sources,
            "frontend": self.frontend,
        }

    def finalize(self) -> None:
        drivers = set(self.inputs) | set(self.next_state) | set(self.combinational)
        referenced = {str(n.value) for n in self.nodes if n.op == "ref"}
        unresolved = (referenced | set(self.outputs)) - drivers
        if unresolved:
            raise SequentialError("undriven signal(s): " + ", ".join(sorted(unresolved)))
        if sum(self.signals[s][0] for s in self.next_state) > 16384:
            raise SequentialError("state exceeds 16384 bits")
        dependencies: dict[str, set[str]] = {}
        for name, root in self.combinational.items():
            todo, seen, refs = [root], set(), set()
            while todo:
                i = todo.pop()
                if i in seen:
                    continue
                seen.add(i)
                node = self.nodes[i]
                if node.op == "ref":
                    refs.add(str(node.value))
                todo.extend(node.args)
            dependencies[name] = refs & set(self.combinational)
        # Kahn ordering; a cycle is unsupported, never an unconstrained wire.
        while dependencies:
            ready = sorted(name for name, deps in dependencies.items() if not deps)
            if not ready:
                raise SequentialError("combinational cycle detected")
            self.comb_order.extend(ready)
            for name in ready:
                del dependencies[name]
            for deps in dependencies.values():
                deps.difference_update(ready)


def signed_value(value: int, width: int) -> int:
    return value - (1 << width) if value & (1 << (width - 1)) else value


def evaluate(circuit: Circuit, root: int, env: dict[str, int]) -> int:
    """Iteratively interpret a root in finite-width, two-valued arithmetic."""
    values: dict[int, int] = {}
    stack = [(root, False)]
    while stack:
        i, visited = stack.pop()
        if i in values:
            continue
        n = circuit.nodes[i]
        if not visited:
            stack.append((i, True))
            stack.extend((a, False) for a in reversed(n.args) if a not in values)
            continue
        a = [values[j] for j in n.args]
        mask = (1 << n.width) - 1
        op = n.op
        if op == "ref":
            v = env[str(n.value)]
        elif op == "const":
            v = int(n.value)
        elif op == "cast":
            src = circuit.nodes[n.args[0]]
            v = signed_value(a[0], src.width) if src.signed else a[0]
        elif op == "mux":
            v = a[1] if a[0] else a[2]
        elif op == "slice":
            v = a[0] >> int(n.value)
        elif op == "concat":
            v = 0
            for idx, val in zip(n.args, a, strict=True):
                v = (v << circuit.nodes[idx].width) | val
        elif op in {
            "Add",
            "Subtract",
            "Multiply",
            "BinaryAnd",
            "BinaryOr",
            "BinaryXor",
            "BinaryXnor",
        }:
            if op == "Add":
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
            else:
                v = ~(a[0] ^ a[1])
        elif op in {
            "Equality",
            "Inequality",
            "LessThan",
            "LessThanEqual",
            "GreaterThan",
            "GreaterThanEqual",
        }:
            left, right = (circuit.nodes[j] for j in n.args)
            x = signed_value(a[0], left.width) if left.signed and right.signed else a[0]
            y = signed_value(a[1], right.width) if left.signed and right.signed else a[1]
            if op == "Equality":
                v = int(x == y)
            elif op == "Inequality":
                v = int(x != y)
            elif op == "LessThan":
                v = int(x < y)
            elif op == "LessThanEqual":
                v = int(x <= y)
            elif op == "GreaterThan":
                v = int(x > y)
            else:
                v = int(x >= y)
        elif op in {"LogicalAnd", "LogicalOr"}:
            v = (
                int(bool(a[0]) and bool(a[1]))
                if op == "LogicalAnd"
                else int(bool(a[0]) or bool(a[1]))
            )
        elif op in {
            "LogicalShiftLeft",
            "ArithmeticShiftLeft",
            "LogicalShiftRight",
            "ArithmeticShiftRight",
        }:
            left = circuit.nodes[n.args[0]]
            x = (
                signed_value(a[0], left.width)
                if op == "ArithmeticShiftRight" and left.signed
                else a[0]
            )
            # Avoid constructing huge integers from malicious shift counts.
            if a[1] >= left.width:
                v = -1 if op == "ArithmeticShiftRight" and x < 0 else 0
            elif op.endswith("Left"):
                v = x << a[1]
            else:
                v = x >> a[1]
        elif op == "Plus":
            v = a[0]
        elif op == "Minus":
            v = -a[0]
        elif op == "BitwiseNot":
            v = ~a[0]
        elif op == "LogicalNot":
            v = int(not a[0])
        elif op in {
            "BitwiseAnd",
            "BitwiseNand",
            "BitwiseOr",
            "BitwiseNor",
            "BitwiseXor",
            "BitwiseXnor",
        }:
            w = circuit.nodes[n.args[0]].width
            if op in {"BitwiseAnd", "BitwiseNand"}:
                v = int(a[0] == (1 << w) - 1)
            elif op in {"BitwiseOr", "BitwiseNor"}:
                v = int(a[0] != 0)
            else:
                v = a[0].bit_count() % 2
            if op in {"BitwiseNand", "BitwiseNor", "BitwiseXnor"}:
                v = 1 - v
        else:
            raise SequentialError(f"unhandled IR operator: {op}")
        values[i] = v & mask
    return values[root]


def simulate_frame(
    circuit: Circuit, states: dict[str, int], inputs: dict[str, int]
) -> tuple[dict[str, int], dict[str, int]]:
    """Observe before an edge and compute all nonblocking updates together."""
    env = {**states, **inputs}
    for name in circuit.comb_order:
        env[name] = evaluate(circuit, circuit.combinational[name], env)
    updated = {name: evaluate(circuit, expr, env) for name, expr in circuit.next_state.items()}
    return env, updated

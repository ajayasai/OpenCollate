"""Bounded procedural lowering with explicit blocking / nonblocking semantics.

Ordinary case statements preserve first-match priority. Combinational blocks
must define every written variable on all syntactic paths and may not read a
written variable before its current value is definitely assigned. This rejects
latches and order-sensitive read-before-write blocks rather than guessing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opencollate.sequential_ir import SequentialError

if TYPE_CHECKING:
    from opencollate.sequential_rtl import _Lowerer


class Procedure:
    def __init__(self, lower: _Lowerer, *, sequential: bool) -> None:
        self.lower = lower
        self.sequential = sequential
        self.writes: set[str] = set()

    def _children(self, stmt: Any) -> list[Any]:
        kind = stmt.kind.name
        if kind == "Block":
            if stmt.blockKind.name != "Sequential":
                raise SequentialError("parallel statement blocks are unsupported")
            return [stmt.body]
        if kind == "List":
            return list(stmt.list)
        if kind == "Conditional":
            if (
                len(stmt.conditions) != 1
                or stmt.conditions[0].pattern is not None
                or stmt.check.name != "None_"
            ):
                raise SequentialError("only ordinary if/else guards are supported")
            return [stmt.ifTrue] + ([stmt.ifFalse] if stmt.ifFalse is not None else [])
        if kind == "Case":
            if stmt.condition.name != "Normal" or stmt.check.name != "None_":
                raise SequentialError(
                    "only ordinary case is supported; no wildcard/unique/priority"
                )
            if len(stmt.items) > 256 or sum(len(item.expressions) for item in stmt.items) > 1024:
                raise SequentialError("case exceeds 256 groups or 1024 labels")
            return [item.stmt for item in stmt.items] + (
                [stmt.defaultCase] if stmt.defaultCase is not None else []
            )
        if kind in {"Empty", "ExpressionStatement"}:
            return []
        raise SequentialError(f"unsupported procedural statement: {kind}")

    def collect(self, stmt: Any, depth: int = 0) -> None:
        self.lower.bounded(depth)
        if stmt.kind.name == "ExpressionStatement":
            e = stmt.expr
            if (
                e.kind.name != "Assignment"
                or e.isCompound
                or e.timingControl is not None
                or bool(e.isNonBlocking) != self.sequential
                or e.left.kind.name not in {"NamedValue", "HierarchicalValue"}
            ):
                raise SequentialError(
                    "procedural assignments must be untimed whole-signal "
                    + ("nonblocking" if self.sequential else "blocking")
                    + " assignments"
                )
            self.writes.add(self.lower.symbol_name(e.left.symbol))
        for child in self._children(stmt):
            self.collect(child, depth + 1)

    def expression(self, expr: Any, env: dict[str, int]) -> int:
        lower = self.lower
        old = lower.blocking_environment, lower.blocking_writes
        try:
            if not self.sequential:
                lower.blocking_environment, lower.blocking_writes = env, self.writes
            return lower.expression(expr)
        finally:
            lower.blocking_environment, lower.blocking_writes = old

    def merge(self, condition: int, yes: dict[str, int], no: dict[str, int]) -> dict[str, int]:
        c = self.lower.c
        # Definite assignment is intersection for combinational logic. A missing
        # branch is an old-state hold only for nonblocking clocked statements.
        names = set(yes) | set(no) if self.sequential else set(yes) & set(no)
        out: dict[str, int] = {}
        for name in sorted(names):
            a, b = yes.get(name), no.get(name)
            if a is None:
                a = c.ref(name)
            if b is None:
                b = c.ref(name)
            width, signed = c.signals[name]
            out[name] = a if a == b else c.add("mux", width, signed, (condition, a, b))
        return out

    def walk(self, stmt: Any, env: dict[str, int], depth: int = 0) -> dict[str, int]:
        lower, c = self.lower, self.lower.c
        lower.bounded(depth)
        kind = stmt.kind.name
        children = self._children(stmt)
        if kind == "Empty":
            return env
        if kind in {"List", "Block"}:
            for child in children:
                env = self.walk(child, env, depth + 1)
            return env
        if kind == "ExpressionStatement":
            e = stmt.expr
            name = lower.symbol_name(e.left.symbol)
            if name in c.inputs or name in lower.clock_aliases or name not in c.signals:
                raise SequentialError(f"invalid driven signal: {name}")
            root = self.expression(e.right, env)
            width, signed = c.signals[name]
            if c.nodes[root].width != width:
                raise SequentialError("assignment width was not elaborated consistently")
            # Subsequent blocking reads see the declared target signedness,
            # not the possibly different signedness of its RHS expression.
            if not self.sequential:
                root = c.add("cast", width, signed, (root,))
            c.locations[name] = lower.location(e)
            return {**env, name: root}
        if kind == "Conditional":
            cond = self.expression(stmt.conditions[0].expr, env)
            yes = self.walk(stmt.ifTrue, dict(env), depth + 1)
            no = self.walk(stmt.ifFalse, dict(env), depth + 1) if stmt.ifFalse is not None else env
            return self.merge(cond, yes, no)
        if kind == "Case":
            selector = self.expression(stmt.expr, env)
            branches: list[tuple[int, dict[str, int]]] = []
            for item in stmt.items:
                if not item.expressions:
                    raise SequentialError("empty case label group")
                guard = c.add("const", 1, value=0)
                for label in item.expressions:
                    value = self.expression(label, env)
                    if c.nodes[selector].width != c.nodes[value].width:
                        raise SequentialError(
                            "case operand widths were not elaborated consistently"
                        )
                    equal = c.add("Equality", 1, False, (selector, value))
                    guard = c.add("LogicalOr", 1, False, (guard, equal))
                branches.append((guard, self.walk(item.stmt, dict(env), depth + 1)))
            out = (
                self.walk(stmt.defaultCase, dict(env), depth + 1)
                if stmt.defaultCase is not None
                else dict(env)
            )
            # Reverse assembly means the first source match has highest priority,
            # including overlapping labels and a textually early default clause.
            for guard, branch in reversed(branches):
                out = self.merge(guard, branch, out)
            return out
        raise SequentialError(f"unsupported procedural statement: {kind}")

    def run(self, stmt: Any, pending: dict[str, int] | None = None) -> dict[str, int]:
        self.collect(stmt)
        result = self.walk(stmt, {} if pending is None else pending)
        if not self.sequential and self.writes - set(result):
            raise SequentialError(
                "incomplete combinational assignment (possible latch): "
                + ", ".join(sorted(self.writes - set(result)))
            )
        return result

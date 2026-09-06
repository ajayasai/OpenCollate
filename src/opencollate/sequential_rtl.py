"""Fail-closed lowering of a documented single-clock RTL subset via pyslang.

No regular-expression RTL interpretation or user-supplied transition equations.
Source bytes are read once, hashed, then passed to the elaborating frontend.
"""

from __future__ import annotations

import hashlib
import importlib
import re
from pathlib import Path
from typing import Any

from opencollate.sequential_ir import Circuit, SequentialError

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z", re.ASCII)
BINARY = frozenset(
    {
        "Add",
        "Subtract",
        "Multiply",
        "BinaryAnd",
        "BinaryOr",
        "BinaryXor",
        "BinaryXnor",
        "Equality",
        "Inequality",
        "GreaterThanEqual",
        "GreaterThan",
        "LessThanEqual",
        "LessThan",
        "LogicalAnd",
        "LogicalOr",
        "LogicalShiftLeft",
        "LogicalShiftRight",
        "ArithmeticShiftLeft",
        "ArithmeticShiftRight",
    }
)
UNARY = frozenset(
    {
        "Plus",
        "Minus",
        "BitwiseNot",
        "BitwiseAnd",
        "BitwiseOr",
        "BitwiseXor",
        "BitwiseNand",
        "BitwiseNor",
        "BitwiseXnor",
        "LogicalNot",
    }
)


def _integer(value: Any) -> int:
    unknown = value.hasUnknown
    if bool(unknown() if callable(unknown) else unknown):
        raise SequentialError("X/Z values are outside two-valued synchronous semantics")
    raw = value.value if type(value).__name__ == "ConstantValue" else value
    if type(raw).__name__ != "SVInt":
        raise SequentialError("constant is not an integral bit-vector")
    return int(raw)


class _Lowerer:
    def __init__(self, circuit: Circuit, source_manager: Any) -> None:
        self.c = circuit
        self.sm = source_manager
        self.work = 0

    def bounded(self, depth: int) -> None:
        self.work += 1
        if depth > 64 or self.work > 100000:
            raise SequentialError("RTL expression/statement depth or work limit exceeded")

    def shape(self, dtype: Any, *, declaration: bool = False) -> tuple[int, bool]:
        if not dtype.isSimpleBitVector or not 1 <= int(dtype.bitWidth) <= 256:
            raise SequentialError(
                "only scalar or simple packed bit-vectors of 1..256 bits are supported"
            )
        if declaration and dtype.isPackedArray:
            r = dtype.getBitVectorRange()
            if r.right != 0 or r.left != int(dtype.bitWidth) - 1:
                raise SequentialError("packed ranges must be descending [width-1:0]")
        return int(dtype.bitWidth), bool(dtype.isSigned)

    def location(self, obj: Any) -> dict[str, Any]:
        loc = obj.location if hasattr(obj, "location") else obj.sourceRange.start
        return {
            "source": str(self.sm.getFileName(loc)),
            "line": int(self.sm.getLineNumber(loc)),
            "column": int(self.sm.getColumnNumber(loc)),
        }

    def expression(self, e: Any, depth: int = 0) -> int:
        self.bounded(depth)
        width, signed = self.shape(e.type)
        args: tuple[int, ...]
        kind = e.kind.name
        if kind in {"IntegerLiteral", "UnbasedUnsizedIntegerLiteral"}:
            return self.c.add("const", width, signed, value=_integer(e.value) & ((1 << width) - 1))
        if kind == "NamedValue":
            if e.symbol.kind.name in {"Parameter", "EnumValue"}:
                return self.c.add(
                    "const", width, signed, value=_integer(e.symbol.value) & ((1 << width) - 1)
                )
            return self.c.ref(e.symbol.name)
        if kind == "Conversion":
            return self.c.add("cast", width, signed, (self.expression(e.operand, depth + 1),))
        if kind == "UnaryOp" and e.op.name in UNARY:
            return self.c.add(e.op.name, width, signed, (self.expression(e.operand, depth + 1),))
        if kind == "BinaryOp" and e.op.name in BINARY:
            args = (self.expression(e.left, depth + 1), self.expression(e.right, depth + 1))
            if "Shift" not in e.op.name and e.op.name not in {"LogicalAnd", "LogicalOr"}:
                if self.c.nodes[args[0]].width != self.c.nodes[args[1]].width:
                    raise SequentialError("frontend returned unequal operand widths")
            return self.c.add(e.op.name, width, signed, args)
        if kind == "ConditionalOp":
            if len(e.conditions) != 1 or e.conditions[0].pattern is not None:
                raise SequentialError(
                    "pattern or multiple conditional expression guards are unsupported"
                )
            args = tuple(
                self.expression(x, depth + 1) for x in (e.conditions[0].expr, e.left, e.right)
            )
            return self.c.add("mux", width, signed, args)
        if kind == "Concatenation":
            args = tuple(self.expression(x, depth + 1) for x in e.operands)
            if not args or sum(self.c.nodes[a].width for a in args) != width:
                raise SequentialError("invalid concatenation widths")
            return self.c.add("concat", width, signed, args)
        if kind in {"ElementSelect", "RangeSelect"}:
            src = self.expression(e.value, depth + 1)
            if kind == "ElementSelect":
                if e.selector.constant is None:
                    raise SequentialError("dynamic bit selects are unsupported")
                lo = hi = _integer(e.selector.constant)
            else:
                if (
                    e.selectionKind.name != "Simple"
                    or e.left.constant is None
                    or e.right.constant is None
                ):
                    raise SequentialError("only constant descending part selects are supported")
                hi, lo = _integer(e.left.constant), _integer(e.right.constant)
            bounds = e.value.type.getBitVectorRange()
            if bounds.left < bounds.right:
                raise SequentialError("ascending selection sources are unsupported")
            hi, lo = hi - bounds.right, lo - bounds.right
            if lo < 0 or hi < lo or hi >= self.c.nodes[src].width or hi - lo + 1 != width:
                raise SequentialError("out-of-range or ascending select")
            return self.c.add("slice", width, signed, (src,), value=lo)
        raise SequentialError(f"unsupported RTL expression: {kind}")

    def assignment(self, e: Any, *, sequential: bool) -> tuple[str, int]:
        if e.kind.name != "Assignment" or e.isCompound or e.timingControl is not None:
            raise SequentialError("only simple untimed whole-signal assignments are supported")
        if bool(e.isNonBlocking) != sequential or e.left.kind.name != "NamedValue":
            raise SequentialError(
                "clocked assignments must be nonblocking and target a whole signal"
            )
        name = str(e.left.symbol.name)
        if name in self.c.inputs or name == self.c.clock or name not in self.c.signals:
            raise SequentialError(f"invalid driven signal: {name}")
        root = self.expression(e.right)
        if self.c.nodes[root].width != self.c.signals[name][0]:
            raise SequentialError("assignment width was not elaborated consistently")
        self.c.locations[name] = self.location(e)
        return name, root

    def statement(self, stmt: Any, pending: dict[str, int], depth: int = 0) -> dict[str, int]:
        self.bounded(depth)
        kind = stmt.kind.name
        if kind == "Empty":
            return pending
        if kind == "Block":
            if stmt.blockKind.name != "Sequential":
                raise SequentialError("parallel statement blocks are unsupported")
            return self.statement(stmt.body, pending, depth + 1)
        if kind == "List":
            for item in stmt.list:
                pending = self.statement(item, pending, depth + 1)
            return pending
        if kind == "ExpressionStatement":
            name, root = self.assignment(stmt.expr, sequential=True)
            return {**pending, name: root}
        if kind == "Conditional":
            if (
                len(stmt.conditions) != 1
                or stmt.conditions[0].pattern is not None
                or stmt.check.name != "None_"
            ):
                raise SequentialError("only ordinary if/else guards are supported")
            cond = self.expression(stmt.conditions[0].expr)
            yes = self.statement(stmt.ifTrue, dict(pending), depth + 1)
            no = (
                self.statement(stmt.ifFalse, dict(pending), depth + 1)
                if stmt.ifFalse is not None
                else dict(pending)
            )
            out = dict(pending)
            for name in sorted(set(yes) | set(no)):
                a = yes.get(name)
                b = no.get(name)
                a = self.c.ref(name) if a is None else a
                b = self.c.ref(name) if b is None else b
                width, signed = self.c.signals[name]
                out[name] = a if a == b else self.c.add("mux", width, signed, (cond, a, b))
            return out
        raise SequentialError(f"unsupported clocked statement: {kind}")

    def add_driver(self, name: str, root: int, *, sequential: bool) -> None:
        if name in self.c.combinational or name in self.c.next_state:
            raise SequentialError(f"multiple drivers for {name}")
        target = self.c.next_state if sequential else self.c.combinational
        target[name] = root


def load_circuit(files: list[str], *, root: Path, top: str, clock: str) -> Circuit:
    """Load exact source bytes and lower the selected flat module, or fail."""
    s = importlib.import_module("pyslang")
    circuit = Circuit(top, clock, frontend=str(s.__version__))
    manager = s.SourceManager()
    options = s.ast.CompilationOptions()
    options.topModules = {top}
    compilation = s.ast.Compilation(s.Bag([options]))
    total = 0
    resolved: set[Path] = set()
    for filename in files:
        path = (root / filename).resolve()
        if not path.is_relative_to(root.resolve()) or path in resolved:
            raise SequentialError(
                "source paths must be distinct and remain inside the request directory"
            )
        resolved.add(path)
        with path.open("rb") as stream:
            data = stream.read(1048576 + 1)
        total += len(data)
        if len(data) > 1048576 or total > 4194304:
            raise SequentialError("source byte limit exceeded (1 MiB/file, 4 MiB total)")
        text = data.decode("utf-8")
        if "`" in text:
            raise SequentialError(
                "preprocessor directives/macros are unsupported in sequential input"
            )
        circuit.sources.append(
            {"path": filename, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        )
        compilation.addSyntaxTree(s.syntax.SyntaxTree.fromText(text, manager, filename))
    design = compilation.getRoot()
    errors = [d for d in compilation.getAllDiagnostics() if d.isError()]
    if errors:
        engine = s.DiagnosticEngine(manager)
        raise SequentialError(
            "RTL elaboration failed: " + "; ".join(engine.formatMessage(d) for d in errors[:5])
        )
    if len(design.topInstances) != 1 or design.topInstances[0].name != top:
        raise SequentialError("selected top must elaborate to exactly one module")
    body = design.topInstances[0].body
    lower = _Lowerer(circuit, manager)
    members = list(body)
    if len(members) > 4096:
        raise SequentialError("top module exceeds 4096 members")
    for m in members:
        kind = m.kind.name
        if kind in {"Variable", "Net"}:
            if not IDENTIFIER.fullmatch(m.name):
                raise SequentialError("escaped/non-ASCII signal names are unsupported")
            circuit.signals[m.name] = lower.shape(m.type, declaration=True)
            circuit.locations[m.name] = lower.location(m)
            if m.initializer is not None:
                raise SequentialError(
                    "declaration initializers are unsupported; use explicit assignments/reset"
                )
            if kind == "Net" and (
                m.netType.netKind.name != "Wire"
                or m.delay is not None
                or any(x is not None for x in m.driveStrength)
            ):
                raise SequentialError("only undelayed unstrengthened wire nets are supported")
        elif kind not in {
            "Port",
            "Parameter",
            "ContinuousAssign",
            "ProceduralBlock",
            "EmptyMember",
        }:
            raise SequentialError(f"unsupported module member: {kind}; flatten hierarchy first")
    for port in body.portList:
        if port.internalSymbol is None or port.name != port.internalSymbol.name:
            raise SequentialError("complex port expressions are unsupported")
        if port.direction.name == "In":
            circuit.inputs.append(port.name)
        elif port.direction.name == "Out":
            circuit.outputs.append(port.name)
        else:
            raise SequentialError("inout/ref ports are unsupported")
    if clock not in circuit.inputs or circuit.signals[clock][0] != 1:
        raise SequentialError("clock must be a scalar input")
    circuit.inputs.remove(clock)
    for m in members:
        if m.kind.name == "ContinuousAssign":
            if m.delay is not None or any(x is not None for x in m.driveStrength):
                raise SequentialError("continuous assignment delays/strengths are unsupported")
            name, expr = lower.assignment(m.assignment, sequential=False)
            lower.add_driver(name, expr, sequential=False)
        if m.kind.name == "ProceduralBlock":
            if m.procedureKind.name not in {"AlwaysFF", "Always"} or m.body.kind.name != "Timed":
                raise SequentialError(
                    "only positive-edge clocked always/always_ff blocks are supported"
                )
            event = m.body.timing
            if (
                event.kind.name != "SignalEvent"
                or event.edge.name != "PosEdge"
                or event.iffCondition is not None
                or event.expr.kind.name != "NamedValue"
                or event.expr.symbol.name != clock
            ):
                raise SequentialError(
                    "multiple clocks, asynchronous resets, gated clocks, "
                    "and event qualifiers are unsupported"
                )
            pending = lower.statement(m.body.stmt, {})
            for name, expr in sorted(pending.items()):
                lower.add_driver(name, expr, sequential=True)
    circuit.finalize()
    return circuit

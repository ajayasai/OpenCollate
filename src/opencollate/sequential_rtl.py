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
# Top-relative elaborated names, including constant generate/instance indices.
SIGNAL_PATH_PATTERN = (
    r"[A-Za-z_][A-Za-z0-9_$]*(?:\[-?(?:0|[1-9][0-9]*)\])*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_$]*(?:\[-?(?:0|[1-9][0-9]*)\])*)*"
)
SIGNAL_PATH = re.compile(SIGNAL_PATH_PATTERN + r"\Z", re.ASCII)
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


def _constant(expression: Any) -> int:
    # Implicit instance-array selectors can be literal AST nodes whose cached
    # .constant has not been populated. Never reinterpret a dynamic expression.
    if expression.constant is not None:
        return _integer(expression.constant)
    if expression.kind.name in {"IntegerLiteral", "UnbasedUnsizedIntegerLiteral"}:
        return _integer(expression.value)
    if expression.kind.name == "NamedValue" and expression.symbol.kind.name == "Parameter":
        return _integer(expression.symbol.value)
    raise SequentialError("dynamic bit/part selects are unsupported")


class _Lowerer:
    def __init__(self, circuit: Circuit, source_manager: Any) -> None:
        self.c = circuit
        self.sm = source_manager
        self.work = 0
        self.clock_aliases = {circuit.clock}
        self.blocking_environment: dict[str, int] | None = None
        self.blocking_writes: set[str] = set()

    def bounded(self, depth: int) -> None:
        self.work += 1
        if depth > 64 or self.work > 100000:
            raise SequentialError("RTL expression/statement depth or work limit exceeded")

    def shape(self, dtype: Any, *, declaration: bool = False) -> tuple[int, bool]:
        storage = dtype.canonicalType
        if storage.isEnum:
            storage = storage.baseType
        if not storage.isSimpleBitVector or not 1 <= int(dtype.bitWidth) <= 256:
            raise SequentialError(
                "only scalar or simple packed bit-vectors of 1..256 bits are supported"
            )
        if declaration and storage.isPackedArray:
            r = storage.getBitVectorRange()
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

    def symbol_name(self, symbol: Any) -> str:
        """Use the elaborated instance, never a shared definition/local spelling."""
        prefix = self.c.top + "."
        path = str(symbol.hierarchicalPath)
        name = path[len(prefix) :] if path.startswith(prefix) else ""
        if (
            not IDENTIFIER.fullmatch(symbol.name)
            or not SIGNAL_PATH.fullmatch(name)
            or len(name) > 256
        ):
            raise SequentialError("signal must have a bounded, top-relative ASCII path")
        return name

    def reference(self, symbol: Any) -> int:
        name = self.symbol_name(symbol)
        if name in self.clock_aliases:
            raise SequentialError(f"clock-as-data is unsupported: {name}")
        if self.blocking_environment is not None and name in self.blocking_writes:
            if name not in self.blocking_environment:
                raise SequentialError(f"combinational read before definite assignment: {name}")
            return self.blocking_environment[name]
        return self.c.ref(name)

    def port_output(self, expr: Any, source: str, depth: int = 0) -> int:
        """Substitute the child's value in slang's typed output conversion."""
        self.bounded(depth)
        width, signed = self.shape(expr.type)
        if expr.kind.name == "EmptyArgument":
            if (width, signed) != self.c.signals[source]:
                raise SequentialError("output port placeholder has inconsistent type")
            if source in self.clock_aliases:
                raise SequentialError(f"clock-as-data is unsupported: {source}")
            return self.c.ref(source)
        if expr.kind.name == "Conversion":
            root = self.port_output(expr.operand, source, depth + 1)
            return self.c.add("cast", width, signed, (root,))
        raise SequentialError("unsupported elaborated output port conversion")

    def expression(self, e: Any, depth: int = 0) -> int:
        self.bounded(depth)
        width, signed = self.shape(e.type)
        args: tuple[int, ...]
        kind = e.kind.name
        if kind in {"IntegerLiteral", "UnbasedUnsizedIntegerLiteral"}:
            return self.c.add("const", width, signed, value=_integer(e.value) & ((1 << width) - 1))
        if kind in {"NamedValue", "HierarchicalValue"}:
            if e.symbol.kind.name in {"Parameter", "EnumValue"}:
                return self.c.add(
                    "const", width, signed, value=_integer(e.symbol.value) & ((1 << width) - 1)
                )
            return self.reference(e.symbol)
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
                lo = hi = _constant(e.selector)
            else:
                if e.selectionKind.name != "Simple":
                    raise SequentialError("only constant descending part selects are supported")
                hi, lo = _constant(e.left), _constant(e.right)
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
        if bool(e.isNonBlocking) != sequential or e.left.kind.name not in {
            "NamedValue",
            "HierarchicalValue",
        }:
            raise SequentialError(
                "clocked assignments must be nonblocking and target a whole signal"
            )
        name = self.symbol_name(e.left.symbol)
        if name in self.c.inputs or name in self.clock_aliases or name not in self.c.signals:
            raise SequentialError(f"invalid driven signal: {name}")
        root = self.expression(e.right)
        if self.c.nodes[root].width != self.c.signals[name][0]:
            raise SequentialError("assignment width was not elaborated consistently")
        self.c.locations[name] = self.location(e)
        return name, root

    def statement(self, stmt: Any, pending: dict[str, int], depth: int = 0) -> dict[str, int]:
        from opencollate.sequential_procedural import Procedure

        self.bounded(depth)
        return Procedure(self, sequential=True).run(stmt, pending)

    def add_driver(self, name: str, root: int, *, sequential: bool) -> None:
        if name in self.c.combinational or name in self.c.next_state:
            raise SequentialError(f"multiple drivers for {name}")
        target = self.c.next_state if sequential else self.c.combinational
        target[name] = root


def load_circuit(files: list[str], *, root: Path, top: str, clock: str) -> Circuit:
    """Load exact source bytes and lower the selected elaborated hierarchy, or fail."""
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
    from opencollate.sequential_hierarchy import lower_hierarchy

    lower_hierarchy(design.topInstances[0], _Lowerer(circuit, manager))
    circuit.finalize()
    return circuit

"""Bounded elaborated hierarchy lowering, with explicit data and clock wiring.

Instance bodies are never replaced with cached canonical bodies: those can have
identical definitions but different instance-relative symbols. No HDL is emitted
or reparsed, and no unsupported instantiated logic is black-boxed.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opencollate.sequential_ir import SequentialError

if TYPE_CHECKING:
    from opencollate.sequential_rtl import _Lowerer


@dataclass
class _Binding:
    target: str
    expression: Any
    output_source: str | None = None
    low: int = 0
    width: int = 0
    rhs_low: int = 0


def _collect(top: Any, lower: _Lowerer) -> tuple[list[Any], list[Any]]:
    members: list[Any] = []
    instances: list[Any] = []
    pending = [(top, 0)]
    scopes = 0
    while pending:
        obj, depth = pending.pop()
        scopes += 1
        if depth > 32 or scopes > 4096:
            raise SequentialError("hierarchy exceeds 32 levels or 4096 scopes")
        kind = obj.kind.name
        if kind == "GenerateBlock":
            # Elaborated inactive branches have no hardware semantics.
            if obj.isUninstantiated:
                continue
            children = list(obj)
        elif kind == "GenerateBlockArray":
            if not obj.valid:
                raise SequentialError("invalid elaborated generate array")
            pending.extend((x, depth + 1) for x in reversed(obj.entries))
            continue
        elif kind == "InstanceArray":
            pending.extend((x, depth + 1) for x in reversed(obj.elements))
            continue
        elif kind == "Instance":
            if not obj.isModule:
                raise SequentialError("only module instances are supported")
            instances.append(obj)
            if len(instances) > 1024:
                raise SequentialError("hierarchy exceeds 1024 instances")
            children = list(obj.body)
            lower.c.hierarchy.append(
                {
                    "path": str(obj.hierarchicalPath),
                    "module": str(obj.definition.name),
                    "location": lower.location(obj),
                }
            )
        else:
            raise SequentialError(f"unsupported hierarchy scope: {kind}")
        for m in children:
            k = m.kind.name
            if k in {"Instance", "InstanceArray", "GenerateBlock", "GenerateBlockArray"}:
                pending.append((m, depth + 1))
            else:
                members.append(m)
                if len(members) > 16384:
                    raise SequentialError("elaborated hierarchy exceeds 16384 members")
    # Canonical order is independent of frontend scope traversal order.
    instances.sort(key=lambda x: str(x.hierarchicalPath))
    lower.c.hierarchy.sort(key=lambda x: x["path"])
    return members, instances


def _ports(instance: Any, lower: _Lowerer) -> list[Any]:
    ports = list(instance.body.portList)
    for p in ports:
        if (
            p.kind.name != "Port"
            or p.internalSymbol is None
            or p.name != p.internalSymbol.name
            or p.internalExpr is not None
            or p.initializer is not None
        ):
            raise SequentialError("interface, defaulted, or complex ports are unsupported")
        lower.symbol_name(p.internalSymbol)
        if p.direction.name not in {"In", "Out"}:
            raise SequentialError("inout/ref ports are unsupported")
    return ports


def _destinations(
    expr: Any, lower: _Lowerer, offset: int = 0, depth: int = 0
) -> list[tuple[str, int, int, int]]:
    """Static, nonoverlapping lvalue slices; last concat item is least significant."""
    from opencollate.sequential_rtl import _constant

    lower.bounded(depth)
    width, _ = lower.shape(expr.type)
    if expr.kind.name == "Concatenation":
        result: list[tuple[str, int, int, int]] = []
        used = 0
        for item in reversed(expr.operands):
            result.extend(_destinations(item, lower, offset + used, depth + 1))
            used += lower.shape(item.type)[0]
        if used != width:
            raise SequentialError("inconsistent concatenated output widths")
        return result
    base, low = expr, 0
    if expr.kind.name in {"ElementSelect", "RangeSelect"}:
        base = expr.value
        if expr.kind.name == "ElementSelect":
            low = _constant(expr.selector)
        else:
            if expr.selectionKind.name != "Simple":
                raise SequentialError("only static descending output part selects are supported")
            low, high = _constant(expr.right), _constant(expr.left)
            if high - low + 1 != width:
                raise SequentialError("inconsistent output slice width")
    if base.kind.name not in {"NamedValue", "HierarchicalValue"}:
        raise SequentialError("outputs must target signals or constant slices/concatenations")
    name = lower.symbol_name(base.symbol)
    if name not in lower.c.signals or name in lower.c.inputs or name == lower.c.clock:
        raise SequentialError(f"invalid driven signal: {name}")
    if low < 0 or low + width > lower.c.signals[name][0]:
        raise SequentialError("out-of-range output slice")
    return [(name, low, width, offset)]


def _bindings(left: Any, right: Any, lower: _Lowerer, source: str | None = None) -> list[_Binding]:
    if lower.shape(left.type)[0] != lower.shape(right.type)[0]:
        raise SequentialError("frontend output assignment has inconsistent width")
    return [_Binding(n, right, source, lo, w, off) for n, lo, w, off in _destinations(left, lower)]


def _merge_fragments(fragments: dict[str, list[tuple[int, int, int]]], lower: _Lowerer) -> None:
    for name, parts in sorted(fragments.items()):
        position, roots = 0, []
        for low, width, root in sorted(parts):
            if low != position:
                raise SequentialError(f"partially undriven combinational signal: {name}")
            position += width
            roots.append(root)
        width, signed = lower.c.signals[name]
        if position != width:
            raise SequentialError(f"partially undriven combinational signal: {name}")
        root = (
            roots[0]
            if len(roots) == 1
            else lower.c.add("concat", width, signed, tuple(reversed(roots)))
        )
        lower.add_driver(name, root, sequential=False)


def _alias(b: _Binding, lower: _Lowerer) -> str | None:
    """Recognize only literal one-bit wires, not computed/gated clocks."""
    if lower.c.signals[b.target][0] != 1:
        return None
    e = b.expression
    depth = 0
    while e.kind.name == "Conversion":
        depth += 1
        if depth > 64 or lower.shape(e.type)[0] != 1:
            return None
        e = e.operand
    if lower.shape(e.type)[0] != 1:
        return None
    if b.output_source is not None:
        return b.output_source if e.kind.name == "EmptyArgument" else None
    if e.kind.name in {"NamedValue", "HierarchicalValue"}:
        if e.symbol.kind.name in {"Variable", "Net"}:
            return lower.symbol_name(e.symbol)
    return None


def lower_hierarchy(top: Any, lower: _Lowerer) -> None:
    c = lower.c
    members, instances = _collect(top, lower)
    for m in members:
        kind = m.kind.name
        if kind in {"Variable", "Net"}:
            name = lower.symbol_name(m)
            if name in c.signals:
                raise SequentialError(f"duplicate elaborated signal identity: {name}")
            c.signals[name] = lower.shape(m.type, declaration=True)
            c.locations[name] = lower.location(m)
            if m.initializer is not None:
                raise SequentialError("declaration initializers are unsupported")
            if kind == "Net" and (
                m.netType.netKind.name != "Wire"
                or m.delay is not None
                or any(x is not None for x in m.driveStrength)
            ):
                raise SequentialError("only undelayed unstrengthened wire nets are supported")
        elif kind == "TypeAlias":
            # Packed scalar/vector aliases and integral enums add no runtime logic.
            lower.shape(m, declaration=True)
        elif kind == "TransparentMember" and m.wrapped.kind.name == "EnumValue":
            from opencollate.sequential_rtl import _integer

            lower.shape(m.wrapped.type, declaration=True)
            _integer(m.wrapped.value)
        elif kind not in {
            "Port",
            "Parameter",
            "Genvar",
            "ContinuousAssign",
            "ProceduralBlock",
            "EmptyMember",
        }:
            raise SequentialError(f"unsupported instantiated module member: {kind}")
    for port in _ports(top, lower):
        (c.inputs if port.direction.name == "In" else c.outputs).append(port.name)
    if c.clock not in c.inputs or c.signals[c.clock][0] != 1:
        raise SequentialError("clock must be a scalar input")
    c.inputs.remove(c.clock)

    bindings: list[_Binding] = []
    for inst in instances:
        if inst is top:
            continue
        for port in _ports(inst, lower):
            source = lower.symbol_name(port.internalSymbol)
            connection = inst.getPortConnection(port)
            expr = connection.expression if connection is not None else None
            if port.direction.name == "In":
                if expr is None or expr.kind.name == "EmptyArgument":
                    raise SequentialError(f"unconnected child input: {source}")
                bindings.append(_Binding(source, expr))
            elif expr is not None:
                if (
                    expr.kind.name != "Assignment"
                    or expr.isCompound
                    or expr.isNonBlocking
                    or expr.timingControl is not None
                ):
                    raise SequentialError("unsupported output port connection")
                bindings.extend(_bindings(expr.left, expr.right, lower, source))
    for m in members:
        if m.kind.name == "ContinuousAssign":
            e = m.assignment
            if (
                m.delay is not None
                or any(x is not None for x in m.driveStrength)
                or e.kind.name != "Assignment"
                or e.isCompound
                or e.isNonBlocking
                or e.timingControl is not None
            ):
                raise SequentialError("unsupported continuous assignment")
            bindings.extend(_bindings(e.left, e.right, lower))

    # Validate driver counts before removing clock-only aliases from data logic.
    masks: dict[str, int] = {}
    for b in bindings:
        if not b.width:
            b.width = c.signals[b.target][0]
        mask = ((1 << b.width) - 1) << b.low
        if masks.get(b.target, 0) & mask:
            raise SequentialError(f"multiple combinational/port drivers: {b.target}")
        masks[b.target] = masks.get(b.target, 0) | mask
    successors: dict[str, list[str]] = defaultdict(list)
    for b in bindings:
        alias_source = _alias(b, lower)
        if alias_source is not None:
            successors[alias_source].append(b.target)
    queue = deque([c.clock])
    while queue:
        for target in successors[queue.popleft()]:
            if target not in lower.clock_aliases:
                lower.clock_aliases.add(target)
                queue.append(target)
    if set(c.outputs) & lower.clock_aliases:
        raise SequentialError("clock-as-data output is unsupported")
    c.clock_aliases = sorted(lower.clock_aliases)
    fragments: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for b in bindings:
        if b.target in lower.clock_aliases:
            continue
        expr = (
            lower.port_output(b.expression, b.output_source)
            if b.output_source is not None
            else lower.expression(b.expression)
        )
        if b.rhs_low + b.width > c.nodes[expr].width:
            raise SequentialError("elaborated port/assignment width mismatch")
        if b.rhs_low or b.width != c.nodes[expr].width:
            expr = c.add("slice", b.width, False, (expr,), value=b.rhs_low)
        fragments[b.target].append((b.low, b.width, expr))
    _merge_fragments(fragments, lower)

    for m in members:
        if m.kind.name != "ProceduralBlock":
            continue
        if m.procedureKind.name == "AlwaysComb":
            from opencollate.sequential_procedural import Procedure

            for name, expr in sorted(Procedure(lower, sequential=False).run(m.body).items()):
                lower.add_driver(name, expr, sequential=False)
            continue
        if m.procedureKind.name not in {"AlwaysFF", "Always"} or m.body.kind.name != "Timed":
            raise SequentialError(
                "only positive-edge clocked always/always_ff blocks are supported"
            )
        event = m.body.timing
        if (
            event.kind.name != "SignalEvent"
            or event.edge.name != "PosEdge"
            or event.iffCondition is not None
            or event.expr.kind.name not in {"NamedValue", "HierarchicalValue"}
            or lower.symbol_name(event.expr.symbol) not in lower.clock_aliases
        ):
            raise SequentialError("multiple/gated clocks, asynchronous resets, or event qualifiers")
        for name, expr in sorted(lower.statement(m.body.stmt, {}).items()):
            lower.add_driver(name, expr, sequential=True)

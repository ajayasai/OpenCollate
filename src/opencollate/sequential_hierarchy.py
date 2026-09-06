"""Flatten the elaborated, selected hierarchy without treating cells as black boxes.

Every active member and driver is checked before property-specific reduction.
Names come from the frontend's resolved symbols, not textual replacement. Clock
forwarding is restricted to direct scalar input-port connections.
"""

from __future__ import annotations

import re
from typing import Any

from opencollate.sequential_ir import Circuit, SequentialError
from opencollate.sequential_rtl import IDENTIFIER, _Lowerer

# Bracketed components are elaborated generate indices, never dynamic selects.
SIGNAL_PATH = re.compile(
    r"[A-Za-z_][A-Za-z0-9_$]*(?:\[-?[0-9]+\])?"
    r"(?:\.[A-Za-z_][A-Za-z0-9_$]*(?:\[-?[0-9]+\])?)*\Z",
    re.ASCII,
)
MAX_MEMBERS = 16384
MAX_INSTANCES = 1024
MAX_DEPTH = 32


class _Hierarchy:
    def __init__(self, circuit: Circuit, manager: Any) -> None:
        self.c = circuit
        self.lower = _Lowerer(circuit, manager)
        self.members: list[Any] = []
        self.instances: list[Any] = []
        self.visited = 0

    def walk(self, scope: Any, depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            raise SequentialError("hierarchy exceeds 32 scopes")
        for member in scope:
            self.visited += 1
            if self.visited > MAX_MEMBERS:
                raise SequentialError("elaborated hierarchy exceeds 16384 members")
            kind = member.kind.name
            if kind == "Instance":
                if not member.isModule or not IDENTIFIER.fullmatch(member.name):
                    raise SequentialError("only simply named module instances are supported")
                if len(self.instances) >= MAX_INSTANCES:
                    raise SequentialError("hierarchy exceeds 1024 module instances")
                self.instances.append(member)
                self.walk(member.body, depth + 1)
            elif kind == "GenerateBlock":
                if not member.isUninstantiated:
                    self.walk(member, depth + 1)
            elif kind == "GenerateBlockArray":
                if not member.isUninstantiated:
                    if not member.valid or len(member.entries) > MAX_MEMBERS - self.visited:
                        raise SequentialError("invalid or oversized generate block array")
                    for entry in member.entries:
                        if entry.isUninstantiated:
                            continue
                        self.visited += 1
                        self.walk(entry, depth + 1)
            elif kind in {"Variable", "Net"}:
                self.declare(member)
            elif kind in {"ContinuousAssign", "ProceduralBlock"}:
                self.members.append(member)
            elif kind not in {"Port", "Parameter", "Genvar", "EmptyMember"}:
                raise SequentialError(f"unsupported elaborated module member: {kind}")

    def declare(self, member: Any) -> None:
        name = self.lower.signal_name(member)
        if (
            not IDENTIFIER.fullmatch(member.name)
            or not SIGNAL_PATH.fullmatch(name)
            or len(name) > 1024
            or name in self.c.signals
        ):
            raise SequentialError("ambiguous, escaped, or oversized signal path: " + name)
        self.c.signals[name] = self.lower.shape(member.type, declaration=True)
        self.c.locations[name] = self.lower.location(member)
        if member.initializer is not None:
            raise SequentialError(
                "declaration initializers are unsupported; use explicit assignments/reset"
            )
        if member.kind.name == "Net" and (
            member.netType.netKind.name != "Wire"
            or member.delay is not None
            or any(x is not None for x in member.driveStrength)
        ):
            raise SequentialError("only undelayed unstrengthened wire nets are supported")

    def port_name(self, port: Any) -> str:
        if (
            port.kind.name != "Port"
            or port.internalSymbol is None
            or port.name != port.internalSymbol.name
        ):
            raise SequentialError("complex/interface port expressions are unsupported")
        name = self.lower.signal_name(port.internalSymbol)
        if name not in self.c.signals:
            raise SequentialError("port has no supported internal signal: " + name)
        return name

    def direct_name(self, expr: Any) -> str | None:
        if expr is None:
            return None
        # Scalar signedness-only conversions cannot change a clock's bit pattern.
        while expr.kind.name == "Conversion":
            if int(expr.type.bitWidth) != 1 or int(expr.operand.type.bitWidth) != 1:
                return None
            expr = expr.operand
        if expr.kind.name in {"NamedValue", "HierarchicalValue"}:
            return self.lower.signal_name(expr.symbol)
        return None

    def ports(self, top: Any) -> None:
        for port in top.body.portList:
            name = self.port_name(port)
            if port.direction.name == "In":
                self.c.inputs.append(name)
                self.lower.input_ports.add(name)
            elif port.direction.name == "Out":
                self.c.outputs.append(name)
            else:
                raise SequentialError("inout/ref ports are unsupported")
        if self.c.clock not in self.c.inputs or self.c.signals[self.c.clock][0] != 1:
            raise SequentialError("clock must be a scalar input")
        self.c.inputs.remove(self.c.clock)
        # Instances are in parent-before-child order, so clock identity propagates
        # through arbitrary depths without equating data signals or gated clocks.
        for instance in self.instances:
            connections = list(instance.portConnections)
            if len(connections) != len(instance.body.portList):
                raise SequentialError("incomplete elaborated port connection inventory")
            for conn in connections:
                name = self.port_name(conn.port)
                direction = conn.port.direction.name
                if direction == "In":
                    self.lower.input_ports.add(name)
                    if conn.expression is None:
                        raise SequentialError("unconnected child input: " + name)
                    direct = self.direct_name(conn.expression)
                    if direct in self.lower.clock_aliases:
                        if self.c.signals[name][0] != 1:
                            raise SequentialError("clock connections must remain scalar")
                        self.lower.clock_aliases.add(name)
                elif direction == "Out":
                    # Even an intentionally open output must have an understood
                    # internal driver; reduction never licenses a missing model.
                    self.c.outputs.append(name)
                else:
                    raise SequentialError("inout/ref child ports are unsupported")
        for alias in sorted(self.lower.clock_aliases - {self.c.clock}):
            del self.c.signals[alias]
            del self.c.locations[alias]

    def connect(self) -> None:
        for instance in self.instances:
            for conn in instance.portConnections:
                name = self.lower.signal_name(conn.port.internalSymbol)
                if name in self.lower.clock_aliases:
                    continue
                if conn.port.direction.name == "In":
                    value = self.lower.expression(conn.expression)
                    if self.c.nodes[value].width != self.c.signals[name][0]:
                        raise SequentialError("input port width was not elaborated consistently")
                    self.lower.add_driver(name, value, sequential=False)
                elif conn.expression is not None:
                    # Slang exposes output connections as an assignment whose
                    # EmptyArgument is the child output; conversions belong to
                    # the frontend and must not be guessed from declared widths.
                    self.lower.port_value = self.c.ref(name)
                    try:
                        target, value = self.lower.assignment(conn.expression, sequential=False)
                    finally:
                        self.lower.port_value = None
                    self.lower.add_driver(target, value, sequential=False)

    def drivers(self) -> None:
        for member in self.members:
            if member.kind.name == "ContinuousAssign":
                if member.delay is not None or any(x is not None for x in member.driveStrength):
                    raise SequentialError("continuous assignment delays/strengths are unsupported")
                name, expr = self.lower.assignment(member.assignment, sequential=False)
                self.lower.add_driver(name, expr, sequential=False)
                continue
            if (
                member.procedureKind.name not in {"AlwaysFF", "Always"}
                or member.body.kind.name != "Timed"
            ):
                raise SequentialError(
                    "only positive-edge clocked always/always_ff blocks supported"
                )
            event = member.body.timing
            if (
                event.kind.name != "SignalEvent"
                or event.edge.name != "PosEdge"
                or event.iffCondition is not None
                or event.expr.kind.name not in {"NamedValue", "HierarchicalValue"}
                or self.direct_name(event.expr) not in self.lower.clock_aliases
            ):
                raise SequentialError(
                    "multiple clocks, asynchronous resets, gated clocks, "
                    "and event qualifiers are unsupported"
                )
            pending = self.lower.statement(member.body.stmt, {})
            for name, expr in sorted(pending.items()):
                self.lower.add_driver(name, expr, sequential=True)


def lower_design(circuit: Circuit, top: Any, manager: Any) -> None:
    """Validate and lower every active scope in one selected clock domain."""
    if not top.isModule:
        raise SequentialError("selected top must be a module")
    hierarchy = _Hierarchy(circuit, manager)
    hierarchy.walk(top.body)
    hierarchy.ports(top)
    hierarchy.connect()
    hierarchy.drivers()
    circuit.finalize()

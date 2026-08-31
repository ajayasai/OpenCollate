"""Secure, namespace-tolerant IEEE 1685 IP-XACT component importer.

The parser intentionally does not perform XSD validation or fetch schemas from
the network.  It extracts the consistency facts OpenCollate can reason about,
retains richer address/interface metadata in attributes, and marks every fact
that cannot be resolved.  XML DTDs and entity declarations are rejected.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from itertools import product
from pathlib import Path
from typing import Any, NoReturn
from xml.parsers import expat

from opencollate.diagnostics import Diagnostic, Severity
from opencollate.model import (
    BusShape,
    ClockObservation,
    ComponentKind,
    ComponentObservation,
    DesignObjectObservation,
    Direction,
    FactState,
    IndexRange,
    InterfaceObservation,
    PortObservation,
    PortRole,
    Provenance,
    RegisterFieldObservation,
    RegisterObservation,
    ViewId,
    ViewObservation,
)
from opencollate.parsers.base import (
    Pathish,
    coerce_paths,
    coerce_view,
    infer_role_from_name,
    parser_diagnostic,
    read_source,
)

_MAX_XML_CHARACTERS = 64 * 1024 * 1024
_MAX_XML_DEPTH = 256
_MAX_XML_ELEMENTS = 1_000_000
_MAX_EXPRESSION_NODES = 256
_MAX_EXPRESSION_DEPTH = 64
_MAX_PARAMETER_DEPTH = 64
_MAX_INTEGER_BITS = 4096
_MAX_REGISTER_ARRAY_ELEMENTS = 65_536
_MAX_SELECTION_COLLISION_BITS = 1_000_000

_NAMESPACE_RE = re.compile(
    r"^https?://(?:www\.)?(?:"
    r"spiritconsortium\.org/XMLSchema/SPIRIT|"
    r"accellera\.org/XMLSchema/(?:IPXACT|SPIRIT)"
    r")/1685-(2009|2014|2022)/?$",
    re.IGNORECASE,
)
_MODE_NAMES = {
    "initiator",
    "target",
    "master",
    "slave",
    "system",
    "mirroredInitiator",
    "mirroredTarget",
    "mirroredMaster",
    "mirroredSlave",
    "mirroredSystem",
    "monitor",
}


def _split_qname(name: str) -> tuple[str, str]:
    namespace, separator, local = name.rpartition("|")
    return (namespace, local) if separator else ("", name)


@dataclass(slots=True)
class _XmlNode:
    qname: str
    attributes: dict[str, str]
    line: int
    column: int
    parent: _XmlNode | None = None
    children: list[_XmlNode] = dataclass_field(default_factory=list)
    text_parts: list[str] = dataclass_field(default_factory=list)

    @property
    def namespace(self) -> str:
        return _split_qname(self.qname)[0]

    @property
    def local_name(self) -> str:
        return _split_qname(self.qname)[1]

    @property
    def text(self) -> str:
        return "".join(self.text_parts).strip()

    def child(self, name: str) -> _XmlNode | None:
        return next(iter(self.children_named(name)), None)

    def children_named(self, name: str) -> tuple[_XmlNode, ...]:
        return tuple(
            child
            for child in self.children
            if child.namespace == self.namespace and child.local_name == name
        )

    def descendants_named(self, name: str) -> tuple[_XmlNode, ...]:
        found: list[_XmlNode] = []
        for child in self.children:
            if child.namespace != self.namespace:
                continue
            if child.local_name == name:
                found.append(child)
            found.extend(child.descendants_named(name))
        return tuple(found)

    def attribute(self, name: str) -> str | None:
        for raw_name, value in self.attributes.items():
            if _split_qname(raw_name)[1] == name:
                return value.strip()
        return None


class _XmlSafetyError(ValueError):
    pass


def _raise_xml_safety(message: str) -> NoReturn:
    raise _XmlSafetyError(message)


def _parse_xml(
    text: str,
    path: Path,
    view: ViewId,
) -> tuple[_XmlNode | None, tuple[Diagnostic, ...]]:
    if len(text) > _MAX_XML_CHARACTERS:
        return None, (
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                f"IP-XACT XML exceeds {_MAX_XML_CHARACTERS} characters",
                location=Provenance(str(path), view=view),
            ),
        )

    parser = expat.ParserCreate(namespace_separator="|")
    parser.buffer_text = True
    stack: list[_XmlNode] = []
    roots: list[_XmlNode] = []
    element_count = 0

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal element_count
        element_count += 1
        if element_count > _MAX_XML_ELEMENTS:
            _raise_xml_safety(f"IP-XACT XML exceeds {_MAX_XML_ELEMENTS} elements")
        if len(stack) >= _MAX_XML_DEPTH:
            _raise_xml_safety(f"IP-XACT XML exceeds {_MAX_XML_DEPTH} nesting levels")
        node = _XmlNode(
            name,
            dict(attributes),
            max(1, parser.CurrentLineNumber),
            max(1, parser.CurrentColumnNumber + 1),
            parent=stack[-1] if stack else None,
        )
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    def end(_name: str) -> None:
        if stack:
            stack.pop()

    def data(value: str) -> None:
        if stack:
            stack[-1].text_parts.append(value)

    def reject_doctype(*_args: Any) -> None:
        _raise_xml_safety("IP-XACT XML must not contain a DOCTYPE or entity declaration")

    def reject_external_entity(*_args: Any) -> int:
        reject_doctype(*_args)
        return 0

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.EntityDeclHandler = reject_doctype
    parser.UnparsedEntityDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = reject_external_entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(text, True)
    except _XmlSafetyError as error:
        return None, (
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                str(error),
                location=Provenance(
                    str(path),
                    max(1, parser.CurrentLineNumber),
                    max(1, parser.CurrentColumnNumber + 1),
                    view,
                ),
            ),
        )
    except expat.ExpatError as error:
        return None, (
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                f"Cannot parse IP-XACT XML {path}: {error}",
                location=Provenance(
                    str(path),
                    max(1, error.lineno),
                    max(1, error.offset + 1),
                    view,
                ),
            ),
        )
    if len(roots) != 1:
        return None, (
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                f"IP-XACT XML {path} must contain exactly one root element",
                location=Provenance(str(path), view=view),
            ),
        )
    return roots[0], ()


class _ExpressionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _ParameterEntry:
    identifier: str
    name: str
    expression: str
    node: _XmlNode
    data_type: str | None = None


_VERILOG_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_$])(?:(\d+)\s*)?'([sS])?([bBoOdDhH])([0-9A-Fa-f_xXzZ?]+)"
)
_VHDL_LITERAL = re.compile(r"(?<![A-Za-z0-9_])(2|8|10|16)#([0-9A-Fa-f_]+)#")
_BRACED_REFERENCE = re.compile(r"\$?\{([^{}]+)\}")


def _checked_integer(value: int) -> int:
    if value.bit_length() > _MAX_INTEGER_BITS:
        raise _ExpressionError("integer result exceeds the safety limit")
    return value


def _replace_verilog_literal(match: re.Match[str]) -> str:
    width_text, signed_text, radix_text, digits_text = match.groups()
    if re.search(r"[xXzZ?]", digits_text):
        raise _ExpressionError("four-state numeric literal is not an exact integer")
    radix = {"b": 2, "o": 8, "d": 10, "h": 16}[radix_text.lower()]
    value = int(digits_text.replace("_", ""), radix)
    if signed_text and width_text:
        width = int(width_text)
        if width < 1 or width > _MAX_INTEGER_BITS:
            raise _ExpressionError("signed literal width is outside the safety limit")
        mask = (1 << width) - 1
        value &= mask
        if value & (1 << (width - 1)):
            value -= 1 << width
    return str(_checked_integer(value))


def _replace_vhdl_literal(match: re.Match[str]) -> str:
    return str(_checked_integer(int(match.group(2).replace("_", ""), int(match.group(1)))))


class _ParameterResolver:
    def __init__(
        self,
        entries: Sequence[_ParameterEntry] = (),
        *,
        fixed_values: Mapping[str, int] | None = None,
    ) -> None:
        self.entries: dict[str, _ParameterEntry] = {}
        self.fixed_values = dict(fixed_values or {})
        self.cache: dict[int, int] = {}
        for entry in entries:
            self.entries[entry.identifier] = entry
            self.entries.setdefault(entry.name, entry)

    def extended(self, entries: Sequence[_ParameterEntry]) -> _ParameterResolver:
        result = _ParameterResolver(fixed_values=self.fixed_values)
        result.entries = dict(self.entries)
        result.cache = self.cache
        for entry in entries:
            result.entries[entry.identifier] = entry
            result.entries[entry.name] = entry
        return result

    def resolve_identifier(self, name: str, stack: tuple[int, ...]) -> int:
        if name in self.fixed_values:
            value = self.fixed_values[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise _ExpressionError(f"override {name!r} is not an integer")
            return _checked_integer(value)
        entry = self.entries.get(name)
        if entry is None:
            raise _ExpressionError(f"unknown parameter {name!r}")
        key = id(entry)
        if key in self.cache:
            return self.cache[key]
        if key in stack:
            raise _ExpressionError(f"cyclic parameter reference involving {name!r}")
        if len(stack) >= _MAX_PARAMETER_DEPTH:
            raise _ExpressionError("parameter dependency chain exceeds the safety limit")
        value = _evaluate_integer_expression(entry.expression, self, (*stack, key))
        self.cache[key] = value
        return value

    def evaluate(self, expression: str) -> tuple[int | None, str | None]:
        try:
            return _evaluate_integer_expression(expression, self, ()), None
        except (_ExpressionError, SyntaxError, ValueError, ZeroDivisionError) as error:
            return None, str(error)
        except RecursionError:
            return None, "parameter dependency chain exceeds the safety limit"


class _SafeIntegerEvaluator:
    def __init__(
        self,
        resolver: _ParameterResolver,
        references: Mapping[str, str],
        stack: tuple[int, ...],
    ) -> None:
        self.resolver = resolver
        self.references = references
        self.stack = stack
        self.visited = 0

    def evaluate(self, node: ast.AST, depth: int = 0) -> int:
        self.visited += 1
        if self.visited > _MAX_EXPRESSION_NODES or depth > _MAX_EXPRESSION_DEPTH:
            raise _ExpressionError("expression exceeds the complexity limit")
        if isinstance(node, ast.Expression):
            return self.evaluate(node.body, depth + 1)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, int):
                raise _ExpressionError("expression contains a non-integer literal")
            return _checked_integer(node.value)
        if isinstance(node, ast.Name):
            name = self.references.get(node.id, node.id)
            return self.resolver.resolve_identifier(name, self.stack)
        if isinstance(node, ast.UnaryOp):
            operand = self.evaluate(node.operand, depth + 1)
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.USub):
                return _checked_integer(-operand)
            if isinstance(node.op, ast.Invert):
                return _checked_integer(~operand)
            raise _ExpressionError("unsupported unary operator")
        if isinstance(node, ast.BinOp):
            left = self.evaluate(node.left, depth + 1)
            right = self.evaluate(node.right, depth + 1)
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            elif isinstance(node.op, (ast.Div, ast.FloorDiv)):
                if right == 0:
                    raise _ExpressionError("division by zero")
                if isinstance(node.op, ast.Div) and left % right:
                    raise _ExpressionError("division does not produce an exact integer")
                value = left // right
            elif isinstance(node.op, ast.Mod):
                if right == 0:
                    raise _ExpressionError("division by zero")
                value = left % right
            elif isinstance(node.op, ast.LShift):
                if right < 0 or right > _MAX_INTEGER_BITS:
                    raise _ExpressionError("shift count is outside the safety limit")
                value = left << right
            elif isinstance(node.op, ast.RShift):
                if right < 0 or right > _MAX_INTEGER_BITS:
                    raise _ExpressionError("shift count is outside the safety limit")
                value = left >> right
            elif isinstance(node.op, ast.BitAnd):
                value = left & right
            elif isinstance(node.op, ast.BitOr):
                value = left | right
            elif isinstance(node.op, ast.BitXor):
                value = left ^ right
            elif isinstance(node.op, ast.Pow):
                if right < 0 or right > 128:
                    raise _ExpressionError("exponent is outside the safety limit")
                value = left**right
            else:
                raise _ExpressionError("unsupported binary operator")
            return _checked_integer(value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            arguments = [self.evaluate(argument, depth + 1) for argument in node.args]
            if node.keywords:
                raise _ExpressionError("keyword arguments are not supported")
            function = node.func.id.lower()
            if function in {"clog2", "ceil_log2"} and len(arguments) == 1:
                if arguments[0] < 1:
                    raise _ExpressionError("clog2 argument must be positive")
                return (arguments[0] - 1).bit_length()
            if function == "abs" and len(arguments) == 1:
                return _checked_integer(abs(arguments[0]))
            if function == "min" and arguments:
                return min(arguments)
            if function == "max" and arguments:
                return max(arguments)
            if function == "pow" and len(arguments) == 2:
                if arguments[1] < 0 or arguments[1] > 128:
                    raise _ExpressionError("exponent is outside the safety limit")
                return _checked_integer(pow(arguments[0], arguments[1]))
            raise _ExpressionError(f"unsupported integer function {node.func.id!r}")
        raise _ExpressionError(f"unsupported expression construct {type(node).__name__}")


def _evaluate_integer_expression(
    expression: str,
    resolver: _ParameterResolver,
    stack: tuple[int, ...],
) -> int:
    text = expression.strip()
    if not text:
        raise _ExpressionError("empty expression")
    text = text.replace("$clog2", "clog2")
    text = _VERILOG_LITERAL.sub(_replace_verilog_literal, text)
    text = _VHDL_LITERAL.sub(_replace_vhdl_literal, text)
    references: dict[str, str] = {}

    def replace_reference(match: re.Match[str]) -> str:
        identifier = match.group(1).strip()
        if not identifier:
            raise _ExpressionError("empty braced parameter reference")
        synthetic = f"__ipxact_ref_{len(references)}"
        references[synthetic] = identifier
        return synthetic

    text = _BRACED_REFERENCE.sub(replace_reference, text)
    tree = ast.parse(text, mode="eval")
    return _SafeIntegerEvaluator(resolver, references, stack).evaluate(tree)


@dataclass(frozen=True, slots=True)
class _NumericFact:
    expression: str | None
    value: int | None
    state: FactState
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "expression": self.expression,
            "value": self.value,
            "state": self.state.value,
        }
        if self.reason:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True, slots=True)
class _ArrayElement:
    name: str
    indices: tuple[int, ...]
    labels: tuple[str, ...]
    linear_index: int
    byte_delta: int | None


@dataclass(frozen=True, slots=True)
class _SelectionInfo:
    suffix: str
    selected_bits: frozenset[int] | None
    has_selection: bool
    resolved: bool


def _selection_info(selection: Mapping[str, Any] | None) -> _SelectionInfo:
    if not selection:
        return _SelectionInfo("", None, False, True)
    ranges = selection.get("ranges", [])
    indices = selection.get("indices", [])
    suffix_parts: list[str] = []
    one_dimensional_bits: frozenset[int] | None = None
    resolved = True
    for range_record in ranges:
        left = range_record.get("left", {}).get("value")
        right = range_record.get("right", {}).get("value")
        if not isinstance(left, int) or isinstance(left, bool):
            resolved = False
        if not isinstance(right, int) or isinstance(right, bool):
            resolved = False
        if resolved and isinstance(left, int) and isinstance(right, int):
            suffix_parts.append(f"[{left}:{right}]")
        else:
            suffix_parts.append("[?]")
    for index_record in indices:
        index = index_record.get("value")
        if not isinstance(index, int) or isinstance(index, bool):
            resolved = False
            suffix_parts.append("[?]")
        else:
            suffix_parts.append(f"[{index}]")
    if resolved and len(ranges) == 1 and not indices:
        left = ranges[0]["left"]["value"]
        right = ranges[0]["right"]["value"]
        low, high = sorted((left, right))
        if high - low + 1 <= _MAX_SELECTION_COLLISION_BITS:
            one_dimensional_bits = frozenset(range(low, high + 1))
    elif resolved and len(indices) == 1 and not ranges:
        one_dimensional_bits = frozenset((indices[0]["value"],))
    return _SelectionInfo(
        "".join(suffix_parts),
        one_dimensional_bits,
        bool(ranges or indices),
        resolved,
    )


def _parse_dim_index(text: str, count: int) -> tuple[tuple[str, ...] | None, str | None]:
    value = text.strip()
    if not value:
        return None, "dimIndex is empty"
    if "," in value:
        labels = tuple(part.strip() for part in value.split(","))
        if any(not label for label in labels):
            return None, "dimIndex contains an empty label"
    else:
        range_match = re.fullmatch(r"([+-]?\d+|[A-Za-z])-([+-]?\d+|[A-Za-z])", value)
        if range_match is None:
            labels = (value,)
        else:
            first, last = range_match.groups()
            if first.lstrip("+-").isdigit() and last.lstrip("+-").isdigit():
                start, stop = int(first), int(last)
                step = 1 if stop >= start else -1
                labels = tuple(str(index) for index in range(start, stop + step, step))
            elif first.isalpha() and last.isalpha():
                start, stop = ord(first), ord(last)
                step = 1 if stop >= start else -1
                labels = tuple(chr(index) for index in range(start, stop + step, step))
            else:
                return None, "dimIndex range endpoints use different kinds"
    if len(labels) != count:
        return None, f"dimIndex defines {len(labels)} labels for dimension {count}"
    if len(set(labels)) != len(labels):
        return None, "dimIndex labels are not unique"
    return labels, None


def _parameter_entries(owner: _XmlNode) -> tuple[_ParameterEntry, ...]:
    containers = [*owner.children_named("parameters"), *owner.children_named("moduleParameters")]
    entries: list[_ParameterEntry] = []
    for container in containers:
        for node in container.children:
            if node.namespace != container.namespace or node.local_name not in {
                "parameter",
                "moduleParameter",
            }:
                continue
            name = (_child_text(node, "name") or "").strip()
            identifier = (node.attribute("parameterId") or name).strip()
            value_node = node.child("value")
            expression = value_node.text if value_node is not None else ""
            if not identifier and not name:
                continue
            entries.append(
                _ParameterEntry(
                    identifier=identifier or name,
                    name=name or identifier,
                    expression=expression,
                    node=node,
                    data_type=(
                        node.attribute("type")
                        or node.attribute("dataType")
                        or (value_node.attribute("format") if value_node is not None else None)
                    ),
                )
            )
    return tuple(entries)


def _vlnv(node: _XmlNode | None) -> tuple[str | None, dict[str, str]]:
    if node is None:
        return None, {}
    parts = {
        key: value
        for key in ("vendor", "library", "name", "version")
        if (value := node.attribute(key))
    }
    if not parts:
        return None, {}
    rendered = ":".join(parts.get(key, "") for key in ("vendor", "library", "name", "version"))
    return rendered, parts


def _description(node: _XmlNode) -> str | None:
    child = node.child("description")
    return child.text if child is not None and child.text else None


def _child_text(node: _XmlNode, name: str) -> str | None:
    child = node.child(name)
    return child.text if child is not None and child.text else None


@dataclass(frozen=True, slots=True)
class _Extraction:
    component: ComponentObservation
    interfaces: tuple[InterfaceObservation, ...]
    registers: tuple[RegisterObservation, ...]
    objects: tuple[DesignObjectObservation, ...]
    clocks: tuple[ClockObservation, ...]
    diagnostics: tuple[Diagnostic, ...]
    tainted: bool


class _ComponentExtractor:
    def __init__(
        self,
        root: _XmlNode,
        path: Path,
        view: ViewId,
        component_name: str,
        namespace_version: str | None,
        parameter_values: Mapping[str, int],
    ) -> None:
        self.root = root
        self.path = path
        self.view = view
        self.component_name = component_name
        self.namespace_version = namespace_version
        self.diagnostics: list[Diagnostic] = []
        self.objects: list[DesignObjectObservation] = []
        self.clocks: list[ClockObservation] = []
        self.tainted = False
        self.parameter_entries = _parameter_entries(root)
        self.resolver = _ParameterResolver(
            self.parameter_entries,
            fixed_values=parameter_values,
        )

    def location(self, node: _XmlNode, raw_name: str | None = None) -> Provenance:
        return Provenance(
            str(self.path),
            node.line,
            node.column,
            self.view,
            raw_name=raw_name,
        )

    def report(
        self,
        code: str,
        severity: Severity,
        message: str,
        node: _XmlNode,
        *,
        taint: bool,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.diagnostics.append(
            parser_diagnostic(
                code,
                severity,
                message,
                location=self.location(node),
                metadata=metadata,
            )
        )
        if taint:
            self.tainted = True

    def numeric(
        self,
        node: _XmlNode | None,
        resolver: _ParameterResolver,
        context: str,
        *,
        required: bool = False,
        minimum: int | None = None,
    ) -> _NumericFact:
        if node is None or not node.text:
            state = FactState.TAINTED if required else FactState.UNKNOWN
            if required:
                self.report(
                    "OC1101",
                    Severity.ERROR,
                    f"IP-XACT {context} is missing",
                    node or self.root,
                    taint=True,
                )
            return _NumericFact(None, None, state, "missing" if required else None)
        expression = node.text
        value, reason = resolver.evaluate(expression)
        if value is None:
            self.report(
                "OC1103",
                Severity.WARNING,
                f"IP-XACT {context} expression {expression!r} is unresolved: {reason}",
                node,
                taint=True,
                metadata={"expression": expression, "reason": reason},
            )
            return _NumericFact(expression, None, FactState.TAINTED, reason)
        if minimum is not None and value < minimum:
            reason = f"value must be at least {minimum}"
            self.report(
                "OC1101",
                Severity.ERROR,
                f"IP-XACT {context} resolves to invalid value {value}: {reason}",
                node,
                taint=True,
                metadata={"expression": expression, "value": value},
            )
            return _NumericFact(expression, None, FactState.TAINTED, reason)
        return _NumericFact(expression, value, FactState.KNOWN)

    def parameter_records(
        self,
        entries: Sequence[_ParameterEntry],
        resolver: _ParameterResolver,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for entry in entries:
            value, reason = resolver.evaluate(entry.expression)
            explicitly_non_numeric = (entry.data_type or "").lower() in {
                "string",
                "boolean",
                "float",
                "double",
            }
            state = (
                FactState.KNOWN
                if value is not None
                else FactState.UNSUPPORTED
                if explicitly_non_numeric
                else FactState.UNKNOWN
            )
            records.append(
                {
                    "id": entry.identifier,
                    "name": entry.name,
                    "expression": entry.expression,
                    "value": value,
                    "state": state.value,
                    "reason": reason,
                    "data_type": entry.data_type,
                }
            )
            self.objects.append(
                DesignObjectObservation(
                    "parameter",
                    entry.name,
                    scope=self.component_name,
                    provenance=self.location(entry.node, entry.name),
                    status=state,
                    attributes={"id": entry.identifier, "expression": entry.expression},
                )
            )
        return records

    def extract(self) -> _Extraction:
        ports = self.extract_ports()
        port_names = {port.native_name for port in ports}
        interfaces = self.extract_interfaces(port_names)
        memory_maps, registers = self.extract_memory_maps()
        root_location = self.location(self.root, self.component_name)
        component = ComponentObservation(
            native_name=self.component_name,
            kind=ComponentKind.MODULE,
            ports=tuple(ports),
            provenance=root_location,
            status=FactState.TAINTED if self.tainted else FactState.KNOWN,
            attributes={
                "ipxact_namespace": self.root.namespace,
                "ipxact_version": self.namespace_version,
                "vlnv": {
                    key: _child_text(self.root, key)
                    for key in ("vendor", "library", "name", "version")
                },
                "parameters": self.parameter_records(self.parameter_entries, self.resolver),
                "bus_interfaces": [dict(interface.attributes) for interface in interfaces],
                "memory_maps": memory_maps,
            },
        )
        self.objects.insert(
            0,
            DesignObjectObservation(
                "component",
                self.component_name,
                provenance=root_location,
                status=component.status,
                attributes={"ipxact_version": self.namespace_version},
            ),
        )
        return _Extraction(
            component,
            tuple(interfaces),
            tuple(registers),
            tuple(self.objects),
            tuple(self.clocks),
            tuple(self.diagnostics),
            self.tainted,
        )

    def extract_ports(self) -> list[PortObservation]:
        model = self.root.child("model")
        ports_container = model.child("ports") if model is not None else None
        if ports_container is None:
            return []
        result: list[PortObservation] = []
        for port_node in ports_container.children_named("port"):
            name_node = port_node.child("name")
            name = name_node.text if name_node is not None else ""
            if not name:
                self.report(
                    "OC1101",
                    Severity.ERROR,
                    "IP-XACT component port has no name",
                    port_node,
                    taint=True,
                )
                continue
            resolver = self.resolver.extended(_parameter_entries(port_node))
            location = self.location(port_node, name)
            field_states: dict[str, FactState] = {}
            attributes: dict[str, Any] = {
                "description": _description(port_node),
                "access": _child_text(port_node, "access"),
            }
            status = FactState.KNOWN
            wire = port_node.child("wire")
            if wire is None:
                style = next(
                    (
                        candidate
                        for candidate in ("transactional", "structured")
                        if port_node.child(candidate) is not None
                    ),
                    "unknown",
                )
                self.report(
                    "OC1102",
                    Severity.WARNING,
                    f"IP-XACT {style} port {self.component_name}/{name} is unsupported",
                    port_node,
                    taint=True,
                )
                direction = Direction.UNKNOWN
                role = PortRole.UNKNOWN
                shape = BusShape.unknown()
                status = FactState.UNSUPPORTED
                field_states = {
                    "direction": FactState.UNSUPPORTED,
                    "role": FactState.UNSUPPORTED,
                    "shape": FactState.UNSUPPORTED,
                }
                attributes["port_style"] = style
            else:
                direction_text = _child_text(wire, "direction") or ""
                direction = Direction.parse(direction_text)
                if direction == Direction.UNKNOWN:
                    state = FactState.UNKNOWN if not direction_text else FactState.UNSUPPORTED
                    field_states["direction"] = state
                    self.report(
                        "OC1102" if direction_text else "OC1101",
                        Severity.WARNING if direction_text else Severity.ERROR,
                        (
                            f"IP-XACT port {self.component_name}/{name} has unsupported "
                            f"direction {direction_text!r}"
                            if direction_text
                            else f"IP-XACT port {self.component_name}/{name} has no direction"
                        ),
                        wire,
                        taint=True,
                    )
                role, role_state, qualifier_attributes = self._port_role(wire, name)
                field_states["role"] = role_state
                shape, shape_state, vector_attributes = self._port_shape(
                    port_node,
                    wire,
                    resolver,
                    name,
                )
                if shape_state != FactState.KNOWN:
                    field_states["shape"] = shape_state
                attributes.update(
                    {
                        "port_style": "wire",
                        "direction_text": direction_text,
                        "qualifier": qualifier_attributes,
                        "vectors": vector_attributes[0],
                        "arrays": vector_attributes[1],
                        "all_logical_directions_allowed": wire.attribute(
                            "allLogicalDirectionsAllowed"
                        ),
                    }
                )
                if role == PortRole.CLOCK and role_state == FactState.KNOWN:
                    self.clocks.append(
                        ClockObservation(
                            name,
                            targets=(f"{self.component_name}/{name}",),
                            provenance=location,
                            attributes={"source": "ipxact.qualifier.isClock"},
                        )
                    )
            port = PortObservation(
                name,
                direction,
                role,
                shape,
                location,
                attributes=attributes,
                status=status,
                field_states=field_states,
            )
            result.append(port)
            object_status = (
                FactState.TAINTED if FactState.TAINTED in field_states.values() else status
            )
            self.objects.append(
                DesignObjectObservation(
                    "port",
                    name,
                    scope=self.component_name,
                    provenance=location,
                    status=object_status,
                    attributes={"direction": direction.value, "role": role.value},
                )
            )
        return result

    def _port_role(
        self,
        wire: _XmlNode,
        name: str,
    ) -> tuple[PortRole, FactState, dict[str, Any]]:
        qualifier = wire.child("qualifier")
        values: dict[str, Any] = {}
        if qualifier is not None:
            for key in ("isAddress", "isData", "isClock", "isReset", "isClockEn", "isPowerEn"):
                node = qualifier.child(key)
                if node is not None:
                    values[key] = {
                        "value": node.text.lower() in {"true", "1"},
                        "level": node.attribute("level"),
                    }
            if values.get("isClock", {}).get("value"):
                return PortRole.CLOCK, FactState.KNOWN, values
            if values.get("isReset", {}).get("value"):
                return PortRole.RESET, FactState.KNOWN, values
            if values.get("isData", {}).get("value") or values.get("isAddress", {}).get("value"):
                return PortRole.SIGNAL, FactState.KNOWN, values
        role, state = infer_role_from_name(name)
        return role, state, values

    def _port_shape(
        self,
        port: _XmlNode,
        wire: _XmlNode,
        resolver: _ParameterResolver,
        name: str,
    ) -> tuple[BusShape, FactState, tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        vectors_container = wire.child("vectors")
        vector_nodes = (
            vectors_container.children_named("vector")
            if vectors_container is not None
            else wire.children_named("vector")
        )
        arrays_container = port.child("arrays")
        array_nodes = arrays_container.children_named("array") if arrays_container else ()
        packed: list[IndexRange] = []
        unpacked: list[IndexRange] = []
        vector_records: list[dict[str, Any]] = []
        array_records: list[dict[str, Any]] = []
        tainted = False
        for index, vector in enumerate(vector_nodes):
            left = self.numeric(
                vector.child("left"),
                resolver,
                f"port {self.component_name}/{name} vector {index} left bound",
                required=True,
            )
            right = self.numeric(
                vector.child("right"),
                resolver,
                f"port {self.component_name}/{name} vector {index} right bound",
                required=True,
            )
            vector_records.append(
                {
                    "id": vector.attribute("vectorId"),
                    "left": left.to_dict(),
                    "right": right.to_dict(),
                }
            )
            if left.value is None or right.value is None:
                tainted = True
            else:
                packed.append(IndexRange(left.value, right.value))
        for index, array in enumerate(array_nodes):
            left = self.numeric(
                array.child("left"),
                resolver,
                f"port {self.component_name}/{name} array {index} left bound",
                required=True,
            )
            right = self.numeric(
                array.child("right"),
                resolver,
                f"port {self.component_name}/{name} array {index} right bound",
                required=True,
            )
            array_records.append({"left": left.to_dict(), "right": right.to_dict()})
            if left.value is None or right.value is None:
                tainted = True
            else:
                unpacked.append(IndexRange(left.value, right.value))
        if tainted:
            return (
                BusShape.unknown(),
                FactState.TAINTED,
                (vector_records, array_records),
            )
        if packed:
            shape = BusShape(
                packed=tuple(packed),
                unpacked=tuple(unpacked),
                bit_indices=packed[0].ordered_indices if len(packed) == 1 else (),
                explicit_scalar=False,
            )
        else:
            shape = BusShape(
                width=1,
                unpacked=tuple(unpacked),
                explicit_scalar=True,
            )
        return shape, FactState.KNOWN, (vector_records, array_records)

    def extract_interfaces(self, port_names: set[str]) -> list[InterfaceObservation]:
        container = self.root.child("busInterfaces")
        if container is None:
            return []
        interfaces: list[InterfaceObservation] = []
        for node in container.children_named("busInterface"):
            name_node = node.child("name")
            name = name_node.text if name_node is not None else ""
            if not name:
                self.report(
                    "OC1101",
                    Severity.ERROR,
                    "IP-XACT bus interface has no name",
                    node,
                    taint=True,
                )
                continue
            issue_count = len(self.diagnostics)
            resolver = self.resolver.extended(_parameter_entries(node))
            bus_type, bus_vlnv = _vlnv(node.child("busType"))
            if bus_type is None:
                self.report(
                    "OC1101",
                    Severity.ERROR,
                    f"IP-XACT bus interface {name} has no busType VLNV",
                    node,
                    taint=True,
                )
            abstractions: list[dict[str, Any]] = []
            abstraction_by_port_map: dict[int, str | None] = {}
            for abstraction in node.descendants_named("abstractionType"):
                reference = abstraction.child("abstractionRef") or abstraction
                rendered, vlnv = _vlnv(reference)
                abstractions.append({"vlnv": vlnv, "rendered": rendered})
                for port_map in abstraction.descendants_named("portMap"):
                    abstraction_by_port_map[id(port_map)] = rendered
                if rendered:
                    self.objects.append(
                        DesignObjectObservation(
                            "abstraction_definition",
                            rendered,
                            relation="reference",
                            scope=f"{self.component_name}/{name}",
                            provenance=self.location(reference),
                        )
                    )
            mode, mode_node = self._interface_mode(node)
            if mode is None:
                self.report(
                    "OC1101",
                    Severity.ERROR,
                    f"IP-XACT bus interface {name} has no interface mode",
                    node,
                    taint=True,
                )
            if bus_type:
                self.objects.append(
                    DesignObjectObservation(
                        "bus_definition",
                        bus_type,
                        relation="reference",
                        scope=f"{self.component_name}/{name}",
                        provenance=self.location(node.child("busType") or node),
                    )
                )
            memory_map_ref = self._memory_map_ref(mode_node)
            if memory_map_ref:
                self.objects.append(
                    DesignObjectObservation(
                        "memory_map",
                        memory_map_ref,
                        relation="reference",
                        scope=self.component_name,
                        provenance=self.location(mode_node or node),
                    )
                )

            detailed_maps: list[dict[str, Any]] = []
            compact: dict[str, set[str]] = {}
            physical_uses: dict[str, list[tuple[str, str, _SelectionInfo]]] = {}
            for map_index, port_map in enumerate(node.descendants_named("portMap")):
                logical = port_map.child("logicalPort")
                physical = port_map.child("physicalPort")
                tie_off = port_map.child("logicalTieOff")
                logical_name_node = logical.child("name") if logical is not None else None
                logical_name = logical_name_node.text if logical_name_node is not None else ""
                physical_name_node = physical.child("name") if physical is not None else None
                physical_name = physical_name_node.text if physical_name_node is not None else ""
                if not logical_name:
                    self.report(
                        "OC1101",
                        Severity.ERROR,
                        f"IP-XACT interface {name} portMap {map_index} has no logical port name",
                        port_map,
                        taint=True,
                    )
                if physical is not None and not physical_name:
                    self.report(
                        "OC1101",
                        Severity.ERROR,
                        f"IP-XACT interface {name} portMap {map_index} has no physical port name",
                        port_map,
                        taint=True,
                    )
                logical_selection = self._endpoint_selection(
                    logical,
                    resolver,
                    f"interface {name} logical port {logical_name or map_index}",
                )
                physical_selection = self._endpoint_selection(
                    physical,
                    resolver,
                    f"interface {name} physical port {physical_name or map_index}",
                )
                physical_selection_info = _selection_info(physical_selection)
                physical_expression = (
                    f"{physical_name}{physical_selection_info.suffix}"
                    if physical_name and physical_selection_info.resolved
                    else physical_name
                )
                tie_fact = (
                    self.numeric(
                        tie_off,
                        resolver,
                        f"interface {name} logical tie-off",
                        required=True,
                        minimum=0,
                    )
                    if tie_off is not None
                    else None
                )
                detailed_maps.append(
                    {
                        "logical_port": logical_name or None,
                        "physical_port": physical_name or None,
                        "physical_expression": physical_expression or None,
                        "logical_selection": logical_selection,
                        "physical_selection": physical_selection,
                        "logical_tie_off": tie_fact.to_dict() if tie_fact else None,
                        "abstraction_type": abstraction_by_port_map.get(id(port_map)),
                        "is_informative": _child_text(port_map, "isInformative"),
                    }
                )
                if logical_name and physical_name:
                    compact.setdefault(logical_name, set()).add(physical_expression)
                    physical_uses.setdefault(physical_name, []).append(
                        (logical_name, physical_expression, physical_selection_info)
                    )
                    self.objects.append(
                        DesignObjectObservation(
                            "port",
                            physical_name,
                            relation="reference",
                            scope=self.component_name,
                            provenance=self.location(physical or port_map, physical_name),
                            status=FactState.KNOWN,
                            attributes={"interface": name, "logical_port": logical_name},
                        )
                    )
            conflicting = {key: values for key, values in compact.items() if len(values) > 1}
            if conflicting:
                self.report(
                    "OC1102",
                    Severity.WARNING,
                    (
                        f"IP-XACT interface {name} has abstraction-dependent port mappings; "
                        "all mappings are retained in attributes"
                    ),
                    node,
                    taint=True,
                    metadata={
                        "conflicting_maps": {
                            key: sorted(values) for key, values in conflicting.items()
                        }
                    },
                )
            duplicate_groups: dict[str, list[tuple[str, str, _SelectionInfo]]] = {}
            for physical_name, uses in physical_uses.items():
                unique_uses = {
                    (logical_name, expression): selection
                    for logical_name, expression, selection in uses
                }
                if len({logical for logical, _expression in unique_uses}) > 1:
                    duplicate_groups[physical_name] = [
                        (logical, expression, selection)
                        for (logical, expression), selection in unique_uses.items()
                    ]
            allow_many_to_one = bool(duplicate_groups)
            for physical_name, uses in duplicate_groups.items():
                selections = [selection for _logical, _expression, selection in uses]
                if any(
                    not selection.has_selection
                    or not selection.resolved
                    or selection.selected_bits is None
                    for selection in selections
                ):
                    allow_many_to_one = False
                    self.report(
                        "OC1102",
                        Severity.WARNING,
                        (
                            f"IP-XACT interface {name} maps multiple logical ports to "
                            f"{physical_name} without resolvable one-dimensional disjoint "
                            "physical selections"
                        ),
                        node,
                        taint=True,
                        metadata={
                            "physical_port": physical_name,
                            "mappings": [
                                {"logical": logical, "physical": expression}
                                for logical, expression, _selection in uses
                            ],
                        },
                    )
                    continue
                overlap = any(
                    left.selected_bits & right.selected_bits
                    for index, left in enumerate(selections)
                    for right in selections[index + 1 :]
                    if left.selected_bits is not None and right.selected_bits is not None
                )
                if overlap:
                    allow_many_to_one = False
                    self.report(
                        "OC1101",
                        Severity.ERROR,
                        (
                            f"IP-XACT interface {name} maps overlapping logical-port "
                            f"selections onto physical port {physical_name}"
                        ),
                        node,
                        taint=True,
                        metadata={
                            "physical_port": physical_name,
                            "mappings": [
                                {"logical": logical, "physical": expression}
                                for logical, expression, _selection in uses
                            ],
                        },
                    )
            compact_maps = {
                logical: next(iter(physical))
                for logical, physical in compact.items()
                if len(physical) == 1
            }
            abstraction_values = {
                item["rendered"] for item in abstractions if item["rendered"] is not None
            }
            interface_status = (
                FactState.TAINTED if len(self.diagnostics) > issue_count else FactState.KNOWN
            )
            interface = InterfaceObservation(
                name,
                component=self.component_name,
                bus_type=bus_type,
                abstraction_type=(
                    next(iter(abstraction_values)) if len(abstraction_values) == 1 else None
                ),
                mode=mode,
                port_maps=compact_maps,
                provenance=self.location(node, name),
                status=interface_status,
                attributes={
                    "name": name,
                    "description": _description(node),
                    "bus_type": bus_vlnv,
                    "abstraction_types": abstractions,
                    "mode": mode,
                    "memory_map_ref": memory_map_ref,
                    "port_maps": detailed_maps,
                    "allow_many_to_one": allow_many_to_one,
                    "disjoint_physical_slices": allow_many_to_one,
                    "connection_required": _child_text(node, "connectionRequired"),
                    "endianness": _child_text(node, "endianness"),
                },
            )
            interfaces.append(interface)
            self.objects.append(
                DesignObjectObservation(
                    "interface",
                    name,
                    scope=self.component_name,
                    provenance=self.location(node, name),
                    status=interface_status,
                    attributes={"bus_type": bus_type, "mode": mode},
                )
            )
        return interfaces

    @staticmethod
    def _interface_mode(node: _XmlNode) -> tuple[str | None, _XmlNode | None]:
        direct = node.child("interfaceMode")
        if direct is not None and direct.text:
            return direct.text, direct
        for child in node.children:
            if child.namespace == node.namespace and child.local_name in _MODE_NAMES:
                return child.local_name, child
        return None, None

    @staticmethod
    def _memory_map_ref(mode_node: _XmlNode | None) -> str | None:
        if mode_node is None:
            return None
        reference = mode_node.child("memoryMapRef")
        if reference is None:
            return mode_node.attribute("memoryMapRef")
        return reference.attribute("memoryMapRef") or reference.text or None

    def _endpoint_selection(
        self,
        endpoint: _XmlNode | None,
        resolver: _ParameterResolver,
        context: str,
    ) -> dict[str, Any] | None:
        if endpoint is None:
            return None
        range_nodes = endpoint.descendants_named("range")
        ranges: list[dict[str, Any]] = []
        for index, range_node in enumerate(range_nodes):
            left = self.numeric(
                range_node.child("left"),
                resolver,
                f"{context} range {index} left bound",
                required=True,
            )
            right = self.numeric(
                range_node.child("right"),
                resolver,
                f"{context} range {index} right bound",
                required=True,
            )
            ranges.append({"left": left.to_dict(), "right": right.to_dict()})
        indices = [
            self.numeric(index, resolver, f"{context} index", required=True).to_dict()
            for index in endpoint.descendants_named("index")
        ]
        return {"ranges": ranges, "indices": indices} if ranges or indices else None

    def _address_unit_facts(
        self,
        memory_map: _XmlNode,
        resolver: _ParameterResolver,
        name: str,
    ) -> tuple[_NumericFact, _NumericFact]:
        node = memory_map.child("addressUnitBits")
        if node is None:
            address_unit_bits = _NumericFact(
                None,
                8,
                FactState.KNOWN,
                "IEEE 1685 default when addressUnitBits is omitted",
            )
        else:
            address_unit_bits = self.numeric(
                node,
                resolver,
                f"memory map {name} addressUnitBits",
                required=True,
                minimum=1,
            )
        if address_unit_bits.value is None:
            return (
                address_unit_bits,
                _NumericFact(
                    None,
                    None,
                    FactState.TAINTED,
                    "addressUnitBits is unresolved",
                ),
            )
        if address_unit_bits.value % 8:
            self.report(
                "OC1102",
                Severity.WARNING,
                (
                    f"IP-XACT memory map {name} uses addressUnitBits="
                    f"{address_unit_bits.value}; canonical byte addresses require a "
                    "whole number of bytes per addressable unit"
                ),
                node or memory_map,
                taint=True,
                metadata={"address_unit_bits": address_unit_bits.value},
            )
            return (
                address_unit_bits,
                _NumericFact(
                    None,
                    None,
                    FactState.UNSUPPORTED,
                    "addressable unit is not an integral number of bytes",
                ),
            )
        return (
            address_unit_bits,
            _NumericFact(
                None,
                address_unit_bits.value // 8,
                FactState.KNOWN,
                "derived from addressUnitBits",
            ),
        )

    def _scale_address_fact(
        self,
        fact: _NumericFact,
        bytes_per_address_unit: _NumericFact,
        node: _XmlNode,
        context: str,
    ) -> _NumericFact:
        if fact.value is None:
            return _NumericFact(fact.expression, None, fact.state, fact.reason)
        if bytes_per_address_unit.value is None:
            return _NumericFact(
                fact.expression,
                None,
                FactState.TAINTED,
                bytes_per_address_unit.reason,
            )
        try:
            value = _checked_integer(fact.value * bytes_per_address_unit.value)
        except _ExpressionError as error:
            self.report(
                "OC1101",
                Severity.ERROR,
                f"IP-XACT {context} cannot be represented safely in bytes: {error}",
                node,
                taint=True,
            )
            return _NumericFact(fact.expression, None, FactState.TAINTED, str(error))
        return _NumericFact(fact.expression, value, FactState.KNOWN)

    @staticmethod
    def _array_syntax_present(node: _XmlNode) -> bool:
        return any(
            (
                node.child("array") is not None,
                bool(node.children_named("dim")),
                node.child("dimIncrement") is not None,
                node.child("dimIndex") is not None,
            )
        )

    def _register_array_elements(
        self,
        node: _XmlNode,
        name: str,
        resolver: _ParameterResolver,
        size: _NumericFact,
        address_unit_bits: _NumericFact,
        bytes_per_address_unit: _NumericFact,
    ) -> tuple[tuple[_ArrayElement, ...], dict[str, Any], bool]:
        array = node.child("array")
        direct_dimensions = node.children_named("dim")
        wrapped_dimensions = array.children_named("dim") if array is not None else ()
        increment_node = node.child("dimIncrement")
        index_node = node.child("dimIndex")
        stride_node = array.child("stride") if array is not None else None
        syntax = "ieee-1685-2022-array" if array is not None else "legacy-dim"
        attributes: dict[str, Any] = {
            "syntax": syntax,
            "dimensions": [],
            "dim_index": index_node.text if index_node is not None else None,
            "stride_address_units": _NumericFact(None, None, FactState.UNKNOWN).to_dict(),
            "stride_bytes": _NumericFact(None, None, FactState.UNKNOWN).to_dict(),
            "expanded": False,
            "element_count": None,
        }
        if not self._array_syntax_present(node):
            attributes["syntax"] = "scalar"
            attributes["element_count"] = 1
            return (_ArrayElement(name, (), (), 0, 0),), attributes, False

        tainted = False
        if array is not None and direct_dimensions:
            self.report(
                "OC1101",
                Severity.ERROR,
                f"IP-XACT register {name} mixes wrapped and direct array dimensions",
                node,
                taint=True,
            )
            tainted = True
        dimensions = wrapped_dimensions if array is not None else direct_dimensions
        if not dimensions:
            self.report(
                "OC1101",
                Severity.ERROR,
                f"IP-XACT register {name} array has no dimension",
                array or node,
                taint=True,
            )
            tainted = True
        dimension_facts: list[_NumericFact] = []
        for dimension_index, dimension in enumerate(dimensions):
            fact = self.numeric(
                dimension,
                resolver,
                f"register {name} dimension {dimension_index}",
                required=True,
                minimum=1,
            )
            dimension_facts.append(fact)
            attributes["dimensions"].append(
                {
                    **fact.to_dict(),
                    "index_var": dimension.attribute("indexVar"),
                }
            )
            if fact.value is None:
                tainted = True

        element_count = 1
        if not tainted:
            for fact in dimension_facts:
                dimension_value = fact.value
                if dimension_value is None:
                    self.report(
                        "OC1101",
                        Severity.ERROR,
                        f"IP-XACT register {name} has an unresolved array dimension",
                        node,
                        taint=True,
                    )
                    tainted = True
                    break
                if dimension_value > _MAX_REGISTER_ARRAY_ELEMENTS // element_count:
                    self.report(
                        "OC1101",
                        Severity.ERROR,
                        (
                            f"IP-XACT register {name} array exceeds the bounded expansion "
                            f"limit of {_MAX_REGISTER_ARRAY_ELEMENTS} elements"
                        ),
                        node,
                        taint=True,
                        metadata={"limit": _MAX_REGISTER_ARRAY_ELEMENTS},
                    )
                    tainted = True
                    break
                element_count *= dimension_value
        attributes["element_count"] = element_count if not tainted else None

        if stride_node is not None and increment_node is not None:
            self.report(
                "OC1101",
                Severity.ERROR,
                f"IP-XACT register {name} specifies both stride and dimIncrement",
                node,
                taint=True,
            )
            tainted = True
        explicit_stride = stride_node or increment_node
        if explicit_stride is not None:
            stride_address_units = self.numeric(
                explicit_stride,
                resolver,
                f"register {name} array stride",
                required=True,
                minimum=1,
            )
        elif size.value is not None and address_unit_bits.value is not None:
            stride_address_units = _NumericFact(
                None,
                (size.value + address_unit_bits.value - 1) // address_unit_bits.value,
                FactState.KNOWN,
                "implicit tightly packed register stride",
            )
        else:
            stride_address_units = _NumericFact(
                None,
                None,
                FactState.TAINTED,
                "array stride cannot be derived from unresolved register size/addressUnitBits",
            )
            tainted = True
        stride_bytes = self._scale_address_fact(
            stride_address_units,
            bytes_per_address_unit,
            explicit_stride or node,
            f"register {name} array stride",
        )
        attributes["stride_address_units"] = stride_address_units.to_dict()
        attributes["stride_bytes"] = stride_bytes.to_dict()
        attributes["dim_increment"] = (
            stride_address_units.to_dict() if increment_node is not None else None
        )
        if stride_bytes.value is None:
            tainted = True

        labels: tuple[str, ...] | None = None
        if index_node is not None:
            if len(dimension_facts) != 1 or not dimension_facts or dimension_facts[0].value is None:
                self.report(
                    "OC1102",
                    Severity.WARNING,
                    (
                        f"IP-XACT register {name} dimIndex is supported only for one "
                        "resolved dimension"
                    ),
                    index_node,
                    taint=True,
                )
                tainted = True
            else:
                labels, reason = _parse_dim_index(index_node.text, dimension_facts[0].value)
                if labels is None:
                    self.report(
                        "OC1101",
                        Severity.ERROR,
                        f"IP-XACT register {name} has invalid dimIndex: {reason}",
                        index_node,
                        taint=True,
                    )
                    tainted = True

        placeholder_count = name.count("%s")
        if placeholder_count and (placeholder_count != 1 or len(dimension_facts) != 1):
            self.report(
                "OC1102",
                Severity.WARNING,
                (
                    f"IP-XACT register {name} uses a %s name template that cannot be "
                    "mapped unambiguously to its dimensions"
                ),
                node,
                taint=True,
            )
            tainted = True
        if tainted:
            return (_ArrayElement(name, (), (), 0, None),), attributes, True

        dimension_values = tuple(fact.value for fact in dimension_facts)
        if any(value is None for value in dimension_values) or stride_bytes.value is None:
            self.report(
                "OC1101",
                Severity.ERROR,
                f"IP-XACT register {name} array could not be expanded safely",
                node,
                taint=True,
            )
            return (_ArrayElement(name, (), (), 0, None),), attributes, True
        resolved_dimensions = tuple(value for value in dimension_values if value is not None)
        resolved_stride_bytes = stride_bytes.value
        elements: list[_ArrayElement] = []
        seen_names: set[str] = set()
        for linear_index, indices in enumerate(
            product(*(range(value) for value in resolved_dimensions))
        ):
            element_labels = (
                (labels[indices[0]],) if labels is not None else tuple(str(i) for i in indices)
            )
            if placeholder_count:
                element_name = name.replace("%s", element_labels[0])
            else:
                suffix = "".join(f"[{label}]" for label in element_labels)
                element_name = f"{name}{suffix}"
            if element_name in seen_names:
                self.report(
                    "OC1101",
                    Severity.ERROR,
                    f"IP-XACT register array {name} expands to duplicate name {element_name!r}",
                    node,
                    taint=True,
                )
                return (_ArrayElement(name, (), (), 0, None),), attributes, True
            seen_names.add(element_name)
            try:
                byte_delta = _checked_integer(linear_index * resolved_stride_bytes)
            except _ExpressionError as error:
                self.report(
                    "OC1101",
                    Severity.ERROR,
                    f"IP-XACT register array {name} byte offset is unsafe: {error}",
                    node,
                    taint=True,
                )
                return (_ArrayElement(name, (), (), 0, None),), attributes, True
            elements.append(
                _ArrayElement(
                    element_name,
                    tuple(indices),
                    element_labels,
                    linear_index,
                    byte_delta,
                )
            )
        attributes["expanded"] = True
        return tuple(elements), attributes, False

    def extract_memory_maps(self) -> tuple[list[dict[str, Any]], list[RegisterObservation]]:
        container = self.root.child("memoryMaps")
        if container is None:
            return [], []
        maps: list[dict[str, Any]] = []
        registers: list[RegisterObservation] = []
        for memory_map in container.children_named("memoryMap"):
            name_node = memory_map.child("name")
            name = name_node.text if name_node is not None else ""
            if not name:
                self.report(
                    "OC1101",
                    Severity.ERROR,
                    "IP-XACT memoryMap has no name",
                    memory_map,
                    taint=True,
                )
                continue
            resolver = self.resolver.extended(_parameter_entries(memory_map))
            address_unit_bits, bytes_per_address_unit = self._address_unit_facts(
                memory_map,
                resolver,
                name,
            )
            block_records: list[dict[str, Any]] = []
            self.objects.append(
                DesignObjectObservation(
                    "memory_map",
                    name,
                    scope=self.component_name,
                    provenance=self.location(memory_map, name),
                )
            )
            definition_ref = memory_map.child("memoryMapDefinitionRef")
            if definition_ref is not None:
                self.report(
                    "OC1102",
                    Severity.WARNING,
                    f"External memoryMapDefinitionRef on {name} is not expanded",
                    definition_ref,
                    taint=True,
                )
                self.objects.append(
                    DesignObjectObservation(
                        "memory_map_definition",
                        definition_ref.text,
                        relation="reference",
                        scope=f"{self.component_name}/{name}",
                        provenance=self.location(definition_ref),
                        status=FactState.UNSUPPORTED,
                        attributes={
                            "type_definitions": definition_ref.attribute("typeDefinitions")
                        },
                    )
                )
            for block in memory_map.children_named("addressBlock"):
                block_record, block_registers = self._extract_address_block(
                    block,
                    name,
                    resolver,
                    address_unit_bits,
                    bytes_per_address_unit,
                )
                if block_record is not None:
                    block_records.append(block_record)
                    registers.extend(block_registers)
            maps.append(
                {
                    "name": name,
                    "description": _description(memory_map),
                    "address_unit_bits": address_unit_bits.to_dict(),
                    "bytes_per_address_unit": bytes_per_address_unit.to_dict(),
                    "address_unit_bits_defaulted": memory_map.child("addressUnitBits") is None,
                    "address_blocks": block_records,
                    "parameters": self.parameter_records(
                        _parameter_entries(memory_map),
                        resolver,
                    ),
                }
            )
        return maps, registers

    def _extract_address_block(
        self,
        block: _XmlNode,
        memory_map: str,
        parent_resolver: _ParameterResolver,
        address_unit_bits: _NumericFact,
        bytes_per_address_unit: _NumericFact,
    ) -> tuple[dict[str, Any] | None, list[RegisterObservation]]:
        name_node = block.child("name")
        name = name_node.text if name_node is not None else ""
        if not name:
            self.report(
                "OC1101",
                Severity.ERROR,
                f"IP-XACT memory map {memory_map} has an unnamed addressBlock",
                block,
                taint=True,
            )
            return None, []
        resolver = parent_resolver.extended(_parameter_entries(block))
        base = self.numeric(
            block.child("baseAddress"),
            resolver,
            f"address block {memory_map}/{name} baseAddress",
            required=True,
            minimum=0,
        )
        base_bytes = self._scale_address_fact(
            base,
            bytes_per_address_unit,
            block.child("baseAddress") or block,
            f"address block {memory_map}/{name} baseAddress",
        )
        address_range = self.numeric(
            block.child("range"),
            resolver,
            f"address block {memory_map}/{name} range",
            required=True,
            minimum=1,
        )
        width = self.numeric(
            block.child("width"),
            resolver,
            f"address block {memory_map}/{name} width",
            required=True,
            minimum=1,
        )
        array_unsupported = self._array_syntax_present(block)
        if array_unsupported:
            self.report(
                "OC1102",
                Severity.WARNING,
                (
                    f"IP-XACT address block array {memory_map}/{name} is retained but "
                    "its contained register addresses are not expanded"
                ),
                block,
                taint=True,
            )
        self.objects.append(
            DesignObjectObservation(
                "address_block",
                name,
                scope=f"{self.component_name}/{memory_map}",
                provenance=self.location(block, name),
                status=(
                    FactState.KNOWN
                    if not array_unsupported
                    and all(item.value is not None for item in (base_bytes, address_range, width))
                    else FactState.TAINTED
                ),
            )
        )
        registers: list[RegisterObservation] = []
        register_records: list[dict[str, Any]] = []
        register_file_records: list[dict[str, Any]] = []
        for register in block.children_named("register"):
            records, observations = self._extract_register(
                register,
                memory_map,
                name,
                resolver,
                None if array_unsupported else base_bytes.value,
                0,
                address_unit_bits,
                bytes_per_address_unit,
                (),
            )
            register_records.extend(records)
            registers.extend(observations)
        for register_file in block.children_named("registerFile"):
            file_record, file_registers = self._extract_register_file(
                register_file,
                memory_map,
                name,
                resolver,
                None if array_unsupported else base_bytes.value,
                0,
                address_unit_bits,
                bytes_per_address_unit,
                (),
            )
            if file_record is not None:
                register_file_records.append(file_record)
                registers.extend(file_registers)
        definition_ref = block.child("addressBlockDefinitionRef")
        if definition_ref is not None:
            self.report(
                "OC1102",
                Severity.WARNING,
                f"External addressBlockDefinitionRef on {memory_map}/{name} is not expanded",
                definition_ref,
                taint=True,
            )
        return (
            {
                "name": name,
                "description": _description(block),
                "base_address": base_bytes.to_dict(),
                "base_address_address_units": base.to_dict(),
                "range": address_range.to_dict(),
                "width": width.to_dict(),
                "usage": _child_text(block, "usage"),
                "access": _child_text(block, "access"),
                "registers": register_records,
                "register_files": register_file_records,
                "parameters": self.parameter_records(_parameter_entries(block), resolver),
            },
            registers,
        )

    def _extract_register_file(
        self,
        node: _XmlNode,
        memory_map: str,
        address_block: str,
        parent_resolver: _ParameterResolver,
        block_base: int | None,
        parent_offset: int | None,
        address_unit_bits: _NumericFact,
        bytes_per_address_unit: _NumericFact,
        parent_files: tuple[str, ...],
    ) -> tuple[dict[str, Any] | None, list[RegisterObservation]]:
        name_node = node.child("name")
        name = name_node.text if name_node is not None else ""
        if not name:
            self.report(
                "OC1101",
                Severity.ERROR,
                f"IP-XACT address block {address_block} has an unnamed registerFile",
                node,
                taint=True,
            )
            return None, []
        resolver = parent_resolver.extended(_parameter_entries(node))
        offset = self.numeric(
            node.child("addressOffset"),
            resolver,
            f"register file {name} addressOffset",
            required=True,
            minimum=0,
        )
        offset_bytes = self._scale_address_fact(
            offset,
            bytes_per_address_unit,
            node.child("addressOffset") or node,
            f"register file {name} addressOffset",
        )
        local_offset = (
            parent_offset + offset_bytes.value
            if parent_offset is not None and offset_bytes.value is not None
            else None
        )
        array_unsupported = self._array_syntax_present(node)
        if array_unsupported:
            self.report(
                "OC1102",
                Severity.WARNING,
                (
                    f"IP-XACT register file array {name} is retained but its contained "
                    "register addresses are not expanded"
                ),
                node,
                taint=True,
            )
            local_offset = None
        scope = "/".join((self.component_name, memory_map, address_block, *parent_files))
        self.objects.append(
            DesignObjectObservation(
                "register_file",
                name,
                scope=scope,
                provenance=self.location(node, name),
                status=(
                    FactState.KNOWN
                    if offset_bytes.value is not None and not array_unsupported
                    else FactState.TAINTED
                ),
            )
        )
        records: list[dict[str, Any]] = []
        registers: list[RegisterObservation] = []
        for register in node.children_named("register"):
            child_records, child_observations = self._extract_register(
                register,
                memory_map,
                address_block,
                resolver,
                block_base,
                local_offset,
                address_unit_bits,
                bytes_per_address_unit,
                (*parent_files, name),
            )
            records.extend(child_records)
            registers.extend(child_observations)
        nested_records: list[dict[str, Any]] = []
        for nested in node.children_named("registerFile"):
            nested_record, nested_registers = self._extract_register_file(
                nested,
                memory_map,
                address_block,
                resolver,
                block_base,
                local_offset,
                address_unit_bits,
                bytes_per_address_unit,
                (*parent_files, name),
            )
            if nested_record is not None:
                nested_records.append(nested_record)
                registers.extend(nested_registers)
        return (
            {
                "name": name,
                "address_offset": offset_bytes.to_dict(),
                "address_offset_address_units": offset.to_dict(),
                "range": self.numeric(
                    node.child("range"),
                    resolver,
                    f"register file {name} range",
                    minimum=1,
                ).to_dict(),
                "registers": records,
                "register_files": nested_records,
            },
            registers,
        )

    def _extract_register(
        self,
        node: _XmlNode,
        memory_map: str,
        address_block: str,
        parent_resolver: _ParameterResolver,
        block_base: int | None,
        parent_offset: int | None,
        address_unit_bits: _NumericFact,
        bytes_per_address_unit: _NumericFact,
        register_files: tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], list[RegisterObservation]]:
        name_node = node.child("name")
        name = name_node.text if name_node is not None else ""
        if not name:
            self.report(
                "OC1101",
                Severity.ERROR,
                f"IP-XACT address block {address_block} has an unnamed register",
                node,
                taint=True,
            )
            return [], []
        resolver = parent_resolver.extended(_parameter_entries(node))
        offset = self.numeric(
            node.child("addressOffset"),
            resolver,
            f"register {name} addressOffset",
            required=True,
            minimum=0,
        )
        offset_bytes = self._scale_address_fact(
            offset,
            bytes_per_address_unit,
            node.child("addressOffset") or node,
            f"register {name} addressOffset",
        )
        size = self.numeric(
            node.child("size"),
            resolver,
            f"register {name} size",
            required=True,
            minimum=1,
        )
        scope = "/".join((self.component_name, memory_map, address_block, *register_files))
        field_scope = f"{scope}/{name}"
        fields: list[RegisterFieldObservation] = []
        field_records: list[dict[str, Any]] = []
        for field in node.children_named("field"):
            field_record, field_observation = self._extract_field(
                field,
                name,
                resolver,
                field_scope,
            )
            if field_record is not None and field_observation is not None:
                field_records.append(field_record)
                fields.append(field_observation)
        definition_ref = node.child("registerDefinitionRef")
        unsupported_definition = definition_ref is not None
        if definition_ref is not None:
            self.report(
                "OC1102",
                Severity.WARNING,
                f"External registerDefinitionRef on {name} is not expanded",
                definition_ref,
                taint=True,
            )
        array_elements, array_attributes, array_tainted = self._register_array_elements(
            node,
            name,
            resolver,
            size,
            address_unit_bits,
            bytes_per_address_unit,
        )
        location = self.location(node, name)
        observations: list[RegisterObservation] = []
        records: list[dict[str, Any]] = []
        for element in array_elements:
            local_byte_offset = (
                offset_bytes.value + element.byte_delta
                if offset_bytes.value is not None and element.byte_delta is not None
                else None
            )
            total_offset = (
                parent_offset + local_byte_offset
                if parent_offset is not None and local_byte_offset is not None
                else None
            )
            absolute = (
                block_base + total_offset
                if block_base is not None and total_offset is not None
                else None
            )
            status = (
                FactState.TAINTED
                if unsupported_definition
                or array_tainted
                or local_byte_offset is None
                or total_offset is None
                or absolute is None
                or size.value is None
                or any(field.status != FactState.KNOWN for field in fields)
                else FactState.KNOWN
            )
            local_offset_fact = _NumericFact(
                offset.expression,
                local_byte_offset,
                FactState.KNOWN if local_byte_offset is not None else FactState.TAINTED,
                None if local_byte_offset is not None else "register array offset is unresolved",
            )
            observation = RegisterObservation(
                element.name,
                component=self.component_name,
                memory_map=memory_map,
                address_block=address_block,
                address_offset=total_offset,
                absolute_address=absolute,
                size_bits=size.value,
                access=_child_text(node, "access"),
                fields=tuple(fields),
                provenance=location,
                status=status,
                attributes={
                    "description": _description(node),
                    "local_address_offset": local_offset_fact.to_dict(),
                    "local_address_offset_address_units": offset.to_dict(),
                    "address_unit_bits": address_unit_bits.to_dict(),
                    "bytes_per_address_unit": bytes_per_address_unit.to_dict(),
                    "register_files": list(register_files),
                    "dimensions": array_attributes["dimensions"],
                    "dim_increment": array_attributes.get("dim_increment"),
                    "array": dict(array_attributes),
                    "array_template_name": name if array_attributes["syntax"] != "scalar" else None,
                    "array_indices": list(element.indices),
                    "array_index_labels": list(element.labels),
                    "array_linear_index": element.linear_index,
                    "volatile": _child_text(node, "volatile"),
                    "fields": field_records,
                },
            )
            observations.append(observation)
            self.objects.append(
                DesignObjectObservation(
                    "register",
                    element.name,
                    scope=scope,
                    provenance=location,
                    status=status,
                    attributes={
                        "absolute_address": absolute,
                        "size_bits": size.value,
                        "array_template_name": name,
                        "array_indices": list(element.indices),
                    },
                )
            )
            records.append(
                {
                    "name": element.name,
                    "source_name": name,
                    "description": _description(node),
                    "address_offset": local_offset_fact.to_dict(),
                    "address_offset_address_units": offset.to_dict(),
                    "absolute_address": absolute,
                    "size": size.to_dict(),
                    "access": observation.access,
                    "fields": field_records,
                    "array_indices": list(element.indices),
                    "array_index_labels": list(element.labels),
                    "status": status.value,
                }
            )
        return records, observations

    def _extract_field(
        self,
        node: _XmlNode,
        register_name: str,
        parent_resolver: _ParameterResolver,
        register_scope: str,
    ) -> tuple[dict[str, Any] | None, RegisterFieldObservation | None]:
        name_node = node.child("name")
        name = name_node.text if name_node is not None else ""
        if not name:
            self.report(
                "OC1101",
                Severity.ERROR,
                f"IP-XACT register {register_name} has an unnamed field",
                node,
                taint=True,
            )
            return None, None
        resolver = parent_resolver.extended(_parameter_entries(node))
        offset = self.numeric(
            node.child("bitOffset"),
            resolver,
            f"field {register_name}/{name} bitOffset",
            required=node.child("bitRange") is None,
            minimum=0,
        )
        width = self.numeric(
            node.child("bitWidth"),
            resolver,
            f"field {register_name}/{name} bitWidth",
            required=node.child("bitRange") is None,
            minimum=1,
        )
        bit_range = node.child("bitRange")
        range_record: dict[str, Any] | None = None
        if bit_range is not None:
            left = self.numeric(
                bit_range.child("left"),
                resolver,
                f"field {register_name}/{name} bitRange left",
                required=True,
                minimum=0,
            )
            right = self.numeric(
                bit_range.child("right"),
                resolver,
                f"field {register_name}/{name} bitRange right",
                required=True,
                minimum=0,
            )
            range_record = {"left": left.to_dict(), "right": right.to_dict()}
            if left.value is not None and right.value is not None:
                offset = _NumericFact(
                    f"min({left.expression}, {right.expression})",
                    min(left.value, right.value),
                    FactState.KNOWN,
                )
                width = _NumericFact(
                    f"abs({left.expression} - {right.expression}) + 1",
                    abs(left.value - right.value) + 1,
                    FactState.KNOWN,
                )
        resets: list[dict[str, Any]] = []
        resets_container = node.child("resets")
        if resets_container is not None:
            for reset in resets_container.children_named("reset"):
                value = self.numeric(
                    reset.child("value"),
                    resolver,
                    f"field {register_name}/{name} reset value",
                    required=True,
                    minimum=0,
                )
                mask = self.numeric(
                    reset.child("mask"),
                    resolver,
                    f"field {register_name}/{name} reset mask",
                    minimum=0,
                )
                resets.append(
                    {
                        "value": value.to_dict(),
                        "mask": mask.to_dict(),
                        "reset_type_ref": reset.attribute("resetTypeRef"),
                    }
                )
        reset_value = None
        if len(resets) == 1 and resets[0]["value"]["state"] == FactState.KNOWN.value:
            reset_value = resets[0]["value"]["value"]
        status = (
            FactState.KNOWN
            if offset.value is not None
            and width.value is not None
            and all(item["value"]["state"] == FactState.KNOWN.value for item in resets)
            else FactState.TAINTED
        )
        location = self.location(node, name)
        observation = RegisterFieldObservation(
            name,
            bit_offset=offset.value,
            bit_width=width.value,
            access=_child_text(node, "access"),
            reset_value=reset_value,
            provenance=location,
            status=status,
            attributes={
                "description": _description(node),
                "bit_offset": offset.to_dict(),
                "bit_width": width.to_dict(),
                "bit_range": range_record,
                "resets": resets,
                "volatile": _child_text(node, "volatile"),
            },
        )
        self.objects.append(
            DesignObjectObservation(
                "register_field",
                name,
                scope=register_scope,
                provenance=location,
                status=status,
                attributes={"bit_offset": offset.value, "bit_width": width.value},
            )
        )
        return (
            {
                "name": name,
                "description": _description(node),
                "bit_offset": offset.to_dict(),
                "bit_width": width.to_dict(),
                "bit_range": range_record,
                "access": observation.access,
                "resets": resets,
                "status": status.value,
            },
            observation,
        )


def _mark_extraction_tainted(extraction: _Extraction) -> _Extraction:
    component = replace(
        extraction.component,
        ports=tuple(replace(port, status=FactState.TAINTED) for port in extraction.component.ports),
        status=FactState.TAINTED,
    )
    registers = tuple(
        replace(
            item,
            fields=tuple(replace(field, status=FactState.TAINTED) for field in item.fields),
            status=FactState.TAINTED,
        )
        for item in extraction.registers
    )
    return _Extraction(
        component,
        tuple(replace(item, status=FactState.TAINTED) for item in extraction.interfaces),
        registers,
        tuple(replace(item, status=FactState.TAINTED) for item in extraction.objects),
        tuple(replace(item, status=FactState.TAINTED) for item in extraction.clocks),
        extraction.diagnostics,
        True,
    )


def parse_ipxact(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    parameter_values: Mapping[str, int] | None = None,
) -> ViewObservation:
    """Parse IP-XACT component documents from IEEE 1685-2009/2014/2022.

    ``parameter_values`` supplies integer overrides for otherwise external
    expression identifiers.  The importer never executes source expressions.
    """

    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="ipxact", name=view_name)
    overrides = dict(parameter_values or {})
    if any(isinstance(value, bool) or not isinstance(value, int) for value in overrides.values()):
        raise TypeError("IP-XACT parameter_values must map names to integers")

    components: list[ComponentObservation] = []
    interfaces: list[InterfaceObservation] = []
    registers: list[RegisterObservation] = []
    objects: list[DesignObjectObservation] = []
    clocks: list[ClockObservation] = []
    diagnostics: list[Diagnostic] = []
    tainted_scopes: set[str] = set()
    complete = True
    namespace_versions: dict[str, str | None] = {}
    encodings: dict[str, str] = {}
    for path in source_paths:
        source = read_source(path, view)
        diagnostics.extend(source.diagnostics)
        encodings[str(path)] = source.encoding
        if not source.text:
            complete = False
            tainted_scopes.add("*")
            continue
        root, xml_diagnostics = _parse_xml(source.text, path, view)
        diagnostics.extend(xml_diagnostics)
        if root is None:
            complete = False
            tainted_scopes.add("*")
            continue
        if root.local_name != "component":
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    f"IP-XACT document root is {root.local_name!r}, expected 'component'",
                    location=Provenance(str(path), root.line, root.column, view),
                )
            )
            complete = False
            tainted_scopes.add("*")
            continue
        namespace_match = _NAMESPACE_RE.fullmatch(root.namespace)
        namespace_version = namespace_match.group(1) if namespace_match else None
        namespace_versions[str(path)] = namespace_version
        namespace_tainted = namespace_version is None
        if namespace_tainted:
            diagnostics.append(
                parser_diagnostic(
                    "OC1102",
                    Severity.WARNING,
                    (
                        f"Unsupported IP-XACT namespace {root.namespace!r}; local-name "
                        "extraction is retained but marked tainted"
                    ),
                    location=Provenance(str(path), root.line, root.column, view),
                    metadata={"namespace": root.namespace},
                )
            )
        name_node = root.child("name")
        component_name = name_node.text if name_node is not None else ""
        if not component_name:
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    "IP-XACT component has no name",
                    location=Provenance(str(path), root.line, root.column, view),
                )
            )
            complete = False
            tainted_scopes.add("*")
            continue
        extraction = _ComponentExtractor(
            root,
            path,
            view,
            component_name,
            namespace_version,
            overrides,
        ).extract()
        if source.tainted or namespace_tainted:
            extraction = _mark_extraction_tainted(extraction)
        diagnostics.extend(extraction.diagnostics)
        components.append(extraction.component)
        interfaces.extend(extraction.interfaces)
        registers.extend(extraction.registers)
        objects.extend(extraction.objects)
        clocks.extend(extraction.clocks)
        if extraction.tainted or source.tainted or namespace_tainted:
            complete = False
            tainted_scopes.add(component_name)

    return ViewObservation(
        view,
        tuple(components),
        diagnostics=tuple(diagnostics),
        complete=complete,
        tainted_scopes=frozenset(tainted_scopes),
        objects=tuple(objects),
        clocks=tuple(clocks),
        interfaces=tuple(interfaces),
        registers=tuple(registers),
        attributes={
            "parser": "stdlib-expat-ipxact",
            "source_files": [str(path) for path in source_paths],
            "namespace_versions": namespace_versions,
            "encodings": encodings,
        },
    )


def parse_ip_xact(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    parameter_values: Mapping[str, int] | None = None,
) -> ViewObservation:
    """Spelling-compatible alias for :func:`parse_ipxact`."""

    return parse_ipxact(
        paths,
        view_id=view_id,
        view_name=view_name,
        parameter_values=parameter_values,
    )


class IpxactParser:
    format_name = "ipxact"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_ipxact(paths, view_id=view_id, **options)


IPXACTParser = IpxactParser

__all__ = [
    "IPXACTParser",
    "IpxactParser",
    "parse_ip_xact",
    "parse_ipxact",
]

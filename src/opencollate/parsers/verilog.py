"""SystemVerilog and Verilog import through the slang compiler frontend."""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencollate.boolean import BooleanSyntaxError, parse_boolean
from opencollate.diagnostics import Diagnostic, Severity
from opencollate.model import (
    BusShape,
    ComponentKind,
    ComponentObservation,
    ConnectivityEdge,
    ConnectivityEndpoint,
    DesignObjectObservation,
    Direction,
    FactState,
    IndexRange,
    PortObservation,
    Provenance,
    ViewId,
    ViewObservation,
    decoded_identifier,
)
from opencollate.parsers.base import (
    Pathish,
    coerce_paths,
    coerce_view,
    infer_role_from_name,
    parser_diagnostic,
    unavailable_view,
)

_DIAG_NAME = re.compile(r"DiagCode\(([^)]+)\)")
_MAX_DESIGN_OBJECTS = 100_000
_MAX_CONNECTIVITY_ENDPOINTS = 250_000
_MAX_CONNECTIVITY_EDGES = 1_000_000
_MAX_CONNECTIVITY_VECTOR_BITS = 4_096
_MAX_TAINTED_CROSS_PRODUCT = 16_384
_CANONICAL_PATH_RESERVED = frozenset("%/[]*?;\\")
_CONNECTIVITY_INERT_SYMBOLS = frozenset(
    {
        "AttributeSymbol",
        "DefParamSymbol",
        "ElabSystemTaskSymbol",
        "EmptyMemberSymbol",
        "ExplicitImportSymbol",
        "GenvarSymbol",
        "LetDeclSymbol",
        "ParameterSymbol",
        "PropertySymbol",
        "PulseStyleSymbol",
        "SequenceSymbol",
        "SpecifyBlockSymbol",
        "SpecparamSymbol",
        "SubroutineSymbol",
        "SystemTimingCheckSymbol",
        "TimingPathSymbol",
        "TypeParameterSymbol",
        "WildcardImportSymbol",
    }
)


def _canonical_path_segment(value: str) -> str:
    """Encode characters that have structural meaning in endpoint selectors."""

    return "".join(
        f"%{ord(character):02X}" if character in _CANONICAL_PATH_RESERVED else character
        for character in value
    )


def _decode_hierarchical_path(path: str) -> str:
    """Translate slang's SV hierarchy spelling without splitting escaped names.

    Slang preserves the whitespace terminator for escaped identifiers, so a
    path such as ``top.\\irq.status `` can be distinguished from the ordinary
    hierarchy ``top.irq.status``.  A plain ``str.replace`` loses that
    distinction and can silently attach connectivity facts to the wrong
    endpoint.
    """

    segments: list[str] = []
    current: list[str] = []
    escaped = False
    segment_was_escaped = False
    for character in path.strip():
        if escaped:
            if character.isspace():
                escaped = False
            else:
                current.append(character)
            continue
        if character == "\\":
            escaped = True
            segment_was_escaped = True
        elif character == ".":
            segment = "".join(current).strip()
            if segment:
                segments.append(
                    _canonical_path_segment(segment) if segment_was_escaped else segment
                )
            current.clear()
            segment_was_escaped = False
        else:
            current.append(character)
    segment = "".join(current).strip()
    if segment:
        segments.append(_canonical_path_segment(segment) if segment_was_escaped else segment)
    return "/".join(segments)


@dataclass(frozen=True, slots=True)
class _SlangApi:
    module: Any
    syntax_tree: Any
    compilation: Any
    compilation_options: Any
    preprocessor_options: Any


def _load_slang() -> _SlangApi | None:
    try:
        pyslang = importlib.import_module("pyslang")
    except ImportError:
        return None

    # slang 11 placed major APIs into submodules; older supported versions
    # exported them at the package root.  Supporting both costs very little.
    try:
        ast_module = importlib.import_module("pyslang.ast")
        parsing_module = importlib.import_module("pyslang.parsing")
        syntax_module = importlib.import_module("pyslang.syntax")
        Compilation = ast_module.Compilation
        CompilationOptions = ast_module.CompilationOptions
        PreprocessorOptions = parsing_module.PreprocessorOptions
        SyntaxTree = syntax_module.SyntaxTree
    except ImportError:  # pragma: no cover - exercised with pyslang < 11
        Compilation = pyslang.Compilation
        CompilationOptions = pyslang.CompilationOptions
        PreprocessorOptions = pyslang.PreprocessorOptions
        SyntaxTree = pyslang.SyntaxTree
    return _SlangApi(
        pyslang,
        SyntaxTree,
        Compilation,
        CompilationOptions,
        PreprocessorOptions,
    )


def _predefines(defines: Mapping[str, Any] | Sequence[str]) -> list[str]:
    if isinstance(defines, Mapping):
        result: list[str] = []
        for name, value in sorted(defines.items()):
            result.append(str(name) if value is None else f"{name}={value}")
        return result
    return [str(item) for item in defines]


def _source_location(source_manager: Any, location: Any, view: ViewId) -> Provenance:
    try:
        source = str(source_manager.getFileName(location))
        line = int(source_manager.getLineNumber(location))
        column = int(source_manager.getColumnNumber(location))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        source, line, column = "<systemverilog>", 1, 1
    return Provenance(source, max(1, line), max(1, column), view)


def _direction(value: Any) -> Direction:
    name = getattr(value, "name", str(value).rsplit(".", 1)[-1]).lower()
    return {
        "in": Direction.INPUT,
        "input": Direction.INPUT,
        "out": Direction.OUTPUT,
        "output": Direction.OUTPUT,
        "inout": Direction.INOUT,
    }.get(name, Direction.UNKNOWN)


def _shape(data_type: Any) -> tuple[BusShape, FactState, str | None]:
    if data_type is None or bool(getattr(data_type, "isError", False)):
        return BusShape.unknown(), FactState.TAINTED, "slang could not resolve the port type"

    packed: list[IndexRange] = []
    unpacked: list[IndexRange] = []
    current = data_type
    visited: set[int] = set()
    try:
        while id(current) not in visited:
            visited.add(id(current))
            constant_range = getattr(current, "range", None)
            if constant_range is not None and hasattr(constant_range, "left"):
                index_range = IndexRange(
                    int(constant_range.left),
                    int(constant_range.right),
                )
                if bool(getattr(current, "isPackedArray", False)):
                    packed.append(index_range)
                elif bool(getattr(current, "isUnpackedArray", False)):
                    unpacked.append(index_range)
            if not hasattr(current, "elementType"):
                break
            current = current.elementType
        width = int(data_type.bitWidth)
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError) as error:
        return BusShape.unknown(), FactState.UNSUPPORTED, str(error)

    if width < 1:
        return BusShape.unknown(), FactState.UNKNOWN, "port width is not a positive constant"
    primary = packed[0] if len(packed) == 1 else None
    return (
        BusShape(
            width=width,
            left=primary.left if primary else None,
            right=primary.right if primary else None,
            packed=tuple(packed),
            unpacked=tuple(unpacked),
            bit_indices=primary.ordered_indices if primary else (),
            explicit_scalar=not packed and not unpacked and width == 1,
        ),
        FactState.KNOWN,
        None,
    )


def _slang_diagnostics(
    slang: Any,
    compilation: Any,
    view: ViewId,
) -> tuple[tuple[Diagnostic, ...], frozenset[str], bool]:
    result: list[Diagnostic] = []
    tainted: set[str] = set()
    complete = True
    engine = slang.DiagnosticEngine(compilation.sourceManager)
    for raw in compilation.getAllDiagnostics():
        severity_value = engine.getSeverity(raw.code, raw.location)
        severity_name = getattr(severity_value, "name", str(severity_value)).lower()
        is_error = bool(raw.isError())
        # Forcing every definition to be a default top can produce ordinary
        # warnings such as unconnected inputs.  They are not parse facts and
        # would make collateral reports noisy, so retain errors only.
        if not is_error and severity_name not in {"fatal", "error"}:
            continue
        match = _DIAG_NAME.search(str(raw.code))
        backend_code = match.group(1) if match else str(raw.code)
        location = _source_location(compilation.sourceManager, raw.location, view)
        message = engine.formatMessage(raw)
        result.append(
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                f"SystemVerilog: {message}",
                location=location,
                metadata={"backend": "slang", "backend_code": backend_code},
            )
        )
        complete = False
        symbol = getattr(raw, "symbol", None)
        scope = str(getattr(symbol, "name", "")).strip()
        tainted.add(scope or "*")
    return tuple(result), frozenset(tainted), complete


def _extract_functions(
    instance: Any,
    ports: tuple[PortObservation, ...],
    source_manager: Any,
    view: ViewId,
) -> tuple[dict[str, str], list[Diagnostic]]:
    scalar_outputs = {
        port.native_name
        for port in ports
        if port.direction == Direction.OUTPUT and port.shape.width == 1
    }
    functions: dict[str, str] = {}
    diagnostics: list[Diagnostic] = []
    for member in instance.body:
        if type(member).__name__ != "ContinuousAssignSymbol":
            continue
        syntax = getattr(member, "syntax", None)
        if syntax is None or not hasattr(syntax, "left") or not hasattr(syntax, "right"):
            continue
        left = decoded_identifier(str(syntax.left).strip())
        if left not in scalar_outputs:
            continue
        right = str(syntax.right).strip()
        location = _source_location(source_manager, member.location, view)
        try:
            parse_boolean(right)
        except BooleanSyntaxError as error:
            diagnostics.append(
                parser_diagnostic(
                    "OC1102",
                    Severity.WARNING,
                    (
                        f"Cannot compare Boolean function for {instance.name}/{left}: "
                        f"{error.message}"
                    ),
                    location=location,
                    metadata={"expression": right, "offset": error.offset},
                )
            )
            continue
        if left in functions:
            functions.pop(left, None)
            diagnostics.append(
                parser_diagnostic(
                    "OC1102",
                    Severity.WARNING,
                    f"Multiple continuous assignments drive {instance.name}/{left}",
                    location=location,
                )
            )
            continue
        functions[left] = right
    return functions, diagnostics


def _relative_hierarchical_path(symbol: Any, component_name: str) -> str:
    try:
        path = str(symbol.hierarchicalPath)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        path = str(getattr(symbol, "name", ""))
    path = _decode_hierarchical_path(path)
    root = _canonical_path_segment(component_name)
    prefix = f"{root}/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    if path == root:
        return ""
    return path


def _extract_design_objects(
    instance: Any,
    source_manager: Any,
    view: ViewId,
    component_name: str,
) -> tuple[list[DesignObjectObservation], list[Diagnostic]]:
    """Index elaborated hierarchy for SDC and UPF reference checking."""

    objects: list[DesignObjectObservation] = []
    diagnostics: list[Diagnostic] = []
    visited_scopes: set[int] = set()
    limit_reached = False

    def append(item: DesignObjectObservation) -> bool:
        nonlocal limit_reached
        if len(objects) >= _MAX_DESIGN_OBJECTS:
            if not limit_reached:
                diagnostics.append(
                    parser_diagnostic(
                        "OC1102",
                        Severity.WARNING,
                        f"Hierarchy index for {component_name} exceeds "
                        f"{_MAX_DESIGN_OBJECTS:,} objects and was truncated",
                        location=_source_location(source_manager, instance.location, view),
                    )
                )
                limit_reached = True
            return False
        objects.append(item)
        return True

    def walk(scope: Any, port_names: set[str]) -> None:
        if limit_reached or id(scope) in visited_scopes:
            return
        visited_scopes.add(id(scope))
        try:
            members = list(scope)
        except (RuntimeError, TypeError):
            return
        for member in members:
            symbol_type = type(member).__name__
            name = decoded_identifier(str(getattr(member, "name", "")))
            location = _source_location(
                source_manager,
                getattr(member, "location", instance.location),
                view,
            )
            if symbol_type == "InstanceSymbol":
                path = _relative_hierarchical_path(member, component_name) or (
                    _canonical_path_segment(name)
                )
                definition = decoded_identifier(str(getattr(member.definition, "name", "")))
                if not append(
                    DesignObjectObservation(
                        kind="instance",
                        native_name=path,
                        scope=component_name,
                        provenance=location,
                        attributes={
                            "component_type": definition,
                            "hierarchical_path": str(getattr(member, "hierarchicalPath", path)),
                        },
                    )
                ):
                    return
                child_ports: set[str] = set()
                try:
                    raw_ports = list(member.body.portList)
                except (AttributeError, RuntimeError, TypeError):
                    raw_ports = []
                for raw_port in raw_ports:
                    port_name = decoded_identifier(str(raw_port.name))
                    canonical_port_name = _canonical_path_segment(port_name)
                    child_ports.add(port_name)
                    append(
                        DesignObjectObservation(
                            kind="pin",
                            native_name=f"{path}/{canonical_port_name}",
                            scope=component_name,
                            provenance=_source_location(
                                source_manager,
                                raw_port.location,
                                view,
                            ),
                            attributes={
                                "instance": path,
                                "component_type": definition,
                                "port": port_name,
                                "direction": _direction(raw_port.direction).value,
                            },
                        )
                    )
                walk(member.body, child_ports)
                continue
            if symbol_type in {"GenerateBlockArraySymbol", "InstanceArraySymbol"}:
                try:
                    children = list(getattr(member, "entries", member))
                except (RuntimeError, TypeError):
                    children = []
                for child in children:
                    walk(child, set())
                continue
            if symbol_type in {"GenerateBlockSymbol", "CheckerInstanceSymbol"}:
                walk(member, set())
                continue
            if symbol_type not in {"NetSymbol", "VariableSymbol"} or not name:
                continue
            if name in port_names:
                continue
            path = _relative_hierarchical_path(member, component_name) or (
                _canonical_path_segment(name)
            )
            append(
                DesignObjectObservation(
                    kind="net",
                    native_name=path,
                    scope=component_name,
                    provenance=location,
                    attributes={"slang_symbol": symbol_type},
                )
            )

    root_ports: set[str] = set()
    try:
        raw_root_ports = list(instance.body.portList)
    except (AttributeError, RuntimeError, TypeError):
        raw_root_ports = []
    for raw_port in raw_root_ports:
        port_name = decoded_identifier(str(raw_port.name))
        root_ports.add(port_name)
        append(
            DesignObjectObservation(
                kind="port",
                native_name=port_name,
                scope=component_name,
                provenance=_source_location(source_manager, raw_port.location, view),
                attributes={"direction": _direction(raw_port.direction).value},
            )
        )
    walk(instance.body, root_ports)
    return objects, diagnostics


@dataclass(frozen=True, slots=True)
class _ConnectivityExpression:
    bits: tuple[tuple[ConnectivityEndpoint, bool], ...] = ()
    supported: bool = True
    reason: str | None = None


def _extract_connectivity(
    instance: Any,
    source_manager: Any,
    view: ViewId,
) -> tuple[
    tuple[ConnectivityEndpoint, ...],
    tuple[ConnectivityEdge, ...],
    tuple[Diagnostic, ...],
    bool,
]:
    """Extract an exact, bounded transparent connectivity graph from slang.

    The extractor deliberately stops at procedural or non-transparent logic.
    Possible edges across those frontiers are marked tainted so they can make
    an absence result inconclusive without being used as proof of a path.
    """

    endpoints: dict[str, ConnectivityEndpoint] = {}
    edges: list[ConnectivityEdge] = []
    edge_keys: set[tuple[str, str, str, FactState, bool | None]] = set()
    diagnostics: list[Diagnostic] = []
    reported: set[tuple[str, str]] = set()
    visited_instances: set[str] = set()
    driven: set[str] = set()
    connectivity_complete = True

    def location(symbol: Any) -> Provenance:
        return _source_location(
            source_manager,
            getattr(symbol, "location", instance.location),
            view,
        )

    def report_once(key: str, message: str, symbol: Any) -> None:
        marker = (key, message)
        if marker in reported:
            return
        reported.add(marker)
        # Ordinary procedural or combinational logic is expected RTL.  Its
        # frontier becomes an OC6505 only when a connectivity requirement
        # actually depends on it; emitting parser warnings for every such cone
        # would make projects with no connectivity intent noisy.  Global
        # resource-limit failures remain immediate parser diagnostics.
        if key != "*":
            return
        diagnostics.append(
            parser_diagnostic(
                "OC1102",
                Severity.WARNING,
                message,
                location=location(symbol),
                metadata={"analysis": "static_connectivity", "frontier": key},
            )
        )

    def hierarchical_name(symbol: Any) -> str:
        try:
            raw = str(symbol.hierarchicalPath)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            raw = str(getattr(symbol, "name", ""))
        return _decode_hierarchical_path(raw)

    def endpoint_bits(symbol: Any) -> tuple[ConnectivityEndpoint, ...]:
        nonlocal connectivity_complete
        name = hierarchical_name(symbol)
        if not name:
            return ()
        data_type = getattr(symbol, "type", None)
        if data_type is None:
            data_type = getattr(symbol, "declaredType", None)
        shape, state, detail = _shape(data_type)
        if state != FactState.KNOWN or shape.width is None:
            connectivity_complete = False
            report_once(
                name,
                f"Static connectivity cannot resolve the shape of {name}: "
                f"{detail or 'unknown type'}",
                symbol,
            )
            return ()
        if shape.width > _MAX_CONNECTIVITY_VECTOR_BITS:
            connectivity_complete = False
            report_once(
                name,
                f"Static connectivity vector {name} has {shape.width:,} bits; "
                f"the per-vector limit is {_MAX_CONNECTIVITY_VECTOR_BITS:,}",
                symbol,
            )
            return ()
        if shape.width == 1:
            scalar_indices = shape.ordered_indices
            indices: tuple[int | None, ...] = (
                tuple(scalar_indices) if scalar_indices is not None else (None,)
            )
        else:
            ordered = shape.ordered_indices
            if ordered is None or len(ordered) != shape.width:
                connectivity_complete = False
                report_once(
                    name,
                    f"Static connectivity does not flatten multidimensional or "
                    f"non-indexed vector {name}",
                    symbol,
                )
                return ()
            indices = tuple(ordered)
        result: list[ConnectivityEndpoint] = []
        for ordinal, bit_index in enumerate(indices):
            candidate = ConnectivityEndpoint(
                name,
                bit_index=bit_index,
                ordinal=ordinal,
                width=shape.width,
                provenance=location(symbol),
                attributes={"slang_symbol": type(symbol).__name__},
            )
            existing = endpoints.get(candidate.key)
            if existing is None:
                if len(endpoints) >= _MAX_CONNECTIVITY_ENDPOINTS:
                    connectivity_complete = False
                    report_once(
                        "*",
                        f"Static connectivity exceeds the {_MAX_CONNECTIVITY_ENDPOINTS:,}-"
                        "endpoint limit",
                        symbol,
                    )
                    return ()
                endpoints[candidate.key] = candidate
                existing = candidate
            result.append(existing)
        return tuple(result)

    def constant_integer(expression: Any) -> int | None:
        try:
            constant = expression.constant
            has_unknown = (
                constant.hasUnknown() if callable(constant.hasUnknown) else constant.hasUnknown
            )
            if has_unknown:
                return None
            value = constant.value
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
        try:
            return int(value)
        except (OverflowError, TypeError, ValueError):
            return None

    def flatten(expression: Any) -> _ConnectivityExpression:
        if expression is None:
            return _ConnectivityExpression()
        expression_type = type(expression).__name__
        if expression_type == "AssignmentExpression":
            return flatten(getattr(expression, "left", None))
        if expression_type == "NamedValueExpression":
            symbol = expression.getSymbolReference()
            bits = endpoint_bits(symbol) if symbol is not None else ()
            return _ConnectivityExpression(tuple((item, False) for item in bits), bool(bits))
        if expression_type == "ElementSelectExpression":
            value = getattr(expression, "value", None)
            symbol = value.getSymbolReference() if value is not None else None
            selector = constant_integer(getattr(expression, "selector", None))
            if symbol is None or selector is None:
                return _ConnectivityExpression(
                    supported=False,
                    reason="dynamic or non-symbol element selection",
                )
            selected = tuple(item for item in endpoint_bits(symbol) if item.bit_index == selector)
            if len(selected) != 1:
                return _ConnectivityExpression(
                    supported=False,
                    reason=f"element index {selector} is outside the resolved signal range",
                )
            return _ConnectivityExpression(((selected[0], False),))
        if expression_type == "RangeSelectExpression":
            value = getattr(expression, "value", None)
            symbol = value.getSymbolReference() if value is not None else None
            left = constant_integer(getattr(expression, "left", None))
            right = constant_integer(getattr(expression, "right", None))
            selection_kind = getattr(getattr(expression, "selectionKind", None), "name", "")
            if symbol is None or left is None or right is None or selection_kind != "Simple":
                return _ConnectivityExpression(
                    supported=False,
                    reason="dynamic or indexed part selection",
                )
            step = 1 if right > left else -1
            wanted = tuple(range(left, right + step, step))
            available = {item.bit_index: item for item in endpoint_bits(symbol)}
            if any(index not in available for index in wanted):
                return _ConnectivityExpression(
                    supported=False,
                    reason=f"part selection [{left}:{right}] exceeds the resolved signal range",
                )
            return _ConnectivityExpression(
                tuple((available[index], False) for index in wanted),
            )
        if expression_type == "ConcatenationExpression":
            flattened: list[tuple[ConnectivityEndpoint, bool]] = []
            for operand in getattr(expression, "operands", ()):
                part = flatten(operand)
                if not part.supported:
                    return part
                flattened.extend(part.bits)
            return _ConnectivityExpression(tuple(flattened))
        if expression_type == "UnaryExpression":
            operation = getattr(getattr(expression, "op", None), "name", "")
            operand = flatten(getattr(expression, "operand", None))
            if not operand.supported:
                return operand
            if operation == "BitwiseNot" or (operation == "LogicalNot" and len(operand.bits) == 1):
                return _ConnectivityExpression(
                    tuple((item, not inverted) for item, inverted in operand.bits)
                )
            return _ConnectivityExpression(
                supported=False,
                reason=f"unary operation {operation or '<unknown>'}",
            )
        if expression_type == "ConversionExpression":
            operand = flatten(getattr(expression, "operand", None))
            if not operand.supported or not operand.bits:
                return operand
            converted_shape, converted_state, _detail = _shape(getattr(expression, "type", None))
            if converted_state == FactState.KNOWN and converted_shape.width == len(operand.bits):
                return operand
            return _ConnectivityExpression(
                supported=False,
                reason="width-changing or unresolved conversion",
            )
        if expression_type in {
            "EmptyArgumentExpression",
            "IntegerLiteral",
            "UnbasedUnsizedIntegerLiteral",
        }:
            # A constant or explicitly unconnected port has no signal source.
            return _ConnectivityExpression()
        return _ConnectivityExpression(
            supported=False,
            reason=f"{expression_type} is outside the transparent expression subset",
        )

    def referenced_bits(expression: Any) -> tuple[ConnectivityEndpoint, ...]:
        result: dict[str, ConnectivityEndpoint] = {}

        def visitor(item: Any) -> None:
            if type(item).__name__ != "NamedValueExpression":
                return
            try:
                symbol = item.getSymbolReference()
            except (AttributeError, RuntimeError, TypeError, ValueError):
                return
            if symbol is None:
                return
            for endpoint in endpoint_bits(symbol):
                result[endpoint.key] = endpoint

        try:
            expression.visit(visitor)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return ()
        return tuple(result[key] for key in sorted(result))

    def append_edge(
        source: ConnectivityEndpoint,
        sink: ConnectivityEndpoint,
        *,
        kind: str,
        inverted: bool | None,
        provenance: Provenance,
        status: FactState,
        reason: str | None = None,
    ) -> None:
        nonlocal connectivity_complete
        key = (source.key, sink.key, kind, status, inverted)
        if key in edge_keys:
            return
        if len(edges) >= _MAX_CONNECTIVITY_EDGES:
            connectivity_complete = False
            report_once(
                "*",
                f"Static connectivity exceeds the {_MAX_CONNECTIVITY_EDGES:,}-edge limit",
                instance,
            )
            return
        edge_keys.add(key)
        edges.append(
            ConnectivityEdge(
                source,
                sink,
                kind=kind,
                inverted=inverted,
                provenance=provenance,
                status=status,
                attributes={"reason": reason} if reason else {},
            )
        )

    def connect_exact(
        sources: _ConnectivityExpression,
        sinks: _ConnectivityExpression,
        *,
        kind: str,
        provenance: Provenance,
    ) -> bool:
        if not sources.supported or not sinks.supported:
            return False
        if not sources.bits:
            driven.update(item.key for item, _inverted in sinks.bits)
            return True
        if len(sources.bits) != len(sinks.bits):
            return False
        for (source, source_inverted), (sink, sink_inverted) in zip(
            sources.bits, sinks.bits, strict=True
        ):
            append_edge(
                source,
                sink,
                kind=kind,
                inverted=source_inverted ^ sink_inverted,
                provenance=provenance,
                status=FactState.KNOWN,
            )
            driven.add(sink.key)
        return True

    def connect_tainted(
        sources: Sequence[ConnectivityEndpoint],
        sinks: Sequence[ConnectivityEndpoint],
        *,
        kind: str,
        provenance: Provenance,
        reason: str,
    ) -> None:
        nonlocal connectivity_complete
        if not sources or not sinks:
            connectivity_complete = False
            return
        if len(sources) * len(sinks) > _MAX_TAINTED_CROSS_PRODUCT:
            connectivity_complete = False
            report_once(
                "*",
                "Unsupported connectivity frontier is too wide to represent safely",
                instance,
            )
            return
        for source in sources:
            for sink in sinks:
                append_edge(
                    source,
                    sink,
                    kind=kind,
                    inverted=None,
                    provenance=provenance,
                    status=FactState.TAINTED,
                    reason=reason,
                )

    def process_net_alias(member: Any) -> bool:
        """Model a Verilog ``alias`` as an exact bidirectional connection.

        Return ``True`` when the alias cannot be represented exactly and must
        therefore leave an opaque connectivity frontier.
        """

        try:
            expressions = list(member.netReferences)
        except (AttributeError, RuntimeError, TypeError):
            expressions = []
        flattened = tuple(flatten(expression) for expression in expressions)
        if len(flattened) < 2 or any(
            not expression.supported or not expression.bits for expression in flattened
        ):
            return True
        anchor = flattened[0]
        for peer in flattened[1:]:
            if not connect_exact(
                anchor,
                peer,
                kind="net_alias",
                provenance=location(member),
            ) or not connect_exact(
                peer,
                anchor,
                kind="net_alias",
                provenance=location(member),
            ):
                return True
        return False

    def process_primitive(member: Any) -> bool:
        """Model transparent built-in primitives; taint all other kinds."""

        try:
            primitive_name = decoded_identifier(str(member.primitiveType.name)).lower()
            connections = list(member.portConnections)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return True
        flattened = tuple(flatten(expression) for expression in connections)
        edge_location = location(member)
        if primitive_name in {"buf", "not"} and len(flattened) >= 2:
            source = flattened[-1]
            if primitive_name == "not" and source.supported:
                source = _ConnectivityExpression(
                    tuple((endpoint, not inverted) for endpoint, inverted in source.bits)
                )
            if not source.supported or not source.bits:
                return True
            return not all(
                connect_exact(
                    source,
                    sink,
                    kind=f"primitive_{primitive_name}",
                    provenance=edge_location,
                )
                for sink in flattened[:-1]
            )
        if primitive_name in {"rtran", "tran"} and len(flattened) == 2:
            left, right = flattened
            return not (
                connect_exact(
                    left,
                    right,
                    kind=f"primitive_{primitive_name}",
                    provenance=edge_location,
                )
                and connect_exact(
                    right,
                    left,
                    kind=f"primitive_{primitive_name}",
                    provenance=edge_location,
                )
            )

        # Gate, switch, and user-defined primitive behavior can make signals
        # reachable but is not an identity transform.  Represent all referenced
        # endpoints as a possible frontier and keep the global graph incomplete
        # if the bounded cross-product cannot be emitted.
        referenced: dict[str, ConnectivityEndpoint] = {}
        for expression in connections:
            for endpoint in referenced_bits(expression):
                referenced[endpoint.key] = endpoint
        possible = tuple(referenced[key] for key in sorted(referenced))
        connect_tainted(
            possible,
            possible,
            kind="unsupported_primitive",
            provenance=edge_location,
            reason=f"primitive {primitive_name or '<unknown>'} is non-transparent",
        )
        return True

    def process_assignment(member: Any) -> None:
        nonlocal connectivity_complete
        assignment = getattr(member, "assignment", None)
        if assignment is None:
            return
        left_expression = getattr(assignment, "left", None)
        right_expression = getattr(assignment, "right", None)
        left = flatten(left_expression)
        right = flatten(right_expression)
        edge_location = location(member)
        if connect_exact(right, left, kind="assign", provenance=edge_location):
            return
        reason = right.reason or left.reason or "assignment width conversion"
        sinks = tuple(item for item, _inverted in left.bits)
        if not sinks:
            connectivity_complete = False
            report_once(
                hierarchical_name(member) or "*",
                f"Static connectivity cannot identify an assignment sink: {reason}",
                member,
            )
            return
        driven.update(item.key for item in sinks)
        sources = referenced_bits(right_expression)
        connect_tainted(
            sources,
            sinks,
            kind="unsupported_assign",
            provenance=edge_location,
            reason=reason,
        )
        report_once(
            sinks[0].native_name,
            f"Static connectivity stops at assignment to {sinks[0].native_name}: {reason}",
            member,
        )

    def process_port_connections(child: Any) -> None:
        try:
            connections = list(child.portConnections)
        except (AttributeError, RuntimeError, TypeError):
            connections = []
        for connection in connections:
            formal_symbol = getattr(connection, "port", None)
            expression = getattr(connection, "expression", None)
            if formal_symbol is None:
                continue
            formal = _ConnectivityExpression(
                tuple((item, False) for item in endpoint_bits(formal_symbol))
            )
            direction = _direction(getattr(formal_symbol, "direction", None))
            edge_location = location(child)
            actual = flatten(expression)
            if direction == Direction.UNKNOWN:
                actual_bits = tuple(item for item, _inverted in actual.bits)
                if not actual_bits:
                    actual_bits = referenced_bits(expression)
                formal_bits = tuple(item for item, _inverted in formal.bits)
                if not actual_bits and not formal_bits and actual.supported:
                    continue
                reason = "port direction is outside the input/output/inout connectivity subset"
                connect_tainted(
                    actual_bits,
                    formal_bits,
                    kind="unsupported_port_direction",
                    provenance=edge_location,
                    reason=reason,
                )
                connect_tainted(
                    formal_bits,
                    actual_bits,
                    kind="unsupported_port_direction",
                    provenance=edge_location,
                    reason=reason,
                )
                report_once(
                    hierarchical_name(formal_symbol),
                    f"Static connectivity stops at port "
                    f"{hierarchical_name(formal_symbol)}: {reason}",
                    child,
                )
                continue
            if direction == Direction.OUTPUT:
                if connect_exact(formal, actual, kind="port", provenance=edge_location):
                    continue
                if actual.supported and not actual.bits:
                    continue
                sources = tuple(item for item, _inverted in formal.bits)
                sinks = referenced_bits(expression)
            else:
                if connect_exact(actual, formal, kind="port", provenance=edge_location):
                    if direction == Direction.INOUT:
                        connect_exact(formal, actual, kind="port", provenance=edge_location)
                    continue
                sources = referenced_bits(expression)
                sinks = tuple(item for item, _inverted in formal.bits)
            if not sources and not sinks:
                # An explicitly empty connection is a known absence, not a
                # possible hidden path.
                continue
            reason = actual.reason or "port width conversion or unsupported connection"
            connect_tainted(
                sources,
                sinks,
                kind="unsupported_port",
                provenance=edge_location,
                reason=reason,
            )
            if direction == Direction.INOUT:
                connect_tainted(
                    sinks,
                    sources,
                    kind="unsupported_port",
                    provenance=edge_location,
                    reason=reason,
                )
            report_once(
                hierarchical_name(formal_symbol),
                f"Static connectivity stops at port {hierarchical_name(formal_symbol)}: {reason}",
                child,
            )

    def walk_scope(scope: Any) -> bool:
        nonlocal connectivity_complete
        has_opaque_behavior = False
        try:
            members = list(scope)
        except (RuntimeError, TypeError):
            return True
        for member in members:
            symbol_type = type(member).__name__
            if symbol_type in {"PortSymbol", "NetSymbol", "VariableSymbol"}:
                endpoint_bits(member)
            elif symbol_type == "ContinuousAssignSymbol":
                process_assignment(member)
            elif symbol_type == "NetAliasSymbol":
                has_opaque_behavior = process_net_alias(member) or has_opaque_behavior
            elif symbol_type == "PrimitiveInstanceSymbol":
                has_opaque_behavior = process_primitive(member) or has_opaque_behavior
            elif symbol_type == "InstanceSymbol":
                process_port_connections(member)
                walk_instance(member)
            elif symbol_type in {"GenerateBlockArraySymbol", "InstanceArraySymbol"}:
                try:
                    children = list(getattr(member, "entries", member))
                except (RuntimeError, TypeError):
                    children = []
                for child in children:
                    if type(child).__name__ == "InstanceSymbol":
                        process_port_connections(child)
                        walk_instance(child)
                    else:
                        has_opaque_behavior = walk_scope(child) or has_opaque_behavior
            elif symbol_type in {"GenerateBlockSymbol", "CheckerInstanceSymbol"}:
                has_opaque_behavior = walk_scope(member) or has_opaque_behavior
            elif symbol_type == "ProceduralBlockSymbol":
                has_opaque_behavior = True
            elif symbol_type in _CONNECTIVITY_INERT_SYMBOLS or symbol_type.endswith("Type"):
                continue
            else:
                # New slang symbol kinds and unsupported language constructs
                # must never disappear from an isolation proof.  The broad
                # instance frontier below preserves soundness until a dedicated
                # extractor is implemented.
                has_opaque_behavior = True
        if has_opaque_behavior:
            connectivity_complete = False
        return has_opaque_behavior

    def walk_instance(current: Any) -> None:
        nonlocal connectivity_complete
        instance_name = hierarchical_name(current)
        if not instance_name or instance_name in visited_instances:
            return
        visited_instances.add(instance_name)
        try:
            raw_ports = list(current.body.portList)
        except (AttributeError, RuntimeError, TypeError):
            raw_ports = []
        inputs: list[ConnectivityEndpoint] = []
        outputs: list[ConnectivityEndpoint] = []
        for port in raw_ports:
            bits = endpoint_bits(port)
            direction = _direction(getattr(port, "direction", None))
            if direction in {Direction.INPUT, Direction.INOUT}:
                inputs.extend(bits)
            if direction in {Direction.OUTPUT, Direction.INOUT}:
                outputs.extend(bits)
        has_opaque_behavior = walk_scope(current.body)
        unsupported_outputs = [item for item in outputs if item.key not in driven]
        if has_opaque_behavior:
            # The broad input-to-output frontier protects ordinary top-level
            # checks, but arbitrary internal endpoint requirements could still
            # cross an unindexed opaque dependency.  Keep global absence claims
            # inconclusive until the construct is modeled.
            connectivity_complete = False
            unsupported_outputs = outputs
        if unsupported_outputs and inputs:
            reason = (
                "behavior is outside the static connectivity subset"
                if has_opaque_behavior
                else "output has no supported transparent driver"
            )
            connect_tainted(
                inputs,
                unsupported_outputs,
                kind="unsupported_behavior",
                provenance=location(current),
                reason=reason,
            )
            report_once(
                instance_name,
                f"Static connectivity for {instance_name} is incomplete: {reason}",
                current,
            )

    walk_instance(instance)
    ordered_endpoints = tuple(endpoints[key] for key in sorted(endpoints))
    ordered_edges = tuple(
        sorted(
            edges,
            key=lambda item: (
                item.source.key,
                item.sink.key,
                item.kind,
                item.status.value,
                -1 if item.inverted is None else int(item.inverted),
            ),
        )
    )
    return ordered_endpoints, ordered_edges, tuple(diagnostics), connectivity_complete


def parse_verilog(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    include_dirs: Sequence[Pathish] = (),
    defines: Mapping[str, Any] | Sequence[str] = (),
    top: str | Sequence[str] | None = None,
) -> ViewObservation:
    """Parse and elaborate Verilog/SystemVerilog sources with pyslang.

    With no explicit ``top``, every module definition is elaborated once using
    its default parameters.  This yields a stable library inventory and avoids
    allowing an arbitrary instantiation override to redefine a module's public
    contract.
    """

    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="rtl", name=view_name)
    api = _load_slang()
    if api is None:
        return unavailable_view(
            view=view,
            paths=source_paths,
            code="OC1102",
            message="SystemVerilog parsing requires the MIT-licensed pyslang package",
            help="Install OpenCollate with its SystemVerilog parser dependency.",
        )

    preprocessor = api.preprocessor_options()
    preprocessor.additionalIncludePaths = [str(Path(item)) for item in include_dirs]
    preprocessor.predefines = _predefines(defines)
    parse_options = api.module.Bag([preprocessor])
    source_manager = api.module.SourceManager()
    trees: list[Any] = []
    early_diagnostics: list[Diagnostic] = []
    for path in source_paths:
        try:
            trees.append(api.syntax_tree.fromFile(str(path), source_manager, parse_options))
        except Exception as error:  # pyslang translates several C++ errors to RuntimeError
            early_diagnostics.append(
                parser_diagnostic(
                    "OC1101" if path.exists() else "OC1002",
                    Severity.FATAL,
                    f"Cannot parse SystemVerilog source {path}: {error}",
                    location=Provenance(str(path), view=view),
                    metadata={"backend": "slang"},
                )
            )
    if not trees:
        return ViewObservation(
            view=view,
            diagnostics=tuple(early_diagnostics),
            complete=False,
            tainted_scopes=frozenset(("*",)),
            attributes={"parser": "pyslang"},
        )

    discovery = api.compilation()
    for tree in trees:
        discovery.addSyntaxTree(tree)
    definitions = [
        definition
        for definition in discovery.getDefinitions()
        if getattr(getattr(definition, "definitionKind", None), "name", "") == "Module"
    ]
    available = {str(definition.name) for definition in definitions}
    if top is None:
        selected = available
    elif isinstance(top, str):
        selected = {top}
    else:
        selected = {str(item) for item in top}
    for missing in sorted(selected - available):
        early_diagnostics.append(
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                f"Requested SystemVerilog top {missing!r} is not defined",
                location=Provenance(str(source_paths[0]), view=view),
                metadata={"available_modules": sorted(available)},
            )
        )

    selected_available = selected & available
    if top is not None and not selected_available:
        return ViewObservation(
            view=view,
            diagnostics=tuple(early_diagnostics),
            complete=False,
            tainted_scopes=frozenset(("*",)),
            attributes={
                "parser": "pyslang",
                "pyslang_version": getattr(api.module, "__version__", "unknown"),
                "source_files": [str(path) for path in source_paths],
            },
        )

    compilation_options = api.compilation_options()
    compilation_options.topModules = selected_available
    compilation = api.compilation(api.module.Bag([compilation_options]))
    for tree in trees:
        compilation.addSyntaxTree(tree)
    root = compilation.getRoot()
    slang_diags, tainted, complete = _slang_diagnostics(api.module, compilation, view)
    diagnostics = [*early_diagnostics, *slang_diags]
    if early_diagnostics:
        complete = False

    components: list[ComponentObservation] = []
    design_objects: list[DesignObjectObservation] = []
    connectivity_endpoints: dict[str, ConnectivityEndpoint] = {}
    connectivity_edges: list[ConnectivityEdge] = []
    connectivity_complete = True
    for instance in sorted(root.topInstances, key=lambda item: str(item.name)):
        definition = instance.definition
        if getattr(getattr(definition, "definitionKind", None), "name", "") != "Module":
            continue
        component_location = _source_location(
            compilation.sourceManager,
            definition.location,
            view,
        )
        ports: list[PortObservation] = []
        for raw_port in instance.body.portList:
            name = decoded_identifier(str(raw_port.name))
            port_location = _source_location(
                compilation.sourceManager,
                raw_port.location,
                view,
            )
            if type(raw_port).__name__ != "PortSymbol":
                ports.append(
                    PortObservation(
                        native_name=name,
                        provenance=port_location,
                        status=FactState.UNSUPPORTED,
                        attributes={"slang_symbol": type(raw_port).__name__},
                    )
                )
                diagnostics.append(
                    parser_diagnostic(
                        "OC1102",
                        Severity.WARNING,
                        f"{definition.name}/{name} is an unsupported interface-style port",
                        location=port_location,
                    )
                )
                continue
            direction = _direction(raw_port.direction)
            shape, shape_state, shape_detail = _shape(raw_port.type)
            role, role_state = infer_role_from_name(name)
            field_states: dict[str, FactState] = {"role": role_state}
            if direction == Direction.UNKNOWN:
                field_states["direction"] = FactState.UNSUPPORTED
            if shape_state != FactState.KNOWN:
                field_states["shape"] = shape_state
                diagnostics.append(
                    parser_diagnostic(
                        "OC1103",
                        Severity.WARNING,
                        f"Cannot resolve width for {definition.name}/{name}: {shape_detail}",
                        location=port_location,
                    )
                )
            ports.append(
                PortObservation(
                    native_name=name,
                    direction=direction,
                    role=role,
                    shape=shape,
                    provenance=port_location,
                    attributes={
                        "declared_type": str(raw_port.type),
                        "role_source": "name_heuristic"
                        if role_state == FactState.TAINTED
                        else None,
                    },
                    field_states=field_states,
                )
            )
        port_tuple = tuple(ports)
        functions, function_diags = _extract_functions(
            instance,
            port_tuple,
            compilation.sourceManager,
            view,
        )
        diagnostics.extend(function_diags)
        component_name = decoded_identifier(str(definition.name))
        indexed_objects, object_diags = _extract_design_objects(
            instance,
            compilation.sourceManager,
            view,
            component_name,
        )
        design_objects.extend(indexed_objects)
        diagnostics.extend(object_diags)
        graph_endpoints, graph_edges, graph_diags, graph_complete = _extract_connectivity(
            instance,
            compilation.sourceManager,
            view,
        )
        for endpoint in graph_endpoints:
            connectivity_endpoints.setdefault(endpoint.key, endpoint)
        connectivity_edges.extend(graph_edges)
        diagnostics.extend(graph_diags)
        connectivity_complete = connectivity_complete and graph_complete
        components.append(
            ComponentObservation(
                native_name=component_name,
                kind=ComponentKind.MODULE,
                ports=port_tuple,
                functions=functions,
                provenance=component_location,
                status=(
                    FactState.TAINTED
                    if component_name in tainted or "*" in tainted
                    else FactState.KNOWN
                ),
                attributes={
                    "backend": "pyslang",
                    "definition_kind": "module",
                },
            )
        )

    return ViewObservation(
        view=view,
        components=tuple(components),
        objects=tuple(design_objects),
        diagnostics=tuple(diagnostics),
        complete=complete,
        tainted_scopes=tainted | ({"*"} if early_diagnostics else set()),
        connectivity_endpoints=tuple(
            connectivity_endpoints[key] for key in sorted(connectivity_endpoints)
        ),
        connectivity_edges=tuple(
            sorted(
                connectivity_edges,
                key=lambda item: (
                    item.source.key,
                    item.sink.key,
                    item.kind,
                    item.status.value,
                ),
            )
        ),
        attributes={
            "parser": "pyslang",
            "pyslang_version": getattr(api.module, "__version__", "unknown"),
            "source_files": [str(path) for path in source_paths],
            "include_dirs": [str(Path(path)) for path in include_dirs],
            "defines": _predefines(defines),
            "connectivity_complete": connectivity_complete,
        },
    )


class VerilogParser:
    format_name = "verilog"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_verilog(paths, view_id=view_id, **options)


__all__ = ["VerilogParser", "parse_verilog"]

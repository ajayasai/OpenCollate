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
        diagnostics=tuple(diagnostics),
        complete=complete,
        tainted_scopes=tainted | ({"*"} if early_diagnostics else set()),
        attributes={
            "parser": "pyslang",
            "pyslang_version": getattr(api.module, "__version__", "unknown"),
            "source_files": [str(path) for path in source_paths],
            "include_dirs": [str(Path(path)) for path in include_dirs],
            "defines": _predefines(defines),
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

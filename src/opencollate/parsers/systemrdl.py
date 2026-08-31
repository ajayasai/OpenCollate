"""Bounded SystemRDL 2.0 register-map importer.

The adapter deliberately uses only the public ``systemrdl-compiler`` node API.
SystemRDL permits executable Perl preprocessing and source includes.  Both are
rejected during a complete preflight of every configured compilation unit,
before the optional compiler backend is imported or invoked.
"""

from __future__ import annotations

import importlib
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from opencollate.diagnostics import Diagnostic, Severity
from opencollate.model import (
    FactState,
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
    parser_diagnostic,
    provenance,
    unavailable_view,
)

_MAX_SOURCE_FILES = 256
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_NODE_DEFINITIONS = 250_000
_MAX_REGISTER_DEFINITIONS = 100_000
_MAX_ARRAY_ELEMENTS = 65_536
_MAX_REGISTERS = 250_000
_MAX_FIELDS_PER_REGISTER = 65_536
_MAX_FIELDS = 1_000_000
_MAX_HIERARCHY_DEPTH = 128
_MAX_SEMANTIC_DIAGNOSTICS = 256

# This ordering intentionally mirrors systemrdl-compiler's preprocessing
# tokenizer: comments take precedence over Perl tags and include directives.
_PREFLIGHT_TOKEN = re.compile(
    r"(?P<block>/\*.*?\*/)|(?P<line>//.*?$)|(?P<perl><%)|(?P<include>`include\b)",
    re.DOTALL | re.MULTILINE,
)

_BEHAVIOR_PROPERTIES = (
    "onread",
    "onwrite",
    "hwset",
    "hwclr",
    "we",
    "wel",
    "swwe",
    "swwel",
    "intr",
    "counter",
    "incr",
    "decr",
    "sticky",
    "stickybit",
    "singlepulse",
    "rclr",
    "rset",
    "woclr",
    "woset",
)


@dataclass(frozen=True, slots=True)
class _SystemRdlApi:
    compiler: Any
    compile_error: type[BaseException]
    addrmap_node: type[Any]
    field_node: type[Any]
    mem_node: type[Any]
    reg_node: type[Any]
    signal_node: type[Any]
    version: str


@dataclass(frozen=True, slots=True)
class _SourceUnit:
    path: Path
    text: str


@dataclass(frozen=True, slots=True)
class _BackendMessage:
    severity: str
    text: str
    source: str | None
    line: int | None
    column: int | None


class _MessagePrinter:
    """Small duck-typed systemrdl MessagePrinter that retains structured facts."""

    def __init__(self) -> None:
        self.messages: list[_BackendMessage] = []

    def print_message(self, severity: Any, text: str, source_ref: Any) -> None:
        selection = getattr(source_ref, "line_selection", None)
        column = None
        if isinstance(selection, tuple) and selection and isinstance(selection[0], int):
            column = selection[0] + 1
        raw_line = getattr(source_ref, "line", None)
        line = raw_line if isinstance(raw_line, int) else None
        raw_source = getattr(source_ref, "path", None)
        source = str(raw_source) if raw_source is not None else None
        self.messages.append(
            _BackendMessage(
                severity=str(getattr(severity, "name", severity)).upper(),
                text=str(text),
                source=source,
                line=line,
                column=column,
            )
        )


@dataclass(slots=True)
class _ExtractionContext:
    view: ViewId
    diagnostics: list[Diagnostic]
    tainted_scopes: set[str] = field(default_factory=set)
    semantic_count: int = 0
    suppressed_count: int = 0

    def report(
        self,
        code: str,
        severity: Severity,
        message: str,
        *,
        location: Provenance,
        scope: str,
        help: str | None = None,
        metadata_value: Mapping[str, Any] | None = None,
    ) -> None:
        self.semantic_count += 1
        self.tainted_scopes.add(scope)
        if self.semantic_count > _MAX_SEMANTIC_DIAGNOSTICS:
            self.suppressed_count += 1
            return
        self.diagnostics.append(
            parser_diagnostic(
                code,
                severity,
                message,
                location=location,
                help=help,
                metadata=metadata_value,
            )
        )


def _load_systemrdl() -> _SystemRdlApi | None:
    try:
        systemrdl = importlib.import_module("systemrdl")
        nodes = importlib.import_module("systemrdl.node")
    except ImportError:
        return None
    try:
        version = metadata.version("systemrdl-compiler")
    except metadata.PackageNotFoundError:  # pragma: no cover - editable backend installs
        version = "installed"
    return _SystemRdlApi(
        compiler=systemrdl.RDLCompiler,
        compile_error=systemrdl.RDLCompileError,
        addrmap_node=nodes.AddrmapNode,
        field_node=nodes.FieldNode,
        mem_node=nodes.MemNode,
        reg_node=nodes.RegNode,
        signal_node=nodes.SignalNode,
        version=version,
    )


def _line_column(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    previous_newline = text.rfind("\n", 0, offset)
    return line, offset - previous_newline


def _preflight(
    paths: tuple[Path, ...],
    view: ViewId,
) -> tuple[tuple[_SourceUnit, ...], tuple[Diagnostic, ...]]:
    diagnostics: list[Diagnostic] = []
    units: list[_SourceUnit] = []
    if len(paths) > _MAX_SOURCE_FILES:
        return (), (
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                f"SystemRDL view exceeds the {_MAX_SOURCE_FILES} source-file limit",
                location=provenance(paths[0], view),
                metadata={"limit": _MAX_SOURCE_FILES, "actual": len(paths)},
            ),
        )

    total_bytes = 0
    for path in paths:
        try:
            with path.open("rb") as stream:
                data = stream.read(_MAX_SOURCE_BYTES + 1)
        except OSError as error:
            diagnostics.append(
                parser_diagnostic(
                    "OC1002",
                    Severity.FATAL,
                    f"Cannot read {path}: {error}",
                    location=provenance(path, view),
                )
            )
            continue
        if len(data) > _MAX_SOURCE_BYTES:
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    f"SystemRDL source {path} exceeds the {_MAX_SOURCE_BYTES:,}-byte limit",
                    location=provenance(path, view),
                    metadata={
                        "limit": _MAX_SOURCE_BYTES,
                        "actual": len(data),
                        "truncated": True,
                    },
                )
            )
            continue
        prospective_total = total_bytes + len(data)
        if prospective_total > _MAX_TOTAL_SOURCE_BYTES:
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    "SystemRDL view exceeds the "
                    f"{_MAX_TOTAL_SOURCE_BYTES:,}-byte aggregate source limit",
                    location=provenance(path, view),
                    metadata={"limit": _MAX_TOTAL_SOURCE_BYTES, "actual": prospective_total},
                )
            )
            break
        total_bytes += len(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            prefix = data[: error.start]
            line = prefix.count(b"\n") + 1
            previous_newline = prefix.rfind(b"\n")
            column = error.start - previous_newline
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    f"SystemRDL source {path} is not valid UTF-8 near byte {error.start}",
                    location=provenance(path, view, line=line, column=column),
                    help="Convert SystemRDL compilation units to UTF-8.",
                )
            )
            continue

        rejected: set[str] = set()
        for match in _PREFLIGHT_TOKEN.finditer(text):
            construct = match.lastgroup
            if construct not in {"perl", "include"} or construct in rejected:
                continue
            rejected.add(construct)
            line, column = _line_column(text, match.start())
            if construct == "perl":
                message = (
                    "SystemRDL Perl preprocessing is disabled; executable <% ... %> "
                    "tags were not run"
                )
                help_text = "Provide already-expanded SystemRDL without Perl tags."
            else:
                message = (
                    "SystemRDL source include directives are disabled; list every "
                    "compilation unit explicitly in project configuration"
                )
                help_text = "Remove `include and list the referenced file in this source view."
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    message,
                    location=provenance(path, view, line=line, column=column),
                    help=help_text,
                    metadata={"construct": construct},
                )
            )
        units.append(_SourceUnit(path, text))

    if diagnostics:
        return (), tuple(diagnostics)
    return tuple(units), ()


def _backend_location(
    message: _BackendMessage,
    view: ViewId,
    fallback: Path,
    source_map: Mapping[str, Path],
) -> Provenance:
    source = _mapped_source(message.source, fallback, source_map)
    return Provenance(
        source=source,
        line=max(1, message.line or 1),
        column=max(1, message.column or 1),
        view=view,
    )


def _backend_diagnostics(
    messages: Sequence[_BackendMessage],
    view: ViewId,
    fallback: Path,
    source_map: Mapping[str, Path],
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    has_specific_error = any(
        message.severity in {"ERROR", "FATAL"} and message.source is not None
        for message in messages
    )
    for message in messages:
        if (
            has_specific_error
            and message.source is None
            and message.text.casefold().endswith("due to previous errors")
        ):
            continue
        if message.severity in {"ERROR", "FATAL"}:
            code, severity = "OC1101", Severity.FATAL
        elif message.severity == "WARNING":
            code, severity = "OC1102", Severity.WARNING
        else:
            code, severity = "OC1105", Severity.INFO
        diagnostics.append(
            parser_diagnostic(
                code,
                severity,
                f"SystemRDL compiler: {message.text}",
                location=_backend_location(message, view, fallback, source_map),
                metadata={"backend": "systemrdl-compiler", "backend_severity": message.severity},
            )
        )
    return diagnostics


def _source_key(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def _mapped_source(
    raw_source: object,
    fallback: Path,
    source_map: Mapping[str, Path],
) -> str:
    if raw_source is None:
        return str(fallback)
    text = str(raw_source)
    return str(source_map.get(_source_key(text), Path(text)))


def _source_location(
    node: Any,
    view: ViewId,
    fallback: Path,
    raw_name: str,
    source_map: Mapping[str, Path],
) -> Provenance:
    source_ref = getattr(node, "inst_src_ref", None) or getattr(node, "def_src_ref", None)
    raw_source = getattr(source_ref, "path", None)
    raw_line = getattr(source_ref, "line", None)
    selection = getattr(source_ref, "line_selection", None)
    column = 1
    if isinstance(selection, tuple) and selection and isinstance(selection[0], int):
        column = selection[0] + 1
    return Provenance(
        source=_mapped_source(raw_source, fallback, source_map),
        line=max(1, raw_line if isinstance(raw_line, int) else 1),
        column=max(1, column),
        view=view,
        raw_name=raw_name,
    )


def _enum_name(value: Any) -> str:
    return str(getattr(value, "name", value))


def _access(value: Any) -> str | None:
    if value is None:
        return None
    return {
        "na": "no-access",
        "r": "read-only",
        "rw": "read-write",
        "rw1": "read-writeOnce",
        "w": "write-only",
        "w1": "writeOnce",
    }.get(_enum_name(value).casefold(), _enum_name(value))


def _property(node: Any, name: str, default: Any = None) -> Any:
    try:
        return node.get_property(name)
    except LookupError:
        return default


def _active_semantic(value: Any) -> bool:
    if value is None or value is False:
        return False
    if type(value) is int and value == 0:
        return False
    return True


def _attribute_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_attribute_value(item) for item in value]
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    get_path = getattr(value, "get_path", None)
    if callable(get_path):
        try:
            return str(get_path(hier_separator="/"))
        except (AttributeError, TypeError, ValueError):
            pass
    return str(value)


def _path(node: Any) -> str:
    return str(node.get_path(hier_separator="/"))


def _relative_path(node: Any, top_path: str) -> str:
    path = _path(node)
    prefix = top_path + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _array_product(dimensions: Any, limit: int) -> int:
    if not isinstance(dimensions, (list, tuple)):
        return 1
    result = 1
    for dimension in dimensions:
        if type(dimension) is not int or dimension < 1:
            return limit + 1
        if result > limit // dimension:
            return limit + 1
        result *= dimension
    return result


def _multiplicity(node: Any, top: Any, limit: int) -> int:
    result = 1
    current = node
    while current is not None:
        factor = _array_product(getattr(current, "array_dimensions", None), limit)
        if factor > limit or result > limit // factor:
            return limit + 1
        result *= factor
        if current is top:
            break
        current = getattr(current, "parent", None)
    return result


def _limit_diagnostic(
    view: ViewId,
    fallback: Path,
    message: str,
    *,
    limit: int,
    actual: int,
) -> Diagnostic:
    return parser_diagnostic(
        "OC1101",
        Severity.FATAL,
        message,
        location=provenance(fallback, view),
        metadata={"limit": limit, "actual": actual},
    )


def _validate_model_limits(
    api: _SystemRdlApi,
    top: Any,
    view: ViewId,
    fallback: Path,
) -> tuple[list[Any], list[Any], list[Any], Diagnostic | None]:
    register_templates: list[Any] = []
    memories: list[Any] = []
    signals: list[Any] = []
    node_count = 0
    total_registers = 0
    total_fields = 0
    for node in top.descendants(unroll=False):
        node_count += 1
        if node_count > _MAX_NODE_DEFINITIONS:
            return (
                register_templates,
                memories,
                signals,
                _limit_diagnostic(
                    view,
                    fallback,
                    f"SystemRDL model exceeds the {_MAX_NODE_DEFINITIONS:,} node-definition limit",
                    limit=_MAX_NODE_DEFINITIONS,
                    actual=node_count,
                ),
            )
        depth = _path(node).count("/") + 1
        if depth > _MAX_HIERARCHY_DEPTH:
            return (
                register_templates,
                memories,
                signals,
                _limit_diagnostic(
                    view,
                    fallback,
                    f"SystemRDL model exceeds the {_MAX_HIERARCHY_DEPTH} hierarchy-depth limit",
                    limit=_MAX_HIERARCHY_DEPTH,
                    actual=depth,
                ),
            )
        dimensions = getattr(node, "array_dimensions", None)
        array_elements = _array_product(dimensions, _MAX_ARRAY_ELEMENTS)
        if array_elements > _MAX_ARRAY_ELEMENTS:
            return (
                register_templates,
                memories,
                signals,
                _limit_diagnostic(
                    view,
                    fallback,
                    f"SystemRDL array {_path(node)} exceeds the "
                    f"{_MAX_ARRAY_ELEMENTS:,}-element expansion limit",
                    limit=_MAX_ARRAY_ELEMENTS,
                    actual=array_elements,
                ),
            )
        if isinstance(node, api.reg_node):
            register_templates.append(node)
            if len(register_templates) > _MAX_REGISTER_DEFINITIONS:
                return (
                    register_templates,
                    memories,
                    signals,
                    _limit_diagnostic(
                        view,
                        fallback,
                        "SystemRDL model exceeds the "
                        f"{_MAX_REGISTER_DEFINITIONS:,} register-definition limit",
                        limit=_MAX_REGISTER_DEFINITIONS,
                        actual=len(register_templates),
                    ),
                )
            fields = node.fields(skip_not_present=True)
            if len(fields) > _MAX_FIELDS_PER_REGISTER:
                return (
                    register_templates,
                    memories,
                    signals,
                    _limit_diagnostic(
                        view,
                        fallback,
                        f"SystemRDL register {_path(node)} exceeds the "
                        f"{_MAX_FIELDS_PER_REGISTER:,}-field limit",
                        limit=_MAX_FIELDS_PER_REGISTER,
                        actual=len(fields),
                    ),
                )
            multiplicity = _multiplicity(node, top, _MAX_REGISTERS)
            if multiplicity > _MAX_REGISTERS - total_registers:
                return (
                    register_templates,
                    memories,
                    signals,
                    _limit_diagnostic(
                        view,
                        fallback,
                        f"SystemRDL model exceeds the {_MAX_REGISTERS:,} concrete-register limit",
                        limit=_MAX_REGISTERS,
                        actual=total_registers + multiplicity,
                    ),
                )
            total_registers += multiplicity
            expanded_fields = multiplicity * len(fields)
            if expanded_fields > _MAX_FIELDS - total_fields:
                return (
                    register_templates,
                    memories,
                    signals,
                    _limit_diagnostic(
                        view,
                        fallback,
                        f"SystemRDL model exceeds the {_MAX_FIELDS:,} concrete-field limit",
                        limit=_MAX_FIELDS,
                        actual=total_fields + expanded_fields,
                    ),
                )
            total_fields += expanded_fields
        elif isinstance(node, api.mem_node):
            memories.append(node)
        elif isinstance(node, api.signal_node):
            signals.append(node)
    return register_templates, memories, signals, None


def _field_observation(
    field_node: Any,
    *,
    view: ViewId,
    fallback: Path,
    register_scope: str,
    context: _ExtractionContext,
    source_map: Mapping[str, Path],
) -> RegisterFieldObservation:
    name = str(field_node.inst_name)
    location = _source_location(field_node, view, fallback, name, source_map)
    status = FactState.KNOWN
    reset_value: int | None = None
    raw_reset = _property(field_node, "reset")
    if raw_reset is not None:
        if type(raw_reset) is int:
            reset_value = raw_reset
        else:
            status = FactState.TAINTED
            context.report(
                "OC1103",
                Severity.WARNING,
                f"SystemRDL field {_path(field_node)} has a non-scalar reset that "
                "cannot be compared",
                location=location,
                scope=register_scope,
                metadata_value={"property": "reset", "value_type": type(raw_reset).__name__},
            )

    overlapping = bool(getattr(field_node, "has_overlaps", False))
    if overlapping:
        status = FactState.UNSUPPORTED
        context.report(
            "OC1102",
            Severity.WARNING,
            f"SystemRDL field {_path(field_node)} overlaps another legal access view; "
            "overlap semantics are not compared",
            location=location,
            scope=register_scope,
            help="Review the overlapping read/write views in a native SystemRDL tool.",
        )

    behavior: dict[str, Any] = {}
    for property_name in _BEHAVIOR_PROPERTIES:
        value = _property(field_node, property_name)
        if _active_semantic(value):
            behavior[property_name] = _attribute_value(value)
    if behavior:
        context.report(
            "OC1102",
            Severity.WARNING,
            f"SystemRDL field {_path(field_node)} uses behavioral properties that "
            "OpenCollate records but does not verify: " + ", ".join(sorted(behavior)),
            location=location,
            scope=register_scope,
            help="Use generated RTL checks or formal register verification for behavior.",
            metadata_value={"properties": behavior},
        )

    return RegisterFieldObservation(
        native_name=name,
        bit_offset=int(field_node.low),
        bit_width=int(field_node.width),
        access=_access(_property(field_node, "sw")),
        reset_value=reset_value,
        provenance=location,
        status=status,
        attributes={
            "systemrdl_path": _path(field_node),
            "hw_access": _access(_property(field_node, "hw")),
            "behavior": behavior,
            "overlapping": overlapping,
            "volatile": bool(getattr(field_node, "is_volatile", False)),
            "implements_storage": bool(getattr(field_node, "implements_storage", False)),
        },
    )


def _register_observation(
    node: Any,
    *,
    top: Any,
    top_path: str,
    addrmap_node_type: type[Any],
    component_name: str,
    view: ViewId,
    fallback: Path,
    context: _ExtractionContext,
    source_map: Mapping[str, Path],
) -> RegisterObservation:
    address_block_node = top
    ancestor = getattr(node, "parent", None)
    while ancestor is not None:
        if isinstance(ancestor, addrmap_node_type):
            address_block_node = ancestor
            break
        ancestor = getattr(ancestor, "parent", None)
    address_block_path = _path(address_block_node)
    relative = _relative_path(node, address_block_path)
    parts = relative.split("/")
    name = parts[-1]
    register_files = tuple(parts[:-1])
    address_block = str(address_block_node.inst_name)
    location = _source_location(node, view, fallback, name, source_map)
    status = FactState.KNOWN
    is_alias = bool(getattr(node, "is_alias", False))
    is_virtual = bool(getattr(node, "is_virtual", False))
    if is_alias or is_virtual:
        status = FactState.UNSUPPORTED
        unsupported = "alias" if is_alias else "virtual register"
        context.report(
            "OC1102",
            Severity.WARNING,
            f"SystemRDL {unsupported} {_path(node)} is retained but its special "
            "semantics are not compared",
            location=location,
            scope=relative,
        )

    local_address_offset: int | None = None
    address_block_absolute_address: int | None = None
    try:
        local_address_offset = int(node.address_offset)
        absolute_address = int(node.absolute_address)
        address_block_absolute_address = int(address_block_node.absolute_address)
        address_offset = absolute_address - address_block_absolute_address
        if address_offset < 0:
            raise ValueError("register address precedes its enclosing address-map base")
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        address_offset = None
        absolute_address = None
        status = FactState.TAINTED
        context.report(
            "OC1103",
            Severity.WARNING,
            f"Cannot resolve concrete byte address for SystemRDL register {_path(node)}: {error}",
            location=location,
            scope=relative,
        )

    raw_width = _property(node, "regwidth")
    size_bits = raw_width if type(raw_width) is int and raw_width > 0 else None
    if size_bits is None:
        status = FactState.TAINTED
        context.report(
            "OC1103",
            Severity.WARNING,
            f"Cannot resolve a positive width for SystemRDL register {_path(node)}",
            location=location,
            scope=relative,
        )

    fields = tuple(
        sorted(
            (
                _field_observation(
                    field_node,
                    view=view,
                    fallback=fallback,
                    register_scope=relative,
                    context=context,
                    source_map=source_map,
                )
                for field_node in node.fields(skip_not_present=True)
            ),
            key=lambda item: (
                item.bit_offset if item.bit_offset is not None else -1,
                item.native_name.casefold(),
                item.native_name,
            ),
        )
    )
    accesses = {item.access for item in fields if item.status == FactState.KNOWN and item.access}
    register_access = next(iter(accesses)) if len(accesses) == 1 else None
    raw_indices = getattr(node, "current_idx", None)

    return RegisterObservation(
        native_name=name,
        component=component_name,
        memory_map=str(top.inst_name),
        address_block=address_block,
        address_offset=address_offset,
        absolute_address=absolute_address,
        size_bits=size_bits,
        access=register_access,
        fields=fields,
        provenance=location,
        status=status,
        attributes={
            "systemrdl_path": _path(node),
            "local_address_offset": local_address_offset,
            "address_block_absolute_address": address_block_absolute_address,
            "selected_top_path": top_path,
            "register_files": list(register_files),
            "accesswidth": _attribute_value(_property(node, "accesswidth")),
            "array_dimensions": _attribute_value(getattr(node, "array_dimensions", None)),
            "array_indices": _attribute_value(raw_indices),
            "array_stride": _attribute_value(getattr(node, "array_stride", None)),
            "dontcompare": bool(_property(node, "dontcompare", False)),
            "donttest": bool(_property(node, "donttest", False)),
            "external": bool(getattr(node, "external", False)),
            "is_alias": is_alias,
            "is_virtual": is_virtual,
        },
    )


def parse_systemrdl(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    top: str | None = None,
    component_name: str | None = None,
    include_dirs: Sequence[Pathish] = (),
    defines: Mapping[str, Any] | None = None,
) -> ViewObservation:
    """Compile explicit SystemRDL units and import a selected address map.

    Compilation units are processed in caller order because SystemRDL type
    definitions may be referenced by later files. Source ``include`` and Perl
    preprocessor tags are rejected; callers must list every unit explicitly.
    """

    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="systemrdl", name=view_name)
    if top is not None and (not isinstance(top, str) or not top.strip()):
        raise ValueError("top must be a nonempty string")
    if component_name is not None and (
        not isinstance(component_name, str) or not component_name.strip()
    ):
        raise ValueError("component_name must be a nonempty string")
    if include_dirs:
        raise ValueError(
            "include_dirs are unsupported for SystemRDL; list every compilation unit explicitly"
        )
    if defines is not None and not isinstance(defines, Mapping):
        raise ValueError("defines must be a mapping")
    selected_defines = {} if defines is None else defines
    if any(not isinstance(name, str) or not name for name in selected_defines):
        raise ValueError("define names must be nonempty strings")

    units, preflight_diagnostics = _preflight(source_paths, view)
    if preflight_diagnostics:
        return ViewObservation(
            view=view,
            diagnostics=preflight_diagnostics,
            complete=False,
            tainted_scopes=frozenset(("*",)),
            attributes={
                "parser": "systemrdl-compiler",
                "source_files": [str(path) for path in source_paths],
                "preflight_rejected": True,
            },
        )

    api = _load_systemrdl()
    if api is None:
        return unavailable_view(
            view=view,
            paths=source_paths,
            code="OC1102",
            message="SystemRDL parsing requires the MIT-licensed systemrdl-compiler package",
            help="Install OpenCollate with its SystemRDL parser dependency.",
        )

    printer = _MessagePrinter()
    compiler = api.compiler(message_printer=printer, perl_safe_opcodes=[])
    compiled_defines = {
        str(name): "" if value is None else str(value)
        for name, value in sorted(selected_defines.items(), key=lambda item: str(item[0]))
    }
    root: Any | None = None
    failure: BaseException | None = None
    source_map: dict[str, Path] = {}
    snapshot_directory: tempfile.TemporaryDirectory[str] | None = None
    try:
        snapshot_directory = tempfile.TemporaryDirectory(prefix="opencollate-systemrdl-")
        snapshots: list[Path] = []
        for index, unit in enumerate(units):
            snapshot = Path(snapshot_directory.name) / f"unit-{index:04d}.rdl"
            snapshot.write_bytes(unit.text.encode("utf-8"))
            resolved_snapshot = snapshot.resolve()
            source_map[_source_key(resolved_snapshot)] = unit.path
            snapshots.append(resolved_snapshot)
        for snapshot in snapshots:
            compiler.compile_file(
                str(snapshot),
                incl_search_paths=[],
                defines=compiled_defines,
            )
        root = compiler.elaborate(top_def_name=top.strip() if top is not None else None)
    except Exception as error:  # dependency exceptions are not under one common class
        failure = error

    def finish(result: ViewObservation) -> ViewObservation:
        if snapshot_directory is not None:
            snapshot_directory.cleanup()
        return result

    diagnostics = _backend_diagnostics(printer.messages, view, source_paths[0], source_map)
    if failure is not None:
        if not any(item.severity == Severity.FATAL for item in diagnostics):
            expected = isinstance(failure, api.compile_error) or isinstance(
                failure, (OSError, UnicodeError)
            )
            code = "OC1101" if expected else "OC9001"
            diagnostics.append(
                parser_diagnostic(
                    code,
                    Severity.FATAL,
                    "SystemRDL compilation failed"
                    + (f": {failure}" if expected else f" unexpectedly: {type(failure).__name__}"),
                    location=provenance(source_paths[0], view),
                    metadata={"backend": "systemrdl-compiler"},
                )
            )
        return finish(
            ViewObservation(
                view=view,
                diagnostics=tuple(diagnostics),
                complete=False,
                tainted_scopes=frozenset(("*",)),
                attributes={
                    "parser": "systemrdl-compiler",
                    "backend_version": api.version,
                    "source_files": [str(path) for path in source_paths],
                    "selected_top": top,
                },
            )
        )

    top_node = getattr(root, "top", None)
    if top_node is None or not isinstance(top_node, api.addrmap_node):
        diagnostics.append(
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                "SystemRDL elaboration did not produce a top-level address map",
                location=provenance(source_paths[0], view),
            )
        )
        return finish(
            ViewObservation(
                view=view,
                diagnostics=tuple(diagnostics),
                complete=False,
                tainted_scopes=frozenset(("*",)),
                attributes={"parser": "systemrdl-compiler", "backend_version": api.version},
            )
        )

    templates, memories, signals, limit_diagnostic = _validate_model_limits(
        api, top_node, view, source_paths[0]
    )
    if limit_diagnostic is not None:
        diagnostics.append(limit_diagnostic)
        return finish(
            ViewObservation(
                view=view,
                diagnostics=tuple(diagnostics),
                complete=False,
                tainted_scopes=frozenset(("*",)),
                attributes={
                    "parser": "systemrdl-compiler",
                    "backend_version": api.version,
                    "source_files": [str(path) for path in source_paths],
                    "selected_top": str(top_node.inst_name),
                },
            )
        )

    context = _ExtractionContext(view, diagnostics)
    top_path = _path(top_node)
    selected_component = (
        component_name.strip() if component_name is not None else str(top_node.inst_name)
    )
    if memories:
        location = _source_location(
            memories[0], view, source_paths[0], str(memories[0].inst_name), source_map
        )
        memory_paths = sorted(_relative_path(item, top_path) for item in memories)
        context.report(
            "OC1102",
            Severity.WARNING,
            f"SystemRDL view contains {len(memories)} memory declaration(s); memory "
            "contents and behavior are not imported",
            location=location,
            scope=memory_paths[0],
            metadata_value={"memories": memory_paths[:32]},
        )
    if signals:
        location = _source_location(
            signals[0], view, source_paths[0], str(signals[0].inst_name), source_map
        )
        signal_paths = sorted(_relative_path(item, top_path) for item in signals)
        context.report(
            "OC1105",
            Severity.INFO,
            f"SystemRDL view contains {len(signals)} signal declaration(s); signal behavior "
            "is outside register-map comparison",
            location=location,
            scope=signal_paths[0],
            metadata_value={"signals": signal_paths[:32]},
        )

    user_properties = sorted(str(name) for name in compiler.list_udps())
    if user_properties:
        context.report(
            "OC1102",
            Severity.WARNING,
            "SystemRDL user-defined properties are retained by the compiler but are not "
            "interpreted by OpenCollate: " + ", ".join(user_properties[:32]),
            location=_source_location(
                top_node, view, source_paths[0], str(top_node.inst_name), source_map
            ),
            scope=str(top_node.inst_name),
            metadata_value={"user_defined_properties": user_properties},
        )

    registers = tuple(
        sorted(
            (
                _register_observation(
                    node,
                    top=top_node,
                    top_path=top_path,
                    addrmap_node_type=api.addrmap_node,
                    component_name=selected_component,
                    view=view,
                    fallback=source_paths[0],
                    context=context,
                    source_map=source_map,
                )
                for node in top_node.descendants(unroll=True)
                if isinstance(node, api.reg_node)
            ),
            key=lambda item: (
                item.absolute_address if item.absolute_address is not None else -1,
                str(item.attributes.get("systemrdl_path", "")),
                item.native_name,
            ),
        )
    )
    complete = context.suppressed_count == 0
    if context.suppressed_count:
        context.tainted_scopes.add("*")
        diagnostics.append(
            parser_diagnostic(
                "OC1104",
                Severity.WARNING,
                f"SystemRDL analysis suppressed {context.suppressed_count} additional "
                "unsupported-semantics diagnostic(s) after the "
                f"{_MAX_SEMANTIC_DIAGNOSTICS}-diagnostic limit; the view is tainted",
                location=provenance(source_paths[0], view),
                metadata={
                    "limit": _MAX_SEMANTIC_DIAGNOSTICS,
                    "suppressed": context.suppressed_count,
                },
            )
        )
    if not registers:
        diagnostics.append(
            parser_diagnostic(
                "OC1105",
                Severity.INFO,
                f"SystemRDL top {top_node.inst_name!s} contains no concrete registers",
                location=_source_location(
                    top_node, view, source_paths[0], str(top_node.inst_name), source_map
                ),
            )
        )

    return finish(
        ViewObservation(
            view=view,
            registers=registers,
            diagnostics=tuple(diagnostics),
            complete=complete,
            tainted_scopes=frozenset(context.tainted_scopes),
            attributes={
                "parser": "systemrdl-compiler",
                "backend_version": api.version,
                "source_files": [str(path) for path in source_paths],
                "selected_top": str(top_node.inst_name),
                "component_name": selected_component,
                "register_definitions": len(templates),
                "registers": len(registers),
                "unsupported_semantics": context.semantic_count,
                "defines": compiled_defines,
            },
        )
    )


class SystemRdlParser:
    format_name = "systemrdl"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_systemrdl(paths, view_id=view_id, **options)


SystemRDLParser = SystemRdlParser

__all__ = ["SystemRDLParser", "SystemRdlParser", "parse_systemrdl"]

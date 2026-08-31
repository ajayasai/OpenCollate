"""Tolerant, dependency-free Liberty structural parser.

The importer understands Liberty's generic group / attribute syntax and then
projects only interface facts into the canonical observation model.  Timing
tables and vendor extensions are parsed structurally but deliberately not
interpreted, so new attributes do not break interface consistency checks.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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
    PortRole,
    Provenance,
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


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    raw: str
    value: str
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class _Node:
    kind: str
    name: str
    args: tuple[str, ...]
    value: str | None
    children: tuple[_Node, ...]
    token: _Token

    def groups(self, name: str) -> tuple[_Node, ...]:
        lowered = name.lower()
        return tuple(
            child
            for child in self.children
            if child.kind == "group" and child.name.lower() == lowered
        )

    def attribute(self, name: str) -> str | None:
        lowered = name.lower()
        for child in self.children:
            if child.kind == "attribute" and child.name.lower() == lowered:
                return child.value
        return None


def _advance_position(text: str, line: int, column: int) -> tuple[int, int]:
    if "\n" not in text:
        return line, column + len(text)
    return line + text.count("\n"), len(text.rsplit("\n", 1)[-1]) + 1


def _lex(text: str, path: Path, view: ViewId) -> tuple[list[_Token], list[Diagnostic]]:
    tokens: list[_Token] = []
    diagnostics: list[Diagnostic] = []
    index = 0
    line = 1
    column = 1
    symbols = frozenset("(){}:;,")
    while index < len(text):
        char = text[index]
        if char.isspace():
            line, column = _advance_position(char, line, column)
            index += 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                diagnostics.append(
                    parser_diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        "Unterminated Liberty block comment",
                        location=Provenance(str(path), line, column, view),
                    )
                )
                break
            raw = text[index : end + 2]
            line, column = _advance_position(raw, line, column)
            index = end + 2
            continue
        if text.startswith("//", index):
            end = text.find("\n", index + 2)
            end = len(text) if end < 0 else end
            raw = text[index:end]
            line, column = _advance_position(raw, line, column)
            index = end
            continue
        if char in symbols:
            tokens.append(_Token(char, char, char, line, column))
            index += 1
            column += 1
            continue
        if char == '"':
            start = index
            start_line, start_column = line, column
            index += 1
            escaped = False
            while index < len(text):
                current = text[index]
                if current == '"' and not escaped:
                    index += 1
                    break
                if current == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False
                index += 1
            raw = text[start:index]
            if not raw.endswith('"'):
                diagnostics.append(
                    parser_diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        "Unterminated Liberty string",
                        location=Provenance(str(path), start_line, start_column, view),
                    )
                )
            value = raw[1:-1] if len(raw) >= 2 and raw.endswith('"') else raw[1:]
            value = value.replace("\\\r\n", "").replace("\\\n", "")
            value = value.replace('\\"', '"').replace("\\\\", "\\")
            tokens.append(_Token("string", raw, value, start_line, start_column))
            line, column = _advance_position(raw, line, column)
            continue

        start = index
        start_line, start_column = line, column
        while index < len(text):
            if text[index].isspace() or text[index] in symbols or text[index] == '"':
                break
            if text.startswith("/*", index) or text.startswith("//", index):
                break
            index += 1
        raw = text[start:index]
        if not raw:
            raw = text[index]
            index += 1
        tokens.append(_Token("word", raw, raw, start_line, start_column))
        line, column = _advance_position(raw, line, column)
    tokens.append(_Token("eof", "", "", line, column))
    return tokens, diagnostics


class _Parser:
    def __init__(self, tokens: list[_Token], path: Path, view: ViewId) -> None:
        self.tokens = tokens
        self.path = path
        self.view = view
        self.index = 0
        self.diagnostics: list[Diagnostic] = []

    @property
    def token(self) -> _Token:
        return self.tokens[self.index]

    def advance(self) -> _Token:
        token = self.token
        if token.kind != "eof":
            self.index += 1
        return token

    def location(self, token: _Token) -> Provenance:
        return Provenance(str(self.path), token.line, token.column, self.view)

    def error(self, message: str, token: _Token | None = None) -> None:
        actual = token or self.token
        self.diagnostics.append(
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                message,
                location=self.location(actual),
            )
        )

    def parse(self) -> tuple[_Node, ...]:
        return self.parse_items(in_group=False)

    def parse_items(self, *, in_group: bool) -> tuple[_Node, ...]:
        nodes: list[_Node] = []
        while self.token.kind != "eof":
            if self.token.kind == "}":
                if in_group:
                    self.advance()
                    return tuple(nodes)
                self.error("Unexpected '}' in Liberty source")
                self.advance()
                continue
            node = self.parse_item()
            if node is not None:
                nodes.append(node)
        if in_group:
            self.error("Liberty group is missing its closing '}'")
        return tuple(nodes)

    def parse_item(self) -> _Node | None:
        name_token = self.advance()
        if name_token.kind not in {"word", "string"}:
            self.error(
                f"Expected Liberty attribute or group name, found {name_token.raw!r}", name_token
            )
            self.recover()
            return None
        name = name_token.value
        if self.token.kind == ":":
            self.advance()
            value_tokens = self.until_semicolon()
            return _Node(
                "attribute",
                name,
                (),
                _value(value_tokens),
                (),
                name_token,
            )
        if self.token.kind != "(":
            self.error(f"Expected ':' or '(' after Liberty name {name!r}")
            self.recover()
            return None
        self.advance()
        args = self.arguments()
        if self.token.kind == "{":
            self.advance()
            children = self.parse_items(in_group=True)
            if self.token.kind == ";":
                self.advance()
            return _Node("group", name, args, None, children, name_token)
        if self.token.kind == ";":
            self.advance()
            return _Node("call", name, args, None, (), name_token)
        self.error(f"Expected '{{' or ';' after Liberty call {name!r}")
        self.recover()
        return _Node("call", name, args, None, (), name_token)

    def arguments(self) -> tuple[str, ...]:
        groups: list[list[_Token]] = [[]]
        depth = 0
        while self.token.kind != "eof":
            token = self.advance()
            if token.kind == "(":
                depth += 1
                groups[-1].append(token)
            elif token.kind == ")":
                if depth == 0:
                    return tuple(_value(group) for group in groups if group)
                depth -= 1
                groups[-1].append(token)
            elif token.kind == "," and depth == 0:
                groups.append([])
            else:
                groups[-1].append(token)
        self.error("Unterminated Liberty argument list")
        return tuple(_value(group) for group in groups if group)

    def until_semicolon(self) -> list[_Token]:
        result: list[_Token] = []
        depth = 0
        while self.token.kind != "eof":
            token = self.advance()
            if token.kind == "(":
                depth += 1
            elif token.kind == ")" and depth:
                depth -= 1
            elif token.kind == ";" and depth == 0:
                return result
            elif token.kind == "}" and depth == 0:
                self.index -= 1
                self.error("Liberty attribute is missing its terminating ';'", token)
                return result
            result.append(token)
        self.error("Liberty attribute is missing its terminating ';'")
        return result

    def recover(self) -> None:
        while self.token.kind not in {"eof", ";", "}"}:
            self.advance()
        if self.token.kind == ";":
            self.advance()


_MAX_GROUP_NESTING = 128


def _group_nesting_violation(
    tokens: Sequence[_Token],
    path: Path,
    view: ViewId,
) -> Diagnostic | None:
    nesting = 0
    for token in tokens:
        if token.kind == "{":
            nesting += 1
            if nesting > _MAX_GROUP_NESTING:
                return parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    f"Liberty group nesting exceeds {_MAX_GROUP_NESTING} levels",
                    location=Provenance(str(path), token.line, token.column, view),
                )
        elif token.kind == "}":
            nesting = max(0, nesting - 1)
    return None


def _value(tokens: Sequence[_Token]) -> str:
    if len(tokens) == 1 and tokens[0].kind == "string":
        return tokens[0].value
    result = ""
    previous = ""
    for token in tokens:
        if result and token.kind not in {",", ")"} and previous not in {"(", ","}:
            result += " "
        result += token.value
        previous = token.kind
    return result.strip()


def _integer(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"[+-]?\d+", value.strip())
    return int(value) if match else None


def _boolean(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().strip('"').lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    return None


_RANGE_NAME = re.compile(r"^(.*?)[\[<]\s*(-?\d+)\s*:\s*(-?\d+)\s*[\]>]$")
_BIT_NAME = re.compile(r"^(.*?)[\[<]\s*(-?\d+)\s*[\]>]$")


def _name_shape(name: str) -> tuple[str, BusShape | None]:
    range_match = _RANGE_NAME.fullmatch(name)
    if range_match:
        left, right = int(range_match.group(2)), int(range_match.group(3))
        index_range = IndexRange(left, right)
        return range_match.group(1), BusShape(
            left=left,
            right=right,
            packed=(index_range,),
            bit_indices=index_range.ordered_indices,
            explicit_scalar=False,
        )
    bit_match = _BIT_NAME.fullmatch(name)
    if bit_match:
        bit = int(bit_match.group(2))
        return bit_match.group(1), BusShape(
            width=1,
            bit_indices=(bit,),
            explicit_scalar=False,
        )
    return name, None


def _type_shape(node: _Node | None) -> BusShape:
    if node is None:
        return BusShape.unknown()
    bit_from = _integer(node.attribute("bit_from"))
    bit_to = _integer(node.attribute("bit_to"))
    width = _integer(node.attribute("bit_width"))
    descending = _boolean(node.attribute("downto"))
    if (
        bit_from is not None
        and bit_to is None
        and width is not None
        and width > 0
        and descending is not None
    ):
        bit_to = bit_from - width + 1 if descending is not False else bit_from + width - 1
    elif (
        bit_to is not None
        and bit_from is None
        and width is not None
        and width > 0
        and descending is not None
    ):
        bit_from = bit_to + width - 1 if descending is not False else bit_to - width + 1
    if bit_from is not None and bit_to is not None:
        index_range = IndexRange(bit_from, bit_to)
        return BusShape(
            width=index_range.width,
            left=bit_from,
            right=bit_to,
            packed=(index_range,),
            bit_indices=index_range.ordered_indices,
            explicit_scalar=False,
        )
    if width is not None and width > 0:
        return BusShape(width=width, explicit_scalar=False)
    return BusShape.unknown()


def _port_role(node: _Node, name: str, *, power_pin: bool) -> tuple[PortRole, FactState]:
    if power_pin:
        pg_type = (node.attribute("pg_type") or "").lower()
        if "ground" in pg_type:
            return PortRole.GROUND, FactState.KNOWN
        if "power" in pg_type:
            return PortRole.POWER, FactState.KNOWN
    if _boolean(node.attribute("clock")):
        return PortRole.CLOCK, FactState.KNOWN
    use = node.attribute("use")
    if use:
        parsed = PortRole.parse(use)
        if parsed != PortRole.UNKNOWN:
            return parsed, FactState.KNOWN
    signal_type = node.attribute("signal_type")
    if signal_type:
        parsed = PortRole.parse(signal_type)
        if parsed != PortRole.UNKNOWN:
            return parsed, FactState.KNOWN
    return infer_role_from_name(name)


def _direction_from_node(node: _Node) -> Direction:
    return Direction.parse(node.attribute("direction"))


def _extract_cell(
    cell: _Node,
    type_nodes: dict[str, _Node],
    path: Path,
    view: ViewId,
) -> tuple[ComponentObservation, list[Diagnostic]]:
    cell_name = cell.args[0] if cell.args else "<unnamed-cell>"
    cell_location = Provenance(
        str(path),
        cell.token.line,
        cell.token.column,
        view,
        raw_name=cell_name,
    )
    ports: list[PortObservation] = []
    functions: dict[str, str] = {}
    diagnostics: list[Diagnostic] = []
    candidates = [
        *((node, False) for node in cell.groups("pin")),
        *((node, True) for node in cell.groups("pg_pin")),
        *((node, False) for node in cell.groups("bus")),
        *((node, False) for node in cell.groups("bundle")),
    ]
    for node, power_pin in candidates:
        raw_name = node.args[0] if node.args else "<unnamed-pin>"
        native_name, inline_shape = _name_shape(raw_name)
        node_type = type_nodes.get((node.attribute("bus_type") or "").strip())
        shape = inline_shape or _type_shape(node_type)
        if node.name.lower() in {"pin", "pg_pin"} and inline_shape is None:
            shape = BusShape.scalar()
        shape_state = FactState.KNOWN if shape.known else FactState.UNKNOWN
        direction = _direction_from_node(node)
        role, role_state = _port_role(node, native_name, power_pin=power_pin)
        location = Provenance(
            str(path),
            node.token.line,
            node.token.column,
            view,
            raw_name=raw_name,
        )
        field_states: dict[str, FactState] = {"role": role_state}
        if direction == Direction.UNKNOWN:
            field_states["direction"] = FactState.UNKNOWN
        if shape_state != FactState.KNOWN:
            field_states["shape"] = shape_state
            diagnostics.append(
                parser_diagnostic(
                    "OC1103",
                    Severity.WARNING,
                    f"Liberty bus width is unresolved for {cell_name}/{native_name}",
                    location=location,
                )
            )
        function = node.attribute("function")
        if function:
            try:
                parse_boolean(function)
            except BooleanSyntaxError as error:
                diagnostics.append(
                    parser_diagnostic(
                        "OC1102",
                        Severity.WARNING,
                        (
                            "Unsupported Liberty Boolean function on "
                            f"{cell_name}/{native_name}: {error.message}"
                        ),
                        location=location,
                        metadata={"expression": function, "offset": error.offset},
                    )
                )
            else:
                functions[native_name] = function
        ports.append(
            PortObservation(
                native_name=native_name,
                direction=direction,
                role=role,
                shape=shape,
                provenance=location,
                attributes={
                    "liberty_group": node.name,
                    "bus_type": node.attribute("bus_type"),
                    "pg_type": node.attribute("pg_type"),
                    "function": function,
                    "role_source": "name_heuristic"
                    if role_state == FactState.TAINTED
                    else "explicit",
                },
                field_states=field_states,
            )
        )
    return (
        ComponentObservation(
            native_name=cell_name,
            kind=ComponentKind.CELL,
            ports=tuple(ports),
            functions=functions,
            provenance=cell_location,
            attributes={"liberty_group": "cell"},
        ),
        diagnostics,
    )


def parse_liberty(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
) -> ViewObservation:
    """Parse Liberty interface facts without interpreting timing tables."""

    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="liberty", name=view_name)
    components: list[ComponentObservation] = []
    diagnostics: list[Diagnostic] = []
    tainted: set[str] = set()
    complete = True
    libraries: list[str] = []
    encodings: dict[str, str] = {}
    for path in source_paths:
        source = read_source(path, view)
        diagnostics.extend(source.diagnostics)
        encodings[str(path)] = source.encoding
        if not source.text:
            complete = False
            tainted.add("*")
            continue
        tokens, lexer_diags = _lex(source.text, path, view)
        nesting_diagnostic = _group_nesting_violation(tokens, path, view)
        if nesting_diagnostic is not None:
            diagnostics.extend((*lexer_diags, nesting_diagnostic))
            complete = False
            tainted.add("*")
            continue
        parser = _Parser(tokens, path, view)
        try:
            roots = parser.parse()
        except RecursionError:
            diagnostics.extend(lexer_diags)
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    "Liberty source exceeds the parser nesting limit",
                    location=Provenance(str(path), view=view),
                )
            )
            complete = False
            tainted.add("*")
            continue
        file_diags = [*lexer_diags, *parser.diagnostics]
        diagnostics.extend(file_diags)
        file_tainted = source.tainted or bool(file_diags)
        if file_tainted:
            complete = False
        library_nodes = [
            node for node in roots if node.kind == "group" and node.name.lower() == "library"
        ]
        if not library_nodes:
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    f"{path} contains no Liberty library(...) group",
                    location=Provenance(str(path), view=view),
                )
            )
            complete = False
            tainted.add("*")
            continue
        for library in library_nodes:
            library_name = library.args[0] if library.args else "<unnamed-library>"
            libraries.append(library_name)
            type_nodes = {node.args[0]: node for node in library.groups("type") if node.args}
            for cell in library.groups("cell"):
                component, cell_diags = _extract_cell(cell, type_nodes, path, view)
                if file_tainted:
                    component = ComponentObservation(
                        native_name=component.native_name,
                        kind=component.kind,
                        ports=component.ports,
                        functions=component.functions,
                        provenance=component.provenance,
                        attributes=component.attributes,
                        status=FactState.TAINTED,
                    )
                    tainted.add(component.native_name)
                components.append(component)
                diagnostics.extend(cell_diags)

    seen: dict[str, ComponentObservation] = {}
    for component in components:
        if component.native_name in seen:
            diagnostics.append(
                parser_diagnostic(
                    "OC1104",
                    Severity.WARNING,
                    f"Duplicate Liberty cell definition {component.native_name!r}",
                    location=component.provenance,
                )
            )
            tainted.add(component.native_name)
        else:
            seen[component.native_name] = component
    return ViewObservation(
        view=view,
        components=tuple(components),
        diagnostics=tuple(diagnostics),
        complete=complete,
        tainted_scopes=frozenset(tainted),
        attributes={
            "parser": "stdlib-liberty",
            "source_files": [str(path) for path in source_paths],
            "libraries": libraries,
            "encodings": encodings,
        },
    )


class LibertyParser:
    format_name = "liberty"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_liberty(paths, view_id=view_id, **options)


__all__ = ["LibertyParser", "parse_liberty"]

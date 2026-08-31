"""Safe, dependency-free importer for structural CDL/SPICE netlists.

The importer intentionally does not simulate a circuit, evaluate parameters, or
guess electrical intent.  It extracts subcircuit interfaces and connectivity
from a bounded structural subset and marks malformed or unsupported facts so
that downstream consistency checks do not mistake partial data for truth.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from opencollate.diagnostics import Diagnostic, Severity
from opencollate.model import (
    BusShape,
    ComponentKind,
    ComponentObservation,
    DesignObjectObservation,
    Direction,
    FactState,
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
    parser_diagnostic,
    read_source,
)

_MAX_SOURCE_CHARACTERS = 16 * 1024 * 1024
_MAX_PHYSICAL_LINE_CHARACTERS = 1024 * 1024
_MAX_LOGICAL_LINES = 250_000
_MAX_TOKENS = 2_000_000
_MAX_TOKENS_PER_LINE = 65_536
_MAX_GROUP_DEPTH = 128
_MAX_NAME_CHARACTERS = 16_384
_MAX_OBJECTS = 2_000_000

_DIRECTION_CODES = {
    "i": Direction.INPUT,
    "in": Direction.INPUT,
    "input": Direction.INPUT,
    "o": Direction.OUTPUT,
    "out": Direction.OUTPUT,
    "output": Direction.OUTPUT,
    "b": Direction.INOUT,
    "io": Direction.INOUT,
    "inout": Direction.INOUT,
    "bidir": Direction.INOUT,
    "bidirectional": Direction.INOUT,
}

_ROLE_CODES = {
    "s": PortRole.SIGNAL,
    "signal": PortRole.SIGNAL,
    "c": PortRole.CLOCK,
    "clk": PortRole.CLOCK,
    "clock": PortRole.CLOCK,
    "r": PortRole.RESET,
    "rst": PortRole.RESET,
    "reset": PortRole.RESET,
    "p": PortRole.POWER,
    "power": PortRole.POWER,
    "vdd": PortRole.POWER,
    "g": PortRole.GROUND,
    "ground": PortRole.GROUND,
    "vss": PortRole.GROUND,
    "a": PortRole.ANALOG,
    "analog": PortRole.ANALOG,
    "t": PortRole.TIE,
    "tie": PortRole.TIE,
}


@dataclass(frozen=True, slots=True)
class _Fragment:
    text: str
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class _LogicalLine:
    text: str
    lines: tuple[int, ...]
    columns: tuple[int, ...]
    raw: str
    metadata: bool = False

    @property
    def line(self) -> int:
        return self.lines[0] if self.lines else 1

    @property
    def column(self) -> int:
        return self.columns[0] if self.columns else 1

    def position(self, index: int) -> tuple[int, int]:
        if not self.lines:
            return 1, 1
        position = min(max(index, 0), len(self.lines) - 1)
        return self.lines[position], self.columns[position]


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    raw: str
    line: int
    column: int


@dataclass(slots=True)
class _PortRecord:
    name: str
    raw_name: str
    provenance: Provenance
    direction: Direction = Direction.UNKNOWN
    role: PortRole = PortRole.UNKNOWN
    status: FactState = FactState.KNOWN
    direction_state: FactState = FactState.UNKNOWN
    role_state: FactState = FactState.UNKNOWN
    metadata: list[Mapping[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _InstanceRecord:
    name: str
    provenance: Provenance
    attributes: dict[str, Any]
    status: FactState = FactState.KNOWN


@dataclass(slots=True)
class _NetRecord:
    name: str
    provenance: Provenance
    is_port: bool = False
    connections: list[Mapping[str, Any]] = field(default_factory=list)
    status: FactState = FactState.KNOWN


@dataclass(slots=True)
class _SubcktBuilder:
    name: str
    provenance: Provenance
    ports: list[_PortRecord] = field(default_factory=list)
    parameters: OrderedDict[str, str] = field(default_factory=OrderedDict)
    parameter_spellings: dict[str, str] = field(default_factory=dict)
    instances: list[_InstanceRecord] = field(default_factory=list)
    instance_indices: dict[str, list[int]] = field(default_factory=dict)
    nets: OrderedDict[str, _NetRecord] = field(default_factory=OrderedDict)
    extra_objects: list[DesignObjectObservation] = field(default_factory=list)
    directives: list[Mapping[str, Any]] = field(default_factory=list)
    status: FactState = FactState.KNOWN
    tainted: bool = False
    ends_provenance: Provenance | None = None

    def mark_tainted(self) -> None:
        self.tainted = True
        self.status = FactState.TAINTED


@dataclass(frozen=True, slots=True)
class _FileResult:
    components: tuple[ComponentObservation, ...]
    objects: tuple[DesignObjectObservation, ...]
    diagnostics: tuple[Diagnostic, ...]
    tainted_scopes: frozenset[str]
    complete: bool
    attributes: Mapping[str, Any]


def _case_key(value: str) -> str:
    return value.casefold()


def _flatten_fragments(
    fragments: Sequence[_Fragment],
    *,
    metadata: bool = False,
) -> _LogicalLine:
    characters: list[str] = []
    lines: list[int] = []
    columns: list[int] = []
    raw_fragments: list[str] = []
    for index, fragment in enumerate(fragments):
        if index:
            characters.append(" ")
            lines.append(fragment.line)
            columns.append(max(1, fragment.column - 1))
        raw_fragments.append(fragment.text)
        for offset, character in enumerate(fragment.text):
            characters.append(character)
            lines.append(fragment.line)
            columns.append(fragment.column + offset)
    return _LogicalLine(
        "".join(characters),
        tuple(lines),
        tuple(columns),
        "\n+ ".join(raw_fragments),
        metadata,
    )


def _inline_content(line: str) -> str:
    """Remove common inline comments without interpreting grouped text."""

    quote = ""
    stack: list[str] = []
    escaped = False
    pairs = {"(": ")", "[": "]", "{": "}"}
    index = 0
    while index < len(line):
        character = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if stack:
            if character in pairs:
                stack.append(pairs[character])
            elif character == stack[-1]:
                stack.pop()
            index += 1
            continue
        if character in pairs:
            stack.append(pairs[character])
            index += 1
            continue
        preceded_by_space = index == 0 or line[index - 1].isspace()
        if preceded_by_space and character in {"$", ";"}:
            return line[:index].rstrip()
        if preceded_by_space and line.startswith("//", index):
            return line[:index].rstrip()
        index += 1
    return line.rstrip()


def _comment_metadata(line: str) -> tuple[str, int] | None:
    stripped = line.lstrip()
    leading = len(line) - len(stripped)
    if not stripped.startswith("*"):
        return None
    body = stripped[1:].lstrip()
    body_column = leading + 2 + (len(stripped[1:]) - len(body))
    normalized = body
    if normalized.startswith("."):
        normalized = normalized[1:]
    upper = normalized.upper()
    accepted = (
        "PININFO",
        "PORT_DIRECTION",
        "PIN ",
        "PORT ",
        "|P ",
        "|P(",
    )
    if not any(upper == item.rstrip() or upper.startswith(item) for item in accepted):
        return None
    if upper.startswith("|P"):
        return f".DSPF_PIN {normalized[2:].lstrip()}", body_column
    return f".{normalized}", body_column


class _FileParser:
    def __init__(self, path: Path, text: str, view: ViewId) -> None:
        self.path = path
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.view = view
        self.diagnostics: list[Diagnostic] = []
        self.builders: list[_SubcktBuilder] = []
        self.current: _SubcktBuilder | None = None
        self.top_instances: list[_InstanceRecord] = []
        self.top_instance_indices: dict[str, list[int]] = {}
        self.top_nets: OrderedDict[str, _NetRecord] = OrderedDict()
        self.top_objects: list[DesignObjectObservation] = []
        self.global_nets: OrderedDict[str, tuple[str, Provenance]] = OrderedDict()
        self.top_parameters: OrderedDict[str, str] = OrderedDict()
        self.top_parameter_spellings: dict[str, str] = {}
        self.models: OrderedDict[str, DesignObjectObservation] = OrderedDict()
        self.tainted_scopes: set[str] = set()
        self.complete = True
        self.ended = False
        self.title: str | None = None
        self._logical_line_count = 0
        self._token_count = 0
        self._object_count = 0
        self._limit_reached = False

    def _provenance(
        self,
        line: int,
        column: int,
        raw_name: str | None = None,
    ) -> Provenance:
        return Provenance(str(self.path), line, column, self.view, raw_name)

    def _scope(self) -> str:
        return self.current.name if self.current is not None else "*"

    def _diagnose(
        self,
        code: str,
        severity: Severity,
        message: str,
        *,
        line: int,
        column: int,
        scope: str | None = None,
        taint: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.diagnostics.append(
            parser_diagnostic(
                code,
                severity,
                message,
                location=self._provenance(line, column),
                metadata=metadata,
            )
        )
        if not taint:
            return
        self.complete = False
        tainted_scope = scope or self._scope()
        self.tainted_scopes.add(tainted_scope)
        if self.current is not None and tainted_scope in {"*", self.current.name}:
            self.current.mark_tainted()

    def _resource_limit(self, message: str, line: int, column: int) -> None:
        if self._limit_reached:
            return
        self._limit_reached = True
        self._diagnose(
            "OC1101",
            Severity.FATAL,
            f"CDL/SPICE resource limit exceeded: {message}",
            line=line,
            column=column,
            scope="*",
        )

    def _logical_lines(self) -> tuple[_LogicalLine, ...]:
        if len(self.text) > _MAX_SOURCE_CHARACTERS:
            self._resource_limit(
                f"source contains more than {_MAX_SOURCE_CHARACTERS} characters",
                1,
                1,
            )
            return ()
        result: list[_LogicalLine] = []
        pending: list[_Fragment] = []
        for line_number, physical in enumerate(self.text.split("\n"), 1):
            if len(physical) > _MAX_PHYSICAL_LINE_CHARACTERS:
                self._resource_limit(
                    (
                        "physical line contains more than "
                        f"{_MAX_PHYSICAL_LINE_CHARACTERS} characters"
                    ),
                    line_number,
                    1,
                )
                break
            metadata = _comment_metadata(physical)
            if metadata is not None:
                if pending:
                    result.append(_flatten_fragments(pending))
                    pending = []
                metadata_text, column = metadata
                result.append(
                    _flatten_fragments(
                        (_Fragment(metadata_text, line_number, column),),
                        metadata=True,
                    )
                )
                continue
            stripped = physical.lstrip()
            if not stripped or stripped.startswith("*"):
                continue
            if stripped.startswith("//") or stripped.startswith(";"):
                continue
            content = _inline_content(physical)
            stripped_content = content.lstrip()
            if not stripped_content:
                continue
            column = len(content) - len(stripped_content) + 1
            if stripped_content.startswith("+"):
                continued = stripped_content[1:].lstrip()
                continued_column = column + 1 + (len(stripped_content[1:]) - len(continued))
                if not pending:
                    self._diagnose(
                        "OC1101",
                        Severity.ERROR,
                        "CDL/SPICE continuation has no preceding statement",
                        line=line_number,
                        column=column,
                        scope="*",
                    )
                    continue
                if continued:
                    pending.append(_Fragment(continued, line_number, continued_column))
                continue
            if pending:
                result.append(_flatten_fragments(pending))
            pending = [_Fragment(stripped_content, line_number, column)]
            if len(result) >= _MAX_LOGICAL_LINES:
                self._resource_limit(
                    f"source contains more than {_MAX_LOGICAL_LINES} logical lines",
                    line_number,
                    column,
                )
                pending = []
                break
        if pending and not self._limit_reached:
            result.append(_flatten_fragments(pending))
        if len(result) > _MAX_LOGICAL_LINES:
            line = result[_MAX_LOGICAL_LINES].line
            column = result[_MAX_LOGICAL_LINES].column
            self._resource_limit(
                f"source contains more than {_MAX_LOGICAL_LINES} logical lines",
                line,
                column,
            )
            return tuple(result[:_MAX_LOGICAL_LINES])
        return tuple(result)

    def _tokenize(self, logical: _LogicalLine) -> tuple[_Token, ...]:
        tokens: list[_Token] = []
        text = logical.text
        index = 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                break
            start = index
            line, column = logical.position(start)
            raw: list[str] = []
            value: list[str] = []
            stack: list[str] = []
            quote = ""
            malformed = False
            while index < len(text):
                character = text[index]
                if character == "\\":
                    raw.append(character)
                    index += 1
                    if index >= len(text):
                        self._diagnose(
                            "OC1101",
                            Severity.ERROR,
                            "CDL/SPICE token ends with an incomplete escape",
                            line=line,
                            column=column,
                        )
                        malformed = True
                        break
                    raw.append(text[index])
                    value.append(text[index])
                    index += 1
                    continue
                if quote:
                    raw.append(character)
                    if character == quote:
                        quote = ""
                    else:
                        value.append(character)
                    index += 1
                    continue
                if character in {"'", '"'}:
                    quote = character
                    raw.append(character)
                    index += 1
                    continue
                if character in "([{":
                    if len(stack) >= _MAX_GROUP_DEPTH:
                        self._resource_limit(
                            f"token grouping exceeds depth {_MAX_GROUP_DEPTH}",
                            line,
                            column,
                        )
                        malformed = True
                        break
                    stack.append({"(": ")", "[": "]", "{": "}"}[character])
                    raw.append(character)
                    value.append(character)
                    index += 1
                    continue
                if character in ")]}":
                    if not stack or character != stack[-1]:
                        self._diagnose(
                            "OC1101",
                            Severity.ERROR,
                            f"CDL/SPICE token contains unmatched {character!r}",
                            line=line,
                            column=column,
                        )
                        malformed = True
                    else:
                        stack.pop()
                    raw.append(character)
                    value.append(character)
                    index += 1
                    continue
                if character.isspace() and not stack:
                    break
                raw.append(character)
                value.append(character)
                index += 1
            if quote or stack:
                reason = "quote" if quote else "group"
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    f"CDL/SPICE token has an unterminated {reason}",
                    line=line,
                    column=column,
                )
                malformed = True
            token_value = "".join(value)
            token_raw = "".join(raw)
            if token_value or token_raw:
                tokens.append(_Token(token_value, token_raw, line, column))
            if malformed and self._limit_reached:
                break
            if len(tokens) > _MAX_TOKENS_PER_LINE:
                self._resource_limit(
                    f"logical line contains more than {_MAX_TOKENS_PER_LINE} tokens",
                    line,
                    column,
                )
                return tuple(tokens[:_MAX_TOKENS_PER_LINE])
        self._token_count += len(tokens)
        if self._token_count > _MAX_TOKENS:
            self._resource_limit(
                f"source contains more than {_MAX_TOKENS} tokens",
                logical.line,
                logical.column,
            )
        return tuple(tokens)

    def _valid_name(self, token: _Token, what: str) -> bool:
        if not token.value:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"CDL/SPICE {what} name is empty",
                line=token.line,
                column=token.column,
            )
            return False
        if len(token.value) > _MAX_NAME_CHARACTERS:
            self._resource_limit(
                f"{what} name contains more than {_MAX_NAME_CHARACTERS} characters",
                token.line,
                token.column,
            )
            return False
        return True

    @staticmethod
    def _parameter_boundary(tokens: Sequence[_Token], start: int) -> int:
        for index in range(start, len(tokens)):
            value = tokens[index].value
            if value.casefold() in {"params:", "parameters:", "param:"}:
                return index
            if "=" in value or value == "=":
                return index
            if index + 1 < len(tokens) and tokens[index + 1].value == "=":
                return index
        return len(tokens)

    def _parse_parameters(
        self,
        tokens: Sequence[_Token],
        *,
        scope: str,
    ) -> tuple[OrderedDict[str, str], dict[str, str], tuple[str, ...], bool]:
        values: OrderedDict[str, str] = OrderedDict()
        spellings: dict[str, str] = {}
        raw_tokens = tuple(token.raw for token in tokens)
        valid = True
        index = 0
        if tokens and tokens[0].value.casefold() in {
            "params:",
            "parameters:",
            "param:",
        }:
            index = 1
        while index < len(tokens):
            token = tokens[index]
            name = ""
            value = ""
            if "=" in token.value and token.value != "=":
                name, value = token.value.split("=", 1)
                if not value and index + 1 < len(tokens):
                    index += 1
                    value = tokens[index].value
            elif index + 1 < len(tokens) and tokens[index + 1].value == "=":
                name = token.value
                index += 2
                if index < len(tokens):
                    value = tokens[index].value
                else:
                    index -= 1
            else:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    f"Malformed CDL/SPICE parameter token {token.raw!r}; expected name=value",
                    line=token.line,
                    column=token.column,
                    scope=scope,
                )
                valid = False
                index += 1
                continue
            name = name.strip()
            if not name or not value:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    f"Malformed CDL/SPICE parameter assignment {token.raw!r}",
                    line=token.line,
                    column=token.column,
                    scope=scope,
                )
                valid = False
                index += 1
                continue
            key = _case_key(name)
            if key in values:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    f"Duplicate CDL/SPICE parameter {name!r} in scope {scope!r}",
                    line=token.line,
                    column=token.column,
                    scope=scope,
                )
                valid = False
            else:
                values[key] = value
                spellings[key] = name
            index += 1
        return values, spellings, raw_tokens, valid

    def _start_subckt(self, tokens: Sequence[_Token]) -> None:
        directive = tokens[0]
        if len(tokens) < 2 or not self._valid_name(tokens[1], "subcircuit"):
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "CDL/SPICE .SUBCKT requires a nonempty name",
                line=directive.line,
                column=directive.column,
            )
            return
        if self.current is not None:
            previous = self.current
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                (f"Nested .SUBCKT {tokens[1].value!r} implicitly terminates {previous.name!r}"),
                line=directive.line,
                column=directive.column,
                scope=previous.name,
            )
            previous.mark_tainted()
            self.current = None
        name_token = tokens[1]
        builder = _SubcktBuilder(
            name_token.value,
            self._provenance(
                name_token.line,
                name_token.column,
                raw_name=name_token.raw,
            ),
        )
        self.builders.append(builder)
        self.current = builder
        boundary = self._parameter_boundary(tokens, 2)
        pin_tokens = tokens[2:boundary]
        seen: dict[str, list[int]] = {}
        for pin_token in pin_tokens:
            if not self._valid_name(pin_token, "pin"):
                builder.mark_tainted()
                continue
            key = _case_key(pin_token.value)
            record = _PortRecord(
                pin_token.value,
                pin_token.raw,
                self._provenance(
                    pin_token.line,
                    pin_token.column,
                    raw_name=pin_token.raw,
                ),
            )
            builder.ports.append(record)
            indices = seen.setdefault(key, [])
            indices.append(len(builder.ports) - 1)
            if len(indices) > 1:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    f"Duplicate pin {pin_token.value!r} in .SUBCKT {builder.name!r}",
                    line=pin_token.line,
                    column=pin_token.column,
                    scope=builder.name,
                )
                for port_index in indices:
                    builder.ports[port_index].status = FactState.TAINTED
            self._record_net(
                builder,
                pin_token.value,
                pin_token,
                is_port=True,
            )
        if boundary < len(tokens):
            parameters, spellings, _, valid = self._parse_parameters(
                tokens[boundary:],
                scope=builder.name,
            )
            builder.parameters.update(parameters)
            builder.parameter_spellings.update(spellings)
            if not valid:
                builder.mark_tainted()
        duplicates = [
            item for item in self.builders[:-1] if _case_key(item.name) == _case_key(builder.name)
        ]
        if duplicates:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"Duplicate .SUBCKT definition for {builder.name!r}",
                line=name_token.line,
                column=name_token.column,
                scope=builder.name,
            )
            builder.mark_tainted()
            for duplicate in duplicates:
                duplicate.mark_tainted()
                self.tainted_scopes.add(duplicate.name)

    def _end_subckt(self, tokens: Sequence[_Token]) -> None:
        directive = tokens[0]
        if self.current is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "CDL/SPICE .ENDS has no matching .SUBCKT",
                line=directive.line,
                column=directive.column,
                scope="*",
            )
            return
        builder = self.current
        if len(tokens) > 2:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "CDL/SPICE .ENDS accepts at most one subcircuit name",
                line=tokens[2].line,
                column=tokens[2].column,
                scope=builder.name,
            )
        if len(tokens) >= 2 and _case_key(tokens[1].value) != _case_key(builder.name):
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                (
                    f"CDL/SPICE .ENDS name {tokens[1].value!r} does not match "
                    f".SUBCKT {builder.name!r}"
                ),
                line=tokens[1].line,
                column=tokens[1].column,
                scope=builder.name,
            )
        builder.ends_provenance = self._provenance(directive.line, directive.column)
        self.current = None

    def _record_net(
        self,
        builder: _SubcktBuilder | None,
        name: str,
        token: _Token,
        *,
        is_port: bool = False,
        connection: Mapping[str, Any] | None = None,
        status: FactState = FactState.KNOWN,
    ) -> None:
        target = builder.nets if builder is not None else self.top_nets
        key = _case_key(name)
        record = target.get(key)
        if record is None:
            self._object_count += 1
            if self._object_count > _MAX_OBJECTS:
                self._resource_limit(
                    f"source produces more than {_MAX_OBJECTS} objects",
                    token.line,
                    token.column,
                )
                return
            record = _NetRecord(
                name,
                self._provenance(token.line, token.column, raw_name=token.raw),
                is_port=is_port,
                status=status,
            )
            target[key] = record
        else:
            record.is_port = record.is_port or is_port
            if status != FactState.KNOWN:
                record.status = status
        if connection is not None:
            record.connections.append(dict(connection))

    def _record_instance(
        self,
        name_token: _Token,
        *,
        instance_type: str,
        nodes: Sequence[_Token],
        master: _Token | None,
        parameters: Mapping[str, str],
        parameter_spellings: Mapping[str, str],
        parameter_tokens: Sequence[str],
        raw: str,
        status: FactState,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        builder = self.current
        scope = builder.name if builder is not None else None
        instance_attributes: dict[str, Any] = {
            "instance_type": instance_type,
            "nodes": [node.value for node in nodes],
            "parameters": {
                parameter_spellings.get(key, key): value for key, value in parameters.items()
            },
            "parameter_tokens": list(parameter_tokens),
            "raw": raw,
        }
        if master is not None:
            instance_attributes["master"] = master.value
            instance_attributes["raw_master"] = master.raw
        if attributes:
            instance_attributes.update(attributes)
        instance = _InstanceRecord(
            name_token.value,
            self._provenance(
                name_token.line,
                name_token.column,
                raw_name=name_token.raw,
            ),
            instance_attributes,
            status,
        )
        records = builder.instances if builder is not None else self.top_instances
        indices_by_name = (
            builder.instance_indices if builder is not None else self.top_instance_indices
        )
        records.append(instance)
        indices = indices_by_name.setdefault(_case_key(instance.name), [])
        indices.append(len(records) - 1)
        if len(indices) > 1:
            scope_name = builder.name if builder is not None else "top level"
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"Duplicate instance {instance.name!r} in {scope_name!r}",
                line=name_token.line,
                column=name_token.column,
                scope=builder.name if builder is not None else "*",
            )
            for record_index in indices:
                records[record_index].status = FactState.TAINTED
        for terminal_index, node in enumerate(nodes):
            self._record_net(
                builder,
                node.value,
                node,
                connection={
                    "instance": instance.name,
                    "terminal_index": terminal_index,
                    "instance_type": instance_type,
                },
                status=status if status != FactState.UNSUPPORTED else FactState.TAINTED,
            )
        if master is not None:
            kind = "component" if instance_type == "subcircuit" else "model"
            reference = DesignObjectObservation(
                kind,
                master.value,
                relation="reference",
                provenance=self._provenance(
                    master.line,
                    master.column,
                    raw_name=master.raw,
                ),
                status=status,
                attributes={
                    "instance": instance.name,
                    "scope": scope,
                    "instance_type": instance_type,
                },
            )
            if builder is not None:
                builder.extra_objects.append(reference)
            else:
                self.top_objects.append(reference)

    @staticmethod
    def _slash_index(tokens: Sequence[_Token], start: int) -> int | None:
        return next(
            (index for index in range(start, len(tokens)) if tokens[index].value == "/"),
            None,
        )

    def _parse_instance_parameters(
        self,
        tokens: Sequence[_Token],
        *,
        scope: str,
    ) -> tuple[OrderedDict[str, str], dict[str, str], tuple[str, ...], bool]:
        if not tokens:
            return OrderedDict(), {}, (), True
        return self._parse_parameters(tokens, scope=scope)

    def _parse_mos(self, tokens: Sequence[_Token], logical: _LogicalLine) -> None:
        name = tokens[0]
        scope = self._scope()
        slash = self._slash_index(tokens, 1)
        parameter_start = self._parameter_boundary(tokens, 1)
        master: _Token | None = None
        nodes: Sequence[_Token] = ()
        tail: Sequence[_Token] = ()
        valid = True
        if slash is not None and slash < parameter_start:
            nodes = tokens[1:slash]
            if slash + 1 < len(tokens):
                master = tokens[slash + 1]
                tail = tokens[slash + 2 :]
            else:
                valid = False
        else:
            structural = tokens[1:parameter_start]
            if len(structural) in {4, 5}:
                nodes = structural[:-1]
                master = structural[-1]
                tail = tokens[parameter_start:]
            else:
                valid = False
                nodes = structural[:4]
                master = structural[4] if len(structural) > 4 else None
                tail = tokens[parameter_start:]
        if len(nodes) not in {3, 4} or master is None:
            valid = False
        parameters, spellings, raw_parameters, parameter_valid = self._parse_instance_parameters(
            tail, scope=scope
        )
        valid = valid and parameter_valid
        if not valid:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                (
                    f"Malformed MOS instance {name.value!r}; expected 3 or 4 nodes, "
                    "a model, and optional name=value parameters"
                ),
                line=name.line,
                column=name.column,
                scope=scope,
            )
        self._record_instance(
            name,
            instance_type="mosfet",
            nodes=nodes,
            master=master,
            parameters=parameters,
            parameter_spellings=spellings,
            parameter_tokens=raw_parameters,
            raw=logical.raw,
            status=FactState.KNOWN if valid else FactState.TAINTED,
            attributes={
                "terminal_names": (
                    ["drain", "gate", "source", "bulk"]
                    if len(nodes) == 4
                    else ["drain", "gate", "source"]
                )
            },
        )

    def _parse_passive(self, tokens: Sequence[_Token], logical: _LogicalLine) -> None:
        name = tokens[0]
        designator = name.value[0].casefold()
        instance_type = {"r": "resistor", "c": "capacitor", "l": "inductor"}[designator]
        scope = self._scope()
        slash = self._slash_index(tokens, 1)
        parameter_start = self._parameter_boundary(tokens, 1)
        nodes = tokens[1:3]
        master: _Token | None = None
        value: str | None = None
        tail: Sequence[_Token] = ()
        valid = len(nodes) == 2
        if slash is not None:
            if slash != 3 or slash + 1 >= len(tokens):
                valid = False
                structural_end = min(slash, len(tokens))
                nodes = tokens[1:structural_end][:2]
            else:
                master = tokens[slash + 1]
                tail = tokens[slash + 2 :]
        else:
            structural = tokens[1:parameter_start]
            if len(structural) >= 3:
                nodes = structural[:2]
                value = structural[2].value
                if len(structural) > 3:
                    valid = False
                tail = tokens[parameter_start:]
            elif len(structural) == 2 and parameter_start < len(tokens):
                nodes = structural
                tail = tokens[parameter_start:]
            else:
                valid = False
                nodes = structural[:2]
                tail = tokens[parameter_start:]
        parameters, spellings, raw_parameters, parameter_valid = self._parse_instance_parameters(
            tail, scope=scope
        )
        device_parameter = {"r": "r", "c": "c", "l": "l"}[designator]
        if value is None and master is None and device_parameter not in parameters:
            valid = False
        valid = valid and parameter_valid
        if not valid:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                (
                    f"Malformed {instance_type} instance {name.value!r}; expected two nodes "
                    "and a value, / model, or explicit device parameter"
                ),
                line=name.line,
                column=name.column,
                scope=scope,
            )
        self._record_instance(
            name,
            instance_type=instance_type,
            nodes=nodes,
            master=master,
            parameters=parameters,
            parameter_spellings=spellings,
            parameter_tokens=raw_parameters,
            raw=logical.raw,
            status=FactState.KNOWN if valid else FactState.TAINTED,
            attributes={"value": value, "terminal_names": ["positive", "negative"]},
        )

    def _parse_subckt_instance(
        self,
        tokens: Sequence[_Token],
        logical: _LogicalLine,
    ) -> None:
        name = tokens[0]
        scope = self._scope()
        slash = self._slash_index(tokens, 1)
        parameter_start = self._parameter_boundary(tokens, 1)
        nodes: Sequence[_Token] = ()
        master: _Token | None = None
        tail: Sequence[_Token] = ()
        valid = True
        if slash is not None and slash < parameter_start:
            nodes = tokens[1:slash]
            if slash + 1 < len(tokens):
                master = tokens[slash + 1]
                tail = tokens[slash + 2 :]
            else:
                valid = False
        else:
            structural = tokens[1:parameter_start]
            if structural:
                master = structural[-1]
                nodes = structural[:-1]
                tail = tokens[parameter_start:]
            else:
                valid = False
        parameters, spellings, raw_parameters, parameter_valid = self._parse_instance_parameters(
            tail, scope=scope
        )
        valid = valid and parameter_valid and master is not None
        if not valid:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                (
                    f"Malformed subcircuit instance {name.value!r}; expected nodes, a "
                    "subcircuit name, and optional name=value parameters"
                ),
                line=name.line,
                column=name.column,
                scope=scope,
            )
        self._record_instance(
            name,
            instance_type="subcircuit",
            nodes=nodes,
            master=master,
            parameters=parameters,
            parameter_spellings=spellings,
            parameter_tokens=raw_parameters,
            raw=logical.raw,
            status=FactState.KNOWN if valid else FactState.TAINTED,
        )

    def _unsupported_instance(
        self,
        tokens: Sequence[_Token],
        logical: _LogicalLine,
    ) -> None:
        name = tokens[0]
        self._diagnose(
            "OC1102",
            Severity.WARNING,
            (
                f"Unsupported CDL/SPICE element {name.value!r}; only M, R, C, L, and X "
                "instances are structurally decoded"
            ),
            line=name.line,
            column=name.column,
        )
        self._record_instance(
            name,
            instance_type="unsupported",
            nodes=(),
            master=None,
            parameters={},
            parameter_spellings={},
            parameter_tokens=tuple(token.raw for token in tokens[1:]),
            raw=logical.raw,
            status=FactState.UNSUPPORTED,
            attributes={"designator": name.value[0] if name.value else ""},
        )

    @staticmethod
    def _decode_pin_descriptor(
        descriptor: str,
    ) -> tuple[str, Direction | None, PortRole | None]:
        parts = descriptor.split(":")
        if len(parts) < 2:
            return descriptor, None, None
        direction: Direction | None = None
        role: PortRole | None = None
        cut = len(parts)
        for index in range(len(parts) - 1, 0, -1):
            code = parts[index].strip().casefold()
            decoded_direction = _DIRECTION_CODES.get(code)
            decoded_role = _ROLE_CODES.get(code)
            if decoded_direction is None and decoded_role is None:
                break
            cut = index
            direction = decoded_direction or direction
            role = decoded_role or role
        if cut == len(parts):
            return descriptor, None, None
        return ":".join(parts[:cut]), direction, role

    def _port_records(self, name: str) -> list[_PortRecord]:
        if self.current is None:
            return []
        key = _case_key(name)
        return [port for port in self.current.ports if _case_key(port.name) == key]

    def _apply_pin_metadata(
        self,
        name: str,
        direction: Direction | None,
        role: PortRole | None,
        token: _Token,
        *,
        directive: str,
    ) -> None:
        if not name:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"{directive} contains an empty pin name",
                line=token.line,
                column=token.column,
            )
            return
        if self.current is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"{directive} pin metadata appears outside a .SUBCKT",
                line=token.line,
                column=token.column,
                scope="*",
            )
            self.top_objects.append(
                DesignObjectObservation(
                    "pin",
                    name,
                    relation="reference",
                    provenance=self._provenance(token.line, token.column, token.raw),
                    status=FactState.TAINTED,
                    attributes={"directive": directive},
                )
            )
            return
        records = self._port_records(name)
        if not records:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                (
                    f"{directive} references pin {name!r}, which is absent from "
                    f".SUBCKT {self.current.name!r}"
                ),
                line=token.line,
                column=token.column,
                scope=self.current.name,
            )
            self.current.extra_objects.append(
                DesignObjectObservation(
                    "pin",
                    name,
                    relation="reference",
                    scope=self.current.name,
                    provenance=self._provenance(token.line, token.column, token.raw),
                    status=FactState.TAINTED,
                    attributes={"directive": directive},
                )
            )
            return
        for record in records:
            metadata = {
                "directive": directive,
                "line": token.line,
                "column": token.column,
            }
            duplicate = False
            conflict = False
            if direction is not None:
                if record.direction_state == FactState.KNOWN:
                    duplicate = record.direction == direction
                    conflict = record.direction != direction
                record.direction = direction
                record.direction_state = (
                    FactState.TAINTED if duplicate or conflict else FactState.KNOWN
                )
            if role is not None:
                if record.role_state == FactState.KNOWN:
                    duplicate = duplicate or record.role == role
                    conflict = conflict or record.role != role
                record.role = role
                record.role_state = FactState.TAINTED if duplicate or conflict else FactState.KNOWN
            if direction is None and role is None:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    f"{directive} has no recognized direction or role for pin {name!r}",
                    line=token.line,
                    column=token.column,
                    scope=self.current.name,
                )
                record.status = FactState.TAINTED
            elif duplicate or conflict:
                kind = "Conflicting" if conflict else "Duplicate"
                self._diagnose(
                    "OC1101" if conflict else "OC1104",
                    Severity.ERROR if conflict else Severity.WARNING,
                    f"{kind} {directive} metadata for pin {name!r}",
                    line=token.line,
                    column=token.column,
                    scope=self.current.name,
                )
                record.status = FactState.TAINTED
            metadata["direction"] = direction.value if direction is not None else None
            metadata["role"] = role.value if role is not None else None
            record.metadata.append(metadata)

    def _pininfo(self, tokens: Sequence[_Token]) -> None:
        if len(tokens) < 2:
            directive = tokens[0]
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                ".PININFO requires at least one pin:code descriptor",
                line=directive.line,
                column=directive.column,
            )
            return
        for token in tokens[1:]:
            name, direction, role = self._decode_pin_descriptor(token.value)
            self._apply_pin_metadata(
                name,
                direction,
                role,
                token,
                directive=".PININFO",
            )

    def _port_direction(self, tokens: Sequence[_Token]) -> None:
        payload = tokens[1:]
        if not payload:
            directive = tokens[0]
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                ".PORT_DIRECTION requires pin/direction pairs",
                line=directive.line,
                column=directive.column,
            )
            return
        index = 0
        while index < len(payload):
            token = payload[index]
            name, direction, role = self._decode_pin_descriptor(token.value)
            if direction is None and role is None:
                if index + 1 >= len(payload):
                    self._apply_pin_metadata(
                        name,
                        None,
                        None,
                        token,
                        directive=".PORT_DIRECTION",
                    )
                    break
                code = payload[index + 1].value.casefold()
                direction = _DIRECTION_CODES.get(code)
                role = _ROLE_CODES.get(code)
                index += 1
            self._apply_pin_metadata(
                name,
                direction,
                role,
                token,
                directive=".PORT_DIRECTION",
            )
            index += 1

    @staticmethod
    def _option_values(tokens: Sequence[_Token]) -> Mapping[str, str]:
        result: dict[str, str] = {}
        index = 0
        while index < len(tokens):
            option_text = tokens[index].value
            if "=" in option_text and option_text != "=":
                key, value = option_text.split("=", 1)
                result[key.casefold().lstrip("-")] = value
            elif index + 1 < len(tokens):
                key = option_text.casefold().lstrip("-")
                if key in {"direction", "dir", "use", "role"}:
                    index += 1
                    result[key] = tokens[index].value
            index += 1
        return result

    def _pin_directive(self, tokens: Sequence[_Token]) -> None:
        if len(tokens) < 2:
            directive = tokens[0]
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"{directive.value} requires a pin name and explicit metadata",
                line=directive.line,
                column=directive.column,
            )
            return
        options = self._option_values(tokens[2:])
        direction_text = options.get("direction", options.get("dir"))
        role_text = options.get("use", options.get("role"))
        direction = _DIRECTION_CODES.get(direction_text.casefold()) if direction_text else None
        role = _ROLE_CODES.get(role_text.casefold()) if role_text else None
        self._apply_pin_metadata(
            tokens[1].value,
            direction,
            role,
            tokens[1],
            directive=tokens[0].value.upper(),
        )

    def _dspf_pin(self, tokens: Sequence[_Token]) -> None:
        payload = " ".join(token.value for token in tokens[1:]).strip()
        if payload.startswith("(") and payload.endswith(")"):
            payload = payload[1:-1].strip()
        words = payload.split()
        directive = tokens[0]
        if len(words) < 2:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "DSPF *|P metadata requires a pin name and I/O/B direction",
                line=directive.line,
                column=directive.column,
            )
            return
        direction = _DIRECTION_CODES.get(words[1].casefold())
        self._apply_pin_metadata(
            words[0],
            direction,
            None,
            _Token(words[0], words[0], directive.line, directive.column),
            directive="*|P",
        )

    def _parameters_directive(self, tokens: Sequence[_Token]) -> None:
        scope = self._scope()
        values, spellings, raw, valid = self._parse_parameters(tokens[1:], scope=scope)
        target = self.current.parameters if self.current is not None else self.top_parameters
        target_spellings = (
            self.current.parameter_spellings
            if self.current is not None
            else self.top_parameter_spellings
        )
        for key, value in values.items():
            if key in target:
                token = tokens[1] if len(tokens) > 1 else tokens[0]
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    f"Duplicate CDL/SPICE .PARAM {spellings[key]!r} in scope {scope!r}",
                    line=token.line,
                    column=token.column,
                    scope=scope,
                )
                valid = False
            else:
                target[key] = value
                target_spellings[key] = spellings[key]
        if self.current is not None:
            self.current.directives.append({"directive": ".PARAM", "tokens": list(raw)})
            if not valid:
                self.current.mark_tainted()

    def _global_directive(self, tokens: Sequence[_Token]) -> None:
        if len(tokens) < 2:
            directive = tokens[0]
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                ".GLOBAL requires at least one net name",
                line=directive.line,
                column=directive.column,
                scope="*",
            )
            return
        for token in tokens[1:]:
            if not self._valid_name(token, "global net"):
                continue
            key = _case_key(token.value)
            if key in self.global_nets:
                self._diagnose(
                    "OC1104",
                    Severity.WARNING,
                    f"Duplicate .GLOBAL declaration for net {token.value!r}",
                    line=token.line,
                    column=token.column,
                    scope="*",
                )
                continue
            self.global_nets[key] = (
                token.value,
                self._provenance(token.line, token.column, token.raw),
            )

    def _model_directive(self, tokens: Sequence[_Token], logical: _LogicalLine) -> None:
        directive = tokens[0]
        if len(tokens) < 3:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                ".MODEL requires a model name and type",
                line=directive.line,
                column=directive.column,
            )
            return
        name = tokens[1]
        key = _case_key(name.value)
        model = DesignObjectObservation(
            "model",
            name.value,
            provenance=self._provenance(name.line, name.column, name.raw),
            attributes={
                "model_type": tokens[2].value,
                "tokens": [token.raw for token in tokens[3:]],
                "raw": logical.raw,
            },
        )
        if key in self.models:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"Duplicate .MODEL definition for {name.value!r}",
                line=name.line,
                column=name.column,
            )
            previous = self.models[key]
            self.models[key] = replace(previous, status=FactState.TAINTED)
            if self.current is not None:
                self.current.mark_tainted()
        else:
            self.models[key] = model

    def _unsupported_directive(self, tokens: Sequence[_Token], logical: _LogicalLine) -> None:
        directive = tokens[0]
        self._diagnose(
            "OC1102",
            Severity.WARNING,
            f"Unsupported CDL/SPICE directive {directive.value!r}",
            line=directive.line,
            column=directive.column,
        )
        target = self.current.extra_objects if self.current is not None else self.top_objects
        target.append(
            DesignObjectObservation(
                "directive",
                directive.value,
                provenance=self._provenance(directive.line, directive.column, directive.raw),
                status=FactState.UNSUPPORTED,
                attributes={"tokens": [token.raw for token in tokens[1:]], "raw": logical.raw},
            )
        )

    def _parse_directive(
        self,
        tokens: Sequence[_Token],
        logical: _LogicalLine,
    ) -> None:
        directive = tokens[0].value.casefold()
        if directive == ".subckt":
            self._start_subckt(tokens)
        elif directive == ".ends":
            self._end_subckt(tokens)
        elif directive == ".pininfo":
            self._pininfo(tokens)
        elif directive == ".port_direction":
            self._port_direction(tokens)
        elif directive in {".pin", ".port"}:
            self._pin_directive(tokens)
        elif directive == ".dspf_pin":
            self._dspf_pin(tokens)
        elif directive in {".param", ".params"}:
            self._parameters_directive(tokens)
        elif directive == ".global":
            self._global_directive(tokens)
        elif directive == ".model":
            self._model_directive(tokens, logical)
        elif directive == ".end":
            if self.current is not None:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    f".END encountered before .ENDS for {self.current.name!r}",
                    line=tokens[0].line,
                    column=tokens[0].column,
                    scope=self.current.name,
                )
                self.current = None
            self.ended = True
        else:
            self._unsupported_directive(tokens, logical)

    def _parse_logical(self, logical: _LogicalLine) -> None:
        tokens = self._tokenize(logical)
        if not tokens or self._limit_reached:
            return
        if self.ended:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "CDL/SPICE content appears after .END",
                line=tokens[0].line,
                column=tokens[0].column,
                scope="*",
            )
            return
        first = tokens[0].value
        if first.startswith("."):
            self._parse_directive(tokens, logical)
            return
        if not first:
            return
        designator = first[0].casefold()
        if self.current is None and not self.top_instances and self.title is None:
            if designator not in {"m", "r", "c", "l", "x"}:
                self.title = logical.text
                return
        if not self._valid_name(tokens[0], "instance"):
            return
        if designator == "m":
            self._parse_mos(tokens, logical)
        elif designator in {"r", "c", "l"}:
            self._parse_passive(tokens, logical)
        elif designator == "x":
            self._parse_subckt_instance(tokens, logical)
        else:
            self._unsupported_instance(tokens, logical)

    def _finalize_port(self, record: _PortRecord, tainted: bool) -> PortObservation:
        status = FactState.TAINTED if tainted else record.status
        field_states = {
            "direction": (
                FactState.TAINTED
                if tainted and record.direction_state == FactState.KNOWN
                else record.direction_state
            ),
            "role": (
                FactState.TAINTED
                if tainted and record.role_state == FactState.KNOWN
                else record.role_state
            ),
            "shape": FactState.TAINTED if tainted else FactState.UNKNOWN,
        }
        return PortObservation(
            record.name,
            direction=record.direction,
            role=record.role,
            shape=BusShape.unknown(),
            provenance=record.provenance,
            attributes={
                "raw_name": record.raw_name,
                "metadata": list(record.metadata),
                "shape_reason": "CDL/SPICE subcircuit pins do not declare logical bus width",
            },
            status=status,
            field_states=field_states,
        )

    def _finalize_builder(
        self,
        builder: _SubcktBuilder,
    ) -> tuple[ComponentObservation, tuple[DesignObjectObservation, ...]]:
        tainted = builder.tainted
        component = ComponentObservation(
            builder.name,
            ComponentKind.CELL,
            tuple(self._finalize_port(port, tainted) for port in builder.ports),
            provenance=builder.provenance,
            attributes={
                "parameters": {
                    builder.parameter_spellings.get(key, key): value
                    for key, value in builder.parameters.items()
                },
                "directives": list(builder.directives),
                "ends_provenance": (
                    builder.ends_provenance.to_dict()
                    if builder.ends_provenance is not None
                    else None
                ),
            },
            status=FactState.TAINTED if tainted else builder.status,
        )
        objects: list[DesignObjectObservation] = [
            DesignObjectObservation(
                "component",
                builder.name,
                provenance=builder.provenance,
                status=component.status,
                attributes={"component_kind": ComponentKind.CELL.value},
            )
        ]
        for port in builder.ports:
            objects.append(
                DesignObjectObservation(
                    "pin",
                    port.name,
                    scope=builder.name,
                    provenance=port.provenance,
                    status=FactState.TAINTED if tainted else port.status,
                    attributes={
                        "direction": port.direction.value,
                        "role": port.role.value,
                        "direction_state": port.direction_state.value,
                        "role_state": port.role_state.value,
                        "shape_state": FactState.UNKNOWN.value,
                    },
                )
            )
        for instance in builder.instances:
            objects.append(
                DesignObjectObservation(
                    "instance",
                    instance.name,
                    scope=builder.name,
                    provenance=instance.provenance,
                    status=(
                        FactState.TAINTED
                        if tainted and instance.status == FactState.KNOWN
                        else instance.status
                    ),
                    attributes=instance.attributes,
                )
            )
        for key, net in builder.nets.items():
            is_global = key in self.global_nets
            objects.append(
                DesignObjectObservation(
                    "net",
                    net.name,
                    scope=None if is_global else builder.name,
                    provenance=net.provenance,
                    status=(
                        FactState.TAINTED
                        if tainted and net.status == FactState.KNOWN
                        else net.status
                    ),
                    attributes={
                        "component": builder.name,
                        "is_port": net.is_port,
                        "global": is_global,
                        "connections": list(net.connections),
                    },
                )
            )
        objects.extend(
            replace(item, status=FactState.TAINTED)
            if tainted and item.status == FactState.KNOWN
            else item
            for item in builder.extra_objects
        )
        return component, tuple(objects)

    def parse(self) -> _FileResult:
        logical_lines = self._logical_lines()
        for logical in logical_lines:
            if self._limit_reached:
                break
            self._logical_line_count += 1
            self._parse_logical(logical)
        if self.current is not None:
            builder = self.current
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"CDL/SPICE .SUBCKT {builder.name!r} has no matching .ENDS",
                line=builder.provenance.line,
                column=builder.provenance.column,
                scope=builder.name,
            )
            builder.mark_tainted()
            self.current = None
        components: list[ComponentObservation] = []
        objects: list[DesignObjectObservation] = []
        for builder in self.builders:
            component, builder_objects = self._finalize_builder(builder)
            components.append(component)
            objects.extend(builder_objects)
        for instance in self.top_instances:
            objects.append(
                DesignObjectObservation(
                    "instance",
                    instance.name,
                    provenance=instance.provenance,
                    status=instance.status,
                    attributes=instance.attributes,
                )
            )
        for key, net in self.top_nets.items():
            objects.append(
                DesignObjectObservation(
                    "net",
                    net.name,
                    provenance=net.provenance,
                    status=net.status,
                    attributes={
                        "global": key in self.global_nets,
                        "is_port": False,
                        "connections": list(net.connections),
                    },
                )
            )
        objects.extend(self.top_objects)
        objects.extend(self.models.values())
        for key, (name, location) in self.global_nets.items():
            objects.append(
                DesignObjectObservation(
                    "net",
                    name,
                    provenance=location,
                    status=(FactState.TAINTED if "*" in self.tainted_scopes else FactState.KNOWN),
                    attributes={"global": True, "declaration": ".GLOBAL", "key": key},
                )
            )
        return _FileResult(
            tuple(components),
            tuple(objects),
            tuple(self.diagnostics),
            frozenset(self.tainted_scopes),
            self.complete and not self._limit_reached,
            {
                "title": self.title,
                "logical_lines": self._logical_line_count,
                "tokens": self._token_count,
                "top_parameters": {
                    self.top_parameter_spellings.get(key, key): value
                    for key, value in self.top_parameters.items()
                },
                "ended": self.ended,
            },
        )


def _mark_file_tainted(result: _FileResult) -> _FileResult:
    return _FileResult(
        tuple(
            replace(
                component,
                ports=tuple(
                    replace(
                        port,
                        status=FactState.TAINTED,
                        field_states={
                            **port.field_states,
                            "direction": FactState.TAINTED,
                            "role": FactState.TAINTED,
                            "shape": FactState.TAINTED,
                        },
                    )
                    for port in component.ports
                ),
                status=FactState.TAINTED,
            )
            for component in result.components
        ),
        tuple(replace(item, status=FactState.TAINTED) for item in result.objects),
        result.diagnostics,
        frozenset({"*", *result.tainted_scopes}),
        False,
        result.attributes,
    )


def parse_cdl(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
) -> ViewObservation:
    """Parse structural CDL/SPICE netlists into canonical observations.

    Supported elements are MOSFET (``M``), resistor (``R``), capacitor
    (``C``), inductor (``L``), and subcircuit (``X``) instances.  Parameter
    values are retained as source text and are never evaluated.
    """

    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="cdl", name=view_name)
    components: list[ComponentObservation] = []
    objects: list[DesignObjectObservation] = []
    diagnostics: list[Diagnostic] = []
    tainted_scopes: set[str] = set()
    complete = True
    source_attributes: dict[str, Mapping[str, Any]] = {}
    encodings: dict[str, str] = {}
    seen_components: dict[str, list[int]] = {}
    duplicate_component_keys: set[str] = set()

    for path in source_paths:
        source = read_source(path, view)
        diagnostics.extend(source.diagnostics)
        encodings[str(path)] = source.encoding
        if not source.text:
            complete = False
            tainted_scopes.add("*")
            continue
        result = _FileParser(path, source.text, view).parse()
        if source.tainted:
            result = _mark_file_tainted(result)
        base_index = len(components)
        components.extend(result.components)
        objects.extend(result.objects)
        diagnostics.extend(result.diagnostics)
        tainted_scopes.update(result.tainted_scopes)
        complete = complete and result.complete and not source.tainted
        source_attributes[str(path)] = result.attributes
        for offset, component in enumerate(result.components):
            index = base_index + offset
            indices = seen_components.setdefault(_case_key(component.name), [])
            indices.append(index)
            if len(indices) > 1:
                duplicate_component_keys.add(_case_key(component.name))
                diagnostics.append(
                    parser_diagnostic(
                        "OC1101",
                        Severity.ERROR,
                        f"Duplicate .SUBCKT definition for {component.name!r} across inputs",
                        location=component.provenance,
                    )
                )
                complete = False
                for duplicate_index in indices:
                    duplicate = components[duplicate_index]
                    components[duplicate_index] = replace(
                        duplicate,
                        ports=tuple(
                            replace(port, status=FactState.TAINTED) for port in duplicate.ports
                        ),
                        status=FactState.TAINTED,
                    )
                    tainted_scopes.add(duplicate.name)

    if duplicate_component_keys:
        objects = [
            replace(item, status=FactState.TAINTED)
            if (
                (
                    item.kind == "component"
                    and _case_key(item.native_name) in duplicate_component_keys
                )
                or (item.scope is not None and _case_key(item.scope) in duplicate_component_keys)
            )
            else item
            for item in objects
        ]

    return ViewObservation(
        view,
        tuple(components),
        diagnostics=tuple(diagnostics),
        complete=complete,
        tainted_scopes=frozenset(tainted_scopes),
        objects=tuple(objects),
        attributes={
            "parser": "stdlib-cdl-spice",
            "source_files": [str(path) for path in source_paths],
            "encodings": encodings,
            "sources": source_attributes,
            "supported_instances": ["M", "R", "C", "L", "X"],
            "parameters_evaluated": False,
        },
    )


def parse_spice(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
) -> ViewObservation:
    """Alias for :func:`parse_cdl` using the same conservative subset."""

    return parse_cdl(paths, view_id=view_id, view_name=view_name)


class CdlParser:
    format_name = "cdl"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_cdl(paths, view_id=view_id, **options)


CDLParser = CdlParser
SpiceParser = CdlParser

__all__ = [
    "CDLParser",
    "CdlParser",
    "SpiceParser",
    "parse_cdl",
    "parse_spice",
]

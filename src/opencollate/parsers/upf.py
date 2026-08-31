"""Static, non-executing IEEE 1801/UPF importer.

UPF is Tcl-shaped, but evaluating an arbitrary UPF file would make parsing
environment-dependent and unsafe.  This module recognizes a useful structural
subset directly.  Tcl substitution, control flow, and unrecognized commands are
reported as unsupported facts and taint the affected scope instead of being run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
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
    choose_provenance,
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
class _Word:
    text: str
    raw: str
    line: int
    column: int
    dynamic: bool = False
    braced: bool = False


@dataclass(frozen=True, slots=True)
class _Command:
    words: tuple[_Word, ...]
    path: Path

    @property
    def name(self) -> str:
        return self.words[0].text.lower() if self.words else ""

    @property
    def provenance_word(self) -> _Word:
        return self.words[0]

    @property
    def raw(self) -> str:
        return " ".join(word.raw for word in self.words)


class _Scanner:
    """Small Tcl command/word scanner that performs no substitutions."""

    def __init__(self, text: str, path: Path, view: ViewId) -> None:
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.path = path
        self.view = view
        self.index = 0
        self.line = 1
        self.column = 1
        self.diagnostics: list[Diagnostic] = []
        self.complete = True

    def _peek(self, offset: int = 0) -> str:
        position = self.index + offset
        return self.text[position] if position < len(self.text) else ""

    def _advance(self) -> str:
        character = self._peek()
        if not character:
            return ""
        self.index += 1
        if character == "\n":
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return character

    def _location(self, line: int, column: int) -> Provenance:
        return Provenance(str(self.path), line, column, self.view)

    def _syntax_error(self, message: str, line: int, column: int) -> None:
        self.complete = False
        self.diagnostics.append(
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                f"UPF Tcl syntax error: {message}",
                location=self._location(line, column),
                help=(
                    "Repair the static Tcl syntax; OpenCollate does not evaluate Tcl recovery code."
                ),
            )
        )

    def _continuation(self) -> bool:
        if self._peek() != "\\" or self._peek(1) != "\n":
            return False
        self._advance()
        self._advance()
        while self._peek() in {" ", "\t"}:
            self._advance()
        return True

    def _escape(self) -> str:
        self._advance()  # backslash
        escaped = self._advance()
        return {
            "a": "\a",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
        }.get(escaped, escaped or "\\")

    def _bracket(self, line: int, column: int) -> str:
        start = self.index
        depth = 0
        brace_depth = 0
        quoted = False
        while self._peek():
            character = self._peek()
            if character == "\\":
                if self._continuation():
                    continue
                self._advance()
                if self._peek():
                    self._advance()
                continue
            if brace_depth:
                if character == "{":
                    brace_depth += 1
                elif character == "}":
                    brace_depth -= 1
                self._advance()
                continue
            if character == "{" and not quoted:
                brace_depth = 1
                self._advance()
                continue
            if character == '"':
                quoted = not quoted
                self._advance()
                continue
            if character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                self._advance()
                if depth == 0:
                    return self.text[start : self.index]
                continue
            self._advance()
        self._syntax_error("unterminated command substitution", line, column)
        return self.text[start : self.index]

    def _braced_word(self, line: int, column: int) -> _Word:
        start = self.index
        self._advance()
        depth = 1
        value: list[str] = []
        while self._peek():
            character = self._peek()
            if character == "\\":
                if self._continuation():
                    value.append(" ")
                    continue
                value.append(self._advance())
                if self._peek():
                    value.append(self._advance())
                continue
            if character == "{":
                depth += 1
                value.append(self._advance())
                continue
            if character == "}":
                depth -= 1
                self._advance()
                if depth == 0:
                    return _Word(
                        "".join(value),
                        self.text[start : self.index],
                        line,
                        column,
                        braced=True,
                    )
                value.append("}")
                continue
            value.append(self._advance())
        self._syntax_error("unterminated braced word", line, column)
        return _Word(
            "".join(value),
            self.text[start : self.index],
            line,
            column,
            dynamic=True,
            braced=True,
        )

    def _quoted_word(self, line: int, column: int) -> _Word:
        start = self.index
        self._advance()
        value: list[str] = []
        dynamic = False
        while self._peek():
            character = self._peek()
            if character == '"':
                self._advance()
                return _Word(
                    "".join(value),
                    self.text[start : self.index],
                    line,
                    column,
                    dynamic=dynamic,
                )
            if character == "\\":
                if self._continuation():
                    value.append(" ")
                else:
                    value.append(self._escape())
                continue
            if character == "[":
                dynamic = True
                value.append(self._bracket(self.line, self.column))
                continue
            if character == "$":
                dynamic = True
            value.append(self._advance())
        self._syntax_error("unterminated quoted word", line, column)
        return _Word(
            "".join(value),
            self.text[start : self.index],
            line,
            column,
            dynamic=True,
        )

    def _bare_word(self, line: int, column: int) -> _Word:
        start = self.index
        value: list[str] = []
        dynamic = False
        while self._peek() and self._peek() not in {" ", "\t", "\f", "\v", "\n", ";"}:
            character = self._peek()
            if character == "\\":
                if self._continuation():
                    break
                value.append(self._escape())
                continue
            if character == "[":
                dynamic = True
                value.append(self._bracket(self.line, self.column))
                continue
            if character == "$":
                dynamic = True
            value.append(self._advance())
        return _Word(
            "".join(value),
            self.text[start : self.index],
            line,
            column,
            dynamic=dynamic,
        )

    def scan(self) -> tuple[tuple[_Command, ...], tuple[Diagnostic, ...], bool]:
        commands: list[_Command] = []
        words: list[_Word] = []
        while self._peek():
            if self._continuation():
                continue
            character = self._peek()
            if character in {" ", "\t", "\f", "\v"}:
                self._advance()
                continue
            if character in {"\n", ";"}:
                if words:
                    commands.append(_Command(tuple(words), self.path))
                    words = []
                self._advance()
                continue
            if character == "#" and not words:
                while self._peek() and self._peek() != "\n":
                    self._advance()
                continue
            line, column = self.line, self.column
            if character == "{":
                word = self._braced_word(line, column)
                if self._peek() not in {"", " ", "\t", "\f", "\v", "\n", ";"}:
                    self._syntax_error("extra characters after a braced word", line, column)
                    suffix = self._bare_word(self.line, self.column)
                    word = _Word(
                        word.text + suffix.text,
                        word.raw + suffix.raw,
                        line,
                        column,
                        dynamic=True,
                        braced=True,
                    )
            elif character == '"':
                word = self._quoted_word(line, column)
                if self._peek() not in {"", " ", "\t", "\f", "\v", "\n", ";"}:
                    suffix = self._bare_word(self.line, self.column)
                    word = _Word(
                        word.text + suffix.text,
                        word.raw + suffix.raw,
                        line,
                        column,
                        dynamic=word.dynamic or suffix.dynamic,
                    )
            else:
                word = self._bare_word(line, column)
            if word.raw:
                words.append(word)
        if words:
            commands.append(_Command(tuple(words), self.path))
        return tuple(commands), tuple(self.diagnostics), self.complete


def _split_list(value: str) -> tuple[str, ...] | None:
    """Split a static Tcl list without invoking a Tcl interpreter."""

    items: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        while index < length and value[index].isspace():
            index += 1
        if index >= length:
            break
        result: list[str] = []
        if value[index] == "{":
            index += 1
            depth = 1
            while index < length and depth:
                character = value[index]
                if character == "\\" and index + 1 < length:
                    result.extend((character, value[index + 1]))
                    index += 2
                    continue
                if character == "{":
                    depth += 1
                    if depth > 1:
                        result.append(character)
                elif character == "}":
                    depth -= 1
                    if depth:
                        result.append(character)
                else:
                    result.append(character)
                index += 1
            if depth:
                return None
            if index < length and not value[index].isspace():
                return None
        elif value[index] == '"':
            index += 1
            closed = False
            while index < length:
                character = value[index]
                if character == '"':
                    index += 1
                    closed = True
                    break
                if character == "\\" and index + 1 < length:
                    index += 1
                    result.append(value[index])
                    index += 1
                    continue
                result.append(character)
                index += 1
            if not closed or (index < length and not value[index].isspace()):
                return None
        else:
            while index < length and not value[index].isspace():
                if value[index] == "\\" and index + 1 < length:
                    index += 1
                result.append(value[index])
                index += 1
        items.append("".join(result))
    return tuple(items)


@dataclass(slots=True)
class _Arguments:
    positionals: list[_Word] = field(default_factory=list)
    options: dict[str, list[_Word | None]] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def one(self, name: str) -> _Word | None:
        values = self.options.get(name, ())
        for value in reversed(values):
            if value is not None:
                return value
        return None

    def many(self, name: str) -> tuple[_Word, ...]:
        return tuple(value for value in self.options.get(name, ()) if value is not None)

    def flag(self, name: str) -> bool:
        return name in self.options


def _parse_arguments(
    command: _Command,
    *,
    value_options: Iterable[str],
    flag_options: Iterable[str] = (),
) -> _Arguments:
    valued = set(value_options)
    flags = set(flag_options)
    known = valued | flags
    result = _Arguments()
    words = command.words[1:]
    index = 0
    while index < len(words):
        word = words[index]
        if not word.dynamic and word.text.startswith("-") and len(word.text) > 1:
            name = word.text.lstrip("-").lower().replace("-", "_")
            if name in flags:
                result.options.setdefault(name, []).append(None)
                index += 1
                continue
            if name in valued:
                if index + 1 >= len(words):
                    result.missing.append(name)
                    index += 1
                    continue
                following = words[index + 1]
                following_name = following.text.lstrip("-").lower().replace("-", "_")
                if (
                    not following.dynamic
                    and following.text.startswith("-")
                    and following_name in known
                ):
                    result.missing.append(name)
                    index += 1
                    continue
                result.options.setdefault(name, []).append(following)
                index += 2
                continue
            result.unknown.append(word.text)
            index += 1
            if index < len(words) and not words[index].text.startswith("-"):
                index += 1
            continue
        result.positionals.append(word)
        index += 1
    return result


def _provenance(command: _Command, view: ViewId, word: _Word | None = None) -> Provenance:
    selected = word or command.provenance_word
    return Provenance(
        str(command.path),
        selected.line,
        selected.column,
        view,
        raw_name=selected.raw,
    )


def _word_list(word: _Word | None) -> tuple[str, ...]:
    if word is None or word.dynamic:
        return ()
    parsed = _split_list(word.text)
    if parsed is None:
        return ()
    return parsed


def _word_text(word: _Word | None) -> str | None:
    if word is None or word.dynamic:
        return None
    return word.text


class _Collector:
    def __init__(self, view: ViewId, component_name: str | None) -> None:
        self.view = view
        self.component_name = component_name.strip() if component_name else None
        self.current_scope: str | None = None
        self.complete = True
        self.tainted_scopes: set[str] = set()
        self.tainted_sources: set[str] = set()
        self.diagnostics: list[Diagnostic] = []
        self.objects: list[DesignObjectObservation] = []
        self.design_tops: list[tuple[str, Provenance]] = []
        self.supply_ports: list[dict[str, Any]] = []
        self.records: dict[str, list[dict[str, Any]]] = {
            "power_domains": [],
            "supply_ports": [],
            "supply_nets": [],
            "supply_sets": [],
            "connections": [],
            "domain_supplies": [],
            "isolation": [],
            "retention": [],
            "level_shifters": [],
            "power_switches": [],
            "port_states": [],
            "power_states": [],
            "unsupported_facts": [],
        }
        self.upf_versions: list[str] = []

    def _state(self, command: _Command) -> FactState:
        return FactState.TAINTED if str(command.path) in self.tainted_sources else FactState.KNOWN

    def _scope_for(self, value: str, explicit_scope: str | None = None) -> tuple[str, str | None]:
        scope = explicit_scope if explicit_scope is not None else self.current_scope
        if value.startswith("/"):
            return value.lstrip("/"), None
        if value == "." and scope:
            return scope, None
        return value, scope

    def _object(
        self,
        kind: str,
        name: str,
        relation: str,
        command: _Command,
        *,
        word: _Word | None = None,
        scope: str | None = None,
        state: FactState = FactState.KNOWN,
        **attributes: Any,
    ) -> None:
        if not name:
            return
        native_name, resolved_scope = self._scope_for(name, scope)
        cleaned = {key: value for key, value in attributes.items() if value is not None}
        cleaned.setdefault("command", command.name)
        self.objects.append(
            DesignObjectObservation(
                kind=kind,
                native_name=native_name,
                relation=relation,
                scope=resolved_scope,
                provenance=_provenance(command, self.view, word),
                status=state,
                attributes=cleaned,
            )
        )

    def _reference_many(
        self,
        kind: str,
        names: Iterable[str],
        command: _Command,
        *,
        option: str,
        state: FactState,
        scope: str | None = None,
        **attributes: Any,
    ) -> None:
        for name in names:
            self._object(
                kind,
                name,
                "reference",
                command,
                scope=scope,
                state=state,
                option=option,
                **attributes,
            )

    def _reference_ports(
        self,
        names: Iterable[str],
        command: _Command,
        *,
        option: str,
        state: FactState,
        **attributes: Any,
    ) -> None:
        for name in names:
            kind = "pin" if "/" in name.strip("/") else "port"
            self._object(
                kind,
                name,
                "reference",
                command,
                state=state,
                option=option,
                **attributes,
            )

    def _unsupported(self, command: _Command, reason: str, *, scope: str = "*") -> None:
        self.complete = False
        self.tainted_scopes.add(scope)
        location = _provenance(command, self.view)
        self.diagnostics.append(
            parser_diagnostic(
                "OC1102",
                Severity.WARNING,
                f"UPF command {command.name or '<dynamic>'} is unsupported: {reason}",
                location=location,
                help=(
                    "Rewrite this command as static UPF, or treat the affected scope as "
                    "unverified; OpenCollate never evaluates Tcl."
                ),
            )
        )
        self.records["unsupported_facts"].append(
            {
                "command": command.name or None,
                "raw": command.raw,
                "reason": reason,
                "state": FactState.UNSUPPORTED.value,
                "provenance": location.to_dict(),
            }
        )

    def _malformed(self, command: _Command, reason: str, *, scope: str = "*") -> None:
        self.complete = False
        self.tainted_scopes.add(scope)
        self.diagnostics.append(
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                f"Malformed UPF {command.name}: {reason}",
                location=_provenance(command, self.view),
                help="Supply the required static command name and option values.",
            )
        )

    def _prepare(
        self,
        command: _Command,
        *,
        value_options: Iterable[str],
        flag_options: Iterable[str] = (),
    ) -> tuple[_Arguments, FactState]:
        arguments = _parse_arguments(
            command,
            value_options=value_options,
            flag_options=flag_options,
        )
        state = self._state(command)
        if any(word.dynamic for word in command.words):
            target = next(
                (word.text for word in arguments.positionals if not word.dynamic and word.text),
                "*",
            )
            self._unsupported(
                command, "contains Tcl variable or command substitution", scope=target
            )
            state = FactState.TAINTED
        if arguments.unknown:
            self._unsupported(
                command,
                "unrecognized option(s): " + ", ".join(arguments.unknown),
                scope=(arguments.positionals[0].text if arguments.positionals else "*"),
            )
            state = FactState.TAINTED
        if arguments.missing:
            self._malformed(
                command,
                "missing value for option(s): "
                + ", ".join(f"-{item}" for item in arguments.missing),
                scope=(arguments.positionals[0].text if arguments.positionals else "*"),
            )
            state = FactState.TAINTED
        return arguments, state

    def _name(
        self,
        command: _Command,
        arguments: _Arguments,
    ) -> tuple[str | None, _Word | None]:
        if not arguments.positionals:
            self._malformed(command, "missing object name")
            return None, None
        word = arguments.positionals[0]
        if word.dynamic or not word.text:
            return None, word
        return word.text, word

    @staticmethod
    def _record(
        command: _Command,
        view: ViewId,
        *,
        state: FactState,
        **values: Any,
    ) -> dict[str, Any]:
        return {
            **values,
            "command": command.name,
            "scope": values.get("scope"),
            "state": state.value,
            "provenance": _provenance(command, view).to_dict(),
        }

    def handle(self, command: _Command) -> None:
        if not command.words:
            return
        if command.words[0].dynamic:
            self._unsupported(command, "the command name requires Tcl substitution")
            return
        handlers = {
            "upf_version": self._handle_upf_version,
            "set_design_top": self._handle_design_top,
            "set_scope": self._handle_scope,
            "create_power_domain": self._handle_power_domain,
            "create_supply_port": self._handle_supply_port,
            "create_supply_net": self._handle_supply_net,
            "create_supply_set": self._handle_supply_set,
            "connect_supply_net": self._handle_connect_supply_net,
            "set_domain_supply_net": self._handle_domain_supply,
            "set_isolation": self._handle_isolation,
            "set_isolation_control": self._handle_isolation,
            "set_retention": self._handle_retention,
            "set_retention_control": self._handle_retention,
            "set_level_shifter": self._handle_level_shifter,
            "create_power_switch": self._handle_power_switch,
            "add_port_state": self._handle_port_state,
            "add_power_state": self._handle_power_state,
        }
        handler = handlers.get(command.name)
        if handler is None:
            self._unsupported(command, "command is outside the static importer subset")
            return
        handler(command)

    def _handle_upf_version(self, command: _Command) -> None:
        arguments, _ = self._prepare(command, value_options=())
        version, _ = self._name(command, arguments)
        if version is not None:
            self.upf_versions.append(version)

    def _handle_design_top(self, command: _Command) -> None:
        arguments, state = self._prepare(command, value_options=())
        name, word = self._name(command, arguments)
        if name is None:
            return
        location = _provenance(command, self.view, word)
        self.design_tops.append((name, location))
        self._object(
            "instance",
            name,
            "reference",
            command,
            word=word,
            state=state,
            target_kind="design_top",
        )

    def _handle_scope(self, command: _Command) -> None:
        arguments, state = self._prepare(command, value_options=())
        name, word = self._name(command, arguments)
        if name is None:
            return
        self.current_scope = None if name in {"/", "."} else name.strip("/")
        if self.current_scope:
            self._object(
                "instance", self.current_scope, "reference", command, word=word, state=state
            )

    def _handle_power_domain(self, command: _Command) -> None:
        arguments, state = self._prepare(
            command,
            value_options={"elements", "scope", "atomic"},
            flag_options={"include_scope", "update"},
        )
        name, word = self._name(command, arguments)
        if name is None:
            return
        elements = _word_list(arguments.one("elements"))
        scope = _word_text(arguments.one("scope")) or self.current_scope
        record = self._record(
            command,
            self.view,
            state=state,
            name=name,
            elements=list(elements),
            include_scope=arguments.flag("include_scope"),
            update=arguments.flag("update"),
            atomic=_word_text(arguments.one("atomic")),
            scope=scope,
        )
        self.records["power_domains"].append(record)
        self._object(
            "power_domain",
            name,
            "definition",
            command,
            word=word,
            scope=scope,
            state=state,
            elements=list(elements),
            include_scope=arguments.flag("include_scope"),
            update=arguments.flag("update"),
        )
        self._reference_many(
            "instance",
            elements,
            command,
            option="-elements",
            state=state,
            scope=scope,
            domain=name,
        )

    def _handle_supply_port(self, command: _Command) -> None:
        arguments, state = self._prepare(
            command,
            value_options={"domain", "direction"},
            flag_options={"reuse"},
        )
        name, word = self._name(command, arguments)
        if name is None:
            return
        domain = _word_text(arguments.one("domain"))
        direction_text = _word_text(arguments.one("direction"))
        direction = Direction.parse(direction_text)
        if direction_text is not None and direction == Direction.UNKNOWN:
            self._unsupported(
                command, f"unknown supply-port direction {direction_text!r}", scope=name
            )
            state = FactState.TAINTED
        record = self._record(
            command,
            self.view,
            state=state,
            name=name,
            domain=domain,
            direction=direction.value,
            reuse=arguments.flag("reuse"),
            scope=self.current_scope,
        )
        self.records["supply_ports"].append(record)
        self.supply_ports.append(
            {**record, "provenance_object": _provenance(command, self.view, word)}
        )
        self._object(
            "supply_port",
            name,
            "definition",
            command,
            word=word,
            state=state,
            domain=domain,
            direction=direction.value,
        )
        if domain:
            self._reference_many("power_domain", (domain,), command, option="-domain", state=state)

    def _handle_supply_net(self, command: _Command) -> None:
        arguments, state = self._prepare(
            command,
            value_options={"domain", "resolve"},
            flag_options={"reuse"},
        )
        name, word = self._name(command, arguments)
        if name is None:
            return
        domain = _word_text(arguments.one("domain"))
        record = self._record(
            command,
            self.view,
            state=state,
            name=name,
            domain=domain,
            resolve=_word_text(arguments.one("resolve")),
            reuse=arguments.flag("reuse"),
            scope=self.current_scope,
        )
        self.records["supply_nets"].append(record)
        self._object(
            "supply_net",
            name,
            "definition",
            command,
            word=word,
            state=state,
            domain=domain,
        )
        if domain:
            self._reference_many("power_domain", (domain,), command, option="-domain", state=state)

    def _handle_supply_set(self, command: _Command) -> None:
        arguments, state = self._prepare(
            command,
            value_options={"function", "reference_gnd", "reference_power"},
            flag_options={"update"},
        )
        name, word = self._name(command, arguments)
        if name is None:
            return
        functions = [_word_list(item) for item in arguments.many("function")]
        if any(not item for item in functions):
            self._malformed(command, "-function must be a static Tcl list", scope=name)
            state = FactState.TAINTED
        record = self._record(
            command,
            self.view,
            state=state,
            name=name,
            functions=[list(item) for item in functions if item],
            reference_ground=_word_text(arguments.one("reference_gnd")),
            reference_power=_word_text(arguments.one("reference_power")),
            update=arguments.flag("update"),
            scope=self.current_scope,
        )
        self.records["supply_sets"].append(record)
        self._object(
            "supply_set",
            name,
            "definition",
            command,
            word=word,
            state=state,
            functions=record["functions"],
            update=arguments.flag("update"),
        )
        for function in functions:
            if len(function) >= 2:
                self._reference_many(
                    "supply_net",
                    (function[1],),
                    command,
                    option="-function",
                    state=state,
                    supply_function=function[0],
                )

    def _handle_connect_supply_net(self, command: _Command) -> None:
        arguments, state = self._prepare(
            command,
            value_options={"ports", "pins", "pg_type", "vct"},
            flag_options={"rail_connection"},
        )
        net, word = self._name(command, arguments)
        if net is None:
            return
        ports = _word_list(arguments.one("ports"))
        pins = _word_list(arguments.one("pins"))
        if not ports and not pins:
            self._malformed(command, "requires a static -ports or -pins list", scope=net)
            state = FactState.TAINTED
        record = self._record(
            command,
            self.view,
            state=state,
            net=net,
            ports=list(ports),
            pins=list(pins),
            pg_type=_word_text(arguments.one("pg_type")),
            rail_connection=arguments.flag("rail_connection"),
            scope=self.current_scope,
        )
        self.records["connections"].append(record)
        self._object("supply_net", net, "reference", command, word=word, state=state)
        self._reference_ports(ports, command, option="-ports", state=state)
        self._reference_many("pin", pins, command, option="-pins", state=state)

    def _handle_domain_supply(self, command: _Command) -> None:
        option_names = {
            "primary_power_net",
            "primary_ground_net",
            "primary_power_port",
            "primary_ground_port",
        }
        arguments, state = self._prepare(command, value_options=option_names)
        domain, word = self._name(command, arguments)
        if domain is None:
            return
        values = {name: _word_text(arguments.one(name)) for name in option_names}
        record = self._record(
            command,
            self.view,
            state=state,
            domain=domain,
            scope=self.current_scope,
            **values,
        )
        self.records["domain_supplies"].append(record)
        self._object("power_domain", domain, "reference", command, word=word, state=state)
        for option in ("primary_power_net", "primary_ground_net"):
            if values[option]:
                self._reference_many(
                    "supply_net", (str(values[option]),), command, option=f"-{option}", state=state
                )
        for option in ("primary_power_port", "primary_ground_port"):
            if values[option]:
                self._reference_many(
                    "supply_port",
                    (str(values[option]),),
                    command,
                    option=f"-{option}",
                    state=state,
                )

    def _handle_strategy(
        self,
        command: _Command,
        *,
        bucket: str,
        kind: str,
        value_options: set[str],
        flag_options: set[str] | None = None,
        path_options: set[str] | None = None,
        supply_options: set[str] | None = None,
    ) -> None:
        arguments, state = self._prepare(
            command,
            value_options=value_options,
            flag_options=flag_options or set(),
        )
        name, word = self._name(command, arguments)
        if name is None:
            return
        instance_list_options = {"elements", "exclude_elements"}
        list_options = instance_list_options | (path_options or set())
        serialized: dict[str, Any] = {}
        for option in value_options:
            value = arguments.one(option)
            serialized[option] = (
                list(_word_list(value)) if option in list_options else _word_text(value)
            )
        for option in flag_options or set():
            serialized[option] = arguments.flag(option)
        domain = serialized.get("domain")
        record = self._record(
            command,
            self.view,
            state=state,
            name=name,
            scope=self.current_scope,
            **serialized,
        )
        self.records[bucket].append(record)
        self._object(kind, name, "definition", command, word=word, state=state, domain=domain)
        if domain:
            self._reference_many(
                "power_domain", (str(domain),), command, option="-domain", state=state
            )
        for option in instance_list_options:
            self._reference_many(
                "instance",
                serialized.get(option) or (),
                command,
                option=f"-{option}",
                state=state,
                strategy=name,
                domain=domain,
            )
        for option in path_options or set():
            raw = _word_list(arguments.one(option))
            paths = raw[:1] if raw else ()
            self._reference_ports(
                paths,
                command,
                option=f"-{option}",
                state=state,
                strategy=name,
                domain=domain,
            )
        for option in supply_options or set():
            supply = serialized.get(option)
            if supply:
                kind_name = "supply_set" if option.endswith("supply_set") else "supply_net"
                self._reference_many(
                    kind_name,
                    (str(supply),),
                    command,
                    option=f"-{option}",
                    state=state,
                    strategy=name,
                )

    def _handle_isolation(self, command: _Command) -> None:
        options = {
            "domain",
            "elements",
            "exclude_elements",
            "applies_to",
            "clamp_value",
            "isolation_power_net",
            "isolation_ground_net",
            "isolation_signal",
            "isolation_sense",
            "location",
            "source",
            "sink",
            "diff_supply_only",
        }
        self._handle_strategy(
            command,
            bucket="isolation",
            kind="isolation_control" if command.name.endswith("_control") else "isolation_strategy",
            value_options=options,
            flag_options={"no_isolation", "force_isolation", "update"},
            path_options={"isolation_signal"},
            supply_options={"isolation_power_net", "isolation_ground_net"},
        )

    def _handle_retention(self, command: _Command) -> None:
        options = {
            "domain",
            "elements",
            "exclude_elements",
            "retention_power_net",
            "retention_ground_net",
            "save_signal",
            "restore_signal",
            "save_condition",
            "restore_condition",
            "retention_condition",
            "source",
            "sink",
        }
        self._handle_strategy(
            command,
            bucket="retention",
            kind="retention_control" if command.name.endswith("_control") else "retention_strategy",
            value_options=options,
            flag_options={"update"},
            path_options={"save_signal", "restore_signal"},
            supply_options={"retention_power_net", "retention_ground_net"},
        )

    def _handle_level_shifter(self, command: _Command) -> None:
        options = {
            "domain",
            "elements",
            "exclude_elements",
            "applies_to",
            "rule",
            "threshold",
            "location",
            "input_supply_set",
            "output_supply_set",
            "source",
            "sink",
            "name_prefix",
            "name_suffix",
        }
        self._handle_strategy(
            command,
            bucket="level_shifters",
            kind="level_shifter_strategy",
            value_options=options,
            flag_options={"no_shift", "force_shift", "update"},
            supply_options={"input_supply_set", "output_supply_set"},
        )

    def _handle_power_switch(self, command: _Command) -> None:
        repeated = {
            "input_supply_port",
            "output_supply_port",
            "control_port",
            "on_state",
            "off_state",
            "error_state",
            "acknowledge_port",
        }
        arguments, state = self._prepare(
            command,
            value_options=repeated | {"domain"},
            flag_options={"update"},
        )
        name, word = self._name(command, arguments)
        if name is None:
            return
        domain = _word_text(arguments.one("domain"))
        values = {
            option: [list(_word_list(item)) for item in arguments.many(option)]
            for option in repeated
        }
        record = self._record(
            command,
            self.view,
            state=state,
            name=name,
            domain=domain,
            update=arguments.flag("update"),
            scope=self.current_scope,
            **values,
        )
        self.records["power_switches"].append(record)
        self._object(
            "power_switch",
            name,
            "definition",
            command,
            word=word,
            state=state,
            domain=domain,
        )
        if domain:
            self._reference_many("power_domain", (domain,), command, option="-domain", state=state)
        for option in ("input_supply_port", "output_supply_port"):
            for pair in values[option]:
                if pair:
                    self._object(
                        "supply_port",
                        pair[0],
                        "definition",
                        command,
                        scope=name,
                        state=state,
                        switch=name,
                        option=f"-{option}",
                    )
                if len(pair) >= 2:
                    self._reference_many(
                        "supply_net",
                        (pair[1],),
                        command,
                        option=f"-{option}",
                        state=state,
                        switch=name,
                    )
        for pair in values["control_port"]:
            if pair:
                self._object(
                    "port",
                    pair[0],
                    "definition",
                    command,
                    scope=name,
                    state=state,
                    switch=name,
                    option="-control_port",
                )
            if len(pair) >= 2:
                self._reference_ports(
                    (pair[1],),
                    command,
                    option="-control_port",
                    state=state,
                    switch=name,
                )

    def _handle_port_state(self, command: _Command) -> None:
        arguments, state = self._prepare(command, value_options={"state"})
        target, word = self._name(command, arguments)
        if target is None:
            return
        states = [list(_word_list(item)) for item in arguments.many("state")]
        if not states:
            self._malformed(command, "requires at least one static -state list", scope=target)
            state = FactState.TAINTED
        record = self._record(
            command,
            self.view,
            state=state,
            target=target,
            states=states,
            scope=self.current_scope,
        )
        self.records["port_states"].append(record)
        self._object("supply_port", target, "reference", command, word=word, state=state)
        for item in states:
            if item:
                self._object(
                    "port_state",
                    item[0],
                    "definition",
                    command,
                    scope=target,
                    state=state,
                    target=target,
                    state_expression=item[1:],
                )

    def _handle_power_state(self, command: _Command) -> None:
        arguments, state = self._prepare(command, value_options={"state"})
        target, word = self._name(command, arguments)
        if target is None:
            return
        states = [list(_word_list(item)) for item in arguments.many("state")]
        if not states:
            self._malformed(command, "requires at least one static -state list", scope=target)
            state = FactState.TAINTED
        record = self._record(
            command,
            self.view,
            state=state,
            target=target,
            states=states,
            scope=self.current_scope,
        )
        self.records["power_states"].append(record)
        target_kind = next(
            (
                item.kind
                for item in reversed(self.objects)
                if item.relation == "definition"
                and item.native_name == target
                and item.kind in {"supply_set", "power_domain"}
            ),
            "supply_set",
        )
        self._object(target_kind, target, "reference", command, word=word, state=state)
        for item in states:
            if item:
                self._object(
                    "power_state",
                    item[0],
                    "definition",
                    command,
                    scope=target,
                    state=state,
                    target=target,
                    state_expression=item[1:],
                )

    def _components(self) -> tuple[ComponentObservation, ...]:
        top_name = self.component_name
        unique_tops = {name for name, _ in self.design_tops}
        if top_name is None and len(unique_tops) == 1:
            top_name = next(iter(unique_tops))
        elif top_name is None and len(unique_tops) > 1:
            self.complete = False
            self.tainted_scopes.add("*")
            self.diagnostics.append(
                parser_diagnostic(
                    "OC1104",
                    Severity.WARNING,
                    (
                        "UPF files name multiple design tops; supply ports were not mapped "
                        "to a component"
                    ),
                    location=self.design_tops[0][1],
                )
            )
        if top_name is None:
            return ()

        power_ports = {
            str(record["primary_power_port"])
            for record in self.records["domain_supplies"]
            if record.get("primary_power_port")
        }
        ground_ports = {
            str(record["primary_ground_port"])
            for record in self.records["domain_supplies"]
            if record.get("primary_ground_port")
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in self.supply_ports:
            grouped.setdefault(str(record["name"]), []).append(record)
        ports: list[PortObservation] = []
        for name, records in grouped.items():
            states = {FactState(str(record["state"])) for record in records}
            status = FactState.KNOWN if states == {FactState.KNOWN} else FactState.TAINTED
            directions = {Direction.parse(str(record["direction"])) for record in records}
            direction = next(iter(directions)) if len(directions) == 1 else Direction.UNKNOWN
            direction_state = (
                FactState.KNOWN
                if direction != Direction.UNKNOWN and len(directions) == 1
                else FactState.UNKNOWN
            )
            if len(directions) > 1:
                status = FactState.TAINTED
                direction_state = FactState.TAINTED
                self.diagnostics.append(
                    parser_diagnostic(
                        "OC1104",
                        Severity.WARNING,
                        f"UPF declarations of supply port {name} disagree on direction",
                        location=records[0]["provenance_object"],
                    )
                )
            if name in power_ports and name in ground_ports:
                role, role_state = PortRole.UNKNOWN, FactState.TAINTED
                status = FactState.TAINTED
            elif name in power_ports:
                role, role_state = PortRole.POWER, FactState.KNOWN
            elif name in ground_ports:
                role, role_state = PortRole.GROUND, FactState.KNOWN
            else:
                role, role_state = infer_role_from_name(name)
            ports.append(
                PortObservation(
                    native_name=name,
                    direction=direction,
                    role=role,
                    shape=BusShape.scalar(),
                    provenance=records[0]["provenance_object"],
                    status=status,
                    field_states={"direction": direction_state, "role": role_state},
                    attributes={
                        "upf_supply_port": True,
                        "domains": sorted(
                            {str(record["domain"]) for record in records if record.get("domain")}
                        ),
                    },
                )
            )
        component_status = (
            FactState.TAINTED
            if "*" in self.tainted_scopes or any(port.status != FactState.KNOWN for port in ports)
            else FactState.KNOWN
        )
        top_locations = [location for name, location in self.design_tops if name == top_name]
        provenance = choose_provenance(top_locations + [port.provenance for port in ports])
        return (
            ComponentObservation(
                native_name=top_name,
                kind=ComponentKind.MODULE,
                ports=tuple(sorted(ports, key=lambda item: item.native_name)),
                provenance=provenance,
                status=component_status,
                attributes={
                    "upf_design_top": True,
                    "power_domains": sorted(
                        str(record["name"]) for record in self.records["power_domains"]
                    ),
                },
            ),
        )

    def finish(self, paths: Sequence[Path]) -> ViewObservation:
        attributes: dict[str, Any] = {
            "parser": "native-upf-static",
            "source_files": [str(path) for path in paths],
            "upf_versions": list(dict.fromkeys(self.upf_versions)),
        }
        attributes.update(self.records)
        return ViewObservation(
            view=self.view,
            components=self._components(),
            diagnostics=tuple(self.diagnostics),
            complete=self.complete,
            tainted_scopes=frozenset(self.tainted_scopes),
            objects=tuple(self.objects),
            attributes=attributes,
        )


def parse_upf(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    component_name: str | None = None,
) -> ViewObservation:
    """Parse static UPF structure without evaluating Tcl code.

    ``component_name`` maps statically created supply ports onto an existing
    top-level component.  With no override, a unique ``set_design_top`` command
    provides that mapping; otherwise the UPF objects remain available through
    :attr:`ViewObservation.objects` without guessing a module name.
    """

    if component_name is not None and not component_name.strip():
        raise ValueError("component_name must be a nonempty string")
    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="upf", name=view_name)
    collector = _Collector(view, component_name)
    for path in source_paths:
        source = read_source(path, view)
        collector.diagnostics.extend(source.diagnostics)
        if source.tainted:
            collector.complete = False
            collector.tainted_scopes.add("*")
            collector.tainted_sources.add(str(path))
        if not source.text:
            continue
        scanner = _Scanner(source.text, path, view)
        commands, diagnostics, complete = scanner.scan()
        collector.diagnostics.extend(diagnostics)
        if not complete:
            collector.complete = False
            collector.tainted_scopes.add("*")
        for command in commands:
            collector.handle(command)
    return collector.finish(source_paths)


class UpfParser:
    format_name = "upf"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_upf(paths, view_id=view_id, **options)


__all__ = ["UpfParser", "parse_upf"]

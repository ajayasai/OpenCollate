"""Safe, dependency-free importer for a useful static subset of SDC/Tcl.

The parser never starts a Tcl interpreter and never evaluates commands.  It
tokenizes Tcl's grouping constructs itself, performs only literal/variable
substitution that can be proven static, and recognizes the SDC commands that
produce design-object references or clocks.  Anything outside that subset is
reported and taints the view instead of being guessed or executed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencollate.diagnostics import Diagnostic, Severity
from opencollate.model import (
    ClockObservation,
    DesignObjectObservation,
    FactState,
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

_MAX_SCRIPT_CHARACTERS = 4 * 1024 * 1024
_MAX_COMMANDS = 100_000
_MAX_WORDS = 250_000
_MAX_NESTING = 128

_VARIABLE_NAME = re.compile(r"[A-Za-z0-9_:]+")
_INTEGER = re.compile(r"[+-]?\d+")
_NUMBER = re.compile(r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?")


@dataclass(frozen=True, slots=True)
class TimingConstraintObservation:
    """A supported SDC timing exception or I/O-delay declaration."""

    command: str
    value: float | int | None = None
    objects: tuple[str, ...] = ()
    clocks: tuple[str, ...] = ()
    from_objects: tuple[str, ...] = ()
    to_objects: tuple[str, ...] = ()
    through_objects: tuple[str, ...] = ()
    provenance: Provenance | None = None
    status: FactState = FactState.KNOWN
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError("timing constraint command must not be empty")
        object.__setattr__(self, "command", self.command.strip().lower())
        object.__setattr__(self, "objects", tuple(self.objects))
        object.__setattr__(self, "clocks", tuple(self.clocks))
        object.__setattr__(self, "from_objects", tuple(self.from_objects))
        object.__setattr__(self, "to_objects", tuple(self.to_objects))
        object.__setattr__(self, "through_objects", tuple(self.through_objects))
        object.__setattr__(self, "status", FactState(self.status))
        object.__setattr__(self, "attributes", dict(self.attributes))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "command": self.command,
            "value": self.value,
            "objects": list(self.objects),
            "clocks": list(self.clocks),
            "from_objects": list(self.from_objects),
            "to_objects": list(self.to_objects),
            "through_objects": list(self.through_objects),
            "status": self.status.value,
            "attributes": dict(self.attributes),
        }
        if self.provenance is not None:
            result["provenance"] = self.provenance.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class _Segment:
    kind: str
    value: str
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class _Word:
    segments: tuple[_Segment, ...]
    raw: str
    line: int
    column: int
    braced: bool = False


@dataclass(frozen=True, slots=True)
class _Command:
    words: tuple[_Word, ...]
    line: int
    column: int


class _TclParser:
    """Small Tcl tokenizer concerned with grouping, not command semantics."""

    def __init__(
        self,
        text: str,
        path: Path,
        view: ViewId,
        *,
        line: int = 1,
        column: int = 1,
    ) -> None:
        self.text = text
        self.path = path
        self.view = view
        self.index = 0
        self.line = line
        self.column = column
        self.diagnostics: list[Diagnostic] = []
        self._word_count = 0
        self._command_count = 0
        self._limit_reported = False

    @property
    def eof(self) -> bool:
        return self.index >= len(self.text)

    @property
    def char(self) -> str:
        return "" if self.eof else self.text[self.index]

    def _location(self, line: int | None = None, column: int | None = None) -> Provenance:
        return Provenance(
            str(self.path),
            self.line if line is None else line,
            self.column if column is None else column,
            self.view,
        )

    def _syntax(self, message: str, *, line: int | None = None, column: int | None = None) -> None:
        self.diagnostics.append(
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                message,
                location=self._location(line, column),
            )
        )

    def _consume(self) -> str:
        if self.eof:
            return ""
        value = self.text[self.index]
        self.index += 1
        if value == "\n":
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return value

    def _continuation(self) -> bool:
        return (
            self.char == "\\"
            and self.index + 1 < len(self.text)
            and self.text[self.index + 1] == "\n"
        )

    def _consume_continuation(self) -> str:
        self._consume()
        self._consume()
        while self.char in {" ", "\t"}:
            self._consume()
        return " "

    def _consume_escape(self) -> str:
        line, column = self.line, self.column
        self._consume()
        if self.eof:
            self._syntax("Trailing backslash in SDC/Tcl source", line=line, column=column)
            return "\\"
        if self.char == "\n":
            self._consume()
            while self.char in {" ", "\t"}:
                self._consume()
            return " "
        escaped = self._consume()
        translations = {
            "a": "\a",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
        }
        if escaped in translations:
            return translations[escaped]
        if escaped in {"x", "u", "U"}:
            maximum = {"x": 2, "u": 4, "U": 8}[escaped]
            digits: list[str] = []
            while len(digits) < maximum and self.char and self.char.lower() in "0123456789abcdef":
                digits.append(self._consume())
            if digits:
                try:
                    return chr(int("".join(digits), 16))
                except ValueError:
                    pass
            return escaped
        if escaped in "01234567":
            digits = [escaped]
            while len(digits) < 3 and self.char in "01234567":
                digits.append(self._consume())
            return chr(int("".join(digits), 8))
        return escaped

    @staticmethod
    def _append_literal(segments: list[_Segment], value: str, line: int, column: int) -> None:
        if not value:
            return
        if segments and segments[-1].kind == "literal":
            previous = segments[-1]
            segments[-1] = _Segment(
                "literal", previous.value + value, previous.line, previous.column
            )
        else:
            segments.append(_Segment("literal", value, line, column))

    def _variable_segment(self) -> _Segment | None:
        line, column = self.line, self.column
        self._consume()
        if self.char == "{":
            self._consume()
            name_characters: list[str] = []
            while not self.eof and self.char != "}":
                name_characters.append(self._consume())
            if self.eof:
                self._syntax(
                    "Unterminated braced variable name in SDC/Tcl source",
                    line=line,
                    column=column,
                )
                return _Segment("variable", "".join(name_characters), line, column)
            self._consume()
            return _Segment("variable", "".join(name_characters), line, column)
        match = _VARIABLE_NAME.match(self.text, self.index)
        if match is None:
            return None
        name = match.group(0)
        for _ in name:
            self._consume()
        if self.char == "(":
            array_name = [name, self._consume()]
            depth = 1
            while not self.eof and depth:
                current = self._consume()
                array_name.append(current)
                if current == "\\" and not self.eof:
                    array_name.append(self._consume())
                elif current == "(":
                    depth += 1
                elif current == ")":
                    depth -= 1
            if depth:
                self._syntax(
                    "Unterminated Tcl array variable reference",
                    line=line,
                    column=column,
                )
            return _Segment("array", "".join(array_name), line, column)
        return _Segment("variable", name, line, column)

    def _command_segment(self) -> _Segment:
        line, column = self.line, self.column
        self._consume()
        content_line, content_column = self.line, self.column
        start = self.index
        depth = 1
        brace_depth = 0
        quoted = False
        in_comment = False
        command_start = True
        frames: list[tuple[bool, int, bool, bool]] = []
        escaped = False
        nesting_reported = False
        while not self.eof:
            current = self.char
            if in_comment:
                self._consume()
                if current == "\n":
                    in_comment = False
                    command_start = True
                continue
            if escaped:
                escaped = False
                self._consume()
                continue
            if current == "\\":
                escaped = True
                self._consume()
                continue
            if brace_depth:
                if current == "{":
                    brace_depth += 1
                elif current == "}":
                    brace_depth -= 1
                self._consume()
                continue
            if current == "{":
                brace_depth = 1
                command_start = False
                self._consume()
                continue
            if current == '"':
                quoted = not quoted
                command_start = False
                self._consume()
                continue
            if current == "#" and command_start and not quoted:
                in_comment = True
                self._consume()
                continue
            if current == "[":
                frames.append((quoted, brace_depth, in_comment, command_start))
                depth += 1
                if depth > _MAX_NESTING and not nesting_reported:
                    self._syntax(
                        f"SDC/Tcl command substitution exceeds {_MAX_NESTING} nesting levels",
                        line=line,
                        column=column,
                    )
                    nesting_reported = True
                quoted = False
                brace_depth = 0
                in_comment = False
                command_start = True
                self._consume()
                continue
            if current == "]" and not quoted:
                depth -= 1
                if depth == 0:
                    value = self.text[start : self.index]
                    self._consume()
                    return _Segment("command", value, content_line, content_column)
                quoted, brace_depth, in_comment, command_start = frames.pop()
                self._consume()
                continue
            if not quoted and current in {"\n", ";"}:
                command_start = True
            elif not current.isspace():
                command_start = False
            self._consume()
        self._syntax(
            "Unterminated command substitution in SDC/Tcl source",
            line=line,
            column=column,
        )
        return _Segment("command", self.text[start : self.index], content_line, content_column)

    def _braced_word(self) -> tuple[_Segment, ...]:
        line, column = self.line, self.column
        self._consume()
        depth = 1
        content: list[str] = []
        nesting_reported = False
        while not self.eof:
            if self._continuation():
                content.append(self._consume_continuation())
                continue
            current_line, current_column = self.line, self.column
            current = self._consume()
            if current == "\\" and not self.eof:
                # Braced words preserve backslashes except for line continuations.
                content.append(current)
                content.append(self._consume())
                continue
            if current == "{":
                depth += 1
                if depth > _MAX_NESTING and not nesting_reported:
                    self._syntax(
                        f"SDC/Tcl braced word exceeds {_MAX_NESTING} nesting levels",
                        line=line,
                        column=column,
                    )
                    nesting_reported = True
                content.append(current)
                continue
            if current == "}":
                depth -= 1
                if depth == 0:
                    return (_Segment("literal", "".join(content), line, column),)
                content.append(current)
                continue
            content.append(current)
            del current_line, current_column
        self._syntax("Unterminated braced word in SDC/Tcl source", line=line, column=column)
        return (_Segment("literal", "".join(content), line, column),)

    def _substitutable_word(self, *, quoted: bool) -> tuple[_Segment, ...]:
        segments: list[_Segment] = []
        start_line, start_column = self.line, self.column
        if quoted:
            self._consume()
        while not self.eof:
            if quoted and self.char == '"':
                self._consume()
                if not segments:
                    segments.append(_Segment("literal", "", start_line, start_column))
                return tuple(segments)
            if not quoted and (self.char.isspace() or self.char == ";"):
                break
            line, column = self.line, self.column
            if self.char == "\\":
                self._append_literal(segments, self._consume_escape(), line, column)
            elif self.char == "$":
                variable = self._variable_segment()
                if variable is None:
                    self._append_literal(segments, "$", line, column)
                else:
                    segments.append(variable)
            elif self.char == "[":
                segments.append(self._command_segment())
            else:
                self._append_literal(segments, self._consume(), line, column)
        if quoted:
            self._syntax(
                "Unterminated quoted word in SDC/Tcl source",
                line=start_line,
                column=start_column,
            )
        if not segments:
            segments.append(_Segment("literal", "", start_line, start_column))
        return tuple(segments)

    def _word(self) -> _Word:
        start = self.index
        line, column = self.line, self.column
        braced = self.char == "{"
        if braced:
            segments = self._braced_word()
        elif self.char == '"':
            segments = self._substitutable_word(quoted=True)
        else:
            segments = self._substitutable_word(quoted=False)
        raw = self.text[start : self.index]
        if braced or raw.startswith('"'):
            if not self.eof and not self.char.isspace() and self.char != ";":
                self._syntax(
                    "Extra characters after a grouped SDC/Tcl word",
                    line=self.line,
                    column=self.column,
                )
                while not self.eof and not self.char.isspace() and self.char != ";":
                    self._consume()
        return _Word(segments, raw, line, column, braced=braced)

    def parse(self) -> tuple[_Command, ...]:
        commands: list[_Command] = []
        words: list[_Word] = []
        command_line, command_column = self.line, self.column
        while True:
            while self.char in {" ", "\t", "\f", "\v"} or self._continuation():
                if self._continuation():
                    self._consume_continuation()
                else:
                    self._consume()
            if self.eof:
                break
            if not words and self.char == "#":
                while not self.eof and self.char != "\n":
                    self._consume()
                continue
            if self.char in {"\n", ";"}:
                self._consume()
                if words:
                    commands.append(_Command(tuple(words), command_line, command_column))
                    words = []
                    self._command_count += 1
                command_line, command_column = self.line, self.column
                if self._command_count > _MAX_COMMANDS and not self._limit_reported:
                    self._syntax(f"SDC/Tcl source exceeds {_MAX_COMMANDS} commands")
                    self._limit_reported = True
                    break
                continue
            if not words:
                command_line, command_column = self.line, self.column
            words.append(self._word())
            self._word_count += 1
            if self._word_count > _MAX_WORDS and not self._limit_reported:
                self._syntax(f"SDC/Tcl source exceeds {_MAX_WORDS} words")
                self._limit_reported = True
                break
        if words:
            commands.append(_Command(tuple(words), command_line, command_column))
        return tuple(commands)


@dataclass(frozen=True, slots=True)
class _Value:
    text: str | None = None
    list_items: tuple[str, ...] | None = None
    references: tuple[DesignObjectObservation, ...] = ()
    status: FactState = FactState.KNOWN
    dynamic: bool = False
    display: str = ""


class _ListSyntaxError(ValueError):
    pass


def _decode_list_escape(text: str, index: int) -> tuple[str, int]:
    if index + 1 >= len(text):
        raise _ListSyntaxError("trailing backslash")
    escaped = text[index + 1]
    if escaped == "\n":
        index += 2
        while index < len(text) and text[index] in {" ", "\t"}:
            index += 1
        return " ", index
    translations = {"n": "\n", "r": "\r", "t": "\t", "f": "\f", "v": "\v"}
    return translations.get(escaped, escaped), index + 2


def _split_static_list(text: str) -> tuple[str, ...]:
    """Split a Tcl list without performing variable or command substitution."""

    items: list[str] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        value: list[str] = []
        if text[index] == "{":
            index += 1
            depth = 1
            while index < len(text):
                current = text[index]
                if current == "\\":
                    if index + 1 < len(text) and text[index + 1] == "\n":
                        decoded, index = _decode_list_escape(text, index)
                        value.append(decoded)
                    else:
                        value.append(current)
                        index += 1
                        if index < len(text):
                            value.append(text[index])
                            index += 1
                    continue
                if current == "{":
                    depth += 1
                    if depth > _MAX_NESTING:
                        raise _ListSyntaxError("list exceeds nesting limit")
                    value.append(current)
                    index += 1
                    continue
                if current == "}":
                    depth -= 1
                    index += 1
                    if depth == 0:
                        break
                    value.append(current)
                    continue
                value.append(current)
                index += 1
            if depth:
                raise _ListSyntaxError("unclosed brace")
            if index < len(text) and not text[index].isspace():
                raise _ListSyntaxError("characters after close-brace")
        elif text[index] == '"':
            index += 1
            while index < len(text) and text[index] != '"':
                if text[index] == "\\":
                    decoded, index = _decode_list_escape(text, index)
                    value.append(decoded)
                else:
                    value.append(text[index])
                    index += 1
            if index >= len(text):
                raise _ListSyntaxError("unclosed quote")
            index += 1
            if index < len(text) and not text[index].isspace():
                raise _ListSyntaxError("characters after close-quote")
        else:
            while index < len(text) and not text[index].isspace():
                if text[index] == "\\":
                    decoded, index = _decode_list_escape(text, index)
                    value.append(decoded)
                else:
                    value.append(text[index])
                    index += 1
        items.append("".join(value))
    return tuple(items)


def _state(values: Sequence[_Value]) -> FactState:
    states = {value.status for value in values}
    for candidate in (FactState.UNSUPPORTED, FactState.TAINTED, FactState.UNKNOWN):
        if candidate in states:
            return candidate
    return FactState.KNOWN


def _reference_names(references: Sequence[DesignObjectObservation]) -> tuple[str, ...]:
    return tuple(item.native_name for item in references)


def _has_unescaped_glob(text: str) -> bool:
    escaped = False
    for character in text:
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character in "*?[":
            return True
    return False


class _Evaluator:
    _GET_KINDS = {
        "get_ports": "port",
        "get_pins": "pin",
        "get_cells": "cell",
        "get_clocks": "clock",
    }

    def __init__(self, view: ViewId) -> None:
        self.view = view
        self.variables: dict[str, _Value] = {}
        self.objects: list[DesignObjectObservation] = []
        self.clocks: list[ClockObservation] = []
        self.constraints: list[TimingConstraintObservation] = []
        self.diagnostics: list[Diagnostic] = []
        self.tainted_sources: dict[str, Provenance] = {}

    def _location(self, path: Path, item: _Command | _Word | _Segment) -> Provenance:
        return Provenance(str(path), item.line, item.column, self.view)

    def _issue(
        self,
        code: str,
        severity: Severity,
        message: str,
        path: Path,
        item: _Command | _Word | _Segment,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        location = self._location(path, item)
        self.diagnostics.append(
            parser_diagnostic(
                code,
                severity,
                message,
                location=location,
                metadata=metadata,
            )
        )
        self.tainted_sources.setdefault(str(path), location)

    def _nested_value(
        self,
        segment: _Segment,
        path: Path,
        *,
        context: str,
        depth: int,
    ) -> _Value:
        if depth > _MAX_NESTING:
            self._issue(
                "OC1101",
                Severity.FATAL,
                f"SDC/Tcl evaluation exceeds {_MAX_NESTING} nesting levels",
                path,
                segment,
            )
            return _Value(status=FactState.TAINTED, dynamic=True, display=f"[{segment.value}]")
        nested_parser = _TclParser(
            segment.value,
            path,
            self.view,
            line=segment.line,
            column=segment.column,
        )
        commands = nested_parser.parse()
        self.diagnostics.extend(nested_parser.diagnostics)
        if nested_parser.diagnostics:
            self.tainted_sources.setdefault(str(path), self._location(path, segment))
        if any(diagnostic.severity == Severity.FATAL for diagnostic in nested_parser.diagnostics):
            return _Value(
                status=FactState.TAINTED,
                dynamic=True,
                display=f"[{segment.value}]",
            )
        if len(commands) != 1:
            self._issue(
                "OC1102",
                Severity.WARNING,
                "Command substitution must contain one statically supported command",
                path,
                segment,
            )
            return _Value(status=FactState.UNSUPPORTED, dynamic=True, display=f"[{segment.value}]")
        return self._command(commands[0], path, nested=True, context=context, depth=depth)

    def _word(self, word: _Word, path: Path, *, context: str, depth: int) -> _Value:
        values: list[_Value] = []
        for segment in word.segments:
            if segment.kind == "literal":
                values.append(_Value(text=segment.value, display=segment.value))
            elif segment.kind == "variable":
                variable = self.variables.get(segment.value)
                if variable is None:
                    self._issue(
                        "OC1103",
                        Severity.WARNING,
                        f"SDC variable {segment.value!r} is not statically defined",
                        path,
                        segment,
                        metadata={"variable": segment.value},
                    )
                    values.append(
                        _Value(
                            status=FactState.TAINTED,
                            dynamic=True,
                            display=f"${segment.value}",
                        )
                    )
                else:
                    if variable.status != FactState.KNOWN:
                        self._issue(
                            "OC1103",
                            Severity.WARNING,
                            f"SDC variable {segment.value!r} is not statically resolvable",
                            path,
                            segment,
                            metadata={"variable": segment.value},
                        )
                    values.append(variable)
            elif segment.kind == "array":
                self._issue(
                    "OC1102",
                    Severity.WARNING,
                    f"Tcl array variable {segment.value!r} is not statically supported in SDC",
                    path,
                    segment,
                    metadata={"variable": segment.value},
                )
                values.append(
                    _Value(
                        status=FactState.UNSUPPORTED,
                        dynamic=True,
                        display=f"${segment.value}",
                    )
                )
            else:
                values.append(self._nested_value(segment, path, context=context, depth=depth + 1))

        meaningful = [
            value
            for value in values
            if value.text not in {None, ""}
            or value.list_items is not None
            or value.references
            or value.status != FactState.KNOWN
        ]
        if len(meaningful) == 1 and all(
            value.text in {None, ""} for value in values if value is not meaningful[0]
        ):
            return meaningful[0]
        if not meaningful:
            return _Value(text="", display=word.raw)

        pieces: list[str] = []
        for value in values:
            if value.references or (value.list_items is not None and len(value.list_items) != 1):
                self._issue(
                    "OC1102",
                    Severity.WARNING,
                    "A collection or list cannot be statically concatenated inside an SDC word",
                    path,
                    word,
                    metadata={"word": word.raw, "context": context},
                )
                return _Value(
                    status=FactState.UNSUPPORTED,
                    dynamic=True,
                    display=word.raw,
                )
            if value.list_items is not None:
                pieces.append(value.list_items[0])
            elif value.text is not None:
                pieces.append(value.text)
            else:
                return _Value(
                    status=_state(values),
                    dynamic=True,
                    display=word.raw,
                )
        return _Value(
            text="".join(pieces),
            status=_state(values),
            dynamic=any(value.dynamic for value in values),
            display=word.raw,
        )

    @staticmethod
    def _scalar(value: _Value) -> str | None:
        if value.references:
            return None
        if value.list_items is not None:
            return value.list_items[0] if len(value.list_items) == 1 else None
        return value.text

    def _items(
        self,
        value: _Value,
        path: Path,
        word: _Word,
        *,
        context: str,
    ) -> tuple[str, ...] | None:
        if value.references:
            return None
        if value.list_items is not None:
            return value.list_items
        if value.text is None:
            return None
        try:
            return _split_static_list(value.text)
        except _ListSyntaxError as error:
            self._issue(
                "OC1103",
                Severity.WARNING,
                f"Cannot statically resolve Tcl list in {context}: {error}",
                path,
                word,
                metadata={"value": value.text, "context": context},
            )
            return None

    @staticmethod
    def _option_value(value: _Value) -> Any:
        if value.references:
            return [item.native_name for item in value.references]
        if value.list_items is not None:
            return list(value.list_items)
        return value.text

    def _options(
        self,
        command: str,
        words: Sequence[_Word],
        path: Path,
        *,
        value_options: frozenset[str],
        flag_options: frozenset[str],
        depth: int,
    ) -> tuple[dict[str, list[_Value]], list[tuple[_Word, _Value]], bool]:
        options: dict[str, list[_Value]] = {}
        positionals: list[tuple[_Word, _Value]] = []
        tainted = False
        index = 0
        while index < len(words):
            word = words[index]
            value = self._word(word, path, context=command, depth=depth)
            scalar = self._scalar(value)
            if scalar in flag_options:
                options.setdefault(scalar, []).append(_Value(text="true", display=word.raw))
                index += 1
                continue
            if scalar in value_options:
                if index + 1 >= len(words):
                    self._issue(
                        "OC1103",
                        Severity.WARNING,
                        f"SDC command {command} option {scalar} has no value",
                        path,
                        word,
                    )
                    tainted = True
                    break
                option_word = words[index + 1]
                option_value = self._word(option_word, path, context=command, depth=depth)
                options.setdefault(scalar, []).append(option_value)
                if option_value.status != FactState.KNOWN:
                    tainted = True
                index += 2
                continue
            if scalar is not None and scalar.startswith("-") and _NUMBER.fullmatch(scalar) is None:
                self._issue(
                    "OC1102",
                    Severity.WARNING,
                    f"Unsupported option {scalar!r} on SDC command {command}",
                    path,
                    word,
                    metadata={"command": command, "option": scalar},
                )
                options.setdefault(scalar, []).append(_Value(text="unsupported"))
                tainted = True
                index += 1
                continue
            positionals.append((word, value))
            if value.status != FactState.KNOWN:
                tainted = True
            index += 1
        return options, positionals, tainted

    def _new_reference(
        self,
        *,
        kind: str,
        query: str,
        command: str,
        path: Path,
        word: _Word | _Command,
        match_mode: str = "exact",
        status: FactState = FactState.KNOWN,
        dynamic: bool = False,
        options: Mapping[str, Any] | None = None,
        context: str | None = None,
        implicit: bool = False,
    ) -> DesignObjectObservation:
        name = query if query else "<empty-query>"
        observation = DesignObjectObservation(
            kind=kind,
            native_name=name,
            relation="reference",
            provenance=self._location(path, word),
            status=status,
            attributes={
                "command": command,
                "pattern": match_mode != "exact",
                "dynamic": dynamic or match_mode != "exact",
                "match_mode": match_mode,
                "context": context or command,
                "implicit": implicit,
                "options": dict(options or {}),
            },
        )
        self.objects.append(observation)
        return observation

    def _get(
        self,
        command: str,
        command_node: _Command,
        path: Path,
        *,
        context: str,
        depth: int,
    ) -> _Value:
        kind = self._GET_KINDS[command]
        options, positionals, tainted = self._options(
            command,
            command_node.words[1:],
            path,
            value_options=frozenset({"-filter", "-of_objects"}),
            flag_options=frozenset({"-quiet", "-nocase", "-hierarchical", "-regexp"}),
            depth=depth,
        )
        option_payload = {
            key: [self._option_value(value) for value in values]
            for key, values in sorted(options.items())
        }
        selection_dynamic = bool(options.keys() & {"-filter", "-of_objects"})
        unsupported_selection = any(
            value.text == "unsupported" for values in options.values() for value in values
        )
        if selection_dynamic:
            self._issue(
                "OC1102",
                Severity.WARNING,
                f"{command} filters or -of_objects selection cannot be resolved statically",
                path,
                command_node,
                metadata={"command": command},
            )
            tainted = True
        queries: list[tuple[str, _Word, FactState]] = []
        for word, value in positionals:
            items = self._items(value, path, word, context=command)
            if items is None or value.status != FactState.KNOWN:
                queries.append((value.display or word.raw, word, value.status))
                tainted = True
                continue
            queries.extend((item, word, FactState.KNOWN) for item in items)
        if not queries:
            self._issue(
                "OC1103",
                Severity.WARNING,
                f"{command} has no statically resolvable object query",
                path,
                command_node,
                metadata={"command": command},
            )
            queries.append(("<unresolved-query>", command_node.words[0], FactState.TAINTED))
            tainted = True

        references: list[DesignObjectObservation] = []
        regexp_mode = "-regexp" in options
        for query, word, query_state in queries:
            if regexp_mode:
                match_mode = "regexp"
            else:
                raw_pattern = word.raw if any(char in word.raw for char in "*?[\\") else query
                match_mode = "glob" if _has_unescaped_glob(raw_pattern) else "exact"
            status = (
                FactState.UNSUPPORTED
                if selection_dynamic or unsupported_selection
                else FactState.TAINTED
                if query_state != FactState.KNOWN
                else FactState.KNOWN
            )
            references.append(
                self._new_reference(
                    kind=kind,
                    query=query,
                    command=command,
                    path=path,
                    word=word,
                    match_mode=match_mode,
                    status=status,
                    dynamic=tainted,
                    options=option_payload,
                    context=context,
                )
            )
        reference_state = (
            FactState.UNSUPPORTED
            if selection_dynamic or unsupported_selection
            else FactState.TAINTED
            if tainted
            else FactState.KNOWN
        )
        return _Value(
            references=tuple(references),
            status=reference_state,
            dynamic=tainted or any(item.attributes["dynamic"] for item in references),
            display=f"[{command} ...]",
        )

    def _set(self, command: _Command, path: Path, *, depth: int) -> _Value:
        if len(command.words) != 3:
            self._issue(
                "OC1102",
                Severity.WARNING,
                "Only the static Tcl form 'set name value' is supported in SDC",
                path,
                command,
            )
            return _Value(status=FactState.UNSUPPORTED, dynamic=True, display="set")
        name_value = self._word(command.words[1], path, context="set", depth=depth)
        name = self._scalar(name_value)
        if not name or name_value.status != FactState.KNOWN or "(" in name:
            self._issue(
                "OC1102",
                Severity.WARNING,
                "SDC variable name is not statically resolvable or uses an array",
                path,
                command.words[1],
            )
            return _Value(status=FactState.UNSUPPORTED, dynamic=True, display="set")
        value = self._word(command.words[2], path, context="set", depth=depth)
        self.variables[name] = value
        return value

    def _list(self, command: _Command, path: Path, *, depth: int) -> _Value:
        items: list[str] = []
        values: list[_Value] = []
        for word in command.words[1:]:
            value = self._word(word, path, context="list", depth=depth)
            values.append(value)
            scalar = self._scalar(value)
            if scalar is None or value.status != FactState.KNOWN:
                self._issue(
                    "OC1102",
                    Severity.WARNING,
                    "Tcl list contains a non-static collection or value",
                    path,
                    word,
                )
                return _Value(status=FactState.UNSUPPORTED, dynamic=True, display="list")
            items.append(scalar)
        return _Value(list_items=tuple(items), status=_state(values), display="list")

    def _implicit_references(
        self,
        value: _Value,
        word: _Word,
        path: Path,
        *,
        kind: str,
        command: str,
        ambiguous: bool = False,
    ) -> tuple[DesignObjectObservation, ...]:
        if value.references:
            return value.references
        items = self._items(value, path, word, context=command)
        if items is None:
            return ()
        if ambiguous and items:
            self._issue(
                "OC1102",
                Severity.WARNING,
                (f"{command} literal object targets are ambiguous; use get_ports or get_pins"),
                path,
                word,
            )
        return tuple(
            self._new_reference(
                kind=kind,
                query=item,
                command=command,
                path=path,
                word=word,
                status=FactState.UNSUPPORTED if ambiguous else value.status,
                dynamic=value.dynamic or ambiguous,
                context=command,
                implicit=True,
            )
            for item in items
        )

    def _number(
        self,
        value: _Value,
        path: Path,
        word: _Word | _Command,
        *,
        label: str,
        positive: bool = False,
    ) -> float | None:
        scalar = self._scalar(value)
        try:
            parsed = float(scalar) if scalar is not None and _NUMBER.fullmatch(scalar) else None
        except ValueError:
            parsed = None
        if parsed is None or not math.isfinite(parsed) or (positive and parsed <= 0):
            self._issue(
                "OC1103",
                Severity.WARNING,
                f"SDC {label} is not a valid{' positive' if positive else ''} number",
                path,
                word,
                metadata={"value": scalar},
            )
            return None
        return parsed

    def _integer(
        self,
        value: _Value,
        path: Path,
        word: _Word | _Command,
        *,
        label: str,
        positive: bool = False,
    ) -> int | None:
        scalar = self._scalar(value)
        parsed = int(scalar) if scalar is not None and _INTEGER.fullmatch(scalar) else None
        if parsed is None or (positive and parsed <= 0):
            self._issue(
                "OC1103",
                Severity.WARNING,
                f"SDC {label} is not a valid{' positive' if positive else ''} integer",
                path,
                word,
                metadata={"value": scalar},
            )
            return None
        return parsed

    def _waveform(
        self, value: _Value, path: Path, word: _Word | _Command
    ) -> tuple[float, float] | None:
        if value.list_items is not None:
            items = value.list_items
        elif value.text is not None:
            try:
                items = _split_static_list(value.text)
            except _ListSyntaxError:
                items = ()
        else:
            items = ()
        if len(items) != 2:
            self._issue(
                "OC1103",
                Severity.WARNING,
                "SDC clock waveform must contain exactly two numeric edges",
                path,
                word,
                metadata={"value": self._option_value(value)},
            )
            return None
        try:
            waveform = (float(items[0]), float(items[1]))
        except ValueError:
            waveform = (math.nan, math.nan)
        if not all(math.isfinite(edge) for edge in waveform):
            self._issue(
                "OC1103",
                Severity.WARNING,
                "SDC clock waveform must contain exactly two numeric edges",
                path,
                word,
                metadata={"value": list(items)},
            )
            return None
        return waveform

    def _clock_names(
        self,
        explicit: _Value | None,
        targets: Sequence[DesignObjectObservation],
        command: _Command,
        path: Path,
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        if explicit is not None:
            name = self._scalar(explicit)
            if name and explicit.status == FactState.KNOWN:
                return ((name, _reference_names(targets)),)
            self._issue(
                "OC1103",
                Severity.WARNING,
                "SDC clock name is not statically resolvable",
                path,
                command,
            )
            unresolved = f"<unresolved-clock@{command.line}:{command.column}>"
            return ((unresolved, _reference_names(targets)),)
        exact_targets = [
            target
            for target in targets
            if target.status == FactState.KNOWN and not target.attributes.get("dynamic")
        ]
        if exact_targets and len(exact_targets) == len(targets):
            return tuple((target.native_name, (target.native_name,)) for target in exact_targets)
        self._issue(
            "OC1103",
            Severity.WARNING,
            "SDC clock has neither a static -name nor an exact target-derived name",
            path,
            command,
        )
        return ((f"<unresolved-clock@{command.line}:{command.column}>", _reference_names(targets)),)

    def _create_clock(self, command: _Command, path: Path, *, generated: bool, depth: int) -> None:
        name = "create_generated_clock" if generated else "create_clock"
        if generated:
            value_options = frozenset(
                {
                    "-name",
                    "-source",
                    "-master_clock",
                    "-divide_by",
                    "-multiply_by",
                    "-edges",
                    "-edge_shift",
                    "-duty_cycle",
                    "-phase",
                    "-comment",
                }
            )
            flag_options = frozenset({"-add", "-invert", "-combinational"})
        else:
            value_options = frozenset({"-name", "-period", "-waveform", "-comment"})
            flag_options = frozenset({"-add"})
        options, positionals, tainted = self._options(
            name,
            command.words[1:],
            path,
            value_options=value_options,
            flag_options=flag_options,
            depth=depth,
        )
        targets: list[DesignObjectObservation] = []
        for word, value in positionals:
            targets.extend(
                self._implicit_references(
                    value,
                    word,
                    path,
                    kind="pin" if generated else "port",
                    command=name,
                    ambiguous=True,
                )
            )
        if generated and not targets:
            self._issue(
                "OC1103",
                Severity.WARNING,
                "create_generated_clock requires a statically resolvable target collection",
                path,
                command,
            )
            tainted = True
        if any(target.status != FactState.KNOWN for target in targets):
            tainted = True
        if any(target.attributes.get("dynamic") for target in targets):
            tainted = True

        period: float | None = None
        waveform: tuple[float, float] | None = None
        source_references: tuple[DesignObjectObservation, ...] = ()
        master_references: tuple[DesignObjectObservation, ...] = ()
        numeric_attributes: dict[str, Any] = {}
        if generated:
            source_values = options.get("-source", [])
            if len(source_values) != 1:
                self._issue(
                    "OC1103",
                    Severity.WARNING,
                    "create_generated_clock requires one statically resolvable -source",
                    path,
                    command,
                )
                tainted = True
            else:
                source_word = command.words[0]
                source_references = self._implicit_references(
                    source_values[0],
                    source_word,
                    path,
                    kind="pin",
                    command=name,
                    ambiguous=True,
                )
                if not source_references or any(
                    item.status != FactState.KNOWN for item in source_references
                ):
                    tainted = True
            master_values = options.get("-master_clock", [])
            if master_values:
                master_references = self._implicit_references(
                    master_values[-1],
                    command.words[0],
                    path,
                    kind="clock",
                    command=name,
                )
            for option in ("-divide_by", "-multiply_by"):
                if option in options:
                    parsed = self._integer(
                        options[option][-1], path, command, label=option, positive=True
                    )
                    numeric_attributes[option.lstrip("-")] = parsed
                    tainted = tainted or parsed is None
            for option in ("-duty_cycle", "-phase"):
                if option in options:
                    parsed_float = self._number(options[option][-1], path, command, label=option)
                    numeric_attributes[option.lstrip("-")] = parsed_float
                    tainted = tainted or parsed_float is None
            for option in ("-edges", "-edge_shift"):
                if option in options:
                    value = options[option][-1]
                    items = value.list_items
                    if items is None and value.text is not None:
                        try:
                            items = _split_static_list(value.text)
                        except _ListSyntaxError:
                            items = None
                    if items is None:
                        self._issue(
                            "OC1103",
                            Severity.WARNING,
                            f"{name} {option} is not a static Tcl list",
                            path,
                            command,
                        )
                        tainted = True
                    else:
                        numeric_attributes[option.lstrip("-")] = list(items)
        else:
            period_values = options.get("-period", [])
            if len(period_values) != 1:
                self._issue(
                    "OC1103",
                    Severity.WARNING,
                    "create_clock requires one positive -period",
                    path,
                    command,
                )
                tainted = True
            else:
                period = self._number(
                    period_values[0], path, command, label="clock period", positive=True
                )
                tainted = tainted or period is None
            waveform_values = options.get("-waveform", [])
            if waveform_values:
                waveform = self._waveform(waveform_values[-1], path, command)
                tainted = tainted or waveform is None

        explicit_name = options.get("-name", [None])[-1]
        declarations = self._clock_names(explicit_name, targets, command, path)
        option_payload = {
            key: [self._option_value(value) for value in values]
            for key, values in sorted(options.items())
        }
        for clock_name, clock_targets in declarations:
            self.clocks.append(
                ClockObservation(
                    native_name=clock_name,
                    targets=clock_targets,
                    period=period,
                    waveform=waveform,
                    source=(
                        source_references[0].native_name if len(source_references) == 1 else None
                    ),
                    generated=generated,
                    provenance=self._location(path, command),
                    status=FactState.TAINTED if tainted else FactState.KNOWN,
                    attributes={
                        "command": name,
                        "source_objects": list(_reference_names(source_references)),
                        "master_clocks": list(_reference_names(master_references)),
                        "options": option_payload,
                        **numeric_attributes,
                    },
                )
            )

    def _delay(self, command: _Command, path: Path, *, depth: int) -> None:
        name_value = self._scalar(
            self._word(command.words[0], path, context="command", depth=depth)
        )
        if name_value is None:
            self._issue(
                "OC1101",
                Severity.ERROR,
                "Cannot statically resolve the SDC delay command name",
                path,
                command,
            )
            return
        options, positionals, tainted = self._options(
            name_value,
            command.words[1:],
            path,
            value_options=frozenset({"-clock", "-reference_pin"}),
            flag_options=frozenset(
                {
                    "-clock_fall",
                    "-rise",
                    "-fall",
                    "-max",
                    "-min",
                    "-add_delay",
                    "-network_latency_included",
                    "-source_latency_included",
                }
            ),
            depth=depth,
        )
        delay: float | None = None
        target_references: list[DesignObjectObservation] = []
        if not positionals:
            self._issue(
                "OC1103",
                Severity.WARNING,
                f"{name_value} requires a delay value and port collection",
                path,
                command,
            )
            tainted = True
        else:
            delay = self._number(
                positionals[0][1], path, positionals[0][0], label=f"{name_value} delay"
            )
            tainted = tainted or delay is None
            for word, value in positionals[1:]:
                target_references.extend(
                    self._implicit_references(value, word, path, kind="port", command=name_value)
                )
            if not target_references:
                self._issue(
                    "OC1103",
                    Severity.WARNING,
                    f"{name_value} has no statically resolvable target ports",
                    path,
                    command,
                )
                tainted = True
        clock_references: list[DesignObjectObservation] = []
        for clock_value in options.get("-clock", []):
            clock_references.extend(
                self._implicit_references(
                    clock_value,
                    command.words[0],
                    path,
                    kind="clock",
                    command=name_value,
                )
            )
        reference_pins: list[DesignObjectObservation] = []
        for pin_value in options.get("-reference_pin", []):
            reference_pins.extend(
                self._implicit_references(
                    pin_value,
                    command.words[0],
                    path,
                    kind="pin",
                    command=name_value,
                )
            )
        all_references = [*target_references, *clock_references, *reference_pins]
        tainted = tainted or any(
            item.status != FactState.KNOWN or item.attributes.get("dynamic")
            for item in all_references
        )
        self.constraints.append(
            TimingConstraintObservation(
                command=name_value,
                value=delay,
                objects=_reference_names(target_references),
                clocks=_reference_names(clock_references),
                provenance=self._location(path, command),
                status=FactState.TAINTED if tainted else FactState.KNOWN,
                attributes={
                    "reference_pins": list(_reference_names(reference_pins)),
                    "options": {
                        key: [self._option_value(value) for value in values]
                        for key, values in sorted(options.items())
                    },
                },
            )
        )

    def _path_exception(self, command: _Command, path: Path, *, depth: int) -> None:
        name_value = self._scalar(
            self._word(command.words[0], path, context="command", depth=depth)
        )
        if name_value is None:
            self._issue(
                "OC1101",
                Severity.ERROR,
                "Cannot statically resolve the SDC path-exception command name",
                path,
                command,
            )
            return
        endpoint_options = frozenset(
            {
                "-from",
                "-to",
                "-through",
                "-rise_from",
                "-fall_from",
                "-rise_to",
                "-fall_to",
                "-rise_through",
                "-fall_through",
            }
        )
        options, positionals, tainted = self._options(
            name_value,
            command.words[1:],
            path,
            value_options=endpoint_options | frozenset({"-comment"}),
            flag_options=frozenset({"-setup", "-hold", "-rise", "-fall", "-start", "-end"}),
            depth=depth,
        )
        value: int | None = None
        if name_value == "set_multicycle_path":
            if not positionals:
                self._issue(
                    "OC1103",
                    Severity.WARNING,
                    "set_multicycle_path requires a positive cycle count",
                    path,
                    command,
                )
                tainted = True
            else:
                value = self._integer(
                    positionals[0][1],
                    path,
                    positionals[0][0],
                    label="multicycle count",
                    positive=True,
                )
                tainted = tainted or value is None
                positionals = positionals[1:]
        if positionals:
            self._issue(
                "OC1102",
                Severity.WARNING,
                f"{name_value} has unsupported positional endpoint syntax; use get_* collections",
                path,
                positionals[0][0],
            )
            tainted = True

        endpoint_groups: dict[str, list[DesignObjectObservation]] = {
            "from": [],
            "to": [],
            "through": [],
        }
        for option in endpoint_options:
            group = (
                "from" if option.endswith("from") else "to" if option.endswith("to") else "through"
            )
            for option_value in options.get(option, []):
                if not option_value.references:
                    self._issue(
                        "OC1102",
                        Severity.WARNING,
                        f"{name_value} {option} must use a statically typed get_* collection",
                        path,
                        command,
                    )
                    tainted = True
                    continue
                endpoint_groups[group].extend(option_value.references)
        endpoints = [item for values in endpoint_groups.values() for item in values]
        tainted = tainted or any(
            item.status != FactState.KNOWN or item.attributes.get("dynamic") for item in endpoints
        )
        clock_names = tuple(item.native_name for item in endpoints if item.kind == "clock")
        self.constraints.append(
            TimingConstraintObservation(
                command=name_value,
                value=value,
                clocks=clock_names,
                from_objects=_reference_names(endpoint_groups["from"]),
                to_objects=_reference_names(endpoint_groups["to"]),
                through_objects=_reference_names(endpoint_groups["through"]),
                provenance=self._location(path, command),
                status=FactState.TAINTED if tainted else FactState.KNOWN,
                attributes={
                    "endpoint_kinds": {
                        key: [item.kind for item in values]
                        for key, values in endpoint_groups.items()
                    },
                    "options": {
                        key: [self._option_value(option_value) for option_value in values]
                        for key, values in sorted(options.items())
                    },
                },
            )
        )

    def _command(
        self,
        command: _Command,
        path: Path,
        *,
        nested: bool,
        context: str,
        depth: int,
    ) -> _Value:
        if not command.words:
            return _Value(text="")
        name_value = self._word(command.words[0], path, context="command", depth=depth)
        name = self._scalar(name_value)
        if not name or name_value.status != FactState.KNOWN:
            self._issue(
                "OC1102",
                Severity.WARNING,
                "Dynamic Tcl command names are unsupported in SDC",
                path,
                command,
            )
            return _Value(status=FactState.UNSUPPORTED, dynamic=True, display=command.words[0].raw)
        normalized = name.lower()
        if normalized in self._GET_KINDS:
            return self._get(normalized, command, path, context=context, depth=depth)
        if normalized == "list":
            return self._list(command, path, depth=depth)
        if normalized == "set":
            return self._set(command, path, depth=depth)
        if normalized == "unset":
            if len(command.words) == 2:
                variable = self._scalar(
                    self._word(command.words[1], path, context="unset", depth=depth)
                )
                if variable:
                    self.variables.pop(variable, None)
                    return _Value(text="")
            self._issue(
                "OC1102",
                Severity.WARNING,
                "Only a single static variable name is supported by Tcl unset",
                path,
                command,
            )
            return _Value(status=FactState.UNSUPPORTED, dynamic=True, display="unset")
        if nested:
            self._issue(
                "OC1102",
                Severity.WARNING,
                f"Unsupported Tcl command substitution {name!r}; it was not executed",
                path,
                command,
                metadata={"command": name},
            )
            return _Value(status=FactState.UNSUPPORTED, dynamic=True, display=f"[{name} ...]")
        if normalized == "create_clock":
            self._create_clock(command, path, generated=False, depth=depth)
            return _Value(text="")
        if normalized == "create_generated_clock":
            self._create_clock(command, path, generated=True, depth=depth)
            return _Value(text="")
        if normalized in {"set_input_delay", "set_output_delay"}:
            self._delay(command, path, depth=depth)
            return _Value(text="")
        if normalized in {"set_false_path", "set_multicycle_path"}:
            self._path_exception(command, path, depth=depth)
            return _Value(text="")
        self._issue(
            "OC1102",
            Severity.WARNING,
            f"Unsupported SDC/Tcl command {name!r}; it was not executed",
            path,
            command,
            metadata={"command": name},
        )
        return _Value(status=FactState.UNSUPPORTED, dynamic=True, display=name)

    def evaluate(self, commands: Sequence[_Command], path: Path) -> None:
        for command in commands:
            self._command(command, path, nested=False, context="top-level", depth=0)


def parse_sdc(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
) -> ViewObservation:
    """Parse static SDC references, clocks, delays, and path exceptions safely."""

    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="sdc", name=view_name)
    evaluator = _Evaluator(view)
    diagnostics: list[Diagnostic] = []
    encodings: dict[str, str] = {}
    complete = True
    tainted: set[str] = set()
    for path in source_paths:
        source = read_source(path, view)
        diagnostics.extend(source.diagnostics)
        encodings[str(path)] = source.encoding
        if not source.text:
            complete = False
            tainted.add("*")
            continue
        text = source.text.replace("\r\n", "\n").replace("\r", "\n")
        if len(text) > _MAX_SCRIPT_CHARACTERS:
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    f"SDC source exceeds {_MAX_SCRIPT_CHARACTERS} characters",
                    location=Provenance(str(path), view=view),
                )
            )
            complete = False
            tainted.add("*")
            continue
        parser = _TclParser(text, path, view)
        try:
            commands = parser.parse()
            if not any(diagnostic.severity == Severity.FATAL for diagnostic in parser.diagnostics):
                evaluator.evaluate(commands, path)
        except RecursionError:
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    "SDC/Tcl source exceeds the parser nesting limit",
                    location=Provenance(str(path), view=view),
                )
            )
            complete = False
            tainted.add("*")
            continue
        diagnostics.extend(parser.diagnostics)
        if source.tainted or parser.diagnostics:
            complete = False
            tainted.add("*")

    diagnostics.extend(evaluator.diagnostics)
    for source_path, location in evaluator.tainted_sources.items():
        diagnostics.append(
            parser_diagnostic(
                "OC1104",
                Severity.WARNING,
                f"SDC analysis of {source_path} is incomplete after static-only recovery",
                location=location,
            )
        )
        complete = False
        tainted.add("*")
    if any(diagnostic.is_failure for diagnostic in diagnostics):
        complete = False
    return ViewObservation(
        view=view,
        diagnostics=tuple(diagnostics),
        complete=complete,
        tainted_scopes=frozenset(tainted),
        objects=tuple(evaluator.objects),
        clocks=tuple(evaluator.clocks),
        attributes={
            "parser": "stdlib-sdc-static",
            "source_files": [str(path) for path in source_paths],
            "encodings": encodings,
            "constraints": tuple(evaluator.constraints),
            "static_variables": tuple(sorted(evaluator.variables)),
            "tcl_execution": False,
        },
    )


class SdcParser:
    format_name = "sdc"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_sdc(paths, view_id=view_id, **options)


__all__ = ["SdcParser", "TimingConstraintObservation", "parse_sdc"]

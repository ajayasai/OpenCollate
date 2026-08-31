"""Bounded structural DEF importer.

The importer extracts design interface, placed instances, nets, and endpoint
references.  Routing and other geometry-heavy payloads are skipped at clause or
section boundaries; they are never reinterpreted as logical connectivity.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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
    IndexRange,
    PinMappingObservation,
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
class DefLimits:
    """Resource limits for untrusted DEF input."""

    max_file_bytes: int = 256 * 1024 * 1024
    max_tokens: int = 5_000_000
    max_token_chars: int = 65_536
    max_section_entries: int = 2_000_000
    max_entry_tokens: int = 500_000
    max_parenthesis_depth: int = 256

    def __post_init__(self) -> None:
        for name in (
            "max_file_bytes",
            "max_tokens",
            "max_token_chars",
            "max_section_entries",
            "max_entry_tokens",
            "max_parenthesis_depth",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    raw: str
    line: int
    column: int
    kind: str = "word"

    @property
    def upper(self) -> str:
        return self.text.upper()


class _Lexer:
    def __init__(self, text: str, path: Path, view: ViewId, limits: DefLimits) -> None:
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.path = path
        self.view = view
        self.limits = limits
        self.index = 0
        self.line = 1
        self.column = 1
        self.depth = 0
        self.complete = True
        self.diagnostics: list[Diagnostic] = []

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

    def _issue(self, code: str, message: str, line: int, column: int) -> None:
        self.complete = False
        self.diagnostics.append(
            parser_diagnostic(
                code,
                Severity.FATAL,
                message,
                location=self._location(line, column),
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

    def _quoted(self, line: int, column: int) -> _Token:
        start = self.index
        self._advance()
        value: list[str] = []
        while self._peek():
            if self._peek() == '"':
                self._advance()
                return _Token(
                    "".join(value),
                    self.text[start : self.index],
                    line,
                    column,
                    "string",
                )
            if self._continuation():
                continue
            if self._peek() == "\\":
                self._advance()
                if self._peek():
                    value.append(self._advance())
                continue
            value.append(self._advance())
            if len(value) > self.limits.max_token_chars:
                self._issue(
                    "OC1102",
                    f"DEF token exceeds {self.limits.max_token_chars:,} characters",
                    line,
                    column,
                )
                break
        self._issue("OC1101", "Unterminated quoted DEF string", line, column)
        return _Token("".join(value), self.text[start : self.index], line, column, "string")

    def _word(self, line: int, column: int) -> _Token:
        start = self.index
        value: list[str] = []
        while self._peek():
            character = self._peek()
            if character.isspace() or character in {'"', "(", ")", ";", "+"}:
                break
            if character == "-" and not value and not self._peek(1).isdigit():
                break
            if self._continuation():
                continue
            if character == "\\":
                self._advance()
                if self._peek():
                    # Keep the escape marker in the semantic spelling so an
                    # escaped literal divider stays distinct from hierarchy.
                    value.append("\\")
                    value.append(self._advance())
            else:
                value.append(self._advance())
            if len(value) > self.limits.max_token_chars:
                self._issue(
                    "OC1102",
                    f"DEF token exceeds {self.limits.max_token_chars:,} characters",
                    line,
                    column,
                )
                break
        return _Token("".join(value), self.text[start : self.index], line, column)

    def scan(self) -> tuple[tuple[_Token, ...], tuple[Diagnostic, ...], bool]:
        tokens: list[_Token] = []
        while self._peek() and len(tokens) < self.limits.max_tokens:
            if self._continuation():
                continue
            character = self._peek()
            if character.isspace():
                self._advance()
                continue
            if character == "#":
                while self._peek() and self._peek() != "\n":
                    self._advance()
                continue
            line, column = self.line, self.column
            if character == '"':
                token = self._quoted(line, column)
            elif character in {"(", ")", ";", "+"} or (
                character == "-" and not self._peek(1).isdigit()
            ):
                raw = self._advance()
                token = _Token(raw, raw, line, column, "punct")
                if raw == "(":
                    self.depth += 1
                    if self.depth > self.limits.max_parenthesis_depth:
                        self._issue(
                            "OC1102",
                            (
                                "DEF parenthesis nesting exceeds "
                                f"{self.limits.max_parenthesis_depth:,}"
                            ),
                            line,
                            column,
                        )
                        break
                elif raw == ")":
                    self.depth -= 1
                    if self.depth < 0:
                        self._issue("OC1101", "Unmatched ')' in DEF", line, column)
                        self.depth = 0
            else:
                token = self._word(line, column)
                if not token.raw:
                    self._issue(
                        "OC1101",
                        f"Cannot tokenize DEF character {character!r}",
                        line,
                        column,
                    )
                    self._advance()
                    continue
            tokens.append(token)
        if len(tokens) >= self.limits.max_tokens and self._peek():
            self._issue(
                "OC1102",
                f"DEF token count exceeds {self.limits.max_tokens:,}",
                self.line,
                self.column,
            )
        if self.depth:
            self._issue("OC1101", "Unclosed parenthesis in DEF", self.line, self.column)
        return tuple(tokens), tuple(self.diagnostics), self.complete


@dataclass(slots=True)
class _Placement:
    status: str
    x: int | None = None
    y: int | None = None
    orientation: str | None = None
    state: FactState = FactState.KNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "x": self.x,
            "y": self.y,
            "orientation": self.orientation,
            "state": self.state.value,
        }


@dataclass(slots=True)
class _ComponentEntry:
    name: str
    macro: str
    provenance: Provenance
    raw_name: str
    placement: _Placement | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: FactState = FactState.KNOWN


@dataclass(slots=True)
class _PinEntry:
    name: str
    provenance: Provenance
    raw_name: str
    net: str | None = None
    direction: Direction = Direction.UNKNOWN
    role: PortRole = PortRole.UNKNOWN
    direction_state: FactState = FactState.UNKNOWN
    role_state: FactState = FactState.UNKNOWN
    placement: _Placement | None = None
    layers: list[str] = field(default_factory=list)
    ignored_clauses: list[str] = field(default_factory=list)
    status: FactState = FactState.KNOWN


@dataclass(frozen=True, slots=True)
class _Connection:
    instance: str | None
    pin: str
    provenance: Provenance
    status: FactState = FactState.KNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance": self.instance,
            "pin": self.pin,
            "state": self.status.value,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(slots=True)
class _NetEntry:
    name: str
    provenance: Provenance
    raw_name: str
    special: bool
    connections: list[_Connection] = field(default_factory=list)
    use: PortRole = PortRole.UNKNOWN
    use_state: FactState = FactState.UNKNOWN
    ignored_clauses: list[str] = field(default_factory=list)
    status: FactState = FactState.KNOWN


_PLACEMENT_STATUSES = {"PLACED", "FIXED", "COVER", "UNPLACED"}
_ORIENTATIONS = {"N", "S", "E", "W", "FN", "FS", "FE", "FW"}

_COMPONENT_CLAUSES = {
    "PLACED",
    "FIXED",
    "COVER",
    "UNPLACED",
    "SOURCE",
    "EEQMASTER",
    "GENERATE",
    "WEIGHT",
    "REGION",
    "MASKSHIFT",
    "HALO",
    "ROUTEHALO",
    "PROPERTY",
    "FOREIGN",
}
_PIN_CLAUSES = {
    "NET",
    "SPECIAL",
    "DIRECTION",
    "USE",
    "PORT",
    "LAYER",
    "PLACED",
    "FIXED",
    "COVER",
    "UNPLACED",
    "PROPERTY",
    "ANTENNAPINPARTIALMETALAREA",
    "ANTENNAPINPARTIALMETALSIDEAREA",
    "ANTENNAPINPARTIALCUTAREA",
    "ANTENNAPINDIFFAREA",
    "ANTENNAMODEL",
}
_NET_CLAUSES = {
    "USE",
    "SOURCE",
    "FIXEDBUMP",
    "FREQUENCY",
    "ORIGINAL",
    "PATTERN",
    "ESTCAP",
    "WEIGHT",
    "XTALK",
    "NONDEFAULTRULE",
    "VPIN",
    "SUBNET",
    "PROPERTY",
    "ROUTED",
    "FIXED",
    "COVER",
    "SHIELD",
    "NOSHIELD",
    "VOLTAGE",
    "SPACING",
    "WIDTH",
}
_IGNORED_SECTIONS = {
    "BLOCKAGES",
    "CONSTRAINTS",
    "FILLS",
    "GROUPS",
    "IOTIMINGS",
    "NONDEFAULTRULES",
    "PARTITIONS",
    "PROPERTYDEFINITIONS",
    "REGIONS",
    "SCANCHAINS",
    "SLOTS",
    "STYLES",
    "VIAS",
}
_SIMPLE_STATEMENTS = {
    "ARRAY",
    "BUSBITCHARS",
    "CANPLACE",
    "CANNOTOCCUPY",
    "DEFAULTCAP",
    "DIEAREA",
    "DIVIDERCHAR",
    "FLOORPLAN",
    "GCELLGRID",
    "HISTORY",
    "NAMESCASESENSITIVE",
    "ROW",
    "TECHNOLOGY",
    "TRACKS",
    "UNITS",
    "VERSION",
}


def _provenance(path: Path, view: ViewId, token: _Token) -> Provenance:
    return Provenance(
        str(path),
        token.line,
        token.column,
        view,
        raw_name=token.raw,
    )


def _clauses(tokens: Sequence[_Token]) -> tuple[list[_Token], list[tuple[_Token, list[_Token]]]]:
    base: list[_Token] = []
    clauses: list[tuple[_Token, list[_Token]]] = []
    active: tuple[_Token, list[_Token]] | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "punct" and token.text == "+":
            if index + 1 >= len(tokens):
                break
            keyword = tokens[index + 1]
            active = (keyword, [])
            clauses.append(active)
            index += 2
            continue
        if active is None:
            base.append(token)
        else:
            active[1].append(token)
        index += 1
    return base, clauses


def _first_word(tokens: Sequence[_Token]) -> _Token | None:
    return next((item for item in tokens if item.kind != "punct"), None)


def _contains_unescaped(text: str, character: str) -> bool:
    escaped = False
    for item in text:
        if escaped:
            escaped = False
        elif item == "\\":
            escaped = True
        elif item == character:
            return True
    return False


class _FileParser:
    def __init__(
        self,
        tokens: Sequence[_Token],
        path: Path,
        view: ViewId,
        limits: DefLimits,
    ) -> None:
        self.tokens = tuple(tokens)
        self.path = path
        self.view = view
        self.limits = limits
        self.index = 0
        self.version: str | None = None
        self.design: str | None = None
        self.design_provenance: Provenance | None = None
        self.dividerchar = "/"
        self.busbitchars = "[]"
        self.units: int | None = None
        self.complete = True
        self.tainted: set[str] = set()
        self.diagnostics: list[Diagnostic] = []
        self.components: list[_ComponentEntry] = []
        self.pins: list[_PinEntry] = []
        self.nets: list[_NetEntry] = []
        self.ignored_sections: dict[str, int | None] = {}
        self.section_counts: dict[str, dict[str, int | None]] = {}
        self.sections_seen: set[str] = set()
        self.ended_design = False

    def _peek(self, offset: int = 0) -> _Token | None:
        position = self.index + offset
        return self.tokens[position] if position < len(self.tokens) else None

    def _advance(self) -> _Token | None:
        token = self._peek()
        if token is not None:
            self.index += 1
        return token

    def _advance_required(self, context: str, *, scope: str = "*") -> _Token | None:
        token = self._advance()
        if token is None:
            self._issue(
                "OC1101",
                Severity.FATAL,
                f"Unexpected end of DEF input while reading {context}",
                scope=scope,
            )
        return token

    def _at(self, text: str, offset: int = 0) -> bool:
        token = self._peek(offset)
        return token is not None and token.upper == text.upper()

    def _location(self, token: _Token | None = None) -> Provenance:
        selected = token or self._peek()
        if selected is None:
            return Provenance(str(self.path), view=self.view)
        return _provenance(self.path, self.view, selected)

    def _issue(
        self,
        code: str,
        severity: Severity,
        message: str,
        *,
        token: _Token | None = None,
        scope: str = "*",
    ) -> None:
        self.complete = False
        self.tainted.add(scope)
        self.diagnostics.append(
            parser_diagnostic(
                code,
                severity,
                message,
                location=self._location(token),
            )
        )

    def _consume_semicolon(self, context: str) -> bool:
        if self._at(";"):
            self._advance()
            return True
        self._issue(
            "OC1101",
            Severity.FATAL,
            f"DEF {context} is missing terminating ';'",
        )
        return False

    def _statement(self) -> list[_Token]:
        result: list[_Token] = []
        while self._peek() is not None and not self._at(";"):
            token = self._advance_required("statement")
            if token is None:
                break
            result.append(token)
        self._consume_semicolon("statement")
        return result

    def _metadata_statement(self, keyword: _Token) -> None:
        values = self._statement()
        first = _first_word(values)
        if keyword.upper == "VERSION" and first is not None:
            self.version = first.text
        elif keyword.upper == "DIVIDERCHAR" and first is not None:
            if len(first.text) != 1:
                self._issue(
                    "OC1101",
                    Severity.FATAL,
                    f"DEF DIVIDERCHAR must contain one character, found {first.text!r}",
                    token=first,
                )
            else:
                self.dividerchar = first.text
        elif keyword.upper == "BUSBITCHARS" and first is not None:
            if len(first.text) != 2:
                self._issue(
                    "OC1101",
                    Severity.FATAL,
                    f"DEF BUSBITCHARS must contain two characters, found {first.text!r}",
                    token=first,
                )
            else:
                self.busbitchars = first.text
        elif keyword.upper == "UNITS":
            numeric = next(
                (item for item in reversed(values) if item.text.lstrip("-").isdigit()),
                None,
            )
            if numeric is not None:
                self.units = int(numeric.text)

    def _design_statement(self, keyword: _Token) -> None:
        values = self._statement()
        name = _first_word(values)
        if name is None:
            self._issue(
                "OC1101",
                Severity.FATAL,
                "DEF DESIGN statement has no design name",
                token=keyword,
            )
            return
        if self.design is not None and self.design != name.text:
            self._issue(
                "OC1101",
                Severity.FATAL,
                f"DEF declares both DESIGN {self.design!r} and {name.text!r}",
                token=name,
            )
        self.design = name.text
        self.design_provenance = _provenance(self.path, self.view, name)

    def _section_header(self, section: _Token) -> int | None:
        values = self._statement()
        first = _first_word(values)
        if first is None or not first.text.isdigit():
            if section.upper != "PROPERTYDEFINITIONS":
                self._issue(
                    "OC1101",
                    Severity.FATAL,
                    f"DEF {section.upper} section has an invalid entry count",
                    token=first or section,
                    scope=self.design or "*",
                )
            return None
        return int(first.text)

    def _read_entry(self, section: str) -> list[_Token] | None:
        marker = self._advance_required(f"{section} entry", scope=self.design or "*")
        if marker is None:
            return None
        result: list[_Token] = []
        while self._peek() is not None and not self._at(";"):
            if self._at("END") and self._at(section, 1):
                self._issue(
                    "OC1101",
                    Severity.FATAL,
                    f"DEF {section} entry is missing terminating ';'",
                    token=marker,
                    scope=self.design or "*",
                )
                return None
            if len(result) >= self.limits.max_entry_tokens:
                self._issue(
                    "OC1102",
                    Severity.FATAL,
                    (f"DEF {section} entry exceeds {self.limits.max_entry_tokens:,} tokens"),
                    token=marker,
                    scope="*",
                )
                while self._peek() is not None and not self._at(";"):
                    self._advance()
                break
            token = self._advance_required(f"{section} entry", scope=self.design or "*")
            if token is None:
                return None
            result.append(token)
        self._consume_semicolon(f"{section} entry")
        return result

    def _skip_to_section_end(self, section: str) -> int:
        count = 0
        while self._peek() is not None:
            if self._at("END") and self._at(section, 1):
                self._advance()
                self._advance()
                if self._at(";"):
                    self._advance()
                return count
            if self._at("-"):
                count += 1
            self._advance()
        self._issue(
            "OC1101",
            Severity.FATAL,
            f"DEF {section} section is missing END {section}",
            scope=self.design or "*",
        )
        return count

    def _section(self, section_token: _Token) -> None:
        section = section_token.upper
        self.sections_seen.add(section)
        # PROPERTYDEFINITIONS is the one standard section without an entry-count
        # header.  Its first definition must not be consumed as a fictitious
        # header statement.
        declared = None if section == "PROPERTYDEFINITIONS" else self._section_header(section_token)
        if section in _IGNORED_SECTIONS:
            actual = self._skip_to_section_end(section)
            self.ignored_sections[section] = declared
            self.section_counts[section] = {"declared": declared, "parsed": actual}
            return
        actual = 0
        while self._peek() is not None:
            if self._at("END") and self._at(section, 1):
                self._advance()
                self._advance()
                if self._at(";"):
                    self._advance()
                break
            if not self._at("-"):
                unexpected = self._advance_required(f"{section} section", scope=self.design or "*")
                if unexpected is None:
                    break
                self._issue(
                    "OC1101",
                    Severity.FATAL,
                    f"Unexpected token {unexpected.text!r} in DEF {section} section",
                    token=unexpected,
                    scope=self.design or "*",
                )
                while (
                    self._peek() is not None
                    and not self._at(";")
                    and not (self._at("END") and self._at(section, 1))
                ):
                    self._advance()
                if self._at(";"):
                    self._advance()
                continue
            if actual >= self.limits.max_section_entries:
                self._issue(
                    "OC1102",
                    Severity.FATAL,
                    (f"DEF {section} section exceeds {self.limits.max_section_entries:,} entries"),
                    scope="*",
                )
                self._skip_to_section_end(section)
                break
            entry = self._read_entry(section)
            actual += 1
            if entry is None:
                continue
            if section == "COMPONENTS":
                self._component_entry(entry)
            elif section == "PINS":
                self._pin_entry(entry)
            else:
                self._net_entry(entry, special=section == "SPECIALNETS")
        else:
            self._issue(
                "OC1101",
                Severity.FATAL,
                f"DEF {section} section is missing END {section}",
                scope=self.design or "*",
            )
        self.section_counts[section] = {"declared": declared, "parsed": actual}
        if declared is not None and declared != actual:
            self._issue(
                "OC1101",
                Severity.FATAL,
                f"DEF {section} declares {declared} entries but contains {actual}",
                token=section_token,
                scope=self.design or "*",
            )

    def _placement(self, keyword: _Token, body: Sequence[_Token]) -> _Placement:
        status = keyword.upper.lower()
        if keyword.upper == "UNPLACED" and not body:
            return _Placement(status)
        open_index = next(
            (index for index, item in enumerate(body) if item.kind == "punct" and item.text == "("),
            None,
        )
        if open_index is None or open_index + 3 >= len(body):
            self._issue(
                "OC1101",
                Severity.FATAL,
                f"DEF {keyword.upper} placement is missing '( x y )'",
                token=keyword,
                scope=self.design or "*",
            )
            return _Placement(status, state=FactState.TAINTED)
        x_token, y_token, close = body[open_index + 1 : open_index + 4]
        if close.text != ")":
            self._issue(
                "OC1101",
                Severity.FATAL,
                f"DEF {keyword.upper} placement has malformed coordinates",
                token=keyword,
                scope=self.design or "*",
            )
            return _Placement(status, state=FactState.TAINTED)
        try:
            x, y = int(x_token.text), int(y_token.text)
        except ValueError:
            self._issue(
                "OC1101",
                Severity.FATAL,
                f"DEF placement coordinates must be integers, found {x_token.text} {y_token.text}",
                token=x_token,
                scope=self.design or "*",
            )
            return _Placement(status, state=FactState.TAINTED)
        orientation_token = _first_word(body[open_index + 4 :])
        orientation = orientation_token.upper if orientation_token is not None else None
        state = FactState.KNOWN
        if orientation is None or orientation not in _ORIENTATIONS:
            self._issue(
                "OC1102",
                Severity.WARNING,
                f"DEF placement has unsupported orientation {orientation!r}",
                token=orientation_token or keyword,
                scope=self.design or "*",
            )
            state = FactState.TAINTED
        return _Placement(status, x, y, orientation, state)

    def _component_entry(self, tokens: Sequence[_Token]) -> None:
        base, clauses = _clauses(tokens)
        words = [item for item in base if item.kind != "punct"]
        if len(words) < 2:
            self._issue(
                "OC1101",
                Severity.FATAL,
                "DEF COMPONENTS entry requires an instance and macro name",
                token=words[0] if words else None,
                scope=self.design or "*",
            )
            return
        entry = _ComponentEntry(
            words[0].text,
            words[1].text,
            _provenance(self.path, self.view, words[0]),
            words[0].raw,
        )
        ignored: list[str] = []
        for keyword, body in clauses:
            if keyword.upper in _PLACEMENT_STATUSES:
                placement = self._placement(keyword, body)
                if entry.placement is not None:
                    self._issue(
                        "OC1101",
                        Severity.FATAL,
                        f"DEF component {entry.name} has multiple placement clauses",
                        token=keyword,
                        scope=self.design or "*",
                    )
                    entry.status = FactState.TAINTED
                entry.placement = placement
                if placement.state != FactState.KNOWN:
                    entry.status = FactState.TAINTED
                continue
            if keyword.upper not in _COMPONENT_CLAUSES:
                self._issue(
                    "OC1102",
                    Severity.WARNING,
                    f"Unsupported DEF COMPONENTS clause + {keyword.text}",
                    token=keyword,
                    scope=entry.name,
                )
                entry.status = FactState.TAINTED
            ignored.append(keyword.upper)
        entry.attributes["ignored_clauses"] = ignored
        self.components.append(entry)

    def _pin_entry(self, tokens: Sequence[_Token]) -> None:
        base, clauses = _clauses(tokens)
        name_token = _first_word(base)
        if name_token is None:
            self._issue(
                "OC1101",
                Severity.FATAL,
                "DEF PINS entry has no pin name",
                scope=self.design or "*",
            )
            return
        entry = _PinEntry(
            name_token.text,
            _provenance(self.path, self.view, name_token),
            name_token.raw,
        )
        for keyword, body in clauses:
            value = _first_word(body)
            if keyword.upper == "NET":
                if value is None:
                    self._issue(
                        "OC1101",
                        Severity.FATAL,
                        f"DEF pin {entry.name} has + NET without a net name",
                        token=keyword,
                        scope=self.design or "*",
                    )
                    entry.status = FactState.TAINTED
                else:
                    entry.net = value.text
                continue
            if keyword.upper == "DIRECTION":
                parsed_direction = Direction.parse(value.text if value is not None else None)
                if parsed_direction == Direction.UNKNOWN:
                    raw_value = value.text if value is not None else None
                    self._issue(
                        "OC1102",
                        Severity.WARNING,
                        (f"DEF pin {entry.name} has unsupported DIRECTION {raw_value!r}"),
                        token=value or keyword,
                        scope=self.design or "*",
                    )
                    entry.direction_state = FactState.UNSUPPORTED
                    entry.status = FactState.TAINTED
                else:
                    entry.direction = parsed_direction
                    entry.direction_state = FactState.KNOWN
                continue
            if keyword.upper == "USE":
                parsed_role = self._role(value.text if value is not None else None)
                if parsed_role == PortRole.UNKNOWN:
                    raw_value = value.text if value is not None else None
                    self._issue(
                        "OC1102",
                        Severity.WARNING,
                        f"DEF pin {entry.name} has unsupported USE {raw_value!r}",
                        token=value or keyword,
                        scope=self.design or "*",
                    )
                    entry.role_state = FactState.UNSUPPORTED
                    entry.status = FactState.TAINTED
                else:
                    entry.role = parsed_role
                    entry.role_state = FactState.KNOWN
                continue
            if keyword.upper in _PLACEMENT_STATUSES:
                placement = self._placement(keyword, body)
                if entry.placement is not None:
                    self._issue(
                        "OC1101",
                        Severity.FATAL,
                        f"DEF pin {entry.name} has multiple placement clauses",
                        token=keyword,
                        scope=self.design or "*",
                    )
                    entry.status = FactState.TAINTED
                entry.placement = placement
                if placement.state != FactState.KNOWN:
                    entry.status = FactState.TAINTED
                continue
            if keyword.upper == "LAYER" and value is not None:
                entry.layers.append(value.text)
                continue
            if keyword.upper not in _PIN_CLAUSES:
                self._issue(
                    "OC1102",
                    Severity.WARNING,
                    f"Unsupported DEF PINS clause + {keyword.text}",
                    token=keyword,
                    scope=self.design or "*",
                )
                entry.status = FactState.TAINTED
            entry.ignored_clauses.append(keyword.upper)
        self.pins.append(entry)

    @staticmethod
    def _role(value: str | None) -> PortRole:
        normalized = (value or "").strip().upper()
        return {
            "SIGNAL": PortRole.SIGNAL,
            "POWER": PortRole.POWER,
            "GROUND": PortRole.GROUND,
            "CLOCK": PortRole.CLOCK,
            "ANALOG": PortRole.ANALOG,
            "TIEOFF": PortRole.TIE,
            "RESET": PortRole.RESET,
            "SCAN": PortRole.SIGNAL,
        }.get(normalized, PortRole.UNKNOWN)

    def _connections(self, tokens: Sequence[_Token]) -> list[_Connection]:
        result: list[_Connection] = []
        index = 1
        while index < len(tokens):
            if tokens[index].kind == "punct" and tokens[index].text == "+":
                break
            if tokens[index].kind != "punct" or tokens[index].text != "(":
                index += 1
                continue
            open_token = tokens[index]
            index += 1
            group: list[_Token] = []
            while index < len(tokens) and not (
                tokens[index].kind == "punct" and tokens[index].text == ")"
            ):
                group.append(tokens[index])
                index += 1
            if index >= len(tokens):
                self._issue(
                    "OC1101",
                    Severity.FATAL,
                    "Unclosed DEF net connection group",
                    token=open_token,
                    scope=self.design or "*",
                )
                break
            index += 1
            words = [item for item in group if item.kind != "punct"]
            synthesized = next((i for i, item in enumerate(words) if item.text == "+"), None)
            if synthesized is not None:
                words = words[:synthesized]
            if len(words) < 2:
                self._issue(
                    "OC1101",
                    Severity.FATAL,
                    "DEF net connection requires an instance/PIN and pin name",
                    token=open_token,
                    scope=self.design or "*",
                )
                continue
            location = _provenance(self.path, self.view, words[0])
            if words[0].upper == "PIN":
                result.append(_Connection(None, words[1].text, location))
            elif words[0].upper == "MUSTJOIN" and len(words) >= 3:
                result.append(_Connection(words[1].text, words[2].text, location))
            else:
                status = FactState.TAINTED if words[0].text == "*" else FactState.KNOWN
                result.append(_Connection(words[0].text, words[1].text, location, status))
        return result

    def _net_entry(self, tokens: Sequence[_Token], *, special: bool) -> None:
        name_token = _first_word(tokens)
        if name_token is None:
            self._issue(
                "OC1101",
                Severity.FATAL,
                "DEF net entry has no net name",
                scope=self.design or "*",
            )
            return
        entry = _NetEntry(
            name_token.text,
            _provenance(self.path, self.view, name_token),
            name_token.raw,
            special,
        )
        entry.connections.extend(self._connections(tokens))
        _, clauses = _clauses(tokens)
        for keyword, body in clauses:
            value = _first_word(body)
            if keyword.upper == "USE":
                role = self._role(value.text if value is not None else None)
                if role == PortRole.UNKNOWN:
                    raw_value = value.text if value is not None else None
                    self._issue(
                        "OC1102",
                        Severity.WARNING,
                        f"DEF net {entry.name} has unsupported USE {raw_value!r}",
                        token=value or keyword,
                        scope=self.design or "*",
                    )
                    entry.use_state = FactState.UNSUPPORTED
                    entry.status = FactState.TAINTED
                else:
                    entry.use = role
                    entry.use_state = FactState.KNOWN
                continue
            if keyword.upper not in _NET_CLAUSES:
                self._issue(
                    "OC1102",
                    Severity.WARNING,
                    f"Unsupported DEF net clause + {keyword.text}",
                    token=keyword,
                    scope=self.design or "*",
                )
                entry.status = FactState.TAINTED
            entry.ignored_clauses.append(keyword.upper)
        self.nets.append(entry)

    def parse(self) -> None:
        supported_sections = {"COMPONENTS", "PINS", "NETS", "SPECIALNETS"}
        while self._peek() is not None:
            if self._at("END") and self._at("DESIGN", 1):
                self._advance()
                self._advance()
                if self._at(";"):
                    self._advance()
                self.ended_design = True
                break
            keyword = self._advance_required("top-level statement", scope=self.design or "*")
            if keyword is None:
                break
            if keyword.upper == "DESIGN":
                self._design_statement(keyword)
            elif keyword.upper in supported_sections | _IGNORED_SECTIONS:
                self._section(keyword)
            elif keyword.upper in _SIMPLE_STATEMENTS:
                self._metadata_statement(keyword)
            elif keyword.upper == "BEGINEXT":
                self._issue(
                    "OC1102",
                    Severity.WARNING,
                    "DEF BEGINEXT vendor extension is not interpreted",
                    token=keyword,
                    scope=self.design or "*",
                )
                while self._peek() is not None and not self._at("ENDEXT"):
                    self._advance()
                if self._at("ENDEXT"):
                    self._advance()
            else:
                self._issue(
                    "OC1102",
                    Severity.WARNING,
                    f"Unsupported DEF top-level statement {keyword.text!r}",
                    token=keyword,
                    scope=self.design or "*",
                )
                self._statement()
        if self.design is None:
            self._issue(
                "OC1101",
                Severity.FATAL,
                "DEF file has no DESIGN statement",
            )
        elif not self.ended_design:
            self._issue(
                "OC1101",
                Severity.FATAL,
                "DEF file is missing END DESIGN",
                scope=self.design,
            )

    def _deduplicate(self) -> None:
        for label, entries in (
            ("component", self.components),
            ("pin", self.pins),
            ("net", self.nets),
        ):
            seen: dict[str, Any] = {}
            for entry in entries:
                if entry.name in seen:
                    entry.status = FactState.TAINTED
                    seen[entry.name].status = FactState.TAINTED
                    self._issue(
                        "OC1104",
                        Severity.WARNING,
                        f"Duplicate DEF {label} definition {entry.name!r}",
                        token=None,
                        scope=self.design or "*",
                    )
                else:
                    seen[entry.name] = entry

    def _reconcile_pin_nets(self) -> None:
        connected: dict[str, set[str]] = {}
        net_roles: dict[str, tuple[PortRole, FactState]] = {}
        for net in self.nets:
            net_roles[net.name] = (net.use, net.use_state)
            for connection in net.connections:
                if connection.instance is None:
                    connected.setdefault(connection.pin, set()).add(net.name)
        for pin in self.pins:
            nets = connected.get(pin.name, set())
            if pin.net is None and len(nets) == 1:
                pin.net = next(iter(nets))
            elif pin.net is not None and nets and nets != {pin.net}:
                pin.status = FactState.TAINTED
                self._issue(
                    "OC1104",
                    Severity.WARNING,
                    (
                        f"DEF pin {pin.name} names net {pin.net!r}, but NETS/SPECIALNETS "
                        f"connect it to {', '.join(sorted(nets))}"
                    ),
                    scope=self.design or "*",
                )
            if pin.role == PortRole.UNKNOWN and pin.net in net_roles:
                role, state = net_roles[pin.net]
                if role != PortRole.UNKNOWN:
                    pin.role, pin.role_state = role, state

    def finish(self, *, encoding: str) -> ViewObservation:
        self._deduplicate()
        self._reconcile_pin_nets()
        design = self.design
        if design is None:
            return ViewObservation(
                self.view,
                diagnostics=tuple(self.diagnostics),
                complete=False,
                tainted_scopes=frozenset(self.tainted or {"*"}),
                attributes=self._attributes(encoding),
            )

        ports, mappings = self._ports_and_mappings(design)
        components: tuple[ComponentObservation, ...] = ()
        # A floorplan-only DEF may legally omit PINS. Only a present PINS
        # section (including PINS 0) authoritatively describes the interface.
        if "PINS" in self.sections_seen:
            components = (
                ComponentObservation(
                    native_name=design,
                    kind=ComponentKind.MODULE,
                    ports=ports,
                    provenance=self.design_provenance,
                    status=(
                        FactState.TAINTED
                        if design in self.tainted or "*" in self.tainted
                        else FactState.KNOWN
                    ),
                    attributes={
                        "def_design": True,
                        "dividerchar": self.dividerchar,
                        "busbitchars": self.busbitchars,
                        "units_per_micron": self.units,
                    },
                ),
            )
        objects = self._objects(design)
        return ViewObservation(
            view=self.view,
            components=components,
            diagnostics=tuple(self.diagnostics),
            complete=self.complete,
            tainted_scopes=frozenset(self.tainted),
            pin_mappings=mappings,
            objects=objects,
            attributes=self._attributes(encoding),
        )

    def _attributes(self, encoding: str) -> dict[str, Any]:
        return {
            "parser": "native-def-structural",
            "source": str(self.path),
            "encoding": encoding,
            "version": self.version,
            "design": self.design,
            "dividerchar": self.dividerchar,
            "busbitchars": self.busbitchars,
            "units_per_micron": self.units,
            "section_counts": self.section_counts,
            "ignored_sections": self.ignored_sections,
        }

    def _pin_parts(self, name: str) -> tuple[str, int | tuple[int, int] | None]:
        opening, closing = map(re.escape, self.busbitchars)
        ranged = re.fullmatch(rf"(.*?){opening}(-?\d+):(-?\d+){closing}", name)
        if ranged:
            return ranged.group(1), (int(ranged.group(2)), int(ranged.group(3)))
        bit = re.fullmatch(rf"(.*?){opening}(-?\d+){closing}", name)
        if bit:
            return bit.group(1), int(bit.group(2))
        return name, None

    def _ports_and_mappings(
        self,
        design: str,
    ) -> tuple[tuple[PortObservation, ...], tuple[PinMappingObservation, ...]]:
        grouped: dict[str, list[tuple[_PinEntry, int | tuple[int, int] | None]]] = {}
        order: list[str] = []
        mappings: list[PinMappingObservation] = []
        for pin in self.pins:
            base, index = self._pin_parts(pin.name)
            if base not in grouped:
                grouped[base] = []
                order.append(base)
            grouped[base].append((pin, index))
        ports: list[PortObservation] = []
        for name in order:
            entries = grouped[name]
            rows = [entry for entry, _ in entries]
            directions = {entry.direction for entry in rows}
            roles = {entry.role for entry in rows}
            direction = next(iter(directions)) if len(directions) == 1 else Direction.UNKNOWN
            role = next(iter(roles)) if len(roles) == 1 else PortRole.UNKNOWN
            status = (
                FactState.TAINTED
                if any(entry.status != FactState.KNOWN for entry in rows)
                else FactState.KNOWN
            )
            field_states: dict[str, FactState] = {}
            if len(directions) != 1:
                field_states["direction"] = FactState.TAINTED
                status = FactState.TAINTED
                self._issue(
                    "OC1104",
                    Severity.WARNING,
                    f"DEF bits of pin {name} disagree on DIRECTION",
                    scope=design,
                )
            else:
                field_states["direction"] = rows[0].direction_state
            if len(roles) != 1:
                field_states["role"] = FactState.TAINTED
                status = FactState.TAINTED
                self._issue(
                    "OC1104",
                    Severity.WARNING,
                    f"DEF bits of pin {name} disagree on USE",
                    scope=design,
                )
            else:
                role_state = rows[0].role_state
                if role == PortRole.UNKNOWN and role_state == FactState.UNKNOWN:
                    role, role_state = infer_role_from_name(name)
                field_states["role"] = role_state
            ranges = [index for _, index in entries if isinstance(index, tuple)]
            bits = [index for _, index in entries if isinstance(index, int)]
            if len(entries) == 1 and ranges:
                left, right = ranges[0]
                span = IndexRange(left, right)
                shape = BusShape(
                    left=left,
                    right=right,
                    packed=(span,),
                    bit_indices=span.ordered_indices,
                    explicit_scalar=False,
                )
            elif len(bits) == len(entries):
                # PINS entry order is inventory order, not a declaration of
                # vector endianness.  Keep a deterministic index set and let
                # the comparison engine treat its ordering as unspecified.
                unique = tuple(sorted(set(bits)))
                shape = BusShape(width=len(unique), bit_indices=unique, explicit_scalar=False)
                if len(unique) != len(bits):
                    field_states["shape"] = FactState.TAINTED
                    status = FactState.TAINTED
            elif len(entries) == 1 and entries[0][1] is None:
                shape = BusShape.scalar()
            else:
                shape = BusShape.unknown()
                field_states["shape"] = FactState.TAINTED
                status = FactState.TAINTED
            ports.append(
                PortObservation(
                    native_name=name,
                    direction=direction,
                    role=role,
                    shape=shape,
                    provenance=rows[0].provenance,
                    status=status,
                    field_states=field_states,
                    attributes={
                        "bit_order_known": not (len(bits) == len(entries) and len(entries) > 1),
                        "def_pin_names": [entry.name for entry in rows],
                        "nets": [entry.net for entry in rows],
                        "placements": [
                            entry.placement.to_dict() if entry.placement else None for entry in rows
                        ],
                        "layers": [list(entry.layers) for entry in rows],
                    },
                )
            )
        return tuple(ports), tuple(mappings)

    def _objects(self, design: str) -> tuple[DesignObjectObservation, ...]:
        objects: list[DesignObjectObservation] = [
            DesignObjectObservation(
                kind="design",
                native_name=design,
                provenance=self.design_provenance,
                status=(
                    FactState.TAINTED
                    if design in self.tainted or "*" in self.tainted
                    else FactState.KNOWN
                ),
                attributes={"source_format": "def"},
            )
        ]
        for component_entry in self.components:
            placement = component_entry.placement.to_dict() if component_entry.placement else None
            objects.append(
                DesignObjectObservation(
                    kind="instance",
                    native_name=component_entry.name,
                    scope=design,
                    provenance=component_entry.provenance,
                    status=component_entry.status,
                    attributes={
                        "component_type": component_entry.macro,
                        "placement": placement,
                        "hierarchical": _contains_unescaped(component_entry.name, self.dividerchar),
                        "def_raw_name": component_entry.raw_name,
                        **component_entry.attributes,
                    },
                )
            )
        for pin_entry in self.pins:
            objects.append(
                DesignObjectObservation(
                    kind="pin",
                    native_name=pin_entry.name,
                    scope=design,
                    provenance=pin_entry.provenance,
                    status=pin_entry.status,
                    attributes={
                        "net": pin_entry.net,
                        "direction": pin_entry.direction.value,
                        "use": pin_entry.role.value,
                        "placement": (
                            pin_entry.placement.to_dict() if pin_entry.placement else None
                        ),
                        "layers": list(pin_entry.layers),
                        "def_raw_name": pin_entry.raw_name,
                    },
                )
            )
        for net_entry in self.nets:
            objects.append(
                DesignObjectObservation(
                    kind="net",
                    native_name=net_entry.name,
                    scope=design,
                    provenance=net_entry.provenance,
                    status=net_entry.status,
                    attributes={
                        "special": net_entry.special,
                        "use": net_entry.use.value,
                        "connections": [item.to_dict() for item in net_entry.connections],
                        "ignored_clauses": list(net_entry.ignored_clauses),
                        "def_raw_name": net_entry.raw_name,
                    },
                )
            )
            for connection in net_entry.connections:
                attributes = {
                    "net": net_entry.name,
                    "special_net": net_entry.special,
                }
                if connection.instance is None:
                    objects.append(
                        DesignObjectObservation(
                            kind="pin",
                            native_name=connection.pin,
                            relation="reference",
                            scope=design,
                            provenance=connection.provenance,
                            status=connection.status,
                            attributes={**attributes, "endpoint_type": "top_pin"},
                        )
                    )
                else:
                    objects.append(
                        DesignObjectObservation(
                            kind="instance",
                            native_name=connection.instance,
                            relation="reference",
                            scope=design,
                            provenance=connection.provenance,
                            status=connection.status,
                            attributes=attributes,
                        )
                    )
                    objects.append(
                        DesignObjectObservation(
                            kind="pin",
                            native_name=f"{connection.instance}/{connection.pin}",
                            relation="reference",
                            scope=design,
                            provenance=connection.provenance,
                            status=connection.status,
                            attributes={**attributes, "instance": connection.instance},
                        )
                    )
        return tuple(objects)


def _oversize_view(path: Path, view: ViewId, limits: DefLimits, size: int) -> ViewObservation:
    diagnostic = parser_diagnostic(
        "OC1102",
        Severity.FATAL,
        f"DEF file {path} is {size:,} bytes; limit is {limits.max_file_bytes:,}",
        location=Provenance(str(path), view=view),
        help="Split the DEF or raise the explicit parser resource limit after review.",
    )
    return ViewObservation(
        view=view,
        diagnostics=(diagnostic,),
        complete=False,
        tainted_scopes=frozenset({"*"}),
        attributes={"parser": "native-def-structural", "source": str(path)},
    )


def parse_def(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    limits: DefLimits | None = None,
) -> ViewObservation:
    """Parse logical and placement facts from one or more DEF files."""

    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="def", name=view_name)
    selected_limits = limits or DefLimits()
    observations: list[ViewObservation] = []
    for path in source_paths:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > selected_limits.max_file_bytes:
            observations.append(_oversize_view(path, view, selected_limits, size))
            continue
        source = read_source(path, view)
        if not source.text:
            observations.append(
                ViewObservation(
                    view=view,
                    diagnostics=source.diagnostics,
                    complete=False,
                    tainted_scopes=frozenset({"*"}),
                    attributes={"parser": "native-def-structural", "source": str(path)},
                )
            )
            continue
        lexer = _Lexer(source.text, path, view, selected_limits)
        tokens, lexer_diagnostics, lex_complete = lexer.scan()
        parser = _FileParser(tokens, path, view, selected_limits)
        parser.diagnostics.extend(source.diagnostics)
        parser.diagnostics.extend(lexer_diagnostics)
        if source.tainted or not lex_complete:
            parser.complete = False
            parser.tainted.add("*")
        parser.parse()
        observations.append(parser.finish(encoding=source.encoding))

    components = tuple(item for observation in observations for item in observation.components)
    diagnostics = tuple(item for observation in observations for item in observation.diagnostics)
    objects = tuple(item for observation in observations for item in observation.objects)
    mappings = tuple(item for observation in observations for item in observation.pin_mappings)
    complete = all(observation.complete for observation in observations)
    tainted = frozenset(
        scope for observation in observations for scope in observation.tainted_scopes
    )
    return ViewObservation(
        view=view,
        components=components,
        diagnostics=diagnostics,
        complete=complete,
        tainted_scopes=tainted,
        pin_mappings=mappings,
        objects=objects,
        attributes={
            "parser": "native-def-structural",
            "source_files": [str(path) for path in source_paths],
            "files": [dict(observation.attributes) for observation in observations],
        },
    )


class DefParser:
    format_name = "def"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_def(paths, view_id=view_id, **options)


__all__ = ["DefLimits", "DefParser", "parse_def"]

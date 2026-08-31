"""Focused LEF macro/pin importer with resilient unknown-statement handling."""

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


@dataclass(slots=True)
class _Pin:
    name: str
    line: int
    column: int
    direction: str | None = None
    use: str | None = None


@dataclass(slots=True)
class _Macro:
    name: str
    line: int
    column: int
    pins: list[_Pin] = field(default_factory=list)
    tainted: bool = False


def _without_comments(text: str) -> tuple[str, bool]:
    """Blank LEF comments while retaining line and column offsets."""

    result = list(text)
    index = 0
    unterminated = False
    in_block = False
    in_string = False
    escaped = False
    while index < len(result):
        if in_block:
            if index + 1 < len(result) and result[index] == "*" and result[index + 1] == "/":
                result[index] = result[index + 1] = " "
                index += 2
                in_block = False
                continue
            if result[index] not in {"\r", "\n"}:
                result[index] = " "
            index += 1
            continue
        if result[index] == '"' and not escaped:
            in_string = not in_string
            index += 1
            continue
        if in_string:
            escaped = result[index] == "\\" and not escaped
            if result[index] != "\\":
                escaped = False
            index += 1
            continue
        escaped = False
        if index + 1 < len(result) and result[index] == "/" and result[index + 1] == "*":
            result[index] = result[index + 1] = " "
            index += 2
            in_block = True
            continue
        if index + 1 < len(result) and result[index] == "/" and result[index + 1] == "/":
            while index < len(result) and result[index] not in {"\r", "\n"}:
                result[index] = " "
                index += 1
            continue
        if result[index] == "#":
            while index < len(result) and result[index] not in {"\r", "\n"}:
                result[index] = " "
                index += 1
            continue
        index += 1
    if in_block:
        unterminated = True
    return "".join(result), unterminated


def _unquote(value: str) -> str:
    stripped = value.strip().rstrip(";").strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        return stripped[1:-1]
    return stripped


def _direction(value: str | None) -> Direction:
    if value is None:
        return Direction.UNKNOWN
    # LEF permits the qualifier ``OUTPUT TRISTATE``; the electrical qualifier
    # does not change the interface direction.
    normalized = value.strip().upper().split()[0]
    return {
        "INPUT": Direction.INPUT,
        "OUTPUT": Direction.OUTPUT,
        "INOUT": Direction.INOUT,
        "FEEDTHRU": Direction.FEEDTHROUGH,
        "FEEDTHROUGH": Direction.FEEDTHROUGH,
    }.get(normalized, Direction.UNKNOWN)


def _role(value: str | None, name: str) -> tuple[PortRole, FactState]:
    if value is not None:
        normalized = value.strip().upper()
        explicit = {
            "SIGNAL": PortRole.SIGNAL,
            "CLOCK": PortRole.CLOCK,
            "POWER": PortRole.POWER,
            "GROUND": PortRole.GROUND,
            "ANALOG": PortRole.ANALOG,
        }.get(normalized)
        if explicit is not None:
            return explicit, FactState.KNOWN
    return infer_role_from_name(name)


def _pin_parts(name: str, bus_chars: tuple[str, str]) -> tuple[str, int | tuple[int, int] | None]:
    opening, closing = map(re.escape, bus_chars)
    range_match = re.fullmatch(
        rf"(.*?){opening}\s*(-?\d+)\s*:\s*(-?\d+)\s*{closing}",
        name,
    )
    if range_match:
        return range_match.group(1), (int(range_match.group(2)), int(range_match.group(3)))
    bit_match = re.fullmatch(rf"(.*?){opening}\s*(-?\d+)\s*{closing}", name)
    if bit_match:
        return bit_match.group(1), int(bit_match.group(2))
    return name, None


def _macro_component(
    macro: _Macro,
    path: Path,
    view: ViewId,
    bus_chars: tuple[str, str],
) -> tuple[ComponentObservation, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    grouped: dict[str, list[tuple[_Pin, int | tuple[int, int] | None]]] = {}
    order: list[str] = []
    for pin in macro.pins:
        base, indices = _pin_parts(pin.name, bus_chars)
        if base not in grouped:
            grouped[base] = []
            order.append(base)
        grouped[base].append((pin, indices))

    ports: list[PortObservation] = []
    for name in order:
        entries = grouped[name]
        first = entries[0][0]
        directions = {_direction(pin.direction) for pin, _ in entries}
        roles = {_role(pin.use, name)[0] for pin, _ in entries}
        direction = next(iter(directions)) if len(directions) == 1 else Direction.UNKNOWN
        role = next(iter(roles)) if len(roles) == 1 else PortRole.UNKNOWN
        role_state = _role(first.use, name)[1] if len(roles) == 1 else FactState.TAINTED
        field_states: dict[str, FactState] = {"role": role_state}
        location = Provenance(str(path), first.line, first.column, view, raw_name=first.name)
        if len(directions) != 1:
            field_states["direction"] = FactState.TAINTED
            diagnostics.append(
                parser_diagnostic(
                    "OC1104",
                    Severity.WARNING,
                    f"LEF bits of {macro.name}/{name} disagree on DIRECTION",
                    location=location,
                )
            )
        elif direction == Direction.UNKNOWN:
            field_states["direction"] = FactState.UNKNOWN
        if len(roles) != 1:
            field_states["role"] = FactState.TAINTED
            diagnostics.append(
                parser_diagnostic(
                    "OC1104",
                    Severity.WARNING,
                    f"LEF bits of {macro.name}/{name} disagree on USE",
                    location=location,
                )
            )

        ranges = [value for _, value in entries if isinstance(value, tuple)]
        bits = [value for _, value in entries if isinstance(value, int)]
        if ranges and len(entries) == 1:
            left, right = ranges[0]
            index_range = IndexRange(left, right)
            shape = BusShape(
                left=left,
                right=right,
                packed=(index_range,),
                bit_indices=index_range.ordered_indices,
                explicit_scalar=False,
            )
        elif bits and len(bits) == len(entries):
            unique_bits = tuple(dict.fromkeys(bits))
            shape = BusShape(
                width=len(unique_bits),
                bit_indices=unique_bits,
                explicit_scalar=False,
            )
            if len(unique_bits) != len(bits):
                field_states["shape"] = FactState.TAINTED
                diagnostics.append(
                    parser_diagnostic(
                        "OC1104",
                        Severity.WARNING,
                        f"LEF macro {macro.name} repeats a physical pin bit of {name}",
                        location=location,
                    )
                )
        elif len(entries) == 1 and entries[0][1] is None:
            shape = BusShape.scalar()
        else:
            shape = BusShape.unknown()
            field_states["shape"] = FactState.TAINTED
            diagnostics.append(
                parser_diagnostic(
                    "OC1104",
                    Severity.WARNING,
                    f"LEF declarations for {macro.name}/{name} mix scalar and bus syntax",
                    location=location,
                )
            )
        ports.append(
            PortObservation(
                native_name=name,
                direction=direction,
                role=role,
                shape=shape,
                provenance=location,
                attributes={
                    "lef_pin_names": [pin.name for pin, _ in entries],
                    "use": first.use,
                    "role_source": (
                        "name_heuristic"
                        if role_state == FactState.TAINTED
                        else "explicit"
                        if role_state == FactState.KNOWN
                        else "unknown"
                    ),
                },
                field_states=field_states,
            )
        )
    return (
        ComponentObservation(
            native_name=macro.name,
            kind=ComponentKind.MACRO,
            ports=tuple(ports),
            provenance=Provenance(str(path), macro.line, macro.column, view, raw_name=macro.name),
            attributes={"busbitchars": "".join(bus_chars)},
            status=FactState.TAINTED if macro.tainted else FactState.KNOWN,
        ),
        diagnostics,
    )


def _parse_lef_file(
    text: str,
    path: Path,
    view: ViewId,
) -> tuple[list[ComponentObservation], list[Diagnostic], set[str], dict[str, Any]]:
    cleaned, unterminated_comment = _without_comments(text)
    diagnostics: list[Diagnostic] = []
    tainted: set[str] = set()
    if unterminated_comment:
        diagnostics.append(
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                "Unterminated LEF block comment",
                location=Provenance(str(path), view=view),
            )
        )
        tainted.add("*")

    bus_chars = ("[", "]")
    divider_char = "/"
    version: str | None = None
    macro: _Macro | None = None
    pin: _Pin | None = None
    pending: tuple[_Pin, str, str, int, int] | None = None
    macros: list[_Macro] = []
    for line_number, raw_line in enumerate(cleaned.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        column = len(raw_line) - len(raw_line.lstrip()) + 1
        if pending is not None:
            pending_pin, key, value, start_line, start_column = pending
            combined = f"{value} {stripped}".strip()
            if ";" not in combined:
                pending = (pending_pin, key, combined, start_line, start_column)
                continue
            completed, stripped = combined.split(";", 1)
            if key == "DIRECTION":
                pending_pin.direction = completed.strip()
            else:
                pending_pin.use = completed.strip()
            pending = None
            stripped = stripped.strip()
            if not stripped:
                continue
        bus_match = re.match(r'^BUSBITCHARS\s+("[^"]*"|\S+)\s*;', stripped, re.I)
        if bus_match:
            value = _unquote(bus_match.group(1))
            if len(value) == 2:
                bus_chars = (value[0], value[1])
            else:
                diagnostics.append(
                    parser_diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        f"LEF BUSBITCHARS must contain two characters, found {value!r}",
                        location=Provenance(str(path), line_number, column, view),
                    )
                )
            continue
        divider_match = re.match(r'^DIVIDERCHAR\s+("[^"]*"|\S+)\s*;', stripped, re.I)
        if divider_match:
            divider_char = _unquote(divider_match.group(1))
            continue
        version_match = re.match(r"^VERSION\s+([^;]+);", stripped, re.I)
        if version_match:
            version = version_match.group(1).strip()
            continue
        macro_match = re.match(r"^MACRO\s+(\S+)", stripped, re.I)
        if macro_match:
            if macro is not None:
                macro.tainted = True
                macros.append(macro)
                diagnostics.append(
                    parser_diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        f"LEF macro {macro.name!r} was not closed before the next MACRO",
                        location=Provenance(str(path), line_number, column, view),
                    )
                )
                tainted.add(macro.name)
            macro = _Macro(macro_match.group(1).rstrip(";"), line_number, column)
            pin = None
            continue
        pin_match = re.match(r"^PIN\s+(\S+)", stripped, re.I)
        if pin_match:
            if macro is None:
                diagnostics.append(
                    parser_diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        "LEF PIN appears outside a MACRO",
                        location=Provenance(str(path), line_number, column, view),
                    )
                )
                tainted.add("*")
                continue
            if pin is not None:
                macro.tainted = True
                diagnostics.append(
                    parser_diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        f"LEF pin {pin.name!r} was not closed before the next PIN",
                        location=Provenance(str(path), line_number, column, view),
                    )
                )
            pin = _Pin(pin_match.group(1).rstrip(";"), line_number, column)
            macro.pins.append(pin)
            continue
        if pin is not None:
            for match in re.finditer(
                r"(?:^|;)\s*(DIRECTION|USE)\s+([^;]+);",
                stripped,
                re.I,
            ):
                key, value = match.group(1).upper(), match.group(2).strip()
                if key == "DIRECTION":
                    pin.direction = value
                else:
                    pin.use = value
            pending_match = re.match(r"^(DIRECTION|USE)\b(.*)$", stripped, re.I)
            if pending_match and ";" not in pending_match.group(2):
                pending = (
                    pin,
                    pending_match.group(1).upper(),
                    pending_match.group(2).strip(),
                    line_number,
                    column,
                )
                continue
        end_match = re.match(r"^END(?:\s+(\S+))?\s*$", stripped, re.I)
        if not end_match:
            continue
        end_name = end_match.group(1)
        if pin is not None and end_name == pin.name:
            pin = None
            continue
        if macro is not None and end_name == macro.name:
            if pin is not None:
                macro.tainted = True
                diagnostics.append(
                    parser_diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        f"LEF macro {macro.name!r} ended before pin {pin.name!r}",
                        location=Provenance(str(path), line_number, column, view),
                    )
                )
                pin = None
            macros.append(macro)
            macro = None
            continue
        # Bare END closes PORT/OBS and does not affect the pin/macro scopes.
        if end_name is None:
            continue
        if pin is not None or macro is not None:
            if pin is not None:
                active = pin.name
            elif macro is not None:
                active = macro.name
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    f"LEF END {end_name} does not match active scope {active}",
                    location=Provenance(str(path), line_number, column, view),
                )
            )
            if macro is not None:
                macro.tainted = True

    if pending is not None:
        pending_pin, key, _, start_line, start_column = pending
        diagnostics.append(
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                f"LEF {key} for pin {pending_pin.name!r} is missing ';'",
                location=Provenance(str(path), start_line, start_column, view),
            )
        )
        if macro is not None:
            macro.tainted = True
    if macro is not None:
        macro.tainted = True
        macros.append(macro)
        tainted.add(macro.name)
        diagnostics.append(
            parser_diagnostic(
                "OC1101",
                Severity.FATAL,
                f"LEF macro {macro.name!r} is missing END {macro.name}",
                location=Provenance(str(path), macro.line, macro.column, view),
            )
        )

    components: list[ComponentObservation] = []
    for item in macros:
        component, item_diags = _macro_component(item, path, view, bus_chars)
        components.append(component)
        diagnostics.extend(item_diags)
        if item.tainted:
            tainted.add(item.name)
    return (
        components,
        diagnostics,
        tainted,
        {
            "version": version,
            "busbitchars": "".join(bus_chars),
            "dividerchar": divider_char,
        },
    )


def parse_lef(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
) -> ViewObservation:
    """Parse LEF MACRO/PIN interface declarations and skip geometry safely."""

    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="lef", name=view_name)
    components: list[ComponentObservation] = []
    diagnostics: list[Diagnostic] = []
    tainted: set[str] = set()
    file_metadata: dict[str, Any] = {}
    complete = True
    for path in source_paths:
        source = read_source(path, view)
        diagnostics.extend(source.diagnostics)
        if not source.text:
            complete = False
            tainted.add("*")
            continue
        parsed, file_diags, file_tainted, metadata = _parse_lef_file(
            source.text,
            path,
            view,
        )
        components.extend(parsed)
        diagnostics.extend(file_diags)
        tainted.update(file_tainted)
        file_metadata[str(path)] = {**metadata, "encoding": source.encoding}
        if source.tainted or file_diags:
            complete = False
            if source.tainted:
                tainted.add("*")

    names: set[str] = set()
    for component in components:
        if component.native_name in names:
            diagnostics.append(
                parser_diagnostic(
                    "OC1104",
                    Severity.WARNING,
                    f"Duplicate LEF macro definition {component.native_name!r}",
                    location=component.provenance,
                )
            )
            tainted.add(component.native_name)
        names.add(component.native_name)
    return ViewObservation(
        view=view,
        components=tuple(components),
        diagnostics=tuple(diagnostics),
        complete=complete,
        tainted_scopes=frozenset(tainted),
        attributes={
            "parser": "stdlib-lef",
            "source_files": [str(path) for path in source_paths],
            "files": file_metadata,
        },
    )


class LefParser:
    format_name = "lef"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_lef(paths, view_id=view_id, **options)


__all__ = ["LefParser", "parse_lef"]

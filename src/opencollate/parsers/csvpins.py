"""String-preserving CSV package/pin-map importer."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from opencollate.diagnostics import Diagnostic, Severity
from opencollate.model import (
    BusShape,
    ComponentKind,
    ComponentObservation,
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


def _header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


_ALIASES: dict[str, str] = {}
for _canonical, _names in {
    "signal": {
        "signal",
        "signal_name",
        "net",
        "net_name",
        "port",
        "port_name",
        "pin",
        "pin_name",
    },
    "die_pad": {"die_pad", "die_pin", "diepad", "pad", "pad_name", "bond_pad"},
    "package_ball": {
        "package_ball",
        "package_pin",
        "package_pad",
        "ball",
        "ball_id",
        "ball_name",
        "bga_ball",
    },
    "component": {"component", "device", "part", "module", "cell", "chip"},
    "direction": {"direction", "dir", "io", "io_type", "pin_direction"},
    "role": {"role", "use", "pin_type", "signal_type", "function"},
    "range": {"range", "bus_range", "vector", "indices"},
    "width": {"width", "bus_width", "bits"},
    "left": {"left", "msb", "from", "bit_from"},
    "right": {"right", "lsb", "to", "bit_to"},
    "bit": {"bit", "index", "bit_index"},
    "domain": {"domain", "power_domain", "voltage_domain"},
}.items():
    for _name in _names:
        _ALIASES[_name] = _canonical


_RANGE = re.compile(r"^[\[<(]?\s*(-?\d+)\s*:\s*(-?\d+)\s*[\])>]?$")
_SIGNAL_RANGE = re.compile(r"^(.*?)[\[<]\s*(-?\d+)\s*:\s*(-?\d+)\s*[\]>]$")
_SIGNAL_BIT = re.compile(r"^(.*?)[\[<]\s*(-?\d+)\s*[\]>]$")


@dataclass(frozen=True, slots=True)
class _PortRow:
    component: str
    name: str
    direction: Direction
    direction_state: FactState
    role: PortRole
    role_state: FactState
    range_value: tuple[int, int] | None
    width: int | None
    bit: int | None
    explicit_scalar: bool
    shape_state: FactState
    provenance: Provenance
    attributes: Mapping[str, Any]


def _integer(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"[+-]?\d+", value.strip()):
        return None
    return int(value)


def _direction(value: str | None) -> Direction:
    if value is None:
        return Direction.UNKNOWN
    normalized = value.strip().lower().replace(" ", "")
    aliases = {
        "i": Direction.INPUT,
        "o": Direction.OUTPUT,
        "b": Direction.INOUT,
        "io": Direction.INOUT,
        "i/o": Direction.INOUT,
    }
    return aliases.get(normalized, Direction.parse(value))


def _role(value: str | None) -> PortRole:
    if value is None:
        return PortRole.UNKNOWN
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "pwr": PortRole.POWER,
        "vdd": PortRole.POWER,
        "vcc": PortRole.POWER,
        "gnd": PortRole.GROUND,
        "vss": PortRole.GROUND,
        "rst": PortRole.RESET,
        "rst_n": PortRole.RESET,
        "reset_n": PortRole.RESET,
    }
    return aliases.get(normalized, PortRole.parse(value))


def _row_shape(
    values: Mapping[str, str],
    signal: str,
) -> tuple[str, tuple[int, int] | None, int | None, int | None, bool]:
    name = signal.strip()
    signal_range = _SIGNAL_RANGE.fullmatch(name)
    if signal_range:
        name = signal_range.group(1)
        return name, (int(signal_range.group(2)), int(signal_range.group(3))), None, None, False
    signal_bit = _SIGNAL_BIT.fullmatch(name)
    inferred_bit = int(signal_bit.group(2)) if signal_bit else None
    if signal_bit:
        name = signal_bit.group(1)
    range_text = values.get("range", "").strip()
    range_match = _RANGE.fullmatch(range_text) if range_text else None
    if range_match:
        return name, (int(range_match.group(1)), int(range_match.group(2))), None, None, False
    left, right = _integer(values.get("left")), _integer(values.get("right"))
    if left is not None and right is not None:
        return name, (left, right), None, None, False
    bit = _integer(values.get("bit"))
    if bit is None:
        bit = inferred_bit
    if bit is not None:
        return name, None, None, bit, False
    width = _integer(values.get("width"))
    if width is not None:
        return name, None, width, None, width == 1
    return name, None, None, None, True


def _resolve_headers(
    headers: Sequence[str],
    column_map: Mapping[str, str],
) -> tuple[list[str | None], list[str]]:
    custom = {_header(source): _header(target) for source, target in column_map.items()}
    resolved: list[str | None] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for raw in headers:
        normalized = _header(raw)
        canonical = custom.get(normalized, _ALIASES.get(normalized))
        if canonical in seen:
            duplicates.append(canonical)
        if canonical is not None:
            seen.add(canonical)
        resolved.append(canonical)
    return resolved, duplicates


def _read_rows(
    reader: Any,
) -> tuple[list[tuple[int, list[str]]], csv.Error | None]:
    rows: list[tuple[int, list[str]]] = []
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return rows, None
        except csv.Error as error:
            return rows, error
        rows.append((max(2, reader.line_num), row))


def _build_ports(
    rows: Sequence[_PortRow],
    diagnostics: list[Diagnostic],
) -> tuple[ComponentObservation, ...]:
    components: dict[str, dict[str, list[_PortRow]]] = {}
    component_order: list[str] = []
    port_order: dict[str, list[str]] = {}
    for row in rows:
        if row.component not in components:
            components[row.component] = {}
            component_order.append(row.component)
            port_order[row.component] = []
        if row.name not in components[row.component]:
            components[row.component][row.name] = []
            port_order[row.component].append(row.name)
        components[row.component][row.name].append(row)

    result: list[ComponentObservation] = []
    for component_name in component_order:
        ports: list[PortObservation] = []
        component_tainted = False
        first_component_row = components[component_name][port_order[component_name][0]][0]
        for name in port_order[component_name]:
            entries = components[component_name][name]
            first = entries[0]
            directions = {entry.direction for entry in entries}
            roles = {entry.role for entry in entries}
            direction = next(iter(directions)) if len(directions) == 1 else Direction.UNKNOWN
            role = next(iter(roles)) if len(roles) == 1 else PortRole.UNKNOWN
            field_states: dict[str, FactState] = {}
            if len(directions) != 1:
                field_states["direction"] = FactState.TAINTED
                component_tainted = True
                diagnostics.append(
                    parser_diagnostic(
                        "OC5006",
                        Severity.ERROR,
                        f"Pin-map rows for {component_name}/{name} disagree on direction",
                        location=first.provenance,
                    )
                )
            else:
                field_states["direction"] = first.direction_state
            if len(roles) != 1:
                field_states["role"] = FactState.TAINTED
                component_tainted = True
                diagnostics.append(
                    parser_diagnostic(
                        "OC5006",
                        Severity.ERROR,
                        f"Pin-map rows for {component_name}/{name} disagree on role/use",
                        location=first.provenance,
                    )
                )
            else:
                field_states["role"] = first.role_state

            row_shape_tainted = any(entry.shape_state != FactState.KNOWN for entry in entries)
            ranges = {entry.range_value for entry in entries if entry.range_value is not None}
            widths = {entry.width for entry in entries if entry.width is not None}
            bits = tuple(entry.bit for entry in entries if entry.bit is not None)
            if len(ranges) == 1 and not bits:
                left, right = next(iter(ranges))
                index_range = IndexRange(left, right)
                shape = BusShape(
                    left=left,
                    right=right,
                    packed=(index_range,),
                    bit_indices=index_range.ordered_indices,
                    explicit_scalar=False,
                )
            elif bits and not ranges:
                unique_bits = tuple(dict.fromkeys(bits))
                shape = BusShape(
                    width=len(unique_bits),
                    bit_indices=unique_bits,
                    explicit_scalar=False,
                )
                if len(unique_bits) != len(bits):
                    field_states["shape"] = FactState.TAINTED
                    component_tainted = True
                    diagnostics.append(
                        parser_diagnostic(
                            "OC5006",
                            Severity.ERROR,
                            f"Pin map repeats bit indices for {component_name}/{name}",
                            location=first.provenance,
                        )
                    )
            elif len(widths) == 1 and not ranges:
                shape = BusShape(width=next(iter(widths)), explicit_scalar=False)
            elif all(entry.explicit_scalar for entry in entries) and not row_shape_tainted:
                shape = BusShape.scalar()
            else:
                shape = BusShape.unknown()
                field_states["shape"] = FactState.TAINTED
                component_tainted = True
                if not row_shape_tainted:
                    diagnostics.append(
                        parser_diagnostic(
                            "OC5006",
                            Severity.ERROR,
                            (
                                f"Pin-map rows for {component_name}/{name} have "
                                "conflicting bus syntax"
                            ),
                            location=first.provenance,
                        )
                    )
            if row_shape_tainted:
                field_states["shape"] = FactState.TAINTED
                component_tainted = True
            ports.append(
                PortObservation(
                    native_name=name,
                    direction=direction,
                    role=role,
                    shape=shape,
                    provenance=first.provenance,
                    attributes={
                        "row_count": len(entries),
                        "domains": sorted(
                            {
                                str(entry.attributes.get("domain"))
                                for entry in entries
                                if entry.attributes.get("domain")
                            }
                        ),
                    },
                    field_states=field_states,
                )
            )
        result.append(
            ComponentObservation(
                native_name=component_name,
                kind=ComponentKind.UNKNOWN,
                ports=tuple(ports),
                provenance=first_component_row.provenance,
                status=FactState.TAINTED if component_tainted else FactState.KNOWN,
                attributes={"source": "pin-map"},
            )
        )
    return tuple(result)


def parse_pin_csv(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    component_name: str | None = None,
    delimiter: str | None = None,
    column_map: Mapping[str, str] | None = None,
) -> ViewObservation:
    """Parse one or more package pin maps with RFC-4180 quoting and BOM support.

    ``column_map`` maps source header spelling to a canonical field name such
    as ``signal``, ``die_pad``, ``package_ball``, ``component``, ``direction``,
    ``role``, ``range``, ``width``, ``left``, ``right``, ``bit``, or ``domain``.
    """

    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="csv", name=view_name)
    custom_columns = column_map or {}
    diagnostics: list[Diagnostic] = []
    port_rows: list[_PortRow] = []
    mappings: list[PinMappingObservation] = []
    tainted: set[str] = set()
    complete = True
    dialects: dict[str, str] = {}
    for path in source_paths:
        source = read_source(path, view)
        diagnostics.extend(source.diagnostics)
        if not source.text:
            complete = False
            tainted.add("*")
            continue
        selected_delimiter = delimiter
        if selected_delimiter is None:
            try:
                selected_delimiter = (
                    csv.Sniffer()
                    .sniff(
                        source.text[:65536],
                        delimiters=",;\t|",
                    )
                    .delimiter
                )
            except csv.Error:
                selected_delimiter = ","
        dialects[str(path)] = selected_delimiter
        reader = csv.reader(
            io.StringIO(source.text, newline=""),
            delimiter=selected_delimiter,
        )
        try:
            headers = next(reader)
        except StopIteration:
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    f"Pin-map CSV {path} is empty",
                    location=Provenance(str(path), view=view),
                )
            )
            complete = False
            tainted.add("*")
            continue
        resolved, duplicates = _resolve_headers(headers, custom_columns)
        if duplicates:
            diagnostics.append(
                parser_diagnostic(
                    "OC5006",
                    Severity.ERROR,
                    (
                        f"Pin-map CSV {path} has duplicate logical columns: "
                        f"{', '.join(sorted(set(duplicates)))}"
                    ),
                    location=Provenance(str(path), view=view),
                )
            )
            complete = False
            tainted.add("*")
        if "signal" not in resolved:
            diagnostics.append(
                parser_diagnostic(
                    "OC5006",
                    Severity.ERROR,
                    f"Pin-map CSV {path} has no signal/pin-name column",
                    location=Provenance(str(path), view=view),
                )
            )
            complete = False
            tainted.add("*")
            continue

        data_rows, csv_error = _read_rows(reader)
        for row_line, raw_row in data_rows:
            if not raw_row or not any(value.strip() for value in raw_row):
                continue
            location = Provenance(str(path), row_line, 1, view)
            if len(raw_row) != len(headers):
                diagnostics.append(
                    parser_diagnostic(
                        "OC5006",
                        Severity.ERROR,
                        (
                            f"Pin-map row {row_line} has {len(raw_row)} fields; "
                            f"header has {len(headers)}"
                        ),
                        location=location,
                    )
                )
                complete = False
                tainted.add("*")
                if len(raw_row) < len(headers):
                    raw_row = [*raw_row, *("" for _ in range(len(headers) - len(raw_row)))]
                else:
                    raw_row = raw_row[: len(headers)]
            values: dict[str, str] = {}
            raw_values: dict[str, str] = {}
            for raw_header, canonical, value in zip(headers, resolved, raw_row, strict=True):
                raw_values[raw_header] = value
                if canonical is not None and canonical not in values:
                    values[canonical] = value.strip()
            signal = values.get("signal", "").strip()
            row_component = values.get("component", "").strip() or component_name or path.stem
            if not signal:
                diagnostics.append(
                    parser_diagnostic(
                        "OC5006",
                        Severity.ERROR,
                        f"Pin-map row {row_line} has no signal name",
                        location=location,
                    )
                )
                complete = False
                tainted.add(row_component)
                mappings.append(
                    PinMappingObservation(
                        die_pad=values.get("die_pad") or None,
                        package_ball=values.get("package_ball") or None,
                        signal=None,
                        component=row_component,
                        provenance=location,
                        attributes={"row": raw_values},
                        status=FactState.TAINTED,
                    )
                )
                continue

            direction_text = values.get("direction")
            direction = _direction(direction_text)
            direction_state = (
                FactState.KNOWN
                if direction != Direction.UNKNOWN
                else FactState.UNKNOWN
                if not direction_text
                else FactState.UNSUPPORTED
            )
            if direction_state == FactState.UNSUPPORTED:
                diagnostics.append(
                    parser_diagnostic(
                        "OC5006",
                        Severity.ERROR,
                        f"Pin-map row {row_line} has unknown direction {direction_text!r}",
                        location=location,
                    )
                )
                complete = False
                tainted.add(row_component)
            role_text = values.get("role")
            role = _role(role_text)
            if role != PortRole.UNKNOWN:
                role_state = FactState.KNOWN
            elif role_text:
                role_state = FactState.UNSUPPORTED
            else:
                role, role_state = infer_role_from_name(signal)
            if role_state == FactState.UNSUPPORTED:
                diagnostics.append(
                    parser_diagnostic(
                        "OC5006",
                        Severity.ERROR,
                        f"Pin-map row {row_line} has unknown role/use {role_text!r}",
                        location=location,
                    )
                )
                complete = False
                tainted.add(row_component)
            name, range_value, width, bit, explicit_scalar = _row_shape(values, signal)
            shape_state = FactState.KNOWN
            invalid_numeric = {
                field: text.strip()
                for field in ("width", "bit", "left", "right")
                if (text := values.get(field, "")).strip() and _integer(text) is None
            }
            if invalid_numeric:
                rendered = ", ".join(
                    f"{field}={value!r}" for field, value in invalid_numeric.items()
                )
                diagnostics.append(
                    parser_diagnostic(
                        "OC5006",
                        Severity.ERROR,
                        f"Pin-map row {row_line} has nonnumeric dimension value: {rendered}",
                        location=location,
                    )
                )
                range_value = None
                width = None
                bit = None
                explicit_scalar = False
                shape_state = FactState.TAINTED
                complete = False
                tainted.add(row_component)
            if width is not None and width < 1:
                diagnostics.append(
                    parser_diagnostic(
                        "OC5006",
                        Severity.ERROR,
                        f"Pin-map row {row_line} has invalid width {width}",
                        location=location,
                    )
                )
                width = None
                explicit_scalar = False
                shape_state = FactState.TAINTED
                complete = False
                tainted.add(row_component)
            raw_range = values.get("range", "").strip()
            if raw_range and _RANGE.fullmatch(raw_range) is None:
                diagnostics.append(
                    parser_diagnostic(
                        "OC5006",
                        Severity.ERROR,
                        f"Pin-map row {row_line} has malformed bus range {raw_range!r}",
                        location=location,
                    )
                )
                range_value = None
                explicit_scalar = False
                shape_state = FactState.TAINTED
                complete = False
                tainted.add(row_component)
            if not invalid_numeric and bool(values.get("left")) != bool(values.get("right")):
                diagnostics.append(
                    parser_diagnostic(
                        "OC5006",
                        Severity.ERROR,
                        f"Pin-map row {row_line} must specify both left/MSB and right/LSB",
                        location=location,
                    )
                )
                range_value = None
                explicit_scalar = False
                shape_state = FactState.TAINTED
                complete = False
                tainted.add(row_component)
            port_rows.append(
                _PortRow(
                    component=row_component,
                    name=name,
                    direction=direction,
                    direction_state=direction_state,
                    role=role,
                    role_state=role_state,
                    range_value=range_value,
                    width=width,
                    bit=bit,
                    explicit_scalar=explicit_scalar,
                    shape_state=shape_state,
                    provenance=location,
                    attributes={**values, "row": raw_values},
                )
            )
            mappings.append(
                PinMappingObservation(
                    die_pad=values.get("die_pad") or None,
                    package_ball=values.get("package_ball") or None,
                    signal=signal,
                    component=row_component,
                    direction=direction,
                    role=role,
                    provenance=location,
                    attributes={"domain": values.get("domain"), "row": raw_values},
                    status=(
                        FactState.UNSUPPORTED
                        if direction_state == FactState.UNSUPPORTED
                        or role_state == FactState.UNSUPPORTED
                        else FactState.TAINTED
                        if shape_state != FactState.KNOWN
                        else FactState.KNOWN
                    ),
                )
            )
        if csv_error is not None:
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    f"Cannot parse pin-map CSV {path}: {csv_error}",
                    location=Provenance(str(path), max(1, reader.line_num), 1, view),
                )
            )
            complete = False
            tainted.add("*")

    components = _build_ports(port_rows, diagnostics)
    for component in components:
        if component.status == FactState.TAINTED:
            tainted.add(component.native_name)
    if any(diagnostic.is_failure for diagnostic in diagnostics):
        complete = False
    return ViewObservation(
        view=view,
        components=components,
        diagnostics=tuple(diagnostics),
        complete=complete,
        tainted_scopes=frozenset(tainted),
        pin_mappings=tuple(mappings),
        attributes={
            "parser": "stdlib-csv",
            "source_files": [str(path) for path in source_paths],
            "dialects": dialects,
        },
    )


class CsvPinMapParser:
    format_name = "csv"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_pin_csv(paths, view_id=view_id, **options)


__all__ = ["CsvPinMapParser", "parse_pin_csv"]

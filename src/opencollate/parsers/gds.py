"""Production-safe structural GDSII stream importer.

This parser reads the native big-endian record stream directly.  It validates
container framing and hierarchy, extracts cell references and text labels, and
streams past geometry without constructing polygons.  Text labels only become
ports when the caller explicitly opts in with layer and/or text-type filters.
"""

from __future__ import annotations

import math
import struct
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
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
from opencollate.parsers.base import Pathish, coerce_paths, coerce_view, parser_diagnostic

_MAX_SOURCE_BYTES = 512 * 1024 * 1024
_MAX_RECORD_BYTES = 65_534
_MAX_RECORDS = 10_000_000
_MAX_STRUCTURES = 1_000_000
_MAX_ELEMENTS = 10_000_000
_MAX_REFERENCES_PER_STRUCTURE = 1_000_000
_MAX_TEXTS_PER_STRUCTURE = 1_000_000
_MAX_XY_POINTS = 1_000_000
_MAX_NAME_BYTES = 16_384
_MAX_TEXT_BYTES = 65_530
_MAX_SKIPPED_RECORDS_PER_ELEMENT = 100_000

_NODATA = 0x00
_BITARRAY = 0x01
_INT2 = 0x02
_INT4 = 0x03
_REAL4 = 0x04
_REAL8 = 0x05
_ASCII = 0x06

_HEADER = 0x00
_BGNLIB = 0x01
_LIBNAME = 0x02
_UNITS = 0x03
_ENDLIB = 0x04
_BGNSTR = 0x05
_STRNAME = 0x06
_ENDSTR = 0x07
_BOUNDARY = 0x08
_PATH = 0x09
_SREF = 0x0A
_AREF = 0x0B
_TEXT = 0x0C
_LAYER = 0x0D
_DATATYPE = 0x0E
_WIDTH = 0x0F
_XY = 0x10
_ENDEL = 0x11
_SNAME = 0x12
_COLROW = 0x13
_TEXTNODE = 0x14
_NODE = 0x15
_TEXTTYPE = 0x16
_PRESENTATION = 0x17
_SPACING = 0x18
_STRING = 0x19
_STRANS = 0x1A
_MAG = 0x1B
_ANGLE = 0x1C
_UINTEGER = 0x1D
_USTRING = 0x1E
_REFLIBS = 0x1F
_FONTS = 0x20
_PATHTYPE = 0x21
_GENERATIONS = 0x22
_ATTRTABLE = 0x23
_STYPTABLE = 0x24
_STRTYPE = 0x25
_ELFLAGS = 0x26
_ELKEY = 0x27
_LINKTYPE = 0x28
_LINKKEYS = 0x29
_NODETYPE = 0x2A
_PROPATTR = 0x2B
_PROPVALUE = 0x2C
_BOX = 0x2D
_BOXTYPE = 0x2E
_PLEX = 0x2F
_BGNEXTN = 0x30
_ENDEXTN = 0x31
_TAPENUM = 0x32
_TAPECODE = 0x33
_STRCLASS = 0x34
_RESERVED = 0x35
_FORMAT = 0x36
_MASK = 0x37
_ENDMASKS = 0x38
_LIBDIRSIZE = 0x39
_SRFNAME = 0x3A
_LIBSECUR = 0x3B

_RECORD_NAMES = {
    _HEADER: "HEADER",
    _BGNLIB: "BGNLIB",
    _LIBNAME: "LIBNAME",
    _UNITS: "UNITS",
    _ENDLIB: "ENDLIB",
    _BGNSTR: "BGNSTR",
    _STRNAME: "STRNAME",
    _ENDSTR: "ENDSTR",
    _BOUNDARY: "BOUNDARY",
    _PATH: "PATH",
    _SREF: "SREF",
    _AREF: "AREF",
    _TEXT: "TEXT",
    _LAYER: "LAYER",
    _DATATYPE: "DATATYPE",
    _WIDTH: "WIDTH",
    _XY: "XY",
    _ENDEL: "ENDEL",
    _SNAME: "SNAME",
    _COLROW: "COLROW",
    _TEXTNODE: "TEXTNODE",
    _NODE: "NODE",
    _TEXTTYPE: "TEXTTYPE",
    _PRESENTATION: "PRESENTATION",
    _SPACING: "SPACING",
    _STRING: "STRING",
    _STRANS: "STRANS",
    _MAG: "MAG",
    _ANGLE: "ANGLE",
    _UINTEGER: "UINTEGER",
    _USTRING: "USTRING",
    _REFLIBS: "REFLIBS",
    _FONTS: "FONTS",
    _PATHTYPE: "PATHTYPE",
    _GENERATIONS: "GENERATIONS",
    _ATTRTABLE: "ATTRTABLE",
    _STYPTABLE: "STYPTABLE",
    _STRTYPE: "STRTYPE",
    _ELFLAGS: "ELFLAGS",
    _ELKEY: "ELKEY",
    _LINKTYPE: "LINKTYPE",
    _LINKKEYS: "LINKKEYS",
    _NODETYPE: "NODETYPE",
    _PROPATTR: "PROPATTR",
    _PROPVALUE: "PROPVALUE",
    _BOX: "BOX",
    _BOXTYPE: "BOXTYPE",
    _PLEX: "PLEX",
    _BGNEXTN: "BGNEXTN",
    _ENDEXTN: "ENDEXTN",
    _TAPENUM: "TAPENUM",
    _TAPECODE: "TAPECODE",
    _STRCLASS: "STRCLASS",
    _RESERVED: "RESERVED",
    _FORMAT: "FORMAT",
    _MASK: "MASK",
    _ENDMASKS: "ENDMASKS",
    _LIBDIRSIZE: "LIBDIRSIZE",
    _SRFNAME: "SRFNAME",
    _LIBSECUR: "LIBSECUR",
}

_GEOMETRY_BEGIN = {
    _BOUNDARY: "boundary",
    _PATH: "path",
    _TEXTNODE: "textnode",
    _NODE: "node",
    _BOX: "box",
}

_KNOWN_SKIPPABLE = {
    _DATATYPE,
    _WIDTH,
    _PRESENTATION,
    _SPACING,
    _STRANS,
    _MAG,
    _ANGLE,
    _UINTEGER,
    _USTRING,
    _REFLIBS,
    _FONTS,
    _PATHTYPE,
    _GENERATIONS,
    _ATTRTABLE,
    _STYPTABLE,
    _STRTYPE,
    _ELFLAGS,
    _ELKEY,
    _LINKTYPE,
    _LINKKEYS,
    _NODETYPE,
    _PROPATTR,
    _PROPVALUE,
    _BOXTYPE,
    _PLEX,
    _BGNEXTN,
    _ENDEXTN,
    _TAPENUM,
    _TAPECODE,
    _STRCLASS,
    _RESERVED,
    _FORMAT,
    _MASK,
    _ENDMASKS,
    _LIBDIRSIZE,
    _SRFNAME,
    _LIBSECUR,
}


@dataclass(frozen=True, slots=True)
class _Record:
    index: int
    offset: int
    length: int
    record_type: int
    data_type: int
    payload: bytes

    @property
    def name(self) -> str:
        return _RECORD_NAMES.get(self.record_type, f"RECORD_0x{self.record_type:02X}")


@dataclass(slots=True)
class _ElementBuilder:
    kind: str
    provenance: Provenance
    record_index: int
    byte_offset: int
    status: FactState = FactState.KNOWN
    layer: int | None = None
    text_type: int | None = None
    xy: tuple[tuple[int, int], ...] | None = None
    string: str | None = None
    string_provenance: Provenance | None = None
    target: str | None = None
    target_provenance: Provenance | None = None
    columns: int | None = None
    rows: int | None = None
    transform: dict[str, Any] = field(default_factory=dict)
    skipped_records: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)

    def taint(self) -> None:
        self.status = FactState.TAINTED


@dataclass(frozen=True, slots=True)
class _TextLabel:
    value: str
    provenance: Provenance
    layer: int | None
    text_type: int | None
    xy: tuple[int, int] | None
    status: FactState
    attributes: Mapping[str, Any]


@dataclass(slots=True)
class _StructureBuilder:
    provenance: Provenance
    record_index: int
    byte_offset: int
    name: str | None = None
    name_provenance: Provenance | None = None
    references: list[DesignObjectObservation] = field(default_factory=list)
    texts: list[_TextLabel] = field(default_factory=list)
    skipped_geometry: int = 0
    status: FactState = FactState.KNOWN
    ended: bool = False

    def taint(self) -> None:
        self.status = FactState.TAINTED


@dataclass(frozen=True, slots=True)
class _FileResult:
    structures: tuple[_StructureBuilder, ...]
    diagnostics: tuple[Diagnostic, ...]
    complete: bool
    tainted_scopes: frozenset[str]
    library_name: str | None
    units: tuple[float, float] | None
    version: int | None
    record_count: int
    skipped_record_counts: Mapping[str, int]


def _decode_real8(payload: bytes) -> float:
    if len(payload) != 8:
        raise ValueError("GDSII REAL8 requires exactly eight bytes")
    if payload == b"\0" * 8:
        return 0.0
    sign = -1.0 if payload[0] & 0x80 else 1.0
    exponent = (payload[0] & 0x7F) - 64
    mantissa = int.from_bytes(payload[1:], "big") / float(1 << 56)
    return sign * mantissa * math.pow(16.0, exponent)


def _coerce_selector(
    value: int | Iterable[int] | None,
    *,
    option: str,
) -> frozenset[int] | None:
    if value is None:
        return None
    items: Iterable[int]
    if isinstance(value, bool):
        raise TypeError(f"{option} must contain integers, not booleans")
    if isinstance(value, int):
        items = (value,)
    else:
        if isinstance(value, (str, bytes)):
            raise TypeError(f"{option} must be an integer or iterable of integers")
        items = value
    result: set[int] = set()
    for item in items:
        if isinstance(item, bool) or not isinstance(item, int):
            raise TypeError(f"{option} must contain integers, not {type(item).__name__}")
        if not 0 <= item <= 32_767:
            raise ValueError(f"{option} values must be between 0 and 32767")
        result.add(item)
    return frozenset(result)


def _coerce_top_cells(value: str | Sequence[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    raw = (value,) if isinstance(value, str) else tuple(value)
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise TypeError("top_cells must contain strings")
        name = item.strip()
        if not name:
            raise ValueError("top_cells must not contain empty names")
        try:
            encoded_name = name.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("top_cells names must be 7-bit ASCII") from error
        if name in seen:
            raise ValueError(f"top_cells contains duplicate name {name!r}")
        if len(encoded_name) > _MAX_NAME_BYTES:
            raise ValueError(f"top cell name exceeds {_MAX_NAME_BYTES} bytes")
        result.append(name)
        seen.add(name)
    return tuple(result)


class _FileParser:
    def __init__(
        self,
        path: Path,
        data: bytes,
        view: ViewId,
    ) -> None:
        self.path = path
        self.data = data
        self.view = view
        self.diagnostics: list[Diagnostic] = []
        self.complete = True
        self.tainted_scopes: set[str] = set()
        self.structures: list[_StructureBuilder] = []
        self.current_structure: _StructureBuilder | None = None
        self.current_element: _ElementBuilder | None = None
        self.seen_header = False
        self.seen_bgnlib = False
        self.seen_endlib = False
        self.library_name: str | None = None
        self.units: tuple[float, float] | None = None
        self.version: int | None = None
        self.record_count = 0
        self.element_count = 0
        self.skipped_record_counts: dict[str, int] = {}
        self._framing_failed = False
        self._resource_exhausted = False

    def _provenance(
        self,
        record: _Record | None = None,
        *,
        raw_name: str | None = None,
        record_index: int = 1,
    ) -> Provenance:
        if record is not None:
            record_index = record.index
        return Provenance(
            str(self.path),
            max(1, record_index),
            1,
            self.view,
            raw_name,
            end_line=max(1, record_index),
            end_column=max(1, (record.length if record is not None else 1)),
        )

    def _scope(self) -> str:
        if self.current_structure is None or self.current_structure.name is None:
            return "*"
        return self.current_structure.name

    def _diagnose(
        self,
        code: str,
        severity: Severity,
        message: str,
        *,
        record: _Record | None = None,
        offset: int = 0,
        record_index: int = 1,
        scope: str | None = None,
        taint: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        location = self._provenance(record, record_index=record_index)
        diagnostic_metadata = {
            "byte_offset": record.offset if record is not None else offset,
            "record_index": record.index if record is not None else record_index,
        }
        if metadata:
            diagnostic_metadata.update(metadata)
        self.diagnostics.append(
            parser_diagnostic(
                code,
                severity,
                message,
                location=location,
                metadata=diagnostic_metadata,
            )
        )
        if not taint:
            return
        self.complete = False
        target = scope or self._scope()
        self.tainted_scopes.add(target)
        if self.current_structure is not None and target in {"*", self._scope()}:
            self.current_structure.taint()
        if self.current_element is not None:
            self.current_element.taint()

    def _fatal_framing(self, message: str, offset: int, record_index: int) -> None:
        if self._framing_failed:
            return
        self._framing_failed = True
        self._diagnose(
            "OC1101",
            Severity.FATAL,
            message,
            offset=offset,
            record_index=record_index,
            scope="*",
        )

    def _resource_limit(self, message: str, record: _Record) -> None:
        if self._resource_exhausted:
            return
        self._resource_exhausted = True
        self._diagnose(
            "OC1101",
            Severity.FATAL,
            message,
            record=record,
        )

    def _records(self) -> Iterator[_Record]:
        if len(self.data) > _MAX_SOURCE_BYTES:
            self._fatal_framing(
                f"GDSII source exceeds {_MAX_SOURCE_BYTES} bytes",
                0,
                1,
            )
            return
        offset = 0
        record_index = 1
        while offset < len(self.data):
            if record_index > _MAX_RECORDS:
                self._fatal_framing(
                    f"GDSII source exceeds {_MAX_RECORDS} records",
                    offset,
                    record_index,
                )
                break
            remaining = len(self.data) - offset
            if remaining < 4:
                self._fatal_framing(
                    f"Truncated GDSII record header: only {remaining} byte(s) remain",
                    offset,
                    record_index,
                )
                break
            length, record_type, data_type = struct.unpack_from(">HBB", self.data, offset)
            if length < 4:
                self._fatal_framing(
                    f"Invalid GDSII record length {length}; minimum is 4",
                    offset,
                    record_index,
                )
                break
            if length % 2:
                self._fatal_framing(
                    f"Invalid odd GDSII record length {length}",
                    offset,
                    record_index,
                )
                break
            if length > _MAX_RECORD_BYTES:
                self._fatal_framing(
                    f"GDSII record length {length} exceeds limit {_MAX_RECORD_BYTES}",
                    offset,
                    record_index,
                )
                break
            if length > remaining:
                self._fatal_framing(
                    (
                        f"Truncated GDSII {_RECORD_NAMES.get(record_type, 'record')} at "
                        f"byte {offset}: declares {length} bytes, only {remaining} remain"
                    ),
                    offset,
                    record_index,
                )
                break
            record = _Record(
                record_index,
                offset,
                length,
                record_type,
                data_type,
                self.data[offset + 4 : offset + length],
            )
            self.record_count += 1
            yield record
            offset += length
            record_index += 1

    def _expect(
        self,
        record: _Record,
        data_type: int,
        *,
        payload_size: int | None = None,
        multiple: int | None = None,
    ) -> bool:
        valid = True
        if record.data_type != data_type:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                (
                    f"GDSII {record.name} has data type 0x{record.data_type:02X}, "
                    f"expected 0x{data_type:02X}"
                ),
                record=record,
            )
            valid = False
        if payload_size is not None and len(record.payload) != payload_size:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                (
                    f"GDSII {record.name} payload is {len(record.payload)} bytes, "
                    f"expected {payload_size}"
                ),
                record=record,
            )
            valid = False
        if multiple is not None and len(record.payload) % multiple:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"GDSII {record.name} payload length is not a multiple of {multiple}",
                record=record,
            )
            valid = False
        return valid

    def _ascii(
        self,
        record: _Record,
        *,
        purpose: str,
        maximum: int,
    ) -> str | None:
        if not self._expect(record, _ASCII):
            return None
        payload = record.payload[:-1] if record.payload.endswith(b"\0") else record.payload
        if len(payload) > maximum:
            self._resource_limit(
                f"GDSII {purpose} exceeds the {maximum}-byte safety limit",
                record,
            )
            return None
        if b"\0" in payload:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"GDSII {purpose} contains an embedded NUL byte",
                record=record,
            )
        try:
            value = payload.decode("ascii")
        except UnicodeDecodeError:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"GDSII {purpose} is not 7-bit ASCII",
                record=record,
            )
            value = payload.decode("ascii", errors="replace")
        if not value:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"GDSII {purpose} must not be empty",
                record=record,
            )
            return None
        return value

    def _int2(self, record: _Record, purpose: str) -> int | None:
        if not self._expect(record, _INT2, payload_size=2):
            return None
        value = struct.unpack(">h", record.payload)[0]
        if purpose in {"layer", "text type"} and value < 0:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"GDSII {purpose} must be nonnegative, got {value}",
                record=record,
            )
            return None
        return value

    def _xy(self, record: _Record, *, retain: bool) -> tuple[tuple[int, int], ...] | None:
        if not self._expect(record, _INT4, multiple=8):
            return None
        count = len(record.payload) // 8
        if count > _MAX_XY_POINTS:
            self._resource_limit(
                f"GDSII XY record exceeds {_MAX_XY_POINTS} points",
                record,
            )
            return None
        if not retain:
            return ()
        return tuple(struct.iter_unpack(">ii", record.payload))

    def _duplicate_field(self, element: _ElementBuilder, field_name: str, record: _Record) -> bool:
        if field_name not in element.seen:
            element.seen.add(field_name)
            return False
        self._diagnose(
            "OC1101",
            Severity.ERROR,
            f"Duplicate GDSII {record.name} in {element.kind.upper()} element",
            record=record,
        )
        element.taint()
        return True

    def _begin_structure(self, record: _Record) -> None:
        self._expect(record, _INT2, payload_size=24)
        if not self.seen_bgnlib or self.seen_endlib:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII BGNSTR appears outside an open library",
                record=record,
                scope="*",
            )
        if self.current_element is not None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "Nested GDSII BGNSTR encountered before ENDEL",
                record=record,
            )
            self._finish_element(record, forced=True)
        if self.current_structure is not None:
            previous = self.current_structure
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "Nested GDSII BGNSTR encountered before ENDSTR",
                record=record,
                scope=previous.name or "*",
            )
            previous.taint()
            self._finish_structure(record, forced=True)
        if len(self.structures) >= _MAX_STRUCTURES:
            self._resource_limit(
                f"GDSII source exceeds {_MAX_STRUCTURES} structures",
                record,
            )
            return
        self.current_structure = _StructureBuilder(
            self._provenance(record),
            record.index,
            record.offset,
        )

    def _structure_name(self, record: _Record) -> None:
        name = self._ascii(record, purpose="structure name", maximum=_MAX_NAME_BYTES)
        if self.current_structure is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII STRNAME appears outside BGNSTR/ENDSTR",
                record=record,
                scope="*",
            )
            return
        if self.current_element is not None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII STRNAME appears inside an element",
                record=record,
            )
        if self.current_structure.name is not None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "Duplicate GDSII STRNAME in one structure",
                record=record,
            )
            return
        if name is not None:
            self.current_structure.name = name
            self.current_structure.name_provenance = self._provenance(record, raw_name=name)

    def _begin_element(self, kind: str, record: _Record) -> None:
        self._expect(record, _NODATA, payload_size=0)
        if self.current_structure is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"GDSII {record.name} appears outside a structure",
                record=record,
                scope="*",
            )
        elif self.current_structure.name is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"GDSII {record.name} appears before STRNAME",
                record=record,
            )
        if self.current_element is not None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"Nested GDSII {record.name} encountered before ENDEL",
                record=record,
            )
            self._finish_element(record, forced=True)
        self.element_count += 1
        if self.element_count > _MAX_ELEMENTS:
            self._resource_limit(
                f"GDSII source exceeds {_MAX_ELEMENTS} elements",
                record,
            )
            return
        self.current_element = _ElementBuilder(
            kind,
            self._provenance(record),
            record.index,
            record.offset,
            status=(
                FactState.TAINTED
                if self.current_structure is None or self.current_structure.name is None
                else FactState.KNOWN
            ),
        )

    def _element_field(self, record: _Record) -> None:
        element = self.current_element
        if element is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"GDSII {record.name} appears outside an element",
                record=record,
            )
            return
        if record.record_type == _LAYER:
            if not self._duplicate_field(element, "layer", record):
                element.layer = self._int2(record, "layer")
        elif record.record_type == _TEXTTYPE:
            if not self._duplicate_field(element, "text_type", record):
                element.text_type = self._int2(record, "text type")
        elif record.record_type == _XY:
            if not self._duplicate_field(element, "xy", record):
                element.xy = self._xy(record, retain=element.kind in {"sref", "aref", "text"})
        elif record.record_type == _STRING:
            if not self._duplicate_field(element, "string", record):
                element.string = self._ascii(
                    record,
                    purpose="text string",
                    maximum=_MAX_TEXT_BYTES,
                )
                element.string_provenance = self._provenance(
                    record,
                    raw_name=element.string,
                )
        elif record.record_type == _SNAME:
            if not self._duplicate_field(element, "target", record):
                element.target = self._ascii(
                    record,
                    purpose="referenced structure name",
                    maximum=_MAX_NAME_BYTES,
                )
                element.target_provenance = self._provenance(
                    record,
                    raw_name=element.target,
                )
        elif record.record_type == _COLROW:
            if not self._duplicate_field(element, "colrow", record):
                if self._expect(record, _INT2, payload_size=4):
                    columns, rows = struct.unpack(">hh", record.payload)
                    if columns <= 0 or rows <= 0:
                        self._diagnose(
                            "OC1101",
                            Severity.ERROR,
                            f"GDSII COLROW must be positive, got {columns} x {rows}",
                            record=record,
                        )
                    else:
                        element.columns = columns
                        element.rows = rows

    def _skippable(self, record: _Record) -> None:
        name = record.name
        self.skipped_record_counts[name] = self.skipped_record_counts.get(name, 0) + 1
        if self.current_element is not None:
            if len(self.current_element.skipped_records) >= _MAX_SKIPPED_RECORDS_PER_ELEMENT:
                self._resource_limit(
                    (
                        "GDSII element exceeds the skipped-record limit of "
                        f"{_MAX_SKIPPED_RECORDS_PER_ELEMENT}"
                    ),
                    record,
                )
                return
            self.current_element.skipped_records.append(name)
        if record.record_type == _STRANS:
            if self.current_element is None:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    "GDSII STRANS appears outside an element",
                    record=record,
                )
            elif not self._duplicate_field(self.current_element, "strans", record) and self._expect(
                record, _BITARRAY, payload_size=2
            ):
                flags = int.from_bytes(record.payload, "big")
                self.current_element.transform["reflection"] = bool(flags & 0x8000)
                self.current_element.transform["absolute_magnification"] = bool(flags & 0x0004)
                self.current_element.transform["absolute_angle"] = bool(flags & 0x0002)
        elif record.record_type in {_MAG, _ANGLE}:
            if self.current_element is None:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    f"GDSII {record.name} appears outside an element",
                    record=record,
                )
            elif not self._duplicate_field(
                self.current_element,
                "mag" if record.record_type == _MAG else "angle",
                record,
            ) and self._expect(record, _REAL8, payload_size=8):
                value = _decode_real8(record.payload)
                field_name = "magnification" if record.record_type == _MAG else "angle"
                self.current_element.transform[field_name] = value

    def _finish_element(self, record: _Record, *, forced: bool = False) -> None:
        element = self.current_element
        if element is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII ENDEL has no matching element",
                record=record,
            )
            return
        if not forced:
            self._expect(record, _NODATA, payload_size=0)
        else:
            element.taint()
        structure = self.current_structure
        if structure is None:
            self.current_element = None
            return
        if element.kind in {"sref", "aref"}:
            expected_xy = 1 if element.kind == "sref" else 3
            valid = element.target is not None and element.xy is not None
            valid = valid and len(element.xy or ()) == expected_xy
            if element.kind == "aref":
                valid = valid and element.columns is not None and element.rows is not None
            if not valid:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    (
                        f"Malformed GDSII {element.kind.upper()}: requires SNAME, "
                        f"{expected_xy} XY point(s)"
                        + (", and COLROW" if element.kind == "aref" else "")
                    ),
                    record=record,
                )
                element.taint()
            if element.target is not None:
                if len(structure.references) >= _MAX_REFERENCES_PER_STRUCTURE:
                    self._resource_limit(
                        (
                            f"GDSII structure exceeds {_MAX_REFERENCES_PER_STRUCTURE} "
                            "cell references"
                        ),
                        record,
                    )
                else:
                    attributes: dict[str, Any] = {
                        "element_kind": element.kind,
                        "target": element.target,
                        "xy": [list(point) for point in element.xy or ()],
                        "transform": dict(element.transform),
                        "record_index": element.record_index,
                        "byte_offset": element.byte_offset,
                        "skipped_records": list(element.skipped_records),
                    }
                    if element.kind == "sref" and element.xy:
                        attributes["origin"] = list(element.xy[0])
                    if element.kind == "aref":
                        attributes["columns"] = element.columns
                        attributes["rows"] = element.rows
                    structure.references.append(
                        DesignObjectObservation(
                            "cell_reference",
                            element.target,
                            relation="reference",
                            scope=structure.name,
                            provenance=element.target_provenance or element.provenance,
                            status=element.status,
                            attributes=attributes,
                        )
                    )
        elif element.kind == "text":
            valid = (
                element.string is not None
                and element.layer is not None
                and element.text_type is not None
                and element.xy is not None
                and len(element.xy) == 1
            )
            if not valid:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    "Malformed GDSII TEXT: requires LAYER, TEXTTYPE, one XY point, and STRING",
                    record=record,
                )
                element.taint()
            if element.string is not None:
                if len(structure.texts) >= _MAX_TEXTS_PER_STRUCTURE:
                    self._resource_limit(
                        f"GDSII structure exceeds {_MAX_TEXTS_PER_STRUCTURE} text elements",
                        record,
                    )
                else:
                    structure.texts.append(
                        _TextLabel(
                            element.string,
                            element.string_provenance or element.provenance,
                            element.layer,
                            element.text_type,
                            element.xy[0] if element.xy and len(element.xy) == 1 else None,
                            element.status,
                            {
                                "transform": dict(element.transform),
                                "record_index": element.record_index,
                                "byte_offset": element.byte_offset,
                                "skipped_records": list(element.skipped_records),
                            },
                        )
                    )
        else:
            structure.skipped_geometry += 1
        self.current_element = None

    def _finish_structure(self, record: _Record, *, forced: bool = False) -> None:
        structure = self.current_structure
        if structure is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII ENDSTR has no matching BGNSTR",
                record=record,
                scope="*",
            )
            return
        if self.current_element is not None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"GDSII {self.current_element.kind.upper()} is missing ENDEL",
                record=record,
            )
            self._finish_element(record, forced=True)
        if not forced:
            self._expect(record, _NODATA, payload_size=0)
        else:
            structure.taint()
        if structure.name is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII structure has no STRNAME",
                record=record,
                scope="*",
            )
            structure.taint()
        structure.ended = not forced
        self.structures.append(structure)
        self.current_structure = None

    def _header(self, record: _Record) -> None:
        valid = self._expect(record, _INT2, payload_size=2)
        if self.seen_header or record.index != 1:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII HEADER must be the first record and appear exactly once",
                record=record,
                scope="*",
            )
        self.seen_header = True
        if valid:
            self.version = struct.unpack(">h", record.payload)[0]
            if self.version <= 0:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    f"GDSII HEADER version must be positive, got {self.version}",
                    record=record,
                    scope="*",
                )

    def _begin_library(self, record: _Record) -> None:
        self._expect(record, _INT2, payload_size=24)
        if not self.seen_header or self.seen_bgnlib or self.seen_endlib:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII BGNLIB must follow HEADER and appear exactly once",
                record=record,
                scope="*",
            )
        self.seen_bgnlib = True

    def _library_name(self, record: _Record) -> None:
        name = self._ascii(record, purpose="library name", maximum=_MAX_NAME_BYTES)
        if not self.seen_bgnlib or self.current_structure is not None or self.seen_endlib:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII LIBNAME appears outside the library header",
                record=record,
                scope="*",
            )
        if self.library_name is not None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "Duplicate GDSII LIBNAME",
                record=record,
                scope="*",
            )
        elif name is not None:
            self.library_name = name

    def _units(self, record: _Record) -> None:
        valid = self._expect(record, _REAL8, payload_size=16)
        if not self.seen_bgnlib or self.current_structure is not None or self.seen_endlib:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII UNITS appears outside the library header",
                record=record,
                scope="*",
            )
        if self.units is not None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "Duplicate GDSII UNITS",
                record=record,
                scope="*",
            )
        elif valid:
            first = _decode_real8(record.payload[:8])
            second = _decode_real8(record.payload[8:])
            if not math.isfinite(first) or not math.isfinite(second) or first <= 0 or second <= 0:
                self._diagnose(
                    "OC1101",
                    Severity.ERROR,
                    "GDSII UNITS values must be finite and positive",
                    record=record,
                    scope="*",
                )
            else:
                self.units = (first, second)

    def _end_library(self, record: _Record) -> None:
        self._expect(record, _NODATA, payload_size=0)
        if not self.seen_bgnlib or self.seen_endlib:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII ENDLIB has no matching BGNLIB or is duplicated",
                record=record,
                scope="*",
            )
        if self.current_structure is not None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII ENDLIB encountered before ENDSTR",
                record=record,
            )
            self._finish_structure(record, forced=True)
        self.seen_endlib = True

    def _unknown(self, record: _Record) -> None:
        self._diagnose(
            "OC1102",
            Severity.WARNING,
            (
                f"Unsupported GDSII record type 0x{record.record_type:02X} "
                f"with data type 0x{record.data_type:02X}; payload skipped"
            ),
            record=record,
        )
        self.skipped_record_counts[record.name] = self.skipped_record_counts.get(record.name, 0) + 1

    def _dispatch(self, record: _Record) -> None:
        record_type = record.record_type
        if self.seen_endlib:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                f"GDSII {record.name} appears after ENDLIB",
                record=record,
                scope="*",
            )
            return
        if record_type == _HEADER:
            self._header(record)
        elif record_type == _BGNLIB:
            self._begin_library(record)
        elif record_type == _LIBNAME:
            self._library_name(record)
        elif record_type == _UNITS:
            self._units(record)
        elif record_type == _BGNSTR:
            self._begin_structure(record)
        elif record_type == _STRNAME:
            self._structure_name(record)
        elif record_type in {_SREF, _AREF, _TEXT}:
            self._begin_element({_SREF: "sref", _AREF: "aref", _TEXT: "text"}[record_type], record)
        elif record_type in _GEOMETRY_BEGIN:
            self._begin_element(_GEOMETRY_BEGIN[record_type], record)
        elif record_type in {_LAYER, _TEXTTYPE, _XY, _STRING, _SNAME, _COLROW}:
            self._element_field(record)
        elif record_type == _ENDEL:
            self._finish_element(record)
        elif record_type == _ENDSTR:
            self._finish_structure(record)
        elif record_type == _ENDLIB:
            self._end_library(record)
        elif record_type in _KNOWN_SKIPPABLE:
            self._skippable(record)
        else:
            self._unknown(record)

    def parse(self) -> _FileResult:
        for record in self._records():
            self._dispatch(record)
            if self._resource_exhausted:
                break
        if not self.seen_header:
            self._diagnose(
                "OC1101",
                Severity.FATAL,
                "GDSII source is missing HEADER",
                offset=0,
                record_index=1,
                scope="*",
            )
        if not self.seen_bgnlib:
            self._diagnose(
                "OC1101",
                Severity.FATAL,
                "GDSII source is missing BGNLIB",
                offset=0,
                record_index=1,
                scope="*",
            )
        elif self.library_name is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII library is missing LIBNAME",
                offset=0,
                record_index=1,
                scope="*",
            )
        if self.seen_bgnlib and self.units is None:
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII library is missing valid UNITS",
                offset=0,
                record_index=1,
                scope="*",
            )
        if self.current_structure is not None:
            synthetic = _Record(
                self.record_count + 1,
                len(self.data),
                4,
                _ENDSTR,
                _NODATA,
                b"",
            )
            self._diagnose(
                "OC1101",
                Severity.ERROR,
                "GDSII structure is truncated before ENDSTR",
                record=synthetic,
            )
            self._finish_structure(synthetic, forced=True)
        if not self.seen_endlib:
            self._diagnose(
                "OC1101",
                Severity.FATAL,
                "GDSII source is missing ENDLIB",
                offset=len(self.data),
                record_index=self.record_count + 1,
                scope="*",
            )
        return _FileResult(
            tuple(self.structures),
            tuple(self.diagnostics),
            self.complete and not self._framing_failed and not self._resource_exhausted,
            frozenset(self.tainted_scopes),
            self.library_name,
            self.units,
            self.version,
            self.record_count,
            dict(self.skipped_record_counts),
        )


def _selected_pin_label(
    label: _TextLabel,
    layers: frozenset[int] | None,
    text_types: frozenset[int] | None,
) -> bool:
    if layers is None and text_types is None:
        return False
    if layers is not None and label.layer not in layers:
        return False
    return text_types is None or label.text_type in text_types


def _structure_observations(
    structure: _StructureBuilder,
    *,
    is_top: bool,
    layers: frozenset[int] | None,
    text_types: frozenset[int] | None,
) -> tuple[ComponentObservation | None, tuple[DesignObjectObservation, ...]]:
    if structure.name is None:
        return None, ()
    grouped_labels: OrderedDict[str, list[_TextLabel]] = OrderedDict()
    for label in structure.texts:
        if _selected_pin_label(label, layers, text_types):
            grouped_labels.setdefault(label.value, []).append(label)
    ports: list[PortObservation] = []
    for name, labels in grouped_labels.items():
        tainted = structure.status != FactState.KNOWN or any(
            label.status != FactState.KNOWN for label in labels
        )
        ports.append(
            PortObservation(
                name,
                Direction.UNKNOWN,
                PortRole.UNKNOWN,
                BusShape.unknown(),
                labels[0].provenance,
                attributes={
                    "source": "gds_text_label",
                    "labels": [
                        {
                            "layer": label.layer,
                            "text_type": label.text_type,
                            "xy": list(label.xy) if label.xy is not None else None,
                            "provenance": label.provenance.to_dict(),
                        }
                        for label in labels
                    ],
                },
                status=FactState.TAINTED if tainted else FactState.KNOWN,
                field_states={
                    "direction": FactState.UNKNOWN,
                    "role": FactState.UNKNOWN,
                    "shape": FactState.UNKNOWN,
                },
            )
        )
    component = ComponentObservation(
        structure.name,
        ComponentKind.CELL,
        tuple(ports),
        provenance=structure.name_provenance or structure.provenance,
        attributes={
            "is_top": is_top,
            "skipped_geometry_elements": structure.skipped_geometry,
            "record_index": structure.record_index,
            "byte_offset": structure.byte_offset,
            "pin_source": (
                "explicit_text_filter" if layers is not None or text_types is not None else "none"
            ),
        },
        status=structure.status,
    )
    objects: list[DesignObjectObservation] = [
        DesignObjectObservation(
            "component",
            structure.name,
            provenance=structure.name_provenance or structure.provenance,
            status=structure.status,
            attributes={"component_kind": ComponentKind.CELL.value, "is_top": is_top},
        )
    ]
    objects.extend(structure.references)
    for label in structure.texts:
        objects.append(
            DesignObjectObservation(
                "text",
                label.value,
                scope=structure.name,
                provenance=label.provenance,
                status=(FactState.TAINTED if structure.status != FactState.KNOWN else label.status),
                attributes={
                    "layer": label.layer,
                    "text_type": label.text_type,
                    "xy": list(label.xy) if label.xy is not None else None,
                    "selected_as_pin": _selected_pin_label(label, layers, text_types),
                    **dict(label.attributes),
                },
            )
        )
    if structure.status != FactState.KNOWN:
        objects = [
            replace(item, status=FactState.TAINTED) if item.status == FactState.KNOWN else item
            for item in objects
        ]
    return component, tuple(objects)


def parse_gds(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    top_cells: str | Sequence[str] | None = None,
    pin_text_layers: int | Iterable[int] | None = None,
    pin_text_types: int | Iterable[int] | None = None,
) -> ViewObservation:
    """Parse native GDSII streams without loading polygon geometry.

    ``pin_text_layers`` and ``pin_text_types`` are opt-in selectors.  If both
    are omitted, text remains text and no port is inferred.  When both are
    supplied, a label must match both filters.
    """

    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="gds", name=view_name)
    requested_tops = _coerce_top_cells(top_cells)
    layers = _coerce_selector(pin_text_layers, option="pin_text_layers")
    text_types = _coerce_selector(pin_text_types, option="pin_text_types")
    structures: list[_StructureBuilder] = []
    diagnostics: list[Diagnostic] = []
    tainted_scopes: set[str] = set()
    complete = True
    source_metadata: dict[str, Mapping[str, Any]] = {}
    for path in source_paths:
        try:
            source_size = path.stat().st_size
        except OSError as error:
            diagnostics.append(
                parser_diagnostic(
                    "OC1002",
                    Severity.FATAL,
                    f"Cannot stat {path}: {error}",
                    location=Provenance(str(path), 1, 1, view),
                )
            )
            complete = False
            tainted_scopes.add("*")
            continue
        if source_size > _MAX_SOURCE_BYTES:
            diagnostics.append(
                parser_diagnostic(
                    "OC1101",
                    Severity.FATAL,
                    f"GDSII source exceeds {_MAX_SOURCE_BYTES} bytes",
                    location=Provenance(str(path), 1, 1, view),
                    metadata={"source_bytes": source_size},
                )
            )
            complete = False
            tainted_scopes.add("*")
            continue
        try:
            data = path.read_bytes()
        except OSError as error:
            diagnostics.append(
                parser_diagnostic(
                    "OC1002",
                    Severity.FATAL,
                    f"Cannot read {path}: {error}",
                    location=Provenance(str(path), 1, 1, view),
                )
            )
            complete = False
            tainted_scopes.add("*")
            continue
        result = _FileParser(path, data, view).parse()
        structures.extend(result.structures)
        diagnostics.extend(result.diagnostics)
        tainted_scopes.update(result.tainted_scopes)
        complete = complete and result.complete
        source_metadata[str(path)] = {
            "library_name": result.library_name,
            "units": list(result.units) if result.units is not None else None,
            "version": result.version,
            "record_count": result.record_count,
            "skipped_record_counts": dict(result.skipped_record_counts),
        }

    by_name: dict[str, list[_StructureBuilder]] = {}
    for structure in structures:
        if structure.name is not None:
            by_name.setdefault(structure.name, []).append(structure)
    for name, duplicates in by_name.items():
        if len(duplicates) < 2:
            continue
        complete = False
        tainted_scopes.add(name)
        diagnostics.append(
            parser_diagnostic(
                "OC1101",
                Severity.ERROR,
                f"Duplicate GDSII structure definition for {name!r}",
                location=duplicates[1].name_provenance or duplicates[1].provenance,
            )
        )
        for structure in duplicates:
            structure.taint()

    known_names = set(by_name)
    ambiguous_reference_keys: set[tuple[str | None, str]] = set()
    for structure in structures:
        for index, reference in enumerate(structure.references):
            candidates = by_name.get(reference.native_name, [])
            if len(candidates) == 1:
                continue
            complete = False
            if structure.name is not None:
                tainted_scopes.add(structure.name)
                structure.taint()
            diagnostic_key = (structure.name, reference.native_name)
            if diagnostic_key not in ambiguous_reference_keys:
                qualifier = "absent" if not candidates else "multiply defined"
                diagnostics.append(
                    parser_diagnostic(
                        "OC1103",
                        Severity.WARNING,
                        (
                            f"GDSII {reference.attributes.get('element_kind', 'cell')} "
                            f"reference targets {qualifier} structure "
                            f"{reference.native_name!r}"
                        ),
                        location=reference.provenance,
                    )
                )
                ambiguous_reference_keys.add(diagnostic_key)
            structure.references[index] = replace(reference, status=FactState.TAINTED)

    referenced = {
        reference.native_name
        for structure in structures
        for reference in structure.references
        if reference.status == FactState.KNOWN and reference.native_name in known_names
    }
    inferred_tops = tuple(name for name in by_name if name not in referenced)
    if requested_tops is None:
        selected_tops = inferred_tops
        top_state = FactState.KNOWN
        if known_names and not selected_tops:
            complete = False
            tainted_scopes.add("*")
            top_state = FactState.UNKNOWN
            location = next(
                (
                    structure.name_provenance or structure.provenance
                    for structure in structures
                    if structure.name is not None
                ),
                None,
            )
            diagnostics.append(
                parser_diagnostic(
                    "OC1103",
                    Severity.WARNING,
                    "GDSII top-cell inference found no unreferenced structure",
                    location=location,
                )
            )
    else:
        selected_tops = tuple(name for name in requested_tops if name in known_names)
        missing_tops = tuple(name for name in requested_tops if name not in known_names)
        top_state = FactState.KNOWN if not missing_tops else FactState.TAINTED
        for name in missing_tops:
            complete = False
            tainted_scopes.add("*")
            diagnostics.append(
                parser_diagnostic(
                    "OC1103",
                    Severity.WARNING,
                    f"Requested GDSII top cell {name!r} is absent",
                    location=Provenance(str(source_paths[0]), 1, 1, view, name),
                )
            )
    selected_top_set = set(selected_tops)
    components: list[ComponentObservation] = []
    objects: list[DesignObjectObservation] = []
    for structure in structures:
        component, structure_objects = _structure_observations(
            structure,
            is_top=structure.name in selected_top_set,
            layers=layers,
            text_types=text_types,
        )
        if component is not None:
            components.append(component)
        objects.extend(structure_objects)

    return ViewObservation(
        view,
        tuple(components),
        diagnostics=tuple(diagnostics),
        complete=complete,
        tainted_scopes=frozenset(tainted_scopes),
        objects=tuple(objects),
        attributes={
            "parser": "stdlib-native-gdsii",
            "source_files": [str(path) for path in source_paths],
            "sources": source_metadata,
            "top_cells": list(selected_tops),
            "top_cell_state": top_state.value,
            "top_cell_source": "explicit" if requested_tops is not None else "inferred",
            "inferred_top_cells": list(inferred_tops),
            "pin_text_layers": sorted(layers) if layers is not None else None,
            "pin_text_types": sorted(text_types) if text_types is not None else None,
            "geometry_materialized": False,
        },
    )


def parse_gdsii(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    top_cells: str | Sequence[str] | None = None,
    pin_text_layers: int | Iterable[int] | None = None,
    pin_text_types: int | Iterable[int] | None = None,
) -> ViewObservation:
    """Alias for :func:`parse_gds`."""

    return parse_gds(
        paths,
        view_id=view_id,
        view_name=view_name,
        top_cells=top_cells,
        pin_text_layers=pin_text_layers,
        pin_text_types=pin_text_types,
    )


class GdsParser:
    format_name = "gds"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_gds(paths, view_id=view_id, **options)


GDSParser = GdsParser
GdsiiParser = GdsParser
GDSIIParser = GdsParser

__all__ = [
    "GDSIIParser",
    "GDSParser",
    "GdsParser",
    "GdsiiParser",
    "parse_gds",
    "parse_gdsii",
]

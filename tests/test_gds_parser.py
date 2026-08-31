from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollate.diagnostics import Severity
from opencollate.model import Direction, FactState, PortRole, ViewId, ViewObservation
from opencollate.parsers import gds
from opencollate.parsers.gds import (
    GDSIIParser,
    GdsiiParser,
    GDSParser,
    GdsParser,
    parse_gds,
    parse_gdsii,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gds"

NODATA = 0
BITARRAY = 1
INT2 = 2
INT4 = 3
REAL8 = 5
ASCII = 6

HEADER = 0x00
BGNLIB = 0x01
LIBNAME = 0x02
UNITS = 0x03
ENDLIB = 0x04
BGNSTR = 0x05
STRNAME = 0x06
ENDSTR = 0x07
BOUNDARY = 0x08
SREF = 0x0A
AREF = 0x0B
TEXT = 0x0C
LAYER = 0x0D
DATATYPE = 0x0E
XY = 0x10
ENDEL = 0x11
SNAME = 0x12
COLROW = 0x13
TEXTTYPE = 0x16
STRING = 0x19
STRANS = 0x1A
MAG = 0x1B
ANGLE = 0x1C


def _record(record_type: int, data_type: int = NODATA, payload: bytes = b"") -> bytes:
    assert (4 + len(payload)) % 2 == 0
    return struct.pack(">HBB", 4 + len(payload), record_type, data_type) + payload


def _ascii(record_type: int, value: str) -> bytes:
    payload = value.encode("ascii")
    if len(payload) % 2:
        payload += b"\0"
    return _record(record_type, ASCII, payload)


def _int2(record_type: int, *values: int) -> bytes:
    return _record(record_type, INT2, struct.pack(f">{len(values)}h", *values))


def _xy(*points: tuple[int, int]) -> bytes:
    flattened = tuple(coordinate for point in points for coordinate in point)
    return _record(XY, INT4, struct.pack(f">{len(flattened)}i", *flattened))


def _real8(value: float) -> bytes:
    if value == 0:
        return b"\0" * 8
    sign = 0x80 if value < 0 else 0
    fraction = abs(value)
    exponent = 64
    while fraction >= 1:
        fraction /= 16
        exponent += 1
    while fraction < 1 / 16:
        fraction *= 16
        exponent -= 1
    mantissa = round(fraction * (1 << 56))
    if mantissa == 1 << 56:
        mantissa //= 16
        exponent += 1
    return bytes((sign | exponent,)) + mantissa.to_bytes(7, "big")


def _preamble(*, units: tuple[float, float] = (0.001, 1e-9)) -> bytes:
    timestamps = struct.pack(">12h", *([0] * 12))
    return b"".join(
        (
            _int2(HEADER, 600),
            _record(BGNLIB, INT2, timestamps),
            _ascii(LIBNAME, "TESTLIB"),
            _record(UNITS, REAL8, _real8(units[0]) + _real8(units[1])),
        )
    )


def _begin_structure(name: str) -> bytes:
    return _record(BGNSTR, INT2, struct.pack(">12h", *([0] * 12))) + _ascii(STRNAME, name)


def _text(label: str, layer: int, text_type: int, point: tuple[int, int]) -> bytes:
    return b"".join(
        (
            _record(TEXT),
            _int2(LAYER, layer),
            _int2(TEXTTYPE, text_type),
            _xy(point),
            _ascii(STRING, label),
            _record(ENDEL),
        )
    )


def _sref(target: str, point: tuple[int, int] = (0, 0)) -> bytes:
    return b"".join(
        (
            _record(SREF),
            _ascii(SNAME, target),
            _xy(point),
            _record(ENDEL),
        )
    )


def _write(tmp_path: Path, data: bytes, name: str = "test.gds") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _load_hex(name: str) -> bytes:
    return bytes.fromhex((FIXTURES / name).read_text(encoding="ascii"))


def _objects(
    view: ViewObservation,
    kind: str,
    *,
    relation: str | None = None,
) -> list[object]:
    return [
        item
        for item in view.objects
        if item.kind == kind and (relation is None or item.relation == relation)
    ]


def test_minimal_native_fixture_parses_units_text_and_no_implicit_pins(tmp_path: Path) -> None:
    source = _write(tmp_path, _load_hex("minimal.gds.hex"))

    view = parse_gds(source)

    assert view.view == ViewId("gds")
    assert view.complete
    assert not view.diagnostics
    assert view.attributes["top_cells"] == ["TOP"]
    assert view.attributes["geometry_materialized"] is False
    assert view.attributes["sources"][str(source)]["library_name"] == "LIB"
    assert view.attributes["sources"][str(source)]["units"] == [1.0, 1.0]
    assert len(view.components) == 1
    assert view.components[0].ports == ()
    text = _objects(view, "text")[0]
    assert text.native_name == "PIN"
    assert text.attributes["layer"] == 10
    assert text.attributes["text_type"] == 5
    assert text.attributes["xy"] == [100, 200]
    assert text.provenance is not None
    assert text.provenance.line == 11
    assert text.attributes["selected_as_pin"] is False


def test_explicit_text_filters_create_only_selected_unknown_ports(tmp_path: Path) -> None:
    source = _write(tmp_path, _load_hex("minimal.gds.hex"))

    view = parse_gds(source, pin_text_layers={10}, pin_text_types=5)

    assert view.complete
    port = view.components[0].ports[0]
    assert port.native_name == "PIN"
    assert port.direction == Direction.UNKNOWN
    assert port.role == PortRole.UNKNOWN
    assert port.shape.width is None
    assert port.state_for("direction") == FactState.UNKNOWN
    assert port.state_for("role") == FactState.UNKNOWN
    assert port.state_for("shape") == FactState.UNKNOWN
    assert view.attributes["pin_text_layers"] == [10]
    assert view.attributes["pin_text_types"] == [5]


def test_hierarchy_references_arrays_transforms_geometry_and_top_inference(
    tmp_path: Path,
) -> None:
    boundary = b"".join(
        (
            _record(BOUNDARY),
            _int2(LAYER, 1),
            _int2(DATATYPE, 0),
            _xy((0, 0), (10, 0), (10, 10), (0, 10), (0, 0)),
            _record(ENDEL),
        )
    )
    sref = b"".join(
        (
            _record(SREF),
            _ascii(SNAME, "CHILD"),
            _record(STRANS, BITARRAY, b"\x80\x00"),
            _record(MAG, REAL8, _real8(2.0)),
            _record(ANGLE, REAL8, _real8(90.0)),
            _xy((5, 7)),
            _record(ENDEL),
        )
    )
    aref = b"".join(
        (
            _record(AREF),
            _ascii(SNAME, "CHILD"),
            _int2(COLROW, 2, 3),
            _xy((0, 0), (20, 0), (0, 30)),
            _record(ENDEL),
        )
    )
    data = b"".join(
        (
            _preamble(),
            _begin_structure("CHILD"),
            boundary,
            _text("MARK", 99, 0, (1, 1)),
            _record(ENDSTR),
            _begin_structure("TOP"),
            sref,
            aref,
            _text("IRQ", 10, 5, (100, 200)),
            _text("IRQ", 10, 5, (110, 210)),
            _text("IGNORE", 11, 5, (0, 0)),
            _record(ENDSTR),
            _record(ENDLIB),
        )
    )
    source = _write(tmp_path, data)

    view = parse_gds(source, pin_text_layers=10, pin_text_types={5})

    assert view.complete
    assert view.attributes["top_cells"] == ["TOP"]
    components = {item.native_name: item for item in view.components}
    assert components["CHILD"].attributes["skipped_geometry_elements"] == 1
    assert [item.native_name for item in components["TOP"].ports] == ["IRQ"]
    assert len(components["TOP"].ports[0].attributes["labels"]) == 2
    references = _objects(view, "cell_reference", relation="reference")
    assert len(references) == 2
    assert references[0].attributes["origin"] == [5, 7]
    assert references[0].attributes["transform"] == {
        "reflection": True,
        "absolute_magnification": False,
        "absolute_angle": False,
        "magnification": pytest.approx(2.0),
        "angle": pytest.approx(90.0),
    }
    assert references[1].attributes["columns"] == 2
    assert references[1].attributes["rows"] == 3
    assert references[1].attributes["xy"] == [[0, 0], [20, 0], [0, 30]]


def test_explicit_top_cells_override_inference_and_missing_names_are_not_invented(
    tmp_path: Path,
) -> None:
    data = b"".join(
        (
            _preamble(),
            _begin_structure("A"),
            _record(ENDSTR),
            _begin_structure("B"),
            _record(ENDSTR),
            _record(ENDLIB),
        )
    )
    source = _write(tmp_path, data)

    view = parse_gds(source, top_cells=("B", "MISSING"), view_id="gds.mask")

    assert view.view == ViewId("gds", "mask")
    assert not view.complete
    assert view.attributes["top_cells"] == ["B"]
    assert view.attributes["top_cell_source"] == "explicit"
    assert view.attributes["top_cell_state"] == FactState.TAINTED.value
    assert any(item.code == "OC1103" and "MISSING" in item.message for item in view.diagnostics)
    assert next(item for item in view.components if item.name == "B").attributes["is_top"]


@pytest.mark.parametrize(
    "data",
    [
        b"\x00\x06\x00",
        b"\x00\x02\x00\x00",
        b"\x00\x05\x00\x00\x00",
        b"\x00\x0c\x02\x06LI",
    ],
)
def test_truncated_and_invalid_record_framing_is_fatal(tmp_path: Path, data: bytes) -> None:
    source = _write(tmp_path, data)

    view = parse_gds(source)

    assert not view.complete
    assert "*" in view.tainted_scopes
    assert any(
        item.code == "OC1101" and item.severity == Severity.FATAL for item in view.diagnostics
    )


def test_truncated_hex_fixture_is_rejected(tmp_path: Path) -> None:
    source = _write(tmp_path, _load_hex("truncated.gds.hex"))

    view = parse_gds(source)

    assert not view.components
    assert any("Truncated" in item.message for item in view.diagnostics)


def test_malformed_nested_records_salvage_names_but_taint_scopes(tmp_path: Path) -> None:
    data = b"".join(
        (
            _preamble(),
            _begin_structure("FIRST"),
            _record(SREF),
            _ascii(SNAME, "FIRST"),
            _record(TEXT),
            _int2(LAYER, 1),
            _int2(TEXTTYPE, 0),
            _xy((0, 0)),
            _ascii(STRING, "T"),
            _record(ENDSTR),
            _begin_structure("SECOND"),
            _ascii(STRNAME, "DUPLICATE"),
            _record(ENDSTR),
            _record(ENDLIB),
            _record(ENDLIB),
        )
    )
    source = _write(tmp_path, data)

    view = parse_gds(source)

    assert not view.complete
    assert {component.native_name for component in view.components} == {"FIRST", "SECOND"}
    assert all(component.status == FactState.TAINTED for component in view.components)
    assert any("Nested GDSII TEXT" in item.message for item in view.diagnostics)
    assert any("Duplicate GDSII STRNAME" in item.message for item in view.diagnostics)
    assert any("after ENDLIB" in item.message for item in view.diagnostics)


def test_malformed_reference_text_and_unknown_record_are_explicit(tmp_path: Path) -> None:
    data = b"".join(
        (
            _preamble(units=(1.0, 1.0)),
            _begin_structure("BAD"),
            _record(SREF),
            _ascii(SNAME, "MISSING"),
            _record(ENDEL),
            _record(AREF),
            _ascii(SNAME, "MISSING"),
            _int2(COLROW, 0, -1),
            _xy((0, 0)),
            _record(ENDEL),
            _record(TEXT),
            _int2(LAYER, -1),
            _int2(TEXTTYPE, 3),
            _int2(TEXTTYPE, 4),
            _ascii(STRING, "LABEL"),
            _record(ENDEL),
            _record(0x7F, ASCII, b"XX"),
            _record(ENDSTR),
            _record(ENDLIB),
        )
    )
    source = _write(tmp_path, data)

    view = parse_gds(source, pin_text_types=3)

    assert not view.complete
    assert {item.code for item in view.diagnostics} >= {"OC1101", "OC1102", "OC1103"}
    assert all(component.status == FactState.TAINTED for component in view.components)
    assert all(item.status == FactState.TAINTED for item in _objects(view, "cell_reference"))
    text = _objects(view, "text")[0]
    assert text.native_name == "LABEL"
    assert text.status == FactState.TAINTED
    assert view.components[0].ports[0].native_name == "LABEL"
    assert view.components[0].ports[0].status == FactState.TAINTED


def test_duplicate_structures_and_cycle_have_unknown_top(tmp_path: Path) -> None:
    data = b"".join(
        (
            _preamble(),
            _begin_structure("A"),
            _sref("B"),
            _record(ENDSTR),
            _begin_structure("B"),
            _sref("A"),
            _record(ENDSTR),
            _begin_structure("A"),
            _record(ENDSTR),
            _record(ENDLIB),
        )
    )
    source = _write(tmp_path, data)

    view = parse_gds(source)

    assert not view.complete
    assert len(view.components) == 3
    assert all(item.status == FactState.TAINTED for item in view.components if item.name == "A")
    assert any("Duplicate GDSII structure" in item.message for item in view.diagnostics)


@pytest.mark.parametrize(
    ("option", "value", "error"),
    [
        ("pin_text_layers", True, TypeError),
        ("pin_text_layers", "10", TypeError),
        ("pin_text_types", [1, "x"], TypeError),
        ("pin_text_types", [-1], ValueError),
        ("top_cells", [""], ValueError),
        ("top_cells", ["A", "A"], ValueError),
        ("top_cells", [1], TypeError),
    ],
)
def test_invalid_options_are_rejected_before_io(
    option: str,
    value: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        parse_gds("does-not-matter.gds", **{option: value})


@pytest.mark.parametrize(
    ("constant", "limit"),
    [
        ("_MAX_SOURCE_BYTES", 8),
        ("_MAX_RECORD_BYTES", 4),
        ("_MAX_RECORDS", 2),
        ("_MAX_STRUCTURES", 0),
        ("_MAX_ELEMENTS", 0),
        ("_MAX_REFERENCES_PER_STRUCTURE", 0),
        ("_MAX_TEXTS_PER_STRUCTURE", 0),
        ("_MAX_XY_POINTS", 0),
        ("_MAX_NAME_BYTES", 1),
        ("_MAX_TEXT_BYTES", 1),
        ("_MAX_SKIPPED_RECORDS_PER_ELEMENT", 0),
    ],
)
def test_resource_limits_fail_closed(
    constant: str,
    limit: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = b"".join(
        (
            _preamble(),
            _begin_structure("LONG"),
            _record(SREF),
            _ascii(SNAME, "LONG"),
            _record(STRANS, BITARRAY, b"\0\0"),
            _xy((0, 0)),
            _record(ENDEL),
            _text("LABEL", 1, 2, (0, 0)),
            _record(ENDSTR),
            _record(ENDLIB),
        )
    )
    source = _write(tmp_path, data, f"{constant}.gds")
    monkeypatch.setattr(gds, constant, limit)

    view = parse_gds(source)

    assert not view.complete
    assert any(
        item.code == "OC1101" and item.severity == Severity.FATAL for item in view.diagnostics
    )


def test_wrong_data_types_missing_library_records_and_non_ascii_are_tainted(
    tmp_path: Path,
) -> None:
    data = b"".join(
        (
            _record(HEADER, ASCII, b"XX"),
            _record(BGNLIB, INT2, struct.pack(">12h", *([0] * 12))),
            _record(LIBNAME, ASCII, b"\xff\0"),
            _begin_structure("CELL"),
            _record(ENDSTR),
        )
    )
    source = _write(tmp_path, data)

    view = parse_gds(source)

    assert not view.complete
    assert any("data type" in item.message for item in view.diagnostics)
    assert any("7-bit ASCII" in item.message for item in view.diagnostics)
    assert any("missing ENDLIB" in item.message for item in view.diagnostics)


def test_parser_aliases_share_api(tmp_path: Path) -> None:
    source = _write(tmp_path, _load_hex("minimal.gds.hex"))

    direct = parse_gdsii(source, view_id="gds.final", top_cells="TOP")
    variants = [
        GdsParser().parse((source,), view_id="gds.final", top_cells="TOP"),
        GDSParser().parse((source,), view_id="gds.final", top_cells="TOP"),
        GdsiiParser().parse((source,), view_id="gds.final", top_cells="TOP"),
        GDSIIParser().parse((source,), view_id="gds.final", top_cells="TOP"),
    ]

    assert all(item == direct for item in variants)
    assert GdsParser.format_name == "gds"


def test_real8_zero_and_invalid_width_are_deterministic() -> None:
    assert gds._decode_real8(b"\0" * 8) == 0.0
    with pytest.raises(ValueError, match="eight bytes"):
        gds._decode_real8(b"\0" * 7)


def test_missing_library_metadata_and_nonpositive_version_are_rejected(tmp_path: Path) -> None:
    timestamps = struct.pack(">12h", *([0] * 12))
    source = _write(
        tmp_path,
        _int2(HEADER, 0) + _record(BGNLIB, INT2, timestamps) + _record(ENDLIB),
    )

    view = parse_gds(source)

    messages = [item.message for item in view.diagnostics]
    assert not view.complete
    assert any("version must be positive" in message for message in messages)
    assert any("missing LIBNAME" in message for message in messages)
    assert any("missing valid UNITS" in message for message in messages)


def test_unique_reference_cycle_has_no_inferred_top(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        b"".join(
            (
                _preamble(),
                _begin_structure("A"),
                _sref("B"),
                _record(ENDSTR),
                _begin_structure("B"),
                _sref("A"),
                _record(ENDSTR),
                _record(ENDLIB),
            )
        ),
    )

    view = parse_gds(source)

    assert not view.complete
    assert view.attributes["top_cells"] == []
    assert view.attributes["top_cell_state"] == FactState.UNKNOWN.value
    assert any("no unreferenced structure" in item.message for item in view.diagnostics)


def test_empty_nameless_and_out_of_context_records_are_recoverable(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        b"".join(
            (
                _preamble(units=(1.0, 1.0)),
                _ascii(STRNAME, "OUTSIDE"),
                _int2(LAYER, 1),
                _record(ENDEL),
                _record(ENDSTR),
                _record(BGNSTR, INT2, struct.pack(">12h", *([0] * 12))),
                _record(TEXT),
                _int2(LAYER, 1),
                _int2(TEXTTYPE, 1),
                _xy((0, 0)),
                _record(STRING, ASCII, b""),
                _record(ENDEL),
                _record(ENDSTR),
                _record(ENDLIB),
            )
        ),
    )

    view = parse_gds(source)

    assert not view.complete
    assert not view.components
    messages = [item.message for item in view.diagnostics]
    assert any("outside BGNSTR" in message for message in messages)
    assert any("outside an element" in message for message in messages)
    assert any("no matching element" in message for message in messages)
    assert any("no STRNAME" in message for message in messages)
    assert any("must not be empty" in message for message in messages)


def test_duplicate_transforms_and_invalid_units_are_tainted(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        b"".join(
            (
                _preamble(units=(0.0, 1.0)),
                _begin_structure("CELL"),
                _record(SREF),
                _ascii(SNAME, "CELL"),
                _record(STRANS, BITARRAY, b"\0\0"),
                _record(STRANS, BITARRAY, b"\0\0"),
                _record(MAG, REAL8, _real8(1.0)),
                _record(MAG, REAL8, _real8(2.0)),
                _xy((0, 0)),
                _record(ENDEL),
                _record(ENDSTR),
                _record(ENDLIB),
            )
        ),
    )

    view = parse_gds(source, top_cells="CELL")

    assert not view.complete
    assert any("UNITS values" in item.message for item in view.diagnostics)
    assert sum("Duplicate GDSII" in item.message for item in view.diagnostics) >= 2


def test_preflight_size_check_does_not_read_oversized_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _write(tmp_path, b"tiny")
    path_type = type(source)
    original_stat = path_type.stat
    original_read_bytes = path_type.read_bytes
    read_attempted = False

    def fake_stat(path: Path, *args: object, **kwargs: object) -> SimpleNamespace:
        if path == source:
            return SimpleNamespace(st_size=gds._MAX_SOURCE_BYTES + 1)
        return SimpleNamespace(st_size=original_stat(path, *args, **kwargs).st_size)

    def fake_read_bytes(path: Path) -> bytes:
        nonlocal read_attempted
        if path == source:
            read_attempted = True
            raise AssertionError("oversized source must not be read")
        return original_read_bytes(path)

    monkeypatch.setattr(path_type, "stat", fake_stat)
    monkeypatch.setattr(path_type, "read_bytes", fake_read_bytes)

    view = parse_gds(source)

    assert not read_attempted
    assert not view.complete
    assert any(item.severity == Severity.FATAL for item in view.diagnostics)


@pytest.mark.parametrize("name", ["töp", "X" * 10])
def test_explicit_top_names_must_be_ascii_and_bounded(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if name.startswith("X"):
        monkeypatch.setattr(gds, "_MAX_NAME_BYTES", 2)
    with pytest.raises(ValueError):
        parse_gds("unused.gds", top_cells=name)

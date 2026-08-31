from __future__ import annotations

from pathlib import Path

import pytest

from opencollate.model import FactState, PortRole, ViewId
from opencollate.parsers import (
    UnsupportedFormatError,
    get_parser,
    infer_format,
    normalize_format,
    parse,
    registered_formats,
)
from opencollate.parsers.base import (
    coerce_paths,
    coerce_view,
    infer_role_from_name,
    provenance,
    read_source,
    unavailable_view,
)


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        (".SV", "verilog"),
        ("rtl", "verilog"),
        ("lib", "liberty"),
        ("timing", "liberty"),
        ("pin-map", "csv"),
        ("package", "csv"),
    ],
)
def test_format_aliases(alias: str, expected: str) -> None:
    assert normalize_format(alias) == expected


def test_unknown_format_is_actionable() -> None:
    with pytest.raises(UnsupportedFormatError, match="supported formats"):
        normalize_format("gds")


def test_format_inference_rejects_unknown_and_mixed_extensions() -> None:
    assert infer_format((Path("a.sv"), Path("b.v"))) == "verilog"
    with pytest.raises(UnsupportedFormatError, match="cannot infer"):
        infer_format((Path("chip.gds"),))
    with pytest.raises(UnsupportedFormatError, match="cannot mix"):
        infer_format((Path("top.sv"), Path("cells.lib")))


def test_registered_parser_inventory_is_stable() -> None:
    assert registered_formats() == ("csv", "lef", "liberty", "verilog")
    assert get_parser("sv").format_name == "verilog"


def test_dispatch_supports_explicit_and_inferred_call_styles(tmp_path: Path) -> None:
    liberty = tmp_path / "cell.lib"
    liberty.write_text(
        "library(x) { cell(c) { pin(A) { direction : input; } } }\n",
        encoding="utf-8",
    )
    inferred = parse(liberty, view_name="tt")
    explicit = parse("liberty", liberty, view_id="liberty.ss")
    keyword = parse(liberty, format="lib")
    assert inferred.view == ViewId("liberty", "tt")
    assert explicit.view == ViewId("liberty", "ss")
    assert keyword.components[0].name == "c"


def test_tsv_dispatch_sets_delimiter(tmp_path: Path) -> None:
    path = tmp_path / "pins.tsv"
    path.write_text(
        "component\tsignal\tdirection\nuart\tirq\toutput\n",
        encoding="utf-8",
    )
    view = parse(path)
    assert view.view == ViewId("csv")
    assert view.components[0].ports[0].name == "irq"


def test_base_path_and_view_coercion() -> None:
    assert coerce_paths("a.sv") == (Path("a.sv"),)
    assert coerce_paths(["a.sv", Path("b.sv")]) == (Path("a.sv"), Path("b.sv"))
    with pytest.raises(ValueError, match="at least one"):
        coerce_paths([])
    assert coerce_view(None, kind="rtl", name="synth") == ViewId("rtl", "synth")
    assert coerce_view("unknown.custom", kind="rtl") == ViewId("rtl", "custom")
    assert coerce_view("liberty.tt", kind="rtl") == ViewId("liberty", "tt")


@pytest.mark.parametrize(
    ("name", "role", "state"),
    [
        ("VSS_IO", PortRole.GROUND, FactState.TAINTED),
        ("vdd_core", PortRole.POWER, FactState.TAINTED),
        ("core_clk_i", PortRole.CLOCK, FactState.TAINTED),
        ("resetn", PortRole.RESET, FactState.TAINTED),
        ("payload", PortRole.UNKNOWN, FactState.UNKNOWN),
    ],
)
def test_role_name_hints_are_never_known_truth(name: str, role: PortRole, state: FactState) -> None:
    assert infer_role_from_name(name) == (role, state)


def test_provenance_clamps_invalid_parser_coordinates() -> None:
    value = provenance("a.sv", ViewId("rtl"), line=0, column=-5, raw_name="A")
    assert (value.line, value.column, value.raw_name) == (1, 1, "A")


def test_read_source_missing_and_non_utf8_are_tainted(tmp_path: Path) -> None:
    missing = read_source(tmp_path / "missing.lib", ViewId("liberty"))
    assert missing.tainted
    assert missing.diagnostics[0].code == "OC1002"

    legacy = tmp_path / "legacy.csv"
    legacy.write_bytes(b"signal\nVDD\xff\n")
    decoded = read_source(legacy, ViewId("csv"))
    assert decoded.tainted
    assert decoded.encoding == "latin-1"
    assert decoded.diagnostics[0].code == "OC1104"


def test_unavailable_view_is_explicitly_unsupported() -> None:
    view = unavailable_view(
        view=ViewId("rtl"),
        paths=(),
        code="OC1102",
        message="backend unavailable",
    )
    assert not view.complete
    assert view.tainted_scopes == frozenset({"*"})
    assert view.attributes["parser_state"] == FactState.UNSUPPORTED.value

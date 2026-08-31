from __future__ import annotations

import csv
from pathlib import Path

import pytest

from opencollate.model import Direction, FactState, PortRole, ViewId
from opencollate.parsers.csvpins import parse_pin_csv


def test_package_map_preserves_ball_names_and_rows() -> None:
    path = Path(__file__).parents[1] / "examples" / "uart" / "package" / "pins.csv"
    view = parse_pin_csv(path, view_id="csv.package")
    assert view.view == ViewId("csv", "package")
    assert view.complete
    assert len(view.pin_mappings) == 6
    duplicate = [mapping for mapping in view.pin_mappings if mapping.package_ball == "B1"]
    assert [mapping.signal for mapping in duplicate] == ["irq_o", "tx_active_o"]


def test_component_inventory_csv_supports_bom_ranges_and_synonyms(tmp_path: Path) -> None:
    path = tmp_path / "inventory.csv"
    path.write_text(
        "Block,Port,Dir,Range,Use,Package Pin\n"
        "uart,data,input,[7:0],signal,A01\n"
        "uart,irq,out,,signal,B02\n"
        "uart,VDD,bidir,,power,C01\n",
        encoding="utf-8-sig",
    )
    view = parse_pin_csv(path)
    assert view.complete
    ports = {port.name: port for port in view.components[0].ports}
    assert ports["data"].shape.width == 8
    assert ports["data"].shape.ascending is False
    assert ports["irq"].direction == Direction.OUTPUT
    assert ports["VDD"].direction == Direction.INOUT
    assert ports["VDD"].role == PortRole.POWER
    assert view.pin_mappings[0].package_ball == "A01"


def test_component_pins_profile_does_not_invent_package_mappings(tmp_path: Path) -> None:
    path = tmp_path / "component-pins.csv"
    path.write_text(
        "component,signal,direction,width\nuart,irq,output,1\n",
        encoding="utf-8",
    )

    view = parse_pin_csv(path, profile="component_pins")

    assert view.complete
    assert view.components[0].ports[0].name == "irq"
    assert view.pin_mappings == ()
    assert view.attributes["profile"] == "component_pins"


def test_package_map_profile_requires_both_physical_endpoint_columns(tmp_path: Path) -> None:
    path = tmp_path / "incomplete-package.csv"
    path.write_text("component,signal,die_pad\nuart,irq,PAD_IRQ\n", encoding="utf-8")

    view = parse_pin_csv(path, profile="package_map")

    assert not view.complete
    assert view.components == ()
    assert view.pin_mappings == ()
    assert "package_ball" in view.diagnostics[0].message


def test_unknown_csv_profile_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "pins.csv"
    path.write_text("signal\nirq\n", encoding="utf-8")

    with pytest.raises(ValueError, match="CSV profile"):
        parse_pin_csv(path, profile="spreadsheet")


def test_one_row_per_bit_is_aggregated_without_losing_order(tmp_path: Path) -> None:
    path = tmp_path / "bits.csv"
    path.write_text(
        "component,pin,direction,bit\n"
        "uart,D,input,3\n"
        "uart,D,input,2\n"
        "uart,D,input,1\n"
        "uart,D,input,0\n",
        encoding="utf-8",
    )
    port = parse_pin_csv(path).components[0].ports[0]
    assert port.shape.width == 4
    assert port.shape.bit_indices == (3, 2, 1, 0)


def test_custom_column_map_is_source_to_canonical(tmp_path: Path) -> None:
    path = tmp_path / "vendor.csv"
    path.write_text(
        "Logical Name;I/O Kind;Device\nirq;output;uart\n",
        encoding="utf-8",
    )
    view = parse_pin_csv(
        path,
        column_map={
            "Logical Name": "signal",
            "I/O Kind": "direction",
            "Device": "component",
        },
    )
    assert view.components[0].name == "uart"
    assert view.components[0].ports[0].direction == Direction.OUTPUT


def test_quoted_newline_is_read_as_one_logical_row(tmp_path: Path) -> None:
    path = tmp_path / "quoted.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["component", "signal", "direction", "domain"])
        writer.writerow(["uart", "alert", "output", "always-on,\nsecure"])
    view = parse_pin_csv(path)
    assert view.complete
    assert len(view.components[0].ports) == 1
    assert view.components[0].ports[0].attributes["domains"] == ["always-on,\nsecure"]


def test_duplicate_logical_headers_are_not_silently_accepted(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.csv"
    path.write_text("signal,pin,direction\nirq,irq,output\n", encoding="utf-8")
    view = parse_pin_csv(path)
    assert not view.complete
    assert "*" in view.tainted_scopes
    assert any(diagnostic.code == "OC5006" for diagnostic in view.diagnostics)


def test_invalid_direction_taints_component(tmp_path: Path) -> None:
    path = tmp_path / "invalid.csv"
    path.write_text("component,signal,direction\nuart,irq,sideways\n", encoding="utf-8")
    view = parse_pin_csv(path)
    assert not view.complete
    assert view.components[0].status == FactState.KNOWN
    assert view.components[0].ports[0].state_for("direction") == FactState.UNSUPPORTED


@pytest.mark.parametrize(
    ("headers", "values", "invalid_field"),
    [
        ("width", "wide", "width"),
        ("bit", "lsb", "bit"),
        ("left,right", "msb,0", "left"),
        ("left,right", "7,lsb", "right"),
    ],
)
def test_nonnumeric_dimensions_taint_shape_instead_of_inventing_scalar(
    tmp_path: Path,
    headers: str,
    values: str,
    invalid_field: str,
) -> None:
    path = tmp_path / f"invalid-{invalid_field}.csv"
    path.write_text(
        f"component,signal,{headers}\nuart,data,{values}\n",
        encoding="utf-8",
    )

    view = parse_pin_csv(path)

    port = view.components[0].ports[0]
    assert not view.complete
    assert "uart" in view.tainted_scopes
    assert port.shape.width is None
    assert port.shape.explicit_scalar is not True
    assert port.state_for("shape") == FactState.TAINTED
    assert view.components[0].status == FactState.TAINTED
    assert view.pin_mappings[0].status == FactState.TAINTED
    assert any(
        diagnostic.code == "OC5006"
        and invalid_field in diagnostic.message
        and "nonnumeric" in diagnostic.message
        for diagnostic in view.diagnostics
    )

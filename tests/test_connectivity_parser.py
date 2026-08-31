from __future__ import annotations

from pathlib import Path

from opencollate.model import (
    ConnectivityExpectation,
    ConnectivityTransform,
    FactState,
    ViewId,
)
from opencollate.parsers.connectivity import parse_connectivity_csv


def test_connectivity_csv_parses_bounded_declarative_intent(tmp_path: Path) -> None:
    source = tmp_path / "connectivity.csv"
    source.write_text(
        "id,source,sink,expect,transform,through,exclude,description\n"
        "UART_TX,top/apb_data[3:0],top/u_uart/data[3:0],reachable,identity,"
        "top/u_xbar,top/test_mux,APB transmit path\n"
        "NO_SECRET,top/secret,top/debug,unreachable,any,,,Isolation\n",
        encoding="utf-8",
    )

    view = parse_connectivity_csv(source, view_name="intent")

    assert view.view == ViewId("connectivity", "intent")
    assert view.complete
    assert not view.diagnostics
    assert len(view.connectivity_requirements) == 2
    required, forbidden = view.connectivity_requirements
    assert required.identifier == "UART_TX"
    assert required.expectation == ConnectivityExpectation.REACHABLE
    assert required.transform == ConnectivityTransform.IDENTITY
    assert required.through == ("top/u_xbar",)
    assert required.exclude == ("top/test_mux",)
    assert forbidden.expectation == ConnectivityExpectation.UNREACHABLE


def test_connectivity_csv_taints_duplicate_and_contradictory_rows(tmp_path: Path) -> None:
    source = tmp_path / "duplicates.csv"
    source.write_text(
        "id,source,sink,expect\nPATH,top/a,top/y,reachable\nPATH,top/a,top/y,unreachable\n",
        encoding="utf-8",
    )

    view = parse_connectivity_csv(source)

    assert not view.complete
    assert [item.code for item in view.diagnostics] == ["OC6509"]
    assert all(item.status == FactState.TAINTED for item in view.connectivity_requirements)


def test_connectivity_csv_rejects_unknown_temporal_columns_and_bad_selectors(
    tmp_path: Path,
) -> None:
    temporal = tmp_path / "temporal.csv"
    temporal.write_text(
        "id,source,sink,expect,latency\nP,top/a,top/y,reachable,2\n",
        encoding="utf-8",
    )
    malformed = tmp_path / "malformed.csv"
    malformed.write_text(
        "id,source,sink,expect\nP,../top/a,top/y[foo],reachable\n",
        encoding="utf-8",
    )

    temporal_view = parse_connectivity_csv(temporal)
    malformed_view = parse_connectivity_csv(malformed)

    assert not temporal_view.complete
    assert temporal_view.diagnostics[0].code == "OC1101"
    assert not temporal_view.connectivity_requirements
    assert not malformed_view.complete
    assert malformed_view.diagnostics[0].code == "OC1101"
    assert not malformed_view.connectivity_requirements


def test_connectivity_csv_enforces_file_limit_before_reading(
    tmp_path: Path, monkeypatch: object
) -> None:
    import opencollate.parsers.connectivity as connectivity

    source = tmp_path / "large.csv"
    source.write_text("id,source,sink,expect\nP,top/a,top/y,reachable\n", encoding="utf-8")
    monkeypatch.setattr(connectivity, "_MAX_FILE_BYTES", 8)  # type: ignore[attr-defined]

    view = connectivity.parse_connectivity_csv(source)

    assert not view.complete
    assert view.diagnostics[0].code == "OC1101"
    assert "exceeds" in view.diagnostics[0].message

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollate.cli import main


def test_systemrdl_source_runs_through_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "registers.rdl").write_text(
        "addrmap uart_regs {\n"
        "  reg { field { sw = rw; reset = 0; } ENABLE[0:0]; } CTRL @ 0x10;\n"
        "};\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        "[project]\nname = 'systemrdl-cli'\n"
        "[sources.systemrdl.registers]\n"
        "files = ['registers.rdl']\n"
        "top = 'uart_regs'\n"
        "component_name = 'uart0'\n",
        encoding="utf-8",
    )

    assert main(["check", str(manifest), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["registers"] == 1
    assert report["diagnostics"] == []


def test_systemrdl_cli_preserves_explicit_compilation_unit_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "z_types.rdl").write_text(
        "reg shared_status_t { field { sw = r; hw = w; } DONE[0:0]; };\n",
        encoding="utf-8",
    )
    (tmp_path / "a_top.rdl").write_text(
        "addrmap multi_top { shared_status_t STATUS @ 0x20; };\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        "[project]\nname = 'ordered-systemrdl'\n"
        "[sources.systemrdl.registers]\n"
        "files = ['z_types.rdl', 'a_top.rdl']\n"
        "top = 'multi_top'\n",
        encoding="utf-8",
    )

    assert main(["check", str(manifest), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["registers"] == 1
    assert report["diagnostics"] == []


def test_systemrdl_and_software_header_address_drift_is_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "registers.rdl").write_text(
        "addrmap uart_regs {\n"
        "  reg { field { sw = rw; reset = 0; } ENABLE[0:0]; } CTRL @ 0x10;\n"
        "};\n",
        encoding="utf-8",
    )
    (tmp_path / "uart.h").write_text(
        "#define UART_CTRL_OFFSET 0x14U\n"
        "#define UART_CTRL_ENABLE_Pos 0U\n"
        "#define UART_CTRL_ENABLE_Msk (1U << UART_CTRL_ENABLE_Pos)\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        "[project]\nname = 'systemrdl-header-drift'\n"
        "[sources.systemrdl.registers]\n"
        "files = ['registers.rdl']\n"
        "top = 'uart_regs'\n"
        "component_name = 'uart0'\n"
        "[sources.header.software]\n"
        "files = ['uart.h']\n"
        "component_name = 'uart0'\n"
        "macro_prefix = 'UART'\n"
        "default_register_width = 32\n",
        encoding="utf-8",
    )

    assert main(["check", str(manifest), "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    finding = next(item for item in report["diagnostics"] if item["code"] == "OC6302")
    assert "uart0/ctrl" in finding["message"]
    assert "C header (software) offset=20" in finding["message"]
    assert "SystemRDL (registers) offset=16" in finding["message"]
    assert {item["view"] for item in finding["evidence"]} == {
        "header.software",
        "systemrdl.registers",
    }


def test_static_connectivity_source_runs_through_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "top.sv").write_text(
        "module top(input logic a, output logic y);\nassign y = a;\nendmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "connectivity.csv").write_text(
        "id,source,sink,expect,transform\nDATA_PATH,top/a,top/y,reachable,identity\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        "[project]\nname = 'connectivity-cli'\n"
        "[sources.rtl.default]\nfiles = ['top.sv']\ntop = 'top'\n"
        "[sources.connectivity.intent]\nfiles = ['connectivity.csv']\n",
        encoding="utf-8",
    )

    assert main(["check", str(manifest), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["diagnostics"] == []


def test_capabilities_advertise_vnext_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["capabilities", "--json"]) == 0
    value = json.loads(capsys.readouterr().out)

    assert value["formats"]["systemrdl_2_0"]["backend"] == "systemrdl-compiler"
    assert value["formats"]["connectivity_csv"]["backend"] == "native-bounded-static"
    assert "report-diff-json" in value["outputs"]


def test_inapplicable_generic_source_field_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "cell.lib").write_text(
        "library(x) { cell(c) { pin(A) { direction : input; } } }\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        "[sources.liberty.tt]\nfiles = ['cell.lib']\nprofile = 'package_map'\n",
        encoding="utf-8",
    )

    assert main(["check", str(manifest)]) == 2
    captured = capsys.readouterr()
    assert "liberty.tt: source field(s) are not supported for this format: profile" in captured.err
    assert "Traceback" not in captured.err


def test_systemrdl_include_directories_are_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "registers.rdl").write_text("addrmap top {};\n", encoding="utf-8")
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        "[sources.systemrdl.registers]\nfiles = ['registers.rdl']\ninclude_dirs = ['includes']\n",
        encoding="utf-8",
    )

    assert main(["check", str(manifest)]) == 2
    captured = capsys.readouterr()
    assert (
        "systemrdl.registers: source field(s) are not supported for this format: include_dirs"
        in captured.err
    )
    assert "Traceback" not in captured.err


def test_systemrdl_empty_mapping_define_is_a_controlled_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "registers.rdl").write_text("addrmap top {};\n", encoding="utf-8")
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        "[sources.systemrdl.registers]\nfiles = ['registers.rdl']\n"
        "[sources.systemrdl.registers.defines]\n\"\" = '1'\n",
        encoding="utf-8",
    )

    assert main(["check", str(manifest)]) == 2
    captured = capsys.readouterr()
    assert "contains an empty define" in captured.err
    assert "Traceback" not in captured.err


def test_upf_overlong_component_name_is_a_controlled_option_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "power.upf").write_text("set_design_top top\n", encoding="utf-8")
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        "[sources.upf.power]\nfiles = ['power.upf']\ncomponent_name = '" + ("x" * 16_385) + "'\n",
        encoding="utf-8",
    )

    assert main(["check", str(manifest)]) == 2
    captured = capsys.readouterr()
    assert "must not exceed 16,384 characters" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("top_cells", "message"),
    (
        (["top", "top"], "contains duplicate name"),
        (["töp"], "must be 7-bit ASCII"),
        (["x" * 16_385], "must not exceed 16,384 bytes"),
    ),
)
def test_gds_top_cell_value_errors_are_controlled_cli_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    top_cells: list[str],
    message: str,
) -> None:
    (tmp_path / "top.gds").write_bytes(b"")
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        "[sources.gds.layout]\nfiles = ['top.gds']\ntop_cells = "
        + json.dumps(top_cells, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    assert main(["check", str(manifest)]) == 2
    captured = capsys.readouterr()
    assert message in captured.err
    assert "Traceback" not in captured.err

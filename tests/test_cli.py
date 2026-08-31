from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from opencollate.cli import main

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples" / "uart" / "opencollate.toml"


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "Catch SoC design-collateral drift" in capsys.readouterr().out


def test_version_uses_argparse_version_action(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as captured:
        main(["--version"])
    assert captured.value.code == 0
    assert capsys.readouterr().out.strip() == "OpenCollate 0.1.0"


def test_capabilities_text_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["capabilities"]) == 0
    assert "verilog/systemverilog" in capsys.readouterr().out
    assert main(["capabilities", "--json"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["formats"]["liberty"]["status"] == "supported"
    assert value["rules"] >= 40


def test_explain_known_and_unknown_rule(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["explain", "oc4101"]) == 0
    assert "width-mismatch" in capsys.readouterr().out
    assert main(["explain", "OC0000"]) == 2
    captured = capsys.readouterr()
    assert "OC1001" in captured.err
    assert "unknown OpenCollate rule" in captured.err


def test_init_is_non_destructive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "project"
    assert main(["init", str(target)]) == 0
    manifest = target / "opencollate.toml"
    assert manifest.is_file()
    assert "[sources.rtl.default]" in manifest.read_text(encoding="utf-8")
    assert main(["init", str(target)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_schema_commands_emit_valid_schemas(tmp_path: Path) -> None:
    for kind in ("report", "contract"):
        target = tmp_path / f"{kind}.schema.json"
        assert main(["schema", kind, "--output", str(target)]) == 0
        schema = json.loads(target.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)


def test_schema_output_directory_is_an_actionable_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["schema", "report", "--output", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "OC1001" in captured.err
    assert "cannot write output" in captured.err
    assert str(tmp_path) in captured.err
    assert "Traceback" not in captured.err


def test_example_check_text_is_actionable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", str(EXAMPLE), "--verbose"]) == 1
    output = capsys.readouterr().out
    assert "ERROR OC4001" in output
    assert "ERROR OC4301" in output
    assert "ERROR OC5003" in output
    assert "fingerprint:" in output
    assert "FAIL: 3 error(s)" in output


def test_example_json_validates_against_bundled_schema(tmp_path: Path) -> None:
    report = tmp_path / "nested" / "report.json"
    schema = tmp_path / "report.schema.json"
    assert main(["check", str(EXAMPLE), "--format", "json", "-o", str(report)]) == 1
    assert main(["schema", "report", "-o", str(schema)]) == 0
    value = json.loads(report.read_text(encoding="utf-8"))
    Draft202012Validator(json.loads(schema.read_text(encoding="utf-8"))).validate(value)
    assert value["summary"]["errors"] == 3


@pytest.mark.parametrize(
    ("format_name", "needle"),
    [("sarif", '"version": "2.1.0"'), ("markdown", "# OpenCollate report")],
)
def test_machine_and_pr_outputs(
    format_name: str,
    needle: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check", str(EXAMPLE), "--format", format_name]) == 1
    assert needle in capsys.readouterr().out


def test_contract_build_writes_schema_valid_contract(tmp_path: Path) -> None:
    contract = tmp_path / "contract.oc.json"
    schema = tmp_path / "contract.schema.json"
    assert main(["contract", "build", str(EXAMPLE), "-o", str(contract)]) == 0
    assert main(["schema", "contract", "-o", str(schema)]) == 0
    value = json.loads(contract.read_text(encoding="utf-8"))
    Draft202012Validator(json.loads(schema.read_text(encoding="utf-8"))).validate(value)
    uart = value["components"][0]
    assert uart["canonical_name"] == "uart"


def test_demo_materializes_and_runs_without_failure_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    demo = tmp_path / "demo"
    assert main(["demo", "--output-dir", str(demo)]) == 0
    output = capsys.readouterr().out
    assert "Synthetic demo:" in output
    assert "OC4001" in output
    assert (demo / "opencollate.toml").is_file()
    assert main(["demo", "--output-dir", str(demo)]) == 2
    assert "already contains" in capsys.readouterr().err


def test_demo_strict_exit_returns_expected_check_status(tmp_path: Path) -> None:
    assert (
        main(
            [
                "demo",
                "--output-dir",
                str(tmp_path / "strict"),
                "--strict-exit",
                "--format",
                "json",
            ]
        )
        == 1
    )


def test_missing_and_unknown_inputs_return_usage_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check", str(tmp_path / "missing.toml")]) == 2
    assert "OC1002" in capsys.readouterr().err

    (tmp_path / "design.gds").write_bytes(b"GDS")
    manifest = tmp_path / "unknown.toml"
    manifest.write_text(
        '[project]\nname="unknown"\n[sources.gds.default]\nfiles=["design.gds"]\n',
        encoding="utf-8",
    )
    assert main(["check", str(manifest)]) == 2
    captured = capsys.readouterr()
    assert "no parser is registered" in captured.err


def test_config_cannot_be_passed_twice(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", str(EXAMPLE), "--config", str(EXAMPLE)]) == 2
    assert "either positionally" in capsys.readouterr().err


def test_unknown_parser_option_is_an_actionable_config_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "cell.lef"
    source.write_text("MACRO cell\nEND cell\n", encoding="utf-8")
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        '[sources.lef.default]\nfiles=["cell.lef"]\ngeometry_mode="guess"\n',
        encoding="utf-8",
    )

    assert main(["check", str(manifest)]) == 2
    captured = capsys.readouterr()
    assert "OC1001" in captured.err
    assert "unsupported source option(s): geometry_mode" in captured.err


def test_invalid_csv_delimiter_is_an_actionable_config_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "pins.csv").write_text("Signal,Direction\nirq,output\n", encoding="utf-8")
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        '[sources.csv.package]\nfiles=["pins.csv"]\ndelimiter="||"\n',
        encoding="utf-8",
    )

    assert main(["check", str(manifest)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "OC1001" in captured.err
    assert "csv.package: source option 'delimiter' must be exactly one character" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "delimiter_toml",
    ('"\\n"', '"\\r"', "'\"'"),
)
def test_reserved_csv_delimiter_is_an_actionable_config_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    delimiter_toml: str,
) -> None:
    (tmp_path / "pins.csv").write_text("Signal,Direction\nirq,output\n", encoding="utf-8")
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        f'[sources.csv.package]\nfiles=["pins.csv"]\ndelimiter={delimiter_toml}\n',
        encoding="utf-8",
    )

    assert main(["check", str(manifest)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "OC1001" in captured.err
    assert "not a valid CSV delimiter" in captured.err
    assert "Traceback" not in captured.err


def test_component_pins_csv_profile_does_not_require_package_columns(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "pins.csv"
    source.write_text(
        "component,signal,direction,width\nuart,irq,output,1\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        "[project]\nname = 'component-pins'\n"
        "[sources.csv.pins]\nfiles = ['pins.csv']\nprofile = 'component_pins'\n",
        encoding="utf-8",
    )

    assert main(["check", str(manifest), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["components"] == 1
    assert report["summary"]["ports"] == 1
    assert not any(item["code"] == "OC5006" for item in report["diagnostics"])

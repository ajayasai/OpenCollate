from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from opencollate import cli as cli_module
from opencollate.baseline import MAX_REPORT_JSON_NESTING, diff_reports
from opencollate.cli import main
from opencollate.reporters.diff import (
    render_diff_json,
    render_diff_markdown,
    render_diff_sarif,
    render_diff_text,
)

EXAMPLE = Path(__file__).parents[1] / "examples" / "uart" / "opencollate.toml"


def _finding(
    fingerprint: str,
    message: str,
    *,
    severity: str = "error",
    waived: bool = False,
) -> dict[str, object]:
    return {
        "code": "OC4001",
        "severity": severity,
        "message": message,
        "fingerprint": fingerprint,
        "waived": waived,
        "suppressed": waived,
        "object": {"kind": "port", "id": "component:x/port:y", "display": "x/y"},
        "evidence": [{"view": "rtl.default", "value": "output"}],
    }


def _report(*findings: dict[str, object], exit_code: int = 1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "tool": {"name": "OpenCollate", "version": "test"},
        "project": "test",
        "status": "pass" if exit_code == 0 else "fail",
        "exit_code": exit_code,
        "summary": {
            "errors": len(findings),
            "warnings": 0,
            "notes": 0,
            "suppressed": 0,
            "views": 1,
            "components": 1,
            "ports": 1,
            "registers": 0,
        },
        "diagnostics": list(findings),
    }


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_diff_renderers_expose_baseline_states() -> None:
    baseline = _report(_finding("same", "old"), _finding("gone", "resolved"))
    current = _report(_finding("same", "changed"), _finding("added", "new"))
    diff = diff_reports(baseline, current)

    text = render_diff_text(diff)
    markdown = render_diff_markdown(diff)
    json_value = json.loads(render_diff_json(diff))
    sarif = json.loads(render_diff_sarif(diff))

    assert "1 new, 1 changed, 1 resolved" in text
    assert "| changed |" in markdown
    assert json_value["summary"]["new"] == 1
    assert {item["baselineState"] for item in sarif["runs"][0]["results"]} == {
        "absent",
        "new",
        "updated",
    }


def test_diff_json_validates_against_bundled_schema(tmp_path: Path) -> None:
    baseline = _report(_finding("same", "old"), _finding("gone", "resolved"))
    current = _report(_finding("same", "changed"), _finding("added", "new"))
    diff_path = tmp_path / "diff.json"
    schema_path = tmp_path / "diff.schema.json"
    _write(diff_path, diff_reports(baseline, current).to_dict())

    assert main(["schema", "diff", "--output", str(schema_path)]) == 0
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(diff_path.read_text(encoding="utf-8")))


def test_report_diff_cli_is_non_gating_by_default_and_can_ratchet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline_path = tmp_path / "baseline.json"
    current_path = tmp_path / "current.json"
    _write(baseline_path, _report())
    _write(current_path, _report(_finding("added", "new")))

    assert main(["report", "diff", str(baseline_path), str(current_path)]) == 0
    assert "1 new" in capsys.readouterr().out
    assert (
        main(
            [
                "report",
                "diff",
                str(baseline_path),
                str(current_path),
                "--fail-on",
                "new",
            ]
        )
        == 1
    )


def test_report_diff_fatal_cannot_be_hidden_by_gate_or_waiver(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    current_path = tmp_path / "current.json"
    _write(baseline_path, _report())
    _write(
        current_path,
        _report(_finding("fatal", "parser failed", severity="fatal", waived=True), exit_code=2),
    )

    assert (
        main(
            [
                "report",
                "diff",
                str(baseline_path),
                str(current_path),
                "--fail-on",
                "none",
            ]
        )
        == 2
    )


def test_review_same_live_result_passes_and_writes_current_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    assert main(["check", str(EXAMPLE), "--format", "json", "-o", str(baseline)]) == 1

    assert (
        main(
            [
                "review",
                str(EXAMPLE),
                "--baseline",
                str(baseline),
                "--write-report",
                str(current),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "0 new, 0 changed" in output
    assert json.loads(current.read_text(encoding="utf-8"))["diagnostics"]


def test_review_new_finding_fails_changed_ratchet(tmp_path: Path) -> None:
    current = tmp_path / "full.json"
    baseline = tmp_path / "baseline.json"
    assert main(["check", str(EXAMPLE), "--format", "json", "-o", str(current)]) == 1
    value = json.loads(current.read_text(encoding="utf-8"))
    value["diagnostics"] = value["diagnostics"][1:]
    _write(baseline, value)

    assert main(["review", str(EXAMPLE), "--baseline", str(baseline)]) == 1


def test_review_rejects_baseline_output_alias_without_modifying_baseline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "full.json"
    assert main(["check", str(EXAMPLE), "--format", "json", "-o", str(current)]) == 1
    value = json.loads(current.read_text(encoding="utf-8"))
    value["diagnostics"] = value["diagnostics"][1:]
    _write(baseline, value)
    original = baseline.read_bytes()

    assert (
        main(
            [
                "review",
                str(EXAMPLE),
                "--baseline",
                str(baseline),
                "--write-report",
                str(baseline),
            ]
        )
        == 2
    )
    assert baseline.read_bytes() == original
    assert "must not alias the same file" in capsys.readouterr().err


def test_saved_diff_rejects_output_alias_without_modifying_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    _write(baseline, _report())
    _write(current, _report(_finding("new", "finding")))
    original = current.read_bytes()

    assert (
        main(
            [
                "report",
                "diff",
                str(baseline),
                str(current),
                "--output",
                str(current),
            ]
        )
        == 2
    )
    assert current.read_bytes() == original
    assert "must not alias the same file" in capsys.readouterr().err


def test_report_diff_rejects_invalid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "broken.json"
    current = tmp_path / "current.json"
    baseline.write_text("{", encoding="utf-8")
    _write(current, _report())

    assert main(["report", "diff", str(baseline), str(current)]) == 2
    assert "cannot parse baseline report" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["review", "diff"])
def test_saved_report_cli_rejects_invalid_utf8(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    baseline = tmp_path / "invalid-utf8.json"
    current = tmp_path / "current.json"
    baseline.write_bytes(b'{"schema_version": 1, "bad": "\xff"}')
    _write(current, _report())
    argv = (
        ["review", str(EXAMPLE), "--baseline", str(baseline)]
        if command == "review"
        else ["report", "diff", str(baseline), str(current)]
    )

    assert main(argv) == 2
    error = capsys.readouterr().err
    assert "cannot decode baseline report" in error
    assert "UTF-8" in error
    assert "Traceback" not in error


def test_saved_report_cli_size_limit_accepts_boundary_and_rejects_one_byte_over(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "oversized.json"
    current = tmp_path / "current.json"
    _write(baseline, _report())
    _write(current, _report())
    boundary = len(baseline.read_bytes())
    monkeypatch.setattr(cli_module, "MAX_REPORT_JSON_BYTES", boundary)

    assert main(["report", "diff", str(baseline), str(current)]) == 0
    capsys.readouterr()

    monkeypatch.setattr(cli_module, "MAX_REPORT_JSON_BYTES", boundary - 1)

    assert main(["report", "diff", str(baseline), str(current)]) == 2
    error = capsys.readouterr().err
    assert "cannot read baseline report" in error
    assert f"{boundary - 1:,}-byte limit" in error


def test_saved_report_recursion_error_is_converted_to_cli_status_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    _write(baseline, _report())
    _write(current, _report())

    def fail_decode(_: str, **_kwargs: object) -> object:
        raise RecursionError("decoder recursion")

    monkeypatch.setattr(cli_module.json, "loads", fail_decode)

    assert main(["report", "diff", str(baseline), str(current)]) == 2
    error = capsys.readouterr().err
    assert "JSON nesting exceeds the supported limit" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("optimized", [False, True])
@pytest.mark.parametrize("command", ["review", "diff"])
def test_deep_saved_report_cli_fails_cleanly_in_normal_and_optimized_python(
    tmp_path: Path,
    optimized: bool,
    command: str,
) -> None:
    baseline = tmp_path / "deep.json"
    current = tmp_path / "current.json"
    nested: object = 0
    for _ in range(MAX_REPORT_JSON_NESTING + 1):
        nested = [nested]
    finding = _finding("deep", "nested")
    finding["metadata"] = nested
    _write(baseline, _report(finding))
    _write(current, _report())
    arguments = (
        ["review", str(EXAMPLE), "--baseline", str(baseline)]
        if command == "review"
        else ["report", "diff", str(baseline), str(current)]
    )
    invocation = [sys.executable]
    if optimized:
        invocation.append("-O")
    invocation.extend(("-m", "opencollate", *arguments))

    result = subprocess.run(
        invocation,
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 2
    assert "cannot parse baseline report" in result.stderr
    assert "JSON nesting exceeds the limit" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("optimized", [False, True])
@pytest.mark.parametrize("command", ["review", "diff"])
def test_oversized_json_integer_cli_fails_cleanly_in_normal_and_optimized_python(
    tmp_path: Path,
    optimized: bool,
    command: str,
) -> None:
    baseline = tmp_path / "huge-integer.json"
    current = tmp_path / "current.json"
    baseline.write_text('{"value":' + "9" * 5000 + "}", encoding="utf-8")
    _write(current, _report())
    arguments = (
        ["review", str(EXAMPLE), "--baseline", str(baseline)]
        if command == "review"
        else ["report", "diff", str(baseline), str(current)]
    )
    invocation = [sys.executable]
    if optimized:
        invocation.append("-O")
    invocation.extend(("-m", "opencollate", *arguments))

    result = subprocess.run(
        invocation,
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "PYTHONINTMAXSTRDIGITS": "4300"},
    )

    assert result.returncode == 2
    assert "cannot parse baseline report" in result.stderr
    assert "invalid JSON value" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("exit_code", [None, "2", True, -1, 3])
def test_report_diff_rejects_untrustworthy_current_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    exit_code: object,
) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    _write(baseline, _report())
    current_value = _report()
    current_value["exit_code"] = exit_code
    _write(current, current_value)

    assert main(["report", "diff", str(baseline), str(current)]) == 2
    assert "current report exit_code must be 0, 1, or 2" in capsys.readouterr().err

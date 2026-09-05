from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from opencollate import symbolic
from opencollate.cli import _schema_text, main
from opencollate.config import load_config
from opencollate.formal import run_obligations
from tests.test_formal import request


def fixture(tmp_path: Path, *, backend: str = "z3", mutate: bool = False) -> Path:
    names = [f"A{i:03}" for i in range(64)]
    (tmp_path / "gate.sv").write_text(
        "module gate("
        + ", ".join("input wire " + name for name in names)
        + ", output wire Y);\n"
        + "assign Y = "
        + " & ".join(names)
        + ";\nendmodule\n"
    )
    expression = "!(" + " | ".join("!" + name for name in (names[:-1] if mutate else names)) + ")"
    (tmp_path / "gate.lib").write_text(
        "library(lib) { cell(gate) {\n"
        + "\n".join("pin(" + name + ") { direction : input; }" for name in names)
        + '\npin(Y) { direction : output; function : "'
        + expression
        + '"; }\n} }\n'
    )
    path = tmp_path / "opencollate.toml"
    path.write_text(
        'schema_version=1\n[sources.rtl.default]\nfiles=["gate.sv"]\n'
        '[sources.liberty.default]\nfiles=["gate.lib"]\n[policy]\n'
        f'boolean_backend="{backend}"\nmax_symbolic_inputs=128\n'
    )
    return path


@pytest.mark.parametrize("mutate", [False, True])
def test_actual_rtl_liberty_pipeline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mutate: bool
) -> None:
    path = fixture(tmp_path, mutate=mutate)
    assert load_config(path).policy.boolean_backend == "z3"
    assert main(["check", str(path), "--format", "json"]) == int(mutate)
    report = json.loads(capsys.readouterr().out)
    assert [d["code"] for d in report["diagnostics"]] == (["OC4301"] if mutate else [])
    if mutate:
        values = report["diagnostics"][0]["metadata"]["counterexample"]
        assert values["A063"] is False
        assert all(values[name] is True for name in values if name != "A063")


def test_legacy_limit_remains_compatible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = fixture(tmp_path, backend="truth_table")
    main(["check", str(path), "--format", "json"])
    assert [d["code"] for d in json.loads(capsys.readouterr().out)["diagnostics"]] == ["OC4302"]


def test_missing_selected_solver_forces_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fixture(tmp_path)

    def unavailable(_: str) -> object:
        raise ModuleNotFoundError("no z3")

    monkeypatch.setattr(symbolic, "import_module", unavailable)
    assert main(["check", str(path), "--format", "json"]) == 2
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["severity"] == "fatal"


@pytest.mark.parametrize(
    ("right", "guard"), [("A", "1"), ("!A", "1"), ("A", "S&!S"), ("A?B:C", "1")]
)
def test_published_schemas(right: str, guard: str) -> None:
    value = request(right=right, assume=guard)
    receipt = run_obligations(value)
    Draft202012Validator(json.loads(_schema_text("formal-request"))).validate(value)
    Draft202012Validator(json.loads(_schema_text("formal-receipt"))).validate(receipt)


def test_html_cli_and_capability(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = fixture(tmp_path, mutate=True)
    output = tmp_path / "review.html"
    assert main(["check", str(path), "--format", "html", "-o", str(output)]) == 1
    assert "Source evidence" in output.read_text()
    assert main(["capabilities", "--json"]) == 0
    capabilities = json.loads(capsys.readouterr().out)
    assert "html" in capabilities["outputs"]
    assert capabilities["symbolic_boolean"]["opt_in"] is True

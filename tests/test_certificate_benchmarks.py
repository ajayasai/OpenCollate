from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmarks.boolean_certificates import main, run_suite


def test_public_certificate_corpus_and_export(tmp_path: Path) -> None:
    first = run_suite(1, export_dir=tmp_path)
    second = run_suite(1)
    assert first["status"] == "pass"
    assert len(first["cases"]) == 15
    assert len(first["poison_controls"]) == 8
    assert all(row["rejected"] for row in first["poison_controls"])
    assert first["result_sha256"] == second["result_sha256"]
    assert len(list(tmp_path.glob("*.cnf"))) == 7
    assert len(list(tmp_path.glob("*.certificate.json"))) == 15
    for formula in tmp_path.glob("*.cnf"):
        proof = formula.with_suffix(".rup")
        assert proof.read_text().splitlines()[-1] == "0"
        assert formula.read_text().startswith("p cnf ")


@pytest.mark.parametrize("repeat", [0, -1, True, 21, "3"])
def test_repeat_bounds(repeat: object) -> None:
    with pytest.raises(ValueError):
        run_suite(repeat)


def test_benchmark_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "results.json"
    assert main(["--repeat", "1", "--json-output", str(output)]) == 0
    assert json.loads(output.read_text())["status"] == "pass"
    assert main(["--repeat", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    with pytest.raises(SystemExit):
        main(["--repeat", "0"])

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from opencollate import certificates
from opencollate.certificates import certify_obligations
from opencollate.cli import _schema_text, main
from opencollate.config import load_config
from tests.test_formal import request
from tests.test_symbolic_integration import fixture


@pytest.mark.parametrize("mutate", [False, True])
def test_source_rtl_liberty_certified_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mutate: bool
) -> None:
    path = fixture(tmp_path, backend="certified", mutate=mutate)
    assert load_config(path).policy.boolean_backend == "certified"
    assert main(["check", str(path), "--format", "json"]) == int(mutate)
    report = json.loads(capsys.readouterr().out)
    assert [row["code"] for row in report["diagnostics"]] == (["OC4301"] if mutate else [])
    if mutate:
        witness = report["diagnostics"][0]["metadata"]["counterexample"]
        assert witness == {f"A{i:03}": i != 63 for i in range(64)}


@pytest.mark.parametrize("backend", ["z3", "certified"])
def test_unsupported_declared_liberty_function_cannot_vanish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: str
) -> None:
    path = fixture(tmp_path, backend=backend)
    lib = tmp_path / "gate.lib"
    text = lib.read_text()
    start, end = text.index('function : "'), text.index('";', text.index('function : "'))
    lib.write_text(text[:start] + 'function : "A000?A001:A002' + text[end:])
    assert main(["check", str(path), "--format", "json"]) == 2
    diagnostics = json.loads(capsys.readouterr().out)["diagnostics"]
    assert any(d["code"] == "OC4302" and d["severity"] == "fatal" for d in diagnostics)
    assert any(d["code"] == "OC1102" for d in diagnostics)


def test_missing_certificate_producer_is_fatal_in_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fixture(tmp_path, backend="certified")

    def absent(_: str) -> object:
        raise ModuleNotFoundError("deliberately unavailable")

    monkeypatch.setattr(certificates, "import_module", absent)
    assert main(["check", str(path), "--format", "json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert any(d["code"] == "OC4302" and d["severity"] == "fatal" for d in report["diagnostics"])


@pytest.mark.parametrize(
    ("right", "guard", "code"), [("A", "1", 0), ("!A", "1", 1), ("A", "S&!S", 2), ("A?B:C", "1", 2)]
)
def test_cli_and_published_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], right: str, guard: str, code: int
) -> None:
    source, output = tmp_path / "request.json", tmp_path / "proof.json"
    value = request(right=right, assume=guard)
    source.write_text(json.dumps(value))
    assert main(["formal", "certify", str(source), "-o", str(output)]) == code
    bundle = json.loads(output.read_text())
    Draft202012Validator(json.loads(_schema_text("formal-certificate"))).validate(bundle)
    assert main(["formal", "verify-certificate", str(source), str(output)]) == code
    result = json.loads(capsys.readouterr().out)
    assert result["solver_invoked"] is False
    assert result["exit_code"] == code


def test_schema_disallows_zero_literals_and_missing_nonvacuity_witness() -> None:
    validator = Draft202012Validator(json.loads(_schema_text("formal-certificate")))
    bundle = certify_obligations(request())
    bundle["results"][0]["proof"] = [[0], []]
    assert list(validator.iter_errors(bundle))
    bundle = certify_obligations(request())
    bundle["results"][0]["guard_witness"] = None
    assert list(validator.iter_errors(bundle))


@pytest.mark.parametrize(
    "option",
    [
        "--timeout-ms",
        "--max-variables",
        "--resource-limit",
        "--max-proof-steps",
        "--max-proof-work",
    ],
)
def test_cli_rejects_zero_limits(tmp_path: Path, option: str) -> None:
    source = tmp_path / "request.json"
    source.write_text(json.dumps(request()))
    assert main(["formal", "certify", str(source), option, "0"]) == 2


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_certificate_output_cannot_clobber_request(tmp_path: Path, alias: str) -> None:
    source = tmp_path / "request.json"
    source.write_text(json.dumps(request()))
    output = source if alias == "same" else tmp_path / "alias.json"
    if alias == "symlink":
        try:
            output.symlink_to(source)
        except OSError:
            pytest.skip("symlinks unavailable on this platform")
    elif alias == "hardlink":
        os.link(source, output)
    assert main(["formal", "certify", str(source), "-o", str(output)]) == 2
    assert json.loads(source.read_text()) == request()


def test_verification_cannot_clobber_its_certificate(tmp_path: Path) -> None:
    source, certificate = tmp_path / "request.json", tmp_path / "certificate.json"
    source.write_text(json.dumps(request()))
    assert main(["formal", "certify", str(source), "-o", str(certificate)]) == 0
    original = certificate.read_bytes()
    assert (
        main(
            ["formal", "verify-certificate", str(source), str(certificate), "-o", str(certificate)]
        )
        == 2
    )
    assert certificate.read_bytes() == original


def test_duplicate_keys_forgery_and_strict_verifier_budget(tmp_path: Path) -> None:
    source, certificate = tmp_path / "request.json", tmp_path / "certificate.json"
    source.write_text('{"schema_version":1,"schema_version":1}')
    assert main(["formal", "certify", str(source)]) == 2
    source.write_text(json.dumps(request("A&B", "B&A")))
    assert main(["formal", "certify", str(source), "-o", str(certificate)]) == 0
    assert (
        main(
            ["formal", "verify-certificate", str(source), str(certificate), "--max-variables", "1"]
        )
        == 2
    )
    certificate.write_text('{"status":"pass","status":"pass"}')
    assert main(["formal", "verify-certificate", str(source), str(certificate)]) == 2


def test_capabilities_do_not_upgrade_sequential_trust(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["capabilities", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["boolean_certificates"]["independent_unsat_certificate_checking"] is True
    assert result["boolean_certificates"]["mechanically_verified_checker"] is False
    assert result["sequential_verification"]["independent_unsat_certificate_checking"] is False
    output = tmp_path / "schema.json"
    assert main(["schema", "formal-certificate", "-o", str(output)]) == 0
    assert json.loads(output.read_text())["title"] == "OpenCollate independent Boolean certificate"

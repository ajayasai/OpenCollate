from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from benchmarks.sequential import case_specs

from opencollate.cli import main
from opencollate.proof_kernel import Limits, Meter, ProofError
from opencollate.sequential_certificate import (
    _binding,
    _check_cover,
    _cover,
    _evidence,
    _load,
    _proof_rows,
    _solve,
    certify,
    verify_certificate,
)
from opencollate.sequential_cnf import base_problem, digest, step_problem
from opencollate.sequential_ir import SequentialError

PIPE = """module pipe #(parameter W=8)(
 input logic clk,rst_n,en,input logic [W-1:0] d,output logic [W-1:0] q);
 always_ff @(posedge clk) begin
   if (!rst_n) q <= '0;
   else if (en) q <= d;
 end
endmodule
"""


def project(tmp: Path, rtl: str = PIPE, **extra: Any) -> dict[str, Any]:
    (tmp / "design.sv").write_text(rtl, encoding="utf-8")
    return {
        "schema_version": 1,
        "semantics": "two-valued-synchronous",
        "files": ["design.sv"],
        "top": "pipe",
        "clock": "clk",
        "depth": 6,
        "induction": 4,
        "reset": {"signal": "rst_n", "active": 0},
        "properties": [
            {
                "id": "data",
                "source": "d",
                "sink": "q",
                "latency": 1,
                "when": [{"signal": "en", "equals": 1, "lag": 1}],
            }
        ],
        **extra,
    }


def checksum(doc: dict[str, Any]) -> dict[str, Any]:
    doc["certificate_sha256"] = digest({k: v for k, v in doc.items() if k != "certificate_sha256"})
    return doc


@pytest.mark.parametrize("width", [1, 8, 64, 256])
def test_pipeline_certificate_and_independent_replay(tmp_path: Path, width: int) -> None:
    request = project(tmp_path, PIPE.replace("W=8", f"W={width}"))
    certificate = certify(request, root=tmp_path)
    assert certificate == certify(request, root=tmp_path)
    result = verify_certificate(request, certificate, root=tmp_path)
    assert result["exit_code"] == 0 and result["status"] == "certificate-verified"
    assert result["results"][0]["induction_depth"] == 1
    assert result["results"][0]["cover_cycle"] >= 2
    assert certificate["claims"][0]["base"]["proof"][-1] == []
    assert certificate["claims"][0]["step"]["proof"][-1] == []


def test_k_two_is_required_and_certified(tmp_path: Path) -> None:
    rtl = """module pipe(input logic clk,output logic q1);
    logic q0;
    always_ff @(posedge clk) begin q0 <= 0; q1 <= q0; end
    endmodule"""
    request = project(
        tmp_path,
        rtl,
        reset=None,
        properties=[{"id": "settled", "sink": "q1", "equals": 0, "start_cycle": 2}],
    )
    certificate = certify(request, root=tmp_path)
    assert certificate["claims"][0]["induction_depth"] == 2
    assert verify_certificate(request, certificate, root=tmp_path)["exit_code"] == 0
    certificate["claims"][0]["induction_depth"] = 1
    c, spec = _load(request, tmp_path)
    step = step_problem(c, spec, spec["properties"][0], 1, Meter())
    certificate["claims"][0]["step"] = _evidence(step, [[]])
    with pytest.raises(ProofError, match="RUP"):
        verify_certificate(request, checksum(certificate), root=tmp_path)


@pytest.mark.parametrize(
    "name,rtl,specification,expected",
    case_specs()[4:],
    ids=lambda v: v if isinstance(v, str) and len(v) < 40 else None,
)
def test_negative_source_corpus_never_issues_a_certificate(
    tmp_path: Path, name: str, rtl: str, specification: dict, expected: str
) -> None:
    (tmp_path / "design.sv").write_text(rtl, encoding="utf-8")
    with pytest.raises((ProofError, SequentialError)):
        certify(specification, root=tmp_path)


def test_no_initial_state_or_reset_value_is_invented(tmp_path: Path) -> None:
    request = project(
        tmp_path,
        reset=None,
        properties=[{"id": "qzero", "sink": "q", "equals": 0, "start_cycle": 0}],
    )
    with pytest.raises(ProofError, match="counterexample"):
        certify(request, root=tmp_path)


def test_multiple_properties_and_history(tmp_path: Path) -> None:
    example = Path("examples/sequential")
    for p in example.glob("*.sv"):
        (tmp_path / p.name).write_bytes(p.read_bytes())
    request = json.loads((example / "request.json").read_text())
    certificate = certify(request, root=tmp_path)
    assert len(certificate["claims"]) == 3
    assert len(verify_certificate(request, certificate, root=tmp_path)["results"]) == 3
    swapped = copy.deepcopy(certificate)
    swapped["claims"].reverse()
    with pytest.raises(ProofError, match="identity"):
        verify_certificate(request, checksum(swapped), root=tmp_path)
    duplicate = copy.deepcopy(certificate)
    duplicate["claims"][1] = duplicate["claims"][0]
    with pytest.raises(ProofError, match="identity"):
        verify_certificate(request, checksum(duplicate), root=tmp_path)


@pytest.mark.parametrize("change", ["source", "assumptions", "property", "depth"])
def test_source_and_request_binding(tmp_path: Path, change: str) -> None:
    request = project(tmp_path)
    certificate = certify(request, root=tmp_path)
    if change == "source":
        (tmp_path / "design.sv").write_text(PIPE + "\n// modified\n")
    elif change == "assumptions":
        request["assumptions"] = {"en": 1}
    elif change == "property":
        request["properties"][0]["latency"] = 2
    else:
        request["depth"] = 5
    with pytest.raises(ProofError, match="binding"):
        verify_certificate(request, certificate, root=tmp_path)


def test_rehashing_everything_cannot_forge_proof_for_changed_rtl(tmp_path: Path) -> None:
    request = project(tmp_path)
    certificate = certify(request, root=tmp_path)
    (tmp_path / "design.sv").write_text(PIPE.replace("q <= d", "q <= ~d"))
    c, spec = _load(request, tmp_path)
    prop = spec["properties"][0]
    unroll, base, cover = base_problem(c, spec, prop, Meter())
    sat, model = _solve(cover, Meter(), proof=False)
    assert sat
    row = certificate["claims"][0]
    row["cover"] = _cover(unroll, model, prop)
    _check_cover(c, spec, prop, row["cover"], Meter())
    row["base"] = _evidence(base, row["base"]["proof"])
    step = step_problem(c, spec, prop, row["induction_depth"], Meter())
    row["step"] = _evidence(step, row["step"]["proof"])
    certificate["binding"] = _binding(c, spec)
    # All metadata and the new source's reachable witness are legitimate;
    # the old proof is nevertheless not a refutation of the broken circuit.
    with pytest.raises(ProofError, match="RUP"):
        verify_certificate(request, checksum(certificate), root=tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        "checksum",
        "extra",
        "version-bool",
        "version",
        "semantics",
        "encoding",
        "claims-type",
        "claims-missing",
        "id",
        "k-bool",
        "k-zero",
        "k-large",
        "base-hash",
        "base-count",
        "base-vars-bool",
        "base-extra",
        "proof-type",
        "proof-truncated",
        "proof-corrupt",
        "step-hash",
    ],
)
def test_malformed_and_tampered_certificates_fail_closed(tmp_path: Path, change: str) -> None:
    request = project(tmp_path)
    certificate = certify(request, root=tmp_path)
    row = certificate["claims"][0]
    if change == "checksum":
        certificate["certificate_sha256"] = "0" * 64
    elif change == "extra":
        certificate["trusted"] = True
    elif change.startswith("version"):
        certificate["schema_version"] = True if change.endswith("bool") else 2
    elif change in {"semantics", "encoding"}:
        certificate[change] = "different"
    elif change == "claims-type":
        certificate["claims"] = {}
    elif change == "claims-missing":
        certificate["claims"] = []
    elif change == "id":
        row["id"] = "other"
    elif change.startswith("k-"):
        row["induction_depth"] = {"k-bool": True, "k-zero": 0, "k-large": 128}[change]
    elif change == "base-hash":
        row["base"]["problem_sha256"] = "0" * 64
    elif change == "base-count":
        row["base"]["clauses"] += 1
    elif change == "base-vars-bool":
        row["base"]["variables"] = True
    elif change == "base-extra":
        row["base"]["accept"] = True
    elif change == "proof-type":
        row["base"]["proof"] = "unsat"
    elif change == "proof-truncated":
        row["base"]["proof"] = row["base"]["proof"][:-1]
    elif change == "proof-corrupt":
        row["base"]["proof"] = [[False], []]
    elif change == "step-hash":
        row["step"]["problem_sha256"] = "0" * 64
    if change != "checksum":
        checksum(certificate)
    with pytest.raises((ProofError, SequentialError)):
        verify_certificate(request, certificate, root=tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "short",
        "long",
        "cycle",
        "bool-cycle",
        "frame-type",
        "inventory",
        "bool-value",
        "range",
        "reset",
        "transition",
        "guard",
        "assumption",
        "comb",
    ],
)
def test_guard_witness_replayed_not_trusted(tmp_path: Path, change: str) -> None:
    rtl = PIPE.replace(
        "output logic [W-1:0] q", "output logic [W-1:0] q, output wire [W-1:0] mirror"
    )
    rtl = rtl.replace("endmodule", "assign mirror = q; endmodule")
    request = project(tmp_path, rtl, assumptions={"en": 1})
    certificate = certify(request, root=tmp_path)
    c, spec = _load(request, tmp_path)
    prop = spec["properties"][0]
    witness = certificate["claims"][0]["cover"]
    if change == "empty":
        witness = []
    elif change == "short":
        witness = witness[:1]
    elif change == "long":
        witness = witness * 100
    elif change == "cycle":
        witness[0]["cycle"] = -1
    elif change == "bool-cycle":
        witness[0]["cycle"] = False
    elif change == "frame-type":
        witness[0]["values"] = []
    elif change == "inventory":
        witness[0]["values"]["injected"] = 0
    elif change == "bool-value":
        witness[0]["values"]["q"] = True
    elif change == "range":
        witness[0]["values"]["q"] = 256
    elif change == "reset":
        witness[0]["values"]["rst_n"] = 1
    elif change == "transition":
        witness[1]["values"]["q"] ^= 1
    elif change == "guard":
        # Check a different guard against the same genuine witness, isolated
        # from metadata binding checks so this exercises guard validation.
        prop["when"] = [{"signal": "en", "equals": 0, "lag": 1}]
    elif change == "assumption":
        witness[0]["values"]["en"] = 0
    elif change == "comb":
        witness[0]["values"]["mirror"] ^= 1
    with pytest.raises((ProofError, SequentialError)):
        _check_cover(c, spec, prop, witness, Meter())


def test_solver_free_verification_in_fresh_process(tmp_path: Path) -> None:
    request = project(tmp_path)
    certificate = certify(request, root=tmp_path)
    (tmp_path / "request.json").write_text(json.dumps(request))
    (tmp_path / "certificate.json").write_text(json.dumps(certificate))
    script = """
import sys, importlib.abc, json
from pathlib import Path
class BlockSolvers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pysat', 'z3'}:
            raise AssertionError('Solver import attempted: ' + fullname)
sys.meta_path.insert(0, BlockSolvers())
from opencollate.sequential_certificate import verify_certificate
root = Path(sys.argv[1])
r = verify_certificate(json.loads((root/'request.json').read_text()),
                       json.loads((root/'certificate.json').read_text()), root=root)
assert r['exit_code'] == 0
assert not any(n.split('.')[0] in {'pysat','z3'} for n in sys.modules)
print('solver-free verification passed')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "solver-free verification passed" in result.stdout


def test_cli_output_safety_and_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    request = project(tmp_path)
    request_path, output = tmp_path / "request.json", tmp_path / "certificate.json"
    request_path.write_text(json.dumps(request))
    assert main(["sequential", "certify", str(request_path), "-o", str(output)]) == 0
    assert main(["sequential", "verify-certificate", str(request_path), str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "certificate-verified"
    for target in (request_path, tmp_path / "design.sv", output):
        before = target.read_bytes()
        assert (
            main(
                [
                    "sequential",
                    "verify-certificate",
                    str(request_path),
                    str(output),
                    "-o",
                    str(target),
                ]
            )
            == 2
        )
        assert target.read_bytes() == before
    link = tmp_path / "hard-link.json"
    os.link(request_path, link)
    assert main(["sequential", "certify", str(request_path), "-o", str(link)]) == 2
    assert json.loads(request_path.read_text()) == request
    before = output.read_bytes()
    for options in (["--proof-work", "1"], ["--timeout-ms", "0"], ["--timeout-ms", "120001"]):
        assert main(["sequential", "certify", str(request_path), "-o", str(output), *options]) == 2
        assert output.read_bytes() == before
    assert main(["sequential", "certify", str(request_path), "-o", str(tmp_path)]) == 2
    (tmp_path / "design.sv").write_text(PIPE.replace("q <= d", "q <= ~d"))
    assert main(["sequential", "certify", str(request_path), "-o", str(output)]) == 2
    assert output.read_bytes() == before
    request_path.write_text('{"depth": 5, "depth": 6}')
    assert main(["sequential", "certify", str(request_path)]) == 2


@pytest.mark.parametrize("limits", [Limits(work=1), Limits(variables=1), Limits(clauses=1)])
def test_resource_exhaustion_never_certifies(tmp_path: Path, limits: Limits) -> None:
    request = project(tmp_path)
    with pytest.raises(ProofError):
        certify(request, root=tmp_path, limits=limits)
    certificate = certify(request, root=tmp_path)
    with pytest.raises(ProofError):
        verify_certificate(request, certificate, root=tmp_path, limits=limits)


@pytest.mark.parametrize("lines", [None, "unsat", [1], ["1"], ["0 0"], ["999 0"], ["frog 0"]])
def test_malformed_producer_drup_rejected(lines: Any) -> None:
    with pytest.raises((ProofError, ValueError)):
        _proof_rows(lines, 2, Meter())


def test_drup_deletions_only_omit_already_unneeded_steps() -> None:
    assert _proof_rows(["1 0", "d 1 0", "0"], 2, Meter()) == [[1], []]
    assert _proof_rows([], 2, Meter()) == [[]]
    assert _proof_rows(["1 0"], 2, Meter()) == [[1], []]
    with pytest.raises(ProofError):
        _proof_rows(["1 0", "0"], 2, Meter(Limits(steps=1)))
    with pytest.raises(ProofError):
        _proof_rows(["1 2 0"], 2, Meter(Limits(literals=1)))


@pytest.mark.parametrize("mode", ["unknown", "invalid-model", "false-model", "bogus-proof"])
def test_untrusted_producer_cannot_create_false_evidence(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    import opencollate.sequential_certificate as module

    class FakeSolver:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeSolver:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def interrupt(self) -> None:
            pass

        def solve_limited(self, **kwargs: Any) -> bool | None:
            return {
                "unknown": None,
                "invalid-model": True,
                "false-model": True,
                "bogus-proof": False,
            }[mode]

        def get_model(self) -> Any:
            return "model" if mode == "invalid-model" else [-1]

        def get_proof(self) -> list[str]:
            return ["0"]

    class FakeModule:
        Solver = FakeSolver

    monkeypatch.setattr(module.importlib, "import_module", lambda name: FakeModule)
    with pytest.raises(ProofError):
        _solve({"variables": 1, "clauses": [[1]]}, Meter(), proof=True)


def test_optional_producer_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import opencollate.sequential_certificate as module

    def missing(name: str) -> Any:
        raise ImportError("not installed")

    monkeypatch.setattr(module.importlib, "import_module", missing)
    with pytest.raises(ProofError, match="certificates"):
        _solve({"variables": 1, "clauses": [[1]]}, Meter(), proof=True)


def test_machine_schemas_and_capabilities(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    import jsonschema

    request = project(tmp_path)
    certificate = certify(request, root=tmp_path)
    result = verify_certificate(request, certificate, root=tmp_path)
    for name, doc in (
        ("sequential-certificate", certificate),
        ("certificate-verification", result),
    ):
        assert main(["schema", name]) == 0
        schema = json.loads(capsys.readouterr().out)
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(doc, schema)
        jsonschema.validate(
            {
                "schema_version": 1,
                "status": "not-certified",
                "exit_code": 2,
                "error": "example failure",
            },
            schema,
        )
        damaged = copy.deepcopy(doc)
        damaged["unknown"] = True
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(damaged, schema)
    assert main(["capabilities", "--json"]) == 0
    capabilities = json.loads(capsys.readouterr().out)
    assert capabilities["proof_certificates"]["independent_unsat_certificate_checking"] is True
    assert capabilities["proof_certificates"]["verification_requires_solver"] is False
    assert capabilities["proof_certificates"]["mechanically_verified_kernel"] is False


def test_benchmark_certificates_and_rejections() -> None:
    from benchmarks.certificates import run_suite

    result = run_suite(repeat=1)
    assert result["status"] == "pass"
    assert len(result["cases"]) == 12
    assert sum(c["expected"] == "certificate-verified" for c in result["cases"]) == 4
    assert all(c["pass"] for c in result["cases"])
    for repeat in (0, 11, True):
        with pytest.raises(ValueError):
            run_suite(repeat=repeat)

"""Producer-independent certificate checks, differential oracles, and forgery tests."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings

from opencollate import certificates
from opencollate.boolean import BoolVar, parse_boolean
from opencollate.certificates import (
    _proof_lines,
    certify_boolean,
    certify_obligations,
    verify_certificate,
)
from opencollate.formal import _digest
from opencollate.proof_kernel import ProofError, ProofLimits
from tests.test_formal import request
from tests.test_symbolic import expr


def seal(bundle: dict) -> dict:
    bundle["certificate_sha256"] = _digest(
        {k: v for k, v in bundle.items() if k != "certificate_sha256"}
    )
    return bundle


@pytest.mark.parametrize(
    ("left", "right", "guard", "status", "code"),
    [
        ("A&B", "B A", "1", "equivalent", 0),
        ("!(A&B)", "!A|!B", "1", "equivalent", 0),
        ("A^B^C", "C^A^B", "1", "equivalent", 0),
        ("A|B", "A&B", "1", "different", 1),
        ("(S&A)|(!S&B)", "A", "S", "equivalent", 0),
        ("(S&A)|(!S&B)", "A", "!S", "different", 1),
        ("A", "A", "S&!S", "vacuous", 2),
        ("0", "1", "1", "different", 1),
        ("1", "1", "1", "equivalent", 0),
        ("0", "0", "0", "vacuous", 2),
        ("A?B:C", "A", "1", "inconclusive", 2),
    ],
)
def test_certificate_outcomes(left: str, right: str, guard: str, status: str, code: int) -> None:
    source = request(left, right, guard)
    bundle = certify_obligations(source)
    assert bundle["results"][0]["status"] == status
    assert bundle["exit_code"] == code
    checked = verify_certificate(source, bundle)
    assert checked["exit_code"] == code
    assert checked["solver_invoked"] is False
    assert checked["checked_results"] == int(status != "inconclusive")


@given(expr, expr, expr)
@settings(max_examples=160, derandomize=True, deadline=None)
def test_against_exhaustive_oracle(left: str, right: str, guard: str) -> None:
    a, b, g = map(parse_boolean, (left, right, guard))
    names = sorted(a.variables() | b.variables() | g.variables())
    guard_witness = counterexample = None
    for bits in product((False, True), repeat=len(names)):
        values = dict(zip(names, bits, strict=True))
        if g.evaluate(values):
            if guard_witness is None:
                guard_witness = values
            if a.evaluate(values) != b.evaluate(values) and counterexample is None:
                counterexample = values
    source = request(left, right, guard)
    bundle = certify_obligations(source)
    row = bundle["results"][0]
    assert row["status"] == (
        "vacuous"
        if guard_witness is None
        else "different"
        if counterexample is not None
        else "equivalent"
    )
    assert row["guard_witness"] == guard_witness
    assert row["counterexample"] == counterexample
    assert verify_certificate(source, bundle)["checked_results"] == 1


@pytest.mark.parametrize("width", [64, 128, 512])
def test_wide_de_morgan_certificates(width: int) -> None:
    names = [f"A{i:04}" for i in range(width)]
    source = request("&".join(names), "!(" + "|".join("!" + name for name in names) + ")")
    first = certify_obligations(source)
    assert first["status"] == "pass", first["results"]
    assert first == certify_obligations(source)
    assert verify_certificate(source, first)["exit_code"] == 0
    assert len(first["results"][0]["variables"]) == width


def test_threaded_producers_do_not_share_native_state() -> None:
    source = request("A^B^C", "C^A^B")
    with ThreadPoolExecutor(max_workers=4) as executor:
        bundles = list(executor.map(certify_obligations, [source] * 12))
    assert all(bundle == bundles[0] for bundle in bundles)
    assert bundles[0]["status"] == "pass"


def test_aliases_and_non_string_ir() -> None:
    assert certify_boolean("X^B", "0", aliases={"X": "A", "B": "A"})["status"] == "equivalent"
    assert (
        certify_boolean(BoolVar("arbitrary not-a-script"), BoolVar("arbitrary not-a-script"))[
            "status"
        ]
        == "equivalent"
    )


def test_result_order_is_canonical_and_incompleteness_dominates() -> None:
    source = request(right="!A")
    source["obligations"].append({"id": "another", "left": "B", "right": "B", "assume": "0"})
    reverse = copy.deepcopy(source)
    reverse["obligations"].reverse()
    bundle = certify_obligations(source)
    assert bundle == certify_obligations(reverse)
    assert bundle["exit_code"] == 2
    assert verify_certificate(reverse, bundle)["checked_results"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"semantics": "sequential"},
        {"encoding": "trust-me"},
        {"request_sha256": "0" * 64},
        {"unknown": 1},
        {"results": []},
        {"results": [None]},
        {"exit_code": True},
        {"exit_code": 1},
        {"status": "fail"},
    ],
)
def test_bundle_forgery_with_recomputed_digest(change: dict) -> None:
    source = request()
    bundle = certify_obligations(source)
    bundle.update(change)
    with pytest.raises(ProofError):
        verify_certificate(source, seal(bundle))


@pytest.mark.parametrize(
    "change",
    [
        {"id": "another"},
        {"status": "proven"},
        {"status": []},
        {"variables": []},
        {"variables": ["A", "A"]},
        {"obligation_sha256": "0" * 64},
        {"cnf_sha256": "0" * 64},
        {"guard_witness": None},
        {"guard_witness": {"A": 0}},
        {"guard_witness": {"A": False, "B": False}},
        {"proof": None},
        {"proof": []},
        {"proof": [[0], []]},
        {"proof": [[], []]},
        {"counterexample": {"A": False}},
        {"reason": False},
        {"reason": "x" * 4097},
        {"extra": "ignore me"},
    ],
)
def test_row_forgery_with_recomputed_digest(change: dict) -> None:
    source = request()
    bundle = certify_obligations(source)
    bundle["results"][0].update(change)
    with pytest.raises(ProofError):
        verify_certificate(source, seal(bundle))


def test_rehashed_false_unsat_claim_is_rejected_by_logic_not_digest() -> None:
    source = request("A", "!A")
    bundle = certify_obligations(source)
    bundle.update(status="pass", exit_code=0)
    bundle["results"][0].update(status="equivalent", counterexample=None, proof=[[]])
    with pytest.raises(ProofError, match="unit propagation"):
        verify_certificate(source, seal(bundle))


def test_guard_witness_is_not_optional_logical_evidence() -> None:
    source = request("A", "A", "S")
    bundle = certify_obligations(source)
    bundle["results"][0]["guard_witness"]["S"] = False
    with pytest.raises(ProofError, match="assumptions witness"):
        verify_certificate(source, seal(bundle))


@pytest.mark.parametrize(
    "change", [{"counterexample": {"A": False}}, {"guard_witness": {"A": False}}, {"proof": None}]
)
def test_vacuous_evidence_cannot_assert_a_witness(change: dict) -> None:
    source = request(assume="0")
    bundle = certify_obligations(source)
    bundle["results"][0].update(change)
    with pytest.raises(ProofError):
        verify_certificate(source, seal(bundle))


@pytest.mark.parametrize(
    "change",
    [{"counterexample": {"A": False, "B": False}}, {"proof": [[]]}, {"counterexample": None}],
)
def test_counterexamples_are_replayed(change: dict) -> None:
    source = request("A", "B")
    bundle = certify_obligations(source)
    bundle["results"][0].update(change)
    with pytest.raises(ProofError):
        verify_certificate(source, seal(bundle))


def test_request_and_checksum_substitutions_are_rejected() -> None:
    source = request()
    bundle = certify_obligations(source)
    for other in (request(right="!A"), request(assume="S"), request(left="B", right="B")):
        with pytest.raises(ProofError, match="different request"):
            verify_certificate(other, bundle)
    bundle["certificate_sha256"] = "0" * 64
    with pytest.raises(ProofError, match="digest"):
        verify_certificate(source, bundle)


@pytest.mark.parametrize(
    "change",
    [
        {"proof": [[]]},
        {"reason": ""},
        {"reason": 5},
        {"variables": None},
        {"variables": [True]},
        {"obligation_sha256": "not a digest"},
    ],
)
def test_inconclusive_rows_cannot_smuggle_evidence(change: dict) -> None:
    source = request(right="A?B:C")
    bundle = certify_obligations(source)
    bundle["results"][0].update(change)
    with pytest.raises(ProofError):
        verify_certificate(source, seal(bundle))


@pytest.mark.parametrize(
    "limits",
    [
        ProofLimits(max_variables=1),
        ProofLimits(max_nodes=1),
        ProofLimits(max_clauses=1),
        ProofLimits(max_literals=1),
        ProofLimits(max_work=1),
    ],
)
def test_resource_exhaustion_is_inconclusive(limits: ProofLimits) -> None:
    source = request("A&B", "!( !A | !B )")
    bundle = certify_obligations(source, limits=limits)
    assert bundle["status"] == "inconclusive"
    assert bundle["results"][0]["proof"] is None
    assert bundle["exit_code"] == 2


def test_verifier_budget_cannot_be_overridden_by_certificate() -> None:
    source = request("A&B", "B&A")
    bundle = certify_obligations(source)
    with pytest.raises(ProofError, match="limit"):
        verify_certificate(source, bundle, limits=ProofLimits(max_variables=1))


def test_total_deadline_never_resets_per_query(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter((0.0, 100.0))
    monkeypatch.setattr(certificates.time, "monotonic", lambda: next(ticks))
    assert certify_boolean("A", "A")["status"] == "inconclusive"


@pytest.mark.parametrize(
    "error", [ModuleNotFoundError("no pysat"), RuntimeError("native crash"), SystemExit(0)]
)
def test_optional_backend_failure_cannot_establish_pass(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    def broken(_: str) -> object:
        raise error

    monkeypatch.setattr(certificates, "import_module", broken)
    row = certify_boolean("A", "A")
    assert row["status"] == "inconclusive"
    assert row["proof"] is row["counterexample"] is row["guard_witness"] is None


def test_lying_unsat_producer_cannot_establish_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(certificates, "_solve", lambda *_: (None, [[]]))
    assert certify_boolean("A", "A")["status"] == "inconclusive"


@pytest.mark.parametrize(
    "native",
    [
        None,
        ["1"],
        ["--1 0"],
        ["+1 0"],
        ["1.0 0"],
        ["١ 0"],
        ["0 0"],
        ["3 0"],
        ["1 -3 0"],
        [True],
        ["d"],
        ["r 1 0"],
    ],
)
def test_native_trace_parser_is_strict(native: object) -> None:
    with pytest.raises(ProofError):
        _proof_lines(native, 2, ProofLimits())


def test_native_deletions_do_not_change_logical_database() -> None:
    assert _proof_lines(["1 1 0", "d 1 0", "1 -1 0", "0"], 2, ProofLimits()) == [[1], []]
    assert _proof_lines([], 2, ProofLimits()) == [[]]
    with pytest.raises(ProofError):
        _proof_lines(["1 0"], 2, ProofLimits(max_steps=1))
    with pytest.raises(ProofError):
        _proof_lines(["1 2 0"], 2, ProofLimits(max_literals=1))
    with pytest.raises(ProofError):
        _proof_lines(["1 " * 20 + "0"], 2, ProofLimits(max_literals=1))


class FakeSolver:
    """Native boundary adversary. No reference implementation is reused."""

    def __init__(
        self, *, answer: object = True, model: object = None, answers: list | None = None
    ) -> None:
        self.answer, self.model = answer, model
        self.answers = iter(answers) if answers else None
        self.closed = False

    def __enter__(self) -> FakeSolver:
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True

    def conf_budget(self, _: int) -> None:
        pass

    def solve_limited(self, **_: object) -> object:
        return next(self.answers) if self.answers is not None else self.answer

    def get_model(self) -> object:
        return self.model

    def get_proof(self) -> list:
        return []

    def interrupt(self) -> None:
        assert not self.closed


@pytest.mark.parametrize(
    "solver",
    [
        FakeSolver(answer=None),
        FakeSolver(answer="unsat"),
        FakeSolver(model=None),
        FakeSolver(model=[True]),
        FakeSolver(model=[0]),
        FakeSolver(model=[1, -1]),
        FakeSolver(model=[1, 1]),
        FakeSolver(model=[1]),
        FakeSolver(model=[1, 2]),
        FakeSolver(model=[1, -2], answers=[True, True, False]),
    ],
)
def test_native_status_and_model_faults_fail_closed(
    monkeypatch: pytest.MonkeyPatch, solver: FakeSolver
) -> None:
    # Exercise the native adapter with a deliberately fake solver on every OS;
    # the real Windows boundary is separately tested in test_certificate_process.
    monkeypatch.setattr(certificates, "_needs_process", lambda: False)
    monkeypatch.setattr(
        certificates, "import_module", lambda _: SimpleNamespace(Glucose3=lambda **_: solver)
    )
    assert certify_boolean("A", "A")["status"] == "inconclusive"
    assert solver.closed


def test_verification_imports_no_sat_or_smt_solver(tmp_path: Path) -> None:
    source = request("A^B^C", "C^A^B")
    bundle = certify_obligations(source)
    assert bundle["status"] == "pass"
    path = tmp_path / "certificate.json"
    path.write_text(json.dumps([source, bundle]))
    script = """
import importlib.abc, json, sys
class BlockSolver(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'pysat', 'z3', 'pysolvers'}:
            raise RuntimeError('solver import attempted: ' + fullname)
sys.meta_path.insert(0, BlockSolver())
from opencollate.certificates import verify_certificate
with open(sys.argv[1], encoding='utf-8') as stream:
    source, certificate = json.load(stream)
result = verify_certificate(source, certificate)
if result['exit_code'] != 0 or result['solver_invoked'] is not False:
    raise SystemExit(3)
if any(name.split('.')[0] in {'pysat', 'z3', 'pysolvers'} for name in sys.modules):
    raise SystemExit(4)
print(json.dumps(result))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["solver_invoked"] is False

"""Native-process boundary: evidence is untrusted even after a clean exit."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollate import _certificate_worker as worker
from opencollate import certificate_process as transport
from opencollate import certificates
from opencollate.boolean_proof_kernel import ProofError, ProofLimits, verify_rup
from opencollate.proof_cnf import compile_boolean
from tests.test_certificates import FakeSolver


def job() -> dict:
    return {"cnf": [[1]], "inputs": {}, "variables": 1, "limits": asdict(ProofLimits())}


def isolated(left: str = "A", right: str = "A", guard: str = "1") -> tuple:
    compiled = compile_boolean(left, right, guard)
    budget = ProofLimits()
    return transport.solve_isolated(
        compiled.cnf("difference"),
        compiled.encoder.inputs,
        compiled.encoder.variables,
        budget,
        time.monotonic() + 5,
    )


@pytest.mark.parametrize(
    "left,right,guard,status",
    [
        ("!(A&B)", "!A|!B", "1", "equivalent"),
        ("A^B^C", "C^A^B", "1", "equivalent"),
        ("A", "!A", "1", "different"),
        ("A", "A", "S", "equivalent"),
        ("A", "A", "S&!S", "vacuous"),
    ],
)
def test_real_isolated_certificates(
    monkeypatch: pytest.MonkeyPatch, left: str, right: str, guard: str, status: str
) -> None:
    monkeypatch.setattr(certificates, "_needs_process", lambda: True)
    result = certificates.certify_boolean(left, right, assume=guard)
    assert result["status"] == status, result
    if status in {"equivalent", "vacuous"}:
        compiled = compile_boolean(left, right, guard)
        query = "guard" if status == "vacuous" else "difference"
        verify_rup(compiled.cnf(query), result["proof"], compiled.encoder.variables)


def test_real_worker_accepts_arbitrary_names_without_executing_them() -> None:
    name = "not Python code: __import__('os').abort()"
    witness, proof = transport.solve_isolated(
        [(1,), (2,)], {name: 2}, 2, ProofLimits(), time.monotonic() + 5
    )
    assert witness == {name: True}
    assert proof is None


def respond(monkeypatch: pytest.MonkeyPatch, data: bytes, code: int = 0) -> list[Path]:
    paths: list[Path] = []

    def run(args: list, **kwargs: object) -> SimpleNamespace:
        assert args[:4] == [sys.executable, "-I", "-m", "opencollate._certificate_worker"]
        assert kwargs["close_fds"] is True
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        output = Path(args[-1])
        paths.append(output.parent)
        output.write_bytes(data)
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(transport.subprocess, "run", run)
    return paths


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"error":null,"error":null}',
        b'{"x":NaN}',
        b'{"witness":null,"proof":[],"error":null}',
        b'{"witness":null,"proof":null,"error":null}',
        b'{"witness":{},"proof":null,"error":null}',
        b'{"witness":{"A":1},"proof":null,"error":null}',
        b'{"witness":{"A":false},"proof":[[]],"error":null}',
        b'{"witness":null,"proof":null,"error":3}',
        b'{"witness":null,"proof":null,"error":""}',
        b'{"witness":null,"proof":null,"error":"failed"}',
    ],
)
def test_malformed_worker_output_is_rejected_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
) -> None:
    paths = respond(monkeypatch, raw)
    with pytest.raises((ValueError, ProofError)):
        isolated()
    assert paths and all(not path.exists() for path in paths)


@pytest.mark.parametrize("code", [-11, -1073740791, 1, 2])
def test_crashed_process_cannot_submit_plausible_evidence(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    paths = respond(monkeypatch, b'{"witness":null,"proof":[[]],"error":null}', code)
    with pytest.raises(ProofError, match="abnormally"):
        isolated()
    assert not paths[0].exists()


def test_clean_exit_is_not_logical_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(certificates, "_needs_process", lambda: True)
    respond(monkeypatch, b'{"witness":null,"proof":[[]],"error":null}')
    row = certificates.certify_boolean("A", "A")
    assert row["status"] == "inconclusive"
    assert "unit propagation" in row["reason"]


def test_oversized_proof_inventory_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    budget = ProofLimits(max_steps=1)
    respond(monkeypatch, b'{"witness":null,"proof":[[],[]],"error":null}')
    with pytest.raises(ProofError, match="proof size"):
        transport.solve_isolated([(1,)], {}, 1, budget, time.monotonic() + 5)


def test_missing_response_never_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    with pytest.raises(OSError):
        isolated()


def test_real_child_crash_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    actual_run = subprocess.run

    def crash(args: list, **kwargs: object) -> subprocess.CompletedProcess:
        return actual_run([sys.executable, "-I", "-c", "import os;os._exit(91)"], **kwargs)

    monkeypatch.setattr(transport.subprocess, "run", crash)
    with pytest.raises(ProofError, match="abnormally.*91"):
        isolated()


def test_real_stalled_child_is_killed_and_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    actual_run = subprocess.run

    def stall(args: list, **kwargs: object) -> subprocess.CompletedProcess:
        return actual_run([sys.executable, "-I", "-c", "import time;time.sleep(60)"], **kwargs)

    monkeypatch.setattr(transport.subprocess, "run", stall)
    budget = ProofLimits(timeout_ms=100)
    started = time.monotonic()
    with pytest.raises(ProofError, match="deadline"):
        transport.solve_isolated([(1,)], {}, 1, budget, started + 0.1)
    assert time.monotonic() - started < 10


def test_completed_after_deadline_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    respond(monkeypatch, b'{"witness":null,"proof":[[]],"error":null}')
    ticks = iter((0.0, 10.0))
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ProofError, match="during producer"):
        transport.solve_isolated([(1,)], {}, 1, ProofLimits(), 1.0)


def test_expired_deadline_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport.subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(ProofError, match="before producer"):
        transport.solve_isolated([(1,)], {}, 1, ProofLimits(), time.monotonic() - 1)


def test_transport_read_and_write_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "message.json"
    monkeypatch.setattr(transport, "MAX_MESSAGE_BYTES", 16)
    with pytest.raises(ProofError, match="transport limit"):
        transport.write_message(path, {"a": "a" * 20})
    path.write_bytes(b" " * 17)
    with pytest.raises(ProofError, match="transport limit"):
        transport.read_message(path)
    transport.write_message(tmp_path / "valid.json", {"a": True})
    assert transport.read_message(tmp_path / "valid.json") == {"a": True}


@pytest.mark.parametrize(
    "change",
    [
        {"unexpected": True},
        {"variables": True},
        {"variables": 0},
        {"variables": 10**9},
        {"limits": {}},
        {"limits": {**asdict(ProofLimits()), "timeout_ms": True}},
        {"inputs": None},
        {"inputs": {"A": True}},
        {"inputs": {"A": 2}},
        {"inputs": {"A": 1, "B": 1}},
        {"inputs": {"": 1}},
        {"cnf": None},
        {"cnf": [[True]]},
        {"cnf": [[0]]},
        {"cnf": [[2]]},
        {"cnf": [[1, -1]]},
        {"cnf": [[1, 1]]},
    ],
)
def test_worker_rejects_bad_requests_without_loading_native(
    monkeypatch: pytest.MonkeyPatch,
    change: dict,
) -> None:
    monkeypatch.setattr(worker, "import_module", lambda _: pytest.fail("loaded native"))
    value = job()
    value.update(change)
    result = worker.produce(value)
    assert result["error"]
    assert result["witness"] is result["proof"] is None


def test_worker_rejects_storage_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "import_module", lambda _: pytest.fail("loaded native"))
    for setting in ("max_clauses", "max_literals"):
        value = job()
        value["limits"][setting] = 1
        value["cnf"] = [[1]] * 4
        assert worker.produce(value)["error"]


def test_worker_errors_never_emit_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_: str) -> None:
        raise ModuleNotFoundError("no solver")

    monkeypatch.setattr(worker, "import_module", broken)
    assert worker.produce(job()) == {
        "witness": None,
        "proof": None,
        "error": "ModuleNotFoundError: no solver",
    }


def test_native_stream_flush_order(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Solver(FakeSolver):
        def get_proof(self) -> list:
            events.append("read")
            return ["0"]

        def __exit__(self, *args: object) -> None:
            events.append("close")
            super().__exit__(*args)

    solver = Solver(answer=False)
    result = certificates._solve_native(
        [(1,), (-1,)],
        {},
        1,
        ProofLimits(),
        time.monotonic() + 5,
        lambda **_: solver,
        flush_proof=lambda: events.append("flush"),
    )
    assert result == (None, [[]])
    assert events == ["flush", "read", "flush", "close"]


def test_sat_refinement_flushes_before_close() -> None:
    events: list[str] = []

    class Solver(FakeSolver):
        def __exit__(self, *args: object) -> None:
            events.append("close")
            super().__exit__(*args)

    result = certificates._solve_native(
        [(1,)],
        {},
        1,
        ProofLimits(),
        time.monotonic() + 5,
        lambda **_: Solver(model=[1]),
        flush_proof=lambda: events.append("flush"),
    )
    assert result == ({}, None)
    assert events == ["flush", "close"]


def test_crt_load_is_restricted_and_errors_are_not_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []

    class Flush:
        code = 0

        def __call__(self, arg: object) -> int:
            calls.append(arg)
            return self.code

    function = Flush()

    def load(name: str, *, winmode: int) -> SimpleNamespace:
        assert name == "ucrtbase.dll" and winmode == 0x00000800
        return SimpleNamespace(fflush=function)

    monkeypatch.setattr(worker.ctypes, "CDLL", load)
    flush = worker._windows_flush()
    flush()
    assert calls == [None]
    function.code = -1
    with pytest.raises(ProofError, match="flush failed"):
        flush()


def test_worker_main_closes_result_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, output = tmp_path / "request.json", tmp_path / "response.json"
    transport.write_message(request, job())
    expected = {"witness": {}, "proof": None, "error": None}
    monkeypatch.setattr(worker, "produce", lambda _: copy.deepcopy(expected))
    monkeypatch.setattr(worker.sys, "argv", ["worker", str(request), str(output)])
    assert worker.main() == 0
    assert json.loads(output.read_text()) == expected
    # Never overwrite an existing response, including a symlink target.
    assert worker.main() == 2
    monkeypatch.setattr(worker.sys, "argv", ["worker"])
    assert worker.main() == 2

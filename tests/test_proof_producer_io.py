from __future__ import annotations

import ctypes
from types import SimpleNamespace
from typing import Any

import pytest

from opencollate import proof_producer_io as module
from opencollate.proof_kernel import ProofError


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_native_flush_precedes_first_proof_read(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    events = []
    proof = ["2 0", "0"]

    def read() -> list[str]:
        events.append("read")
        return proof

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(module, "_flush_windows_streams", lambda: events.append("flush"))
    assert module.read_producer_proof(SimpleNamespace(get_proof=read)) is proof
    assert events == (["flush", "read"] if platform == "win32" else ["read"])


@pytest.mark.parametrize("result", [0, -1])
def test_system_runtime_loading_and_flush_failure(
    monkeypatch: pytest.MonkeyPatch, result: int
) -> None:
    calls = []

    class Flush:
        def __call__(self, pointer: Any) -> int:
            calls.append(pointer)
            return result

    flush = Flush()

    def load(name: str, *, winmode: int) -> Any:
        assert name == "ucrtbase.dll"
        assert winmode == 0x00000800  # LOAD_LIBRARY_SEARCH_SYSTEM32
        return SimpleNamespace(fflush=flush)

    monkeypatch.setattr(ctypes, "CDLL", load)
    if result:
        with pytest.raises(ProofError, match="flush failed"):
            module._flush_windows_streams()
    else:
        module._flush_windows_streams()
    assert calls == [None]
    assert flush.argtypes == [ctypes.c_void_p]
    assert flush.restype is ctypes.c_int


@pytest.mark.parametrize("error", [OSError("missing runtime"), AttributeError("no fflush")])
def test_missing_native_runtime_fails_before_reading(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def load(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(ctypes, "CDLL", load)
    with pytest.raises(ProofError, match="cannot flush"):
        module.read_producer_proof(object())


@pytest.mark.parametrize("outcome", [True, False, None, "exception"])
def test_every_proof_enabled_solve_flushes_before_close(
    monkeypatch: pytest.MonkeyPatch, outcome: Any
) -> None:
    from opencollate import sequential_certificate as certificate
    from opencollate.proof_kernel import Meter

    events = []

    class Solver:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> Solver:
            return self

        def __exit__(self, *args: Any) -> None:
            events.append("close")

        def solve_limited(self, **kwargs: Any) -> Any:
            events.append("solve")
            if outcome == "exception":
                raise RuntimeError("native error")
            return outcome

        def interrupt(self) -> None:
            pass

        def get_model(self) -> list[int]:
            return [1]

        def get_proof(self) -> list[str]:
            return ["0"]

    monkeypatch.setattr(
        certificate.importlib, "import_module", lambda name: SimpleNamespace(Solver=Solver)
    )
    monkeypatch.setattr(certificate, "flush_producer_output", lambda: events.append("flush"))
    monkeypatch.setattr(module, "flush_producer_output", lambda: events.append("read-flush"))
    if outcome is True:
        assert certificate._solve({"variables": 1, "clauses": [[1]]}, Meter(), proof=True)[0]
    elif outcome is False:
        assert not certificate._solve(
            {"variables": 1, "clauses": [[1], [-1]]}, Meter(), proof=True
        )[0]
    else:
        with pytest.raises((ProofError, RuntimeError)):
            certificate._solve({"variables": 1, "clauses": [[1]]}, Meter(), proof=True)
    assert events.index("solve") < events.index("flush") < events.index("close")

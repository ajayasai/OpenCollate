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

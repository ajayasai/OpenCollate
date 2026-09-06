"""Proof-output compatibility for the optional, untrusted native producer.

The receiver never calls this module. Windows PySAT/Glucose wheels disable
setlinebuf, so C stdio buffers can still hold a DRUP suffix when get_proof reads
through Python's independent file object. Flush before the first seek/read;
never try to repair a truncated proof by weakening certificate validation.
"""

from __future__ import annotations

import sys
from typing import Any

from opencollate.proof_kernel import ProofError


def _flush_windows_streams() -> None:
    # Keep the foreign-function interface out of the receiver and non-Windows
    # producer path. Restrict DLL lookup to System32, never the working directory.
    import ctypes

    try:
        runtime = ctypes.CDLL("ucrtbase.dll", winmode=0x00000800)
        flush = runtime.fflush
        flush.argtypes = [ctypes.c_void_p]
        flush.restype = ctypes.c_int
        # PySAT exposes the descriptor, but not the native FILE*. Opening another
        # FILE* would not flush the producer's buffer. fflush(NULL) leaves streams
        # open and flushes all pending output in this runtime, including the proof.
        if flush(None) != 0:
            raise ProofError("native proof-output flush failed")
    except (OSError, AttributeError) as error:
        raise ProofError("cannot flush the Windows proof producer's C runtime") from error


def read_producer_proof(solver: Any) -> Any:
    """Read opaque, still-untrusted evidence after native output is available."""
    if sys.platform == "win32":
        _flush_windows_streams()
    return solver.get_proof()

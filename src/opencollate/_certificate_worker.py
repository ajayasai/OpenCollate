"""Private, single-query worker. Never run its CRT flush in a library caller.

MSVC PySAT proof tracing uses fdopen on a Python-owned descriptor but disables
line buffering. We explicitly flush the worker's UCRT before reading evidence
and before that descriptor closes. One native solver lives in each child, so
there are no stale streams from earlier solver instances. After closing the
JSON response, immediate OS exit avoids native atexit handlers revisiting stale
FILE* handles. The parent owns cleanup, deadline enforcement and verification.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from collections.abc import Callable
from dataclasses import fields
from importlib import import_module
from pathlib import Path
from typing import Any

from opencollate.certificate_process import read_message, write_message
from opencollate.certificates import _solve_native
from opencollate.proof_kernel import ProofError, ProofLimits, clause


def _windows_flush() -> Callable[[], None]:
    # LOAD_LIBRARY_SEARCH_SYSTEM32 prevents a design directory from supplying
    # a fake DLL. Use the Universal CRT, not the legacy msvcrt.dll runtime.
    crt = ctypes.CDLL("ucrtbase.dll", winmode=0x00000800)
    fflush = crt.fflush
    fflush.argtypes = [ctypes.c_void_p]
    fflush.restype = ctypes.c_int

    def flush() -> None:
        if fflush(None) != 0:
            raise ProofError("native proof-stream flush failed")

    return flush


def produce(value: dict[str, Any]) -> dict[str, Any]:
    """Strict data-only request; failures carry no evidence."""
    try:
        if set(value) != {"cnf", "inputs", "variables", "limits"}:
            raise ProofError("invalid producer request fields")
        raw_limits = value["limits"]
        if not isinstance(raw_limits, dict) or set(raw_limits) != {
            f.name for f in fields(ProofLimits)
        }:
            raise ProofError("invalid producer limit fields")
        limits = ProofLimits(**raw_limits)
        deadline = time.monotonic() + limits.timeout_ms / 1000
        variables = value["variables"]
        if type(variables) is not int or not 1 <= variables <= limits.max_nodes * 4:
            raise ProofError("invalid producer variable count")
        inputs = value["inputs"]
        if not isinstance(inputs, dict) or len(inputs) > limits.max_variables:
            raise ProofError("invalid producer input inventory")
        if any(
            not isinstance(k, str)
            or not 1 <= len(k) <= 65536
            or type(v) is not int
            or not 1 <= v <= variables
            for k, v in inputs.items()
        ):
            raise ProofError("invalid producer input variable")
        if len(set(inputs.values())) != len(inputs):
            raise ProofError("duplicate producer input variable")
        raw_cnf = value["cnf"]
        # Queries append up to two root-unit clauses to the compiled base.
        if not isinstance(raw_cnf, list) or len(raw_cnf) > limits.max_clauses + 2:
            raise ProofError("invalid producer clause inventory")
        cnf = [clause(item, variables) for item in raw_cnf]
        if sum(map(len, cnf)) > limits.max_literals + 2:
            raise ProofError("producer literal limit exceeded")
        module = import_module("pysat.solvers")
        flush = _windows_flush() if sys.platform == "win32" else None
        witness, proof = _solve_native(
            cnf, inputs, variables, limits, deadline, module.Glucose3, flush_proof=flush
        )
        return {"witness": witness, "proof": proof, "error": None}
    except (Exception, SystemExit) as error:
        return {"witness": None, "proof": None, "error": f"{type(error).__name__}: {error}"[:4096]}


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        value = read_message(Path(sys.argv[1]))
        result = produce(value)
        write_message(Path(sys.argv[2]), result)
        return 0
    except (Exception, SystemExit):
        return 2


if __name__ == "__main__":
    # JSON is closed; the parent reaps us and cleans our temporary directory.
    # Do not run native CRT atexit handlers on dangling upstream FILE* streams.
    os._exit(main())

"""Bounded, one-shot native proof producer transport.

Used on Windows to isolate the upstream native proof-stream lifetime defect.
The worker is NOT an authority: the caller replays witnesses and checks RUP
against its own CNF. No pickle, shell, executable input, or global CRT flushing
in the host application. This is crash isolation, not an OS security sandbox.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from opencollate.proof_kernel import ProofError, ProofLimits

# Additional transport ceiling, independent of proof/IR limits. A request that
# does not fit is incomplete, not silently truncated. Native memory/disk use is
# not sandboxed; only bytes deserialized in the parent are bounded here.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProofError("duplicate producer message key")
        result[key] = value
    return result


def _nonfinite(_: str) -> Any:
    raise ProofError("non-finite producer message value")


def read_message(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_MESSAGE_BYTES + 1)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProofError("producer message exceeds transport limit")
    value = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_nonfinite)
    if not isinstance(value, dict):
        raise ProofError("producer message must be an object")
    return value


def write_message(path: Path, value: Mapping[str, Any]) -> None:
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProofError("producer message exceeds transport limit")
    # Close before worker os._exit; no data is left in a Python output buffer.
    with path.open("xb") as stream:
        stream.write(raw)


def solve_isolated(
    cnf: list[tuple[int, ...]],
    inputs: Mapping[str, int],
    variables: int,
    limits: ProofLimits,
    deadline: float,
) -> tuple[dict[str, bool] | None, list[list[int]] | None]:
    """Spawn exactly one solver, terminate/reap on timeout, reject crashed output."""
    with TemporaryDirectory(prefix="opencollate-proof-") as directory:
        root = Path(directory)
        request = root / "request.json"
        output = root / "response.json"
        write_message(
            request,
            {
                "cnf": cnf,
                "inputs": inputs,
                "variables": variables,
                "limits": asdict(limits),
            },
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProofError("certificate total time limit exceeded before producer launch")
        try:
            # Fixed installed module through the current interpreter; never a shell.
            completed = subprocess.run(  # nosec B603
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "opencollate._certificate_worker",
                    str(request),
                    str(output),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=remaining,
                check=False,
                close_fds=True,
            )
        except subprocess.TimeoutExpired as error:
            # subprocess.run kills and waits for the child before raising.
            raise ProofError("isolated proof producer exceeded the total deadline") from error
        if time.monotonic() >= deadline:
            raise ProofError("certificate total time limit exceeded during producer execution")
        if completed.returncode != 0:
            # Ignore even syntactically plausible evidence from an abnormal exit.
            raise ProofError(f"isolated proof producer exited abnormally ({completed.returncode})")
        row = read_message(output)
        if set(row) != {"witness", "proof", "error"}:
            raise ProofError("invalid isolated producer response fields")
        if row["error"] is not None:
            if not isinstance(row["error"], str) or not 1 <= len(row["error"]) <= 4096:
                raise ProofError("invalid isolated producer error")
            raise ProofError(row["error"])
        witness, proof = row["witness"], row["proof"]
        if witness is not None:
            if not isinstance(witness, dict) or set(witness) != set(inputs) or proof is not None:
                raise ProofError("invalid isolated producer witness fields")
            if any(type(value) is not bool for value in witness.values()):
                raise ProofError("isolated producer witness values must be Boolean")
        elif not isinstance(proof, list) or not 1 <= len(proof) <= limits.max_steps:
            raise ProofError("invalid isolated producer proof size")
        # Logical checking deliberately remains in certificates._verify_boolean.
        return witness, proof

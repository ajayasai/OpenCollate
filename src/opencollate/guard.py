"""External watchdog for fixed OpenCollate CLI commands, not a security sandbox."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal

# Fixed local interpreter only; never a caller-selected executable.
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_ALLOWED_COMMANDS = frozenset(
    {"check", "review", "contract", "formal", "report", "demo", "sequential"}
)
_MAX_REQUEST_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GuardLimits:
    wall_seconds: float = 60.0
    memory_mib: int | None = None
    output_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            type(self.wall_seconds) not in (int, float)
            or not math.isfinite(self.wall_seconds)
            or not 0.05 <= self.wall_seconds <= 3600
        ):
            raise ValueError("guard wall_seconds must be finite and between 0.05 and 3600")
        if self.memory_mib is not None and (
            type(self.memory_mib) is not int or not 64 <= self.memory_mib <= 1048576
        ):
            raise ValueError("guard memory_mib must be an integer between 64 and 1048576")
        if type(self.output_bytes) is not int or not 1024 <= self.output_bytes <= 64 * 1024 * 1024:
            raise ValueError("guard output_bytes must be between 1024 and 67108864")
        if self.memory_mib is not None and os.name != "posix":
            raise ValueError("guard memory limit is supported only with POSIX RLIMIT_AS")


@dataclass(frozen=True, slots=True)
class GuardResult:
    exit_code: int
    status: str
    stdout: str
    stderr: str
    elapsed_seconds: float
    worker_returncode: int | None
    limits: GuardLimits

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "exit_code": self.exit_code,
            "worker_returncode": self.worker_returncode,
            "elapsed_seconds": self.elapsed_seconds,
            "limits": asdict(self.limits),
            "memory_enforcement": "posix-rlimit-as"
            if self.limits.memory_mib is not None
            else "not-requested",
            "output_enforcement": "posix-per-file-limit"
            if os.name == "posix"
            else "parent-polling",
        }


def _stop(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def run_guarded(argv: Sequence[str], *, limits: GuardLimits | None = None) -> GuardResult:
    """Require both OS completion and a matching normal-return record.

    Python entry points still execute trusted code and have the user's file and
    network permissions. POSIX timeout cancellation kills the worker's process
    group; Windows cancellation only guarantees stopping the direct worker.
    """
    selected = limits or GuardLimits()
    if not argv or not all(type(item) is str for item in argv) or argv[0] not in _ALLOWED_COMMANDS:
        raise ValueError(
            "guard requires a nonrecursive "
            "check/review/contract/formal/report/demo/sequential command"
        )
    payload = json.dumps(
        {"schema_version": 1, "argv": list(argv), "limits": asdict(selected)}, sort_keys=True
    ).encode("utf-8")
    if len(payload) > _MAX_REQUEST_BYTES:
        raise ValueError("guard request exceeds 1 MiB")
    request_digest = hashlib.sha256(payload).hexdigest()
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="opencollate-guard-") as directory:
        root = Path(directory)
        request, completion = root / "request.json", root / "completion.json"
        stdout, stderr = root / "stdout", root / "stderr"
        request.write_bytes(payload)
        status = "completed"
        with stdout.open("wb") as out, stderr.open("wb") as err:
            # Fixed executable/module, structured data, no shell or user command substitution.
            process = subprocess.Popen(  # nosec B603
                [sys.executable, "-m", "opencollate.guard_worker", str(request)],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=os.name == "posix",
            )
            try:
                while process.poll() is None:
                    if time.monotonic() - start >= selected.wall_seconds:
                        status = "timeout"
                        _stop(process)
                        break
                    if (
                        stdout.stat().st_size > selected.output_bytes
                        or stderr.stat().st_size > selected.output_bytes
                    ):
                        status = "output-limit"
                        _stop(process)
                        break
                    time.sleep(0.01)
            except BaseException:
                _stop(process)
                raise
        # Also reap descendants that outlive an otherwise normally returning worker.
        # A Windows worker is stopped directly; this is not a Windows job object.
        _stop(process)
        elapsed = time.monotonic() - start
        if status == "completed" and elapsed >= selected.wall_seconds:
            status = "timeout"
        if (
            stdout.stat().st_size > selected.output_bytes
            or stderr.stat().st_size > selected.output_bytes
        ):
            status = "output-limit"
        record: Any = None
        try:
            with completion.open("rb") as stream:
                content = stream.read(4097)
            if len(content) <= 4096:
                record = json.loads(content)
        except (OSError, ValueError, UnicodeError):
            pass
        valid = (
            type(record) is dict
            and set(record) == {"schema_version", "request_sha256", "exit_code", "normal_return"}
            and type(record["schema_version"]) is int
            and record["schema_version"] == 1
            and record["request_sha256"] == request_digest
            and record["normal_return"] is True
            and type(record["exit_code"]) is int
            and record["exit_code"] in {0, 1, 2, 130}
            and process.returncode == record["exit_code"]
        )
        with stderr.open("rb") as stream:
            err_text = stream.read(selected.output_bytes).decode("utf-8", errors="replace")
        if status == "completed" and valid:
            try:
                with stdout.open("rb") as stream:
                    out_text = stream.read(selected.output_bytes).decode("utf-8")
                return GuardResult(
                    record["exit_code"],
                    "completed",
                    out_text,
                    err_text,
                    elapsed,
                    process.returncode,
                    selected,
                )
            except UnicodeError:
                status = "invalid-output"
        elif status == "completed":
            status = "worker-failed"
        # Never emit a half-written machine report as a result of a killed worker.
        message = f"OC9001: guarded analysis {status}; no complete result was accepted.\n"
        return GuardResult(2, status, "", err_text + message, elapsed, process.returncode, selected)

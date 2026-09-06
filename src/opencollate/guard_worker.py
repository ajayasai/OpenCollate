"""Private worker protocol. Invoke through ``opencollate guard`` only."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


def _resource_limits(limits: dict[str, Any]) -> None:
    if os.name != "posix":
        if limits["memory_mib"] is not None:
            raise ValueError("POSIX memory enforcement is unavailable")
        return
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    output = limits["output_bytes"]
    resource.setrlimit(resource.RLIMIT_FSIZE, (output, output))
    cpu = math.ceil(limits["wall_seconds"]) + 1
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    if limits["memory_mib"] is not None:
        memory = limits["memory_mib"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    request = Path(sys.argv[1])
    try:
        with request.open("rb") as stream:
            payload = stream.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            return 2
        value = json.loads(payload)
        if (
            type(value) is not dict
            or set(value) != {"schema_version", "argv", "limits"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            return 2
        # Validate protocol before applying limits, but import no parser/solver yet.
        from opencollate.guard import _ALLOWED_COMMANDS, GuardLimits

        limits = GuardLimits(**value["limits"])
        argv = value["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(type(item) is str for item in argv)
            or argv[0] not in _ALLOWED_COMMANDS
        ):
            return 2
        _resource_limits(asdict(limits))
    except (OSError, ValueError, TypeError):
        return 2
    normal = False
    try:
        from opencollate.cli import main as cli_main

        code = cli_main(argv)
        sys.stdout.flush()
        sys.stderr.flush()
        normal = type(code) is int and code in {0, 1, 2, 130}
    except KeyboardInterrupt:
        code = 130
    except (Exception, SystemExit) as error:
        code = 2
        try:
            print(f"Guard worker failed: {type(error).__name__}", file=sys.stderr, flush=True)
        except OSError:
            pass
    record = {
        "schema_version": 1,
        "request_sha256": hashlib.sha256(payload).hexdigest(),
        "exit_code": code,
        "normal_return": normal,
    }
    try:
        (request.parent / "completion.json").write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        return 2
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())

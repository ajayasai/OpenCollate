from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from opencollate import guard_worker


@pytest.mark.parametrize("code", [0, 1, 2, 130])
def test_worker_records_only_a_normal_complete_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    payload = json.dumps(
        {
            "schema_version": 1,
            "argv": ["check"],
            "limits": {"wall_seconds": 10, "memory_mib": None, "output_bytes": 1024},
        }
    ).encode()
    request = tmp_path / "request.json"
    request.write_bytes(payload)
    monkeypatch.setattr(sys, "argv", ["guard_worker", str(request)])
    seen = []
    monkeypatch.setattr(guard_worker, "_resource_limits", lambda limits: seen.append(limits))
    monkeypatch.setattr("opencollate.cli.main", lambda argv: code)
    assert guard_worker.main() == code
    record = json.loads((tmp_path / "completion.json").read_text())
    assert record == {
        "schema_version": 1,
        "exit_code": code,
        "normal_return": True,
        "request_sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert seen[0]["output_bytes"] == 1024


@pytest.mark.parametrize("error", [SystemExit(0), RuntimeError("crash"), KeyboardInterrupt()])
def test_worker_does_not_attest_an_exception_as_normal_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema_version": 1, "argv": ["check"], "limits": {}}))
    monkeypatch.setattr(sys, "argv", ["guard_worker", str(request)])
    monkeypatch.setattr(guard_worker, "_resource_limits", lambda limits: None)

    def fail(argv: Any) -> None:
        raise error

    monkeypatch.setattr("opencollate.cli.main", fail)
    assert guard_worker.main() in {2, 130}
    record = json.loads((tmp_path / "completion.json").read_text())
    assert record["normal_return"] is False


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"schema_version": 2, "argv": ["check"], "limits": {}},
        {"schema_version": True, "argv": ["check"], "limits": {}},
        {"schema_version": 1, "argv": ["guard"], "limits": {}},
        {"schema_version": 1, "argv": [], "limits": {}},
        {"schema_version": 1, "argv": [0], "limits": {}},
        {"schema_version": 1, "argv": ["check"], "limits": {"wall_seconds": -1}},
        {"schema_version": 1, "argv": ["check"], "limits": {}, "extra": 1},
    ],
)
def test_invalid_worker_protocol_has_no_success_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    request = tmp_path / "request.json"
    request.write_text(json.dumps(value))
    monkeypatch.setattr(sys, "argv", ["guard_worker", str(request)])
    assert guard_worker.main() == 2
    assert not (tmp_path / "completion.json").exists()

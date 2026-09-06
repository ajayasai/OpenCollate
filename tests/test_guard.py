from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from opencollate.atomic_output import atomic_write_text
from opencollate.cli import main
from opencollate.guard import GuardLimits, run_guarded


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "cell.lef").write_text(
        "MACRO cell\n  PIN A\n    DIRECTION INPUT ;\n    USE SIGNAL ;\n  END A\nEND cell\n",
        encoding="utf-8",
    )
    config = tmp_path / "opencollate.toml"
    config.write_text('[sources.lef.default]\nfiles = ["cell.lef"]\n', encoding="utf-8")
    return config


def plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Install an actual ephemeral entry point visible to the fresh worker."""
    directory = tmp_path / "installed"
    directory.mkdir()
    (directory / "guard_test_plugin.py").write_text(
        "def checker(context):\n" + "\n".join("    " + line for line in body.splitlines()) + "\n",
        encoding="utf-8",
    )
    distribution = directory / "guard_test-1.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: guard-test\nVersion: 1.0\n", encoding="utf-8"
    )
    (distribution / "entry_points.txt").write_text(
        "[opencollate.checkers]\nwatchdog-test = guard_test_plugin:checker\n", encoding="utf-8"
    )
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(directory), str(root / "src"))))
    monkeypatch.delenv("OPENCOLLATE_DISABLE_PLUGINS", raising=False)


def test_real_worker_preserves_complete_json_and_exit_code(project: Path) -> None:
    argv = ["check", str(project), "--format", "json"]
    direct = subprocess.run(
        [sys.executable, "-m", "opencollate", *argv], capture_output=True, check=False
    )
    result = run_guarded(argv, limits=GuardLimits(wall_seconds=10))
    assert result.status == "completed"
    assert result.exit_code == direct.returncode == 0
    assert result.stdout.encode() == direct.stdout
    assert json.loads(result.stdout)["exit_code"] == 0


def test_guard_preserves_violation_and_configuration_failure() -> None:
    root = Path(__file__).resolve().parents[1]
    violation = run_guarded(
        ["check", str(root / "examples/uart/opencollate.toml"), "--format", "json"],
        limits=GuardLimits(wall_seconds=10),
    )
    assert violation.status == "completed" and violation.exit_code == 1
    assert json.loads(violation.stdout)["exit_code"] == 1
    invalid = run_guarded(["check", str(root / "__missing_config__.toml")])
    assert invalid.status == "completed" and invalid.exit_code == 2


def test_os_exit_zero_cannot_be_accepted(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin(tmp_path, monkeypatch, "import os\nos._exit(0)")
    result = run_guarded(["check", str(project), "--format", "json"])
    assert result.worker_returncode == 0
    assert result.status == "worker-failed"
    assert result.exit_code == 2 and result.stdout == ""
    assert "no complete result" in result.stderr


def test_hanging_entrypoint_stopped_by_external_deadline(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin(tmp_path, monkeypatch, "import time\ntime.sleep(60)\nreturn ()")
    result = run_guarded(["check", str(project)], limits=GuardLimits(wall_seconds=0.5))
    assert result.status == "timeout"
    assert result.exit_code == 2 and result.stdout == ""
    # Generous ceiling: verifies termination, not a throughput benchmark.
    assert result.elapsed_seconds < 15


def test_output_flood_is_not_forwarded_as_a_complete_report(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin(tmp_path, monkeypatch, 'print("x" * 100_000, flush=True)\nreturn ()')
    result = run_guarded(
        ["check", str(project)], limits=GuardLimits(wall_seconds=10, output_bytes=1024)
    )
    assert result.exit_code == 2
    assert result.status in {"worker-failed", "output-limit"}
    assert not result.stdout
    assert len(result.stderr) < 2048


@pytest.mark.skipif(os.name != "posix", reason="POSIX RLIMIT_AS")
def test_memory_limit_applied_before_parser_and_plugin_import(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin(tmp_path, monkeypatch, "buffer = bytearray(1024 * 1024 * 1024)\ndel buffer\nreturn ()")
    result = run_guarded(
        ["check", str(project), "--format", "json"],
        limits=GuardLimits(wall_seconds=10, memory_mib=256),
    )
    assert result.exit_code == 2
    assert result.to_dict()["memory_enforcement"] == "posix-rlimit-as"
    # A handled MemoryError is a completed *failed* analysis, not a cacheable pass.
    if result.stdout:
        report = json.loads(result.stdout)
        assert report["exit_code"] == 2
        assert any(item["code"] == "OC9002" for item in report["diagnostics"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"wall_seconds": False},
        {"wall_seconds": float("nan")},
        {"wall_seconds": float("inf")},
        {"wall_seconds": 0},
        {"wall_seconds": 4000},
        {"memory_mib": True},
        {"memory_mib": 1},
        {"output_bytes": True},
        {"output_bytes": 0},
        {"output_bytes": 100 * 1024 * 1024},
    ],
)
def test_invalid_limits_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GuardLimits(**kwargs)


@pytest.mark.parametrize("argv", [[], ["guard"], ["python", "-c", "pass"], [[], "check"]])
def test_no_arbitrary_commands_or_recursive_guards(argv: Any) -> None:
    with pytest.raises(ValueError):
        run_guarded(argv)


def test_guard_cli_status_is_separate_from_machine_report(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = tmp_path / "guard-status.json"
    cache = tmp_path / "cache"
    assert (
        main(
            [
                "guard",
                "--status-output",
                str(status),
                "--",
                "check",
                str(project),
                "--format",
                "json",
                "--cache-dir",
                str(cache),
                "--cache-stats",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["exit_code"] == 0
    assert "stores" in output.err
    assert json.loads(status.read_text())["status"] == "completed"


def test_atomic_failure_preserves_prior_artifact_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.json"
    path.write_text("previous")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated failed publish")

    monkeypatch.setattr("opencollate.atomic_output.os.replace", fail)
    with pytest.raises(OSError):
        atomic_write_text(path, "new complete result")
    assert path.read_text() == "previous"
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_write_publishes_complete_private_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "report.json"
    atomic_write_text(path, '{"result": "complete"}\n')
    assert json.loads(path.read_text()) == {"result": "complete"}
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0


def test_guard_schema_and_capability_inventory(project: Path) -> None:
    from importlib import resources

    from jsonschema import Draft202012Validator

    from opencollate.cli import _capability_data

    schema = json.loads(
        resources.files("opencollate.schemas").joinpath("guard-status.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    result = run_guarded(["check", str(project)])
    validator.validate(result.to_dict())
    timeout = run_guarded(["check", str(project)], limits=GuardLimits(wall_seconds=0.05))
    validator.validate(timeout.to_dict())
    data = _capability_data()
    assert data["incremental_cache"]["always_rechecks_rules"] is True
    assert data["guarded_execution"]["security_sandbox"] is False

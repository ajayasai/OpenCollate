from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from opencollate.atomic_output import atomic_write_text
from opencollate.cli import main
from opencollate.guard import GuardLimits, GuardResult
from opencollate.guard_status import prepare_status_output


@pytest.mark.parametrize(
    "command",
    [
        ["check"],
        ["check", "opencollate.toml"],
        ["review", "opencollate.toml", "--baseline", "protected"],
        ["contract", "build", "opencollate.toml"],
        ["contract", "migrate", "protected"],
        ["contract", "diff", "protected", "other"],
        ["report", "diff", "other", "protected"],
        ["formal", "check", "protected"],
        ["formal", "replay", "request", "protected"],
        ["sequential", "check", "request"],
        ["sequential", "replay", "protected", "receipt"],
    ],
)
def test_existing_input_never_overwritten_or_worker_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    protected = tmp_path / "protected"
    content = b"original configuration, source, request or dependency bytes\n"
    protected.write_bytes(content)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("preflight must reject before invoking the child")

    monkeypatch.setattr("opencollate.guard.run_guarded", forbidden)
    assert main(["guard", "--status-output", "protected", "--", *command]) == 2
    assert protected.read_bytes() == content
    assert "new file" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["hardlink", "symlink", "dangling-symlink"])
def test_status_aliases_rejected_before_execution(tmp_path: Path, kind: str) -> None:
    source, alias = tmp_path / "source", tmp_path / "alias"
    source.write_text("original")
    try:
        if kind == "hardlink":
            os.link(source, alias)
        else:
            alias.symlink_to(source if kind == "symlink" else tmp_path / "missing")
    except OSError:
        pytest.skip("host does not permit creating this link")
    with pytest.raises(ValueError, match="new file"):
        prepare_status_output(alias, ["check"])
    assert source.read_text() == "original"


@pytest.mark.parametrize(
    ("status", "command"),
    [
        ("result.json", ["check", "--output=result.json"]),
        ("result.json", ["check", "-oresult.json"]),
        ("current.json", ["review", "--baseline", "baseline", "--write-report", "current.json"]),
        ("contract.oc.json", ["contract", "build"]),
        ("-", ["contract", "build", "-o", "-"]),
        ("old.v2.json", ["contract", "migrate", "old.json"]),
        ("receipt.json", ["sequential", "check", "request", "-o", "receipt.json"]),
        ("outputs/status.json", ["demo", "--output-dir", "outputs"]),
        ("cache/status.json", ["check", "--cache-dir", "cache"]),
    ],
)
def test_child_output_collision_rejected_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, command: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="output"):
        prepare_status_output(Path(status), command)
    assert not Path(status).exists()


def test_status_created_during_worker_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    status = tmp_path / "status.json"

    def raced(*args: Any, **kwargs: Any) -> GuardResult:
        status.write_text("new input created by another process")
        return GuardResult(0, "completed", "should not be accepted", "", 0.1, 0, GuardLimits())

    monkeypatch.setattr("opencollate.guard.run_guarded", raced)
    assert main(["guard", "--status-output", str(status), "--", "check"]) == 2
    assert status.read_text() == "new input created by another process"
    assert capsys.readouterr().out == ""
    assert list(tmp_path.iterdir()) == [status]


def test_exclusive_status_publication_is_atomic_and_nonclobbering(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    atomic_write_text(status, '{"exit_code": 0}\n', overwrite=False)
    assert json.loads(status.read_text()) == {"exit_code": 0}
    with pytest.raises(FileExistsError):
        atomic_write_text(status, "replacement", overwrite=False)
    assert json.loads(status.read_text()) == {"exit_code": 0}
    assert list(tmp_path.iterdir()) == [status]


def test_hardlink_unavailable_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status = tmp_path / "status.json"

    def unsupported(*args: Any, **kwargs: Any) -> None:
        raise OSError("filesystem does not support hard links")

    monkeypatch.setattr("opencollate.atomic_output.os.link", unsupported)
    with pytest.raises(OSError, match="hard links"):
        atomic_write_text(status, "complete", overwrite=False)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", [[], ["guard"], ["formal"]])
def test_incomplete_child_rejected_for_status_preflight(tmp_path: Path, command: list[str]) -> None:
    with pytest.raises(ValueError):
        prepare_status_output(tmp_path / "fresh.json", command)


def test_original_configuration_overwrite_reproducer(tmp_path: Path) -> None:
    config = tmp_path / "opencollate.toml"
    source = tmp_path / "cell.lef"
    original = '[sources.lef.default]\nfiles = ["cell.lef"]\n'
    config.write_text(original)
    source.write_text("MACRO cell\nEND cell\n")
    assert main(["guard", "--status-output", str(config), "--", "check", str(config)]) == 2
    assert config.read_text() == original


def test_prior_status_file_requires_a_fresh_per_run_name(tmp_path: Path) -> None:
    status = tmp_path / "prior-status.json"
    payload = json.dumps(GuardResult(0, "completed", "", "", 0.1, 0, GuardLimits()).to_dict())
    status.write_text(payload)
    with pytest.raises(ValueError, match="fresh per-run"):
        prepare_status_output(status, ["check"])
    assert status.read_text() == payload

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from opencollate.config import ProjectConfig
from opencollate.diagnostics import Diagnostic
from opencollate.engine import ComparisonEngine
from opencollate.model import ViewId, ViewObservation
from opencollate.parsers import (
    ParserPluginSpec,
    parse,
    register_parser_plugin,
    unregister_parser_plugin,
)
from opencollate.plugins import (
    CheckerPluginSpec,
    register_checker_plugin,
    unregister_checker_plugin,
)


def _run(tmp_path: Path, checker: Any) -> Any:
    register_checker_plugin(CheckerPluginSpec(checker, name="fail-closed-test"))
    try:
        return ComparisonEngine(
            ProjectConfig(path=tmp_path / "config.toml", root=tmp_path, name="test", sources=())
        ).run((ViewObservation(ViewId("rtl")),))
    finally:
        unregister_checker_plugin("fail-closed-test")


@pytest.mark.parametrize("bad", [None, 0, False, "not a diagnostic", {}])
def test_invalid_checker_items_become_fatal_instead_of_crashing(tmp_path: Path, bad: Any) -> None:
    result = _run(tmp_path, lambda context: (bad,))
    assert result.exit_code == 2
    assert any(
        item.code == "OC9002" and item.severity.value == "fatal" for item in result.diagnostics
    )


@pytest.mark.parametrize("code", [0, 1, None])
def test_checker_system_exit_never_creates_a_success(tmp_path: Path, code: Any) -> None:
    def checker(context: Any) -> Any:
        raise SystemExit(code)

    result = _run(tmp_path, checker)
    assert result.exit_code == 2
    assert any(
        item.code == "OC9002" and item.metadata["error_type"] == "SystemExit"
        for item in result.diagnostics
    )


def test_infinite_diagnostic_iterator_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("opencollate.plugins.MAX_CHECKER_DIAGNOSTICS", 5)
    result = _run(
        tmp_path, lambda context: itertools.repeat(Diagnostic.from_rule("OC1105", "duplicate"))
    )
    assert result.exit_code == 2
    assert any("exceeded 5" in item.message for item in result.diagnostics)
    assert len(result.diagnostics) < 5


def test_exact_diagnostic_limit_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opencollate.plugins.MAX_CHECKER_DIAGNOSTICS", 3)
    result = _run(
        tmp_path, lambda context: [Diagnostic.from_rule("OC1105", f"note {i}") for i in range(3)]
    )
    assert result.exit_code == 0
    assert sum(item.code == "OC1105" for item in result.diagnostics) == 3


class _ExitingParser:
    format_name = "exiting_parser_test"

    def parse(self, paths: Any, **options: Any) -> ViewObservation:
        raise SystemExit(0)


def test_parser_system_exit_becomes_fatal_tainted_observation(tmp_path: Path) -> None:
    register_parser_plugin(ParserPluginSpec(_ExitingParser(), name="exiting-parser-test"))
    try:
        observed = parse("exiting_parser_test", (tmp_path / "unused",))
        assert not observed.complete
        assert observed.tainted_scopes == frozenset({"*"})
        assert observed.diagnostics[0].code == "OC9001"
        assert observed.diagnostics[0].severity.value == "fatal"
    finally:
        unregister_parser_plugin("exiting-parser-test")


def test_keyboard_interrupt_is_still_honored(tmp_path: Path) -> None:
    def interrupt(context: Any) -> Any:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, interrupt)


def test_discovery_system_exit_is_retained_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import opencollate.plugins as plugins

    class EntryPoint:
        name = "exiting"
        value = "example:parser"
        dist = None

        def load(self) -> Any:
            raise SystemExit(0)

    monkeypatch.delenv("OPENCOLLATE_DISABLE_PLUGINS", raising=False)
    monkeypatch.setattr(plugins, "_entry_points", lambda group: (EntryPoint(),))
    for discover in (plugins._discover_parsers, plugins._discover_checkers):
        specs, failures = discover()
        assert specs == ()
        assert failures[0].error_type == "SystemExit"

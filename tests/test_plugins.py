from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from opencollate.config import ProjectConfig
from opencollate.diagnostics import Diagnostic
from opencollate.engine import ComparisonEngine
from opencollate.model import ViewId, ViewObservation
from opencollate.parsers import (
    ParserPluginSpec,
    get_parser,
    infer_format,
    normalize_format,
    parse,
    parser_inventory,
    register_parser_plugin,
    unregister_parser_plugin,
)
from opencollate.parsers.base import Pathish, coerce_view
from opencollate.plugins import (
    PLUGIN_API_VERSION,
    CheckerContext,
    CheckerPluginSpec,
    PluginContractError,
    plugin_inventory,
    register_checker_plugin,
    unregister_checker_plugin,
)


class _ToyParser:
    format_name = "toy"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return ViewObservation(
            coerce_view(view_id, kind=self.format_name),
            attributes={
                "paths": [str(path) for path in paths],
                "option": options.get("option"),
            },
        )


class _CrashParser:
    format_name = "crash"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        del paths, view_id, options
        raise RuntimeError("deliberate parser failure")


class _ShadowVerilogParser(_ToyParser):
    format_name = "verilog"


def _project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        path=tmp_path / "opencollate.toml",
        root=tmp_path,
        name="plugin-test",
        sources=(),
    )


def test_runtime_parser_plugin_supports_alias_extension_and_dispatch(tmp_path: Path) -> None:
    spec = ParserPluginSpec(
        parser=_ToyParser(),
        aliases=("toy-format",),
        extensions=("toy",),
        name="toy-parser",
        provider="test-suite",
        version="1.0",
    )
    register_parser_plugin(spec)
    try:
        source = tmp_path / "design.toy"
        assert normalize_format("toy-format") == "toy"
        assert infer_format((source,)) == "toy"
        assert get_parser("toy") is spec.parser

        observation = parse(source, option=42, view_name="sample")
        assert observation.view == ViewId("toy", "sample")
        assert observation.attributes["option"] == 42

        registration = next(
            item for item in parser_inventory()["registrations"] if item["format"] == "toy"
        )
        assert registration == {
            "format": "toy",
            "aliases": ["toy_format"],
            "extensions": [".toy"],
            "provider": "test-suite",
            "version": "1.0",
            "plugin": "toy_parser",
            "builtin": False,
        }
    finally:
        unregister_parser_plugin("toy-parser")


def test_parser_plugin_cannot_shadow_a_builtin_format() -> None:
    spec = ParserPluginSpec(
        parser=_ShadowVerilogParser(),
        name="shadow-verilog",
        provider="test-suite",
    )
    register_parser_plugin(spec)
    try:
        inventory = parser_inventory()
        failure = next(item for item in inventory["failures"] if item["name"] == "shadow_verilog")
        assert failure["error_type"] == "PluginConflictError"
        assert "already owned by OpenCollate" in failure["message"]
        assert get_parser("verilog").__class__.__name__ == "VerilogParser"
    finally:
        unregister_parser_plugin("shadow-verilog")


def test_parser_plugin_crash_is_a_fatal_tainted_observation(tmp_path: Path) -> None:
    register_parser_plugin(
        ParserPluginSpec(
            parser=_CrashParser(),
            name="crash-parser",
            extensions=(".crash",),
            provider="test-suite",
            version="9.9",
        )
    )
    try:
        observation = parse(tmp_path / "source.crash")
        assert not observation.complete
        assert observation.tainted_scopes == frozenset({"*"})
        assert observation.diagnostics[0].code == "OC9001"
        assert observation.diagnostics[0].severity.value == "fatal"
        assert observation.diagnostics[0].metadata["provider"] == "test-suite"
    finally:
        unregister_parser_plugin("crash-parser")


def test_plugin_api_version_is_explicitly_rejected() -> None:
    with pytest.raises(PluginContractError, match="unsupported"):
        ParserPluginSpec(parser=_ToyParser(), api_version=PLUGIN_API_VERSION + 1)


def test_checker_plugin_receives_context_and_contributes_diagnostics(tmp_path: Path) -> None:
    seen: list[CheckerContext] = []

    def checker(context: CheckerContext) -> tuple[Diagnostic, ...]:
        seen.append(context)
        return (
            Diagnostic.from_rule(
                "OC1105",
                "The test checker ran against the canonical design.",
                metadata={"plugin_test": True},
            ),
        )

    register_checker_plugin(
        CheckerPluginSpec(
            checker=checker,
            name="context-checker",
            provider="test-suite",
            version="1.0",
        )
    )
    try:
        result = ComparisonEngine(_project(tmp_path)).run(
            (ViewObservation(ViewId("custom", "default")),)
        )
        assert any(
            item.code == "OC1105" and item.metadata.get("plugin_test") is True
            for item in result.diagnostics
        )
        assert len(seen) == 1
        assert seen[0].generated_contract == result.generated_contract
        assert seen[0].design == result.design
        assert seen[0].observations[0].view == ViewId("custom", "default")
    finally:
        unregister_checker_plugin("context-checker")


def test_checker_plugin_crash_cannot_turn_into_a_pass(tmp_path: Path) -> None:
    def checker(context: CheckerContext) -> tuple[Diagnostic, ...]:
        del context
        raise RuntimeError("deliberate checker failure")

    register_checker_plugin(
        CheckerPluginSpec(
            checker=checker,
            name="crash-checker",
            provider="test-suite",
            version="2.0",
        )
    )
    try:
        result = ComparisonEngine(_project(tmp_path)).run(())
        finding = next(item for item in result.diagnostics if item.code == "OC9002")
        assert finding.severity.value == "fatal"
        assert finding.metadata["plugin"] == "crash_checker"
        assert result.exit_code == 2
    finally:
        unregister_checker_plugin("crash-checker")


def test_capability_inventory_exposes_versioned_plugin_contract() -> None:
    inventory = plugin_inventory()
    assert inventory["api_version"] == PLUGIN_API_VERSION
    assert inventory["entry_point_groups"] == {
        "parsers": "opencollate.parsers",
        "checkers": "opencollate.checkers",
    }
    assert isinstance(inventory["failures"], list)

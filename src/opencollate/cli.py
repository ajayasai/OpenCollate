"""Dependency-light command-line interface for OpenCollate."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import Any

from opencollate import __version__
from opencollate.catalog import get_rule, iter_rules
from opencollate.config import ConfigError, ProjectConfig, SourceConfig, load_config
from opencollate.demo import write_demo
from opencollate.engine import ComparisonEngine, EngineResult, write_contract
from opencollate.model import ViewObservation
from opencollate.reporters import render_json, render_markdown, render_sarif, render_text


class CliError(RuntimeError):
    """A user-actionable command failure."""

    def __init__(self, message: str, *, code: str = "OC1001") -> None:
        super().__init__(message)
        self.code = code


def _config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "config",
        nargs="?",
        help="project configuration (default: opencollate.toml)",
    )
    parser.add_argument("-c", "--config", dest="config_option", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencollate",
        description="Catch SoC design-collateral drift with explainable diagnostics.",
    )
    parser.add_argument("--version", action="version", version=f"OpenCollate {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser("check", help="compare every configured design view")
    _config_argument(check)
    check.add_argument(
        "--format",
        choices=("text", "json", "sarif", "markdown"),
        default="text",
        help="report format (default: text)",
    )
    check.add_argument("-o", "--output", help="write the report to this path instead of stdout")
    check.add_argument(
        "--deny-warnings",
        action="store_true",
        help="return status 1 when an unwaived warning is present",
    )
    check.add_argument("--verbose", action="store_true", help="include diagnostic fingerprints")
    check.set_defaults(handler=_command_check)

    demo = subparsers.add_parser("demo", help="run a self-contained synthetic demonstration")
    demo.add_argument("--output-dir", help="keep generated sources in this directory")
    demo.add_argument(
        "--format",
        choices=("text", "json", "sarif", "markdown"),
        default="text",
    )
    demo.add_argument(
        "--strict-exit",
        action="store_true",
        help="return the demo check status (the deliberate inconsistencies produce 1)",
    )
    demo.set_defaults(handler=_command_demo)

    init = subparsers.add_parser("init", help="write a documented starter configuration")
    init.add_argument("path", nargs="?", default=".")
    init.set_defaults(handler=_command_init)

    capabilities = subparsers.add_parser(
        "capabilities", help="show parser and checker capabilities"
    )
    capabilities.add_argument("--json", action="store_true", dest="as_json")
    capabilities.set_defaults(handler=_command_capabilities)

    explain = subparsers.add_parser("explain", help="explain a stable diagnostic code")
    explain.add_argument("code")
    explain.set_defaults(handler=_command_explain)

    schema = subparsers.add_parser("schema", help="print a bundled JSON Schema")
    schema.add_argument("kind", nargs="?", choices=("report", "contract"), default="report")
    schema.add_argument("-o", "--output")
    schema.set_defaults(handler=_command_schema)

    contract = subparsers.add_parser("contract", help="manage canonical design contracts")
    contract_subparsers = contract.add_subparsers(dest="contract_command", required=True)
    build = contract_subparsers.add_parser("build", help="build a contract from configured views")
    _config_argument(build)
    build.add_argument("-o", "--output", default="contract.oc.json")
    build.set_defaults(handler=_command_contract_build)
    return parser


def _selected_config(args: argparse.Namespace) -> Path:
    positional = getattr(args, "config", None)
    optional = getattr(args, "config_option", None)
    if positional and optional:
        raise CliError("pass the configuration either positionally or with --config, not both")
    return Path(optional or positional or "opencollate.toml")


def _parse_source(source: SourceConfig) -> ViewObservation:
    paths = source.expand_files()
    kind = source.view.kind.lower()
    options = dict(source.options)
    if kind in {"rtl", "sv", "systemverilog", "verilog"}:
        _reject_unknown_source_options(source, options, {"top"})
        _validate_top_option(source, options)
        from opencollate.parsers.verilog import parse_verilog

        return parse_verilog(
            paths,
            view_id=source.view,
            include_dirs=source.include_dirs,
            defines=source.defines,
            **options,
        )
    if kind in {"lib", "liberty"}:
        _reject_unknown_source_options(source, options, set())
        from opencollate.parsers.liberty import parse_liberty

        return parse_liberty(paths, view_id=source.view, **options)
    if kind == "lef":
        _reject_unknown_source_options(source, options, set())
        from opencollate.parsers.lef import parse_lef

        return parse_lef(paths, view_id=source.view, **options)
    if kind in {"csv", "pinmap", "pin_map"}:
        _reject_unknown_source_options(source, options, {"component_name", "delimiter"})
        _validate_string_option(source, options, "component_name")
        _validate_string_option(source, options, "delimiter", length=1)
        _validate_csv_delimiter(source, options)
        from opencollate.parsers.csvpins import parse_pin_csv

        return parse_pin_csv(
            paths,
            view_id=source.view,
            column_map={
                source_name: canonical for canonical, source_name in source.columns.items()
            },
            **options,
        )
    raise CliError(
        f"no parser is registered for source view {source.view}",
        code="OC1001",
    )


def _reject_unknown_source_options(
    source: SourceConfig,
    options: dict[str, Any],
    supported: set[str],
) -> None:
    unknown = sorted(set(options) - supported)
    if not unknown:
        return
    rendered = ", ".join(unknown)
    raise CliError(f"{source.view}: unsupported source option(s): {rendered}")


def _validate_top_option(source: SourceConfig, options: dict[str, Any]) -> None:
    if "top" not in options:
        return
    value = options["top"]
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, list) or not names or not all(isinstance(item, str) for item in names):
        raise CliError(f"{source.view}: source option 'top' must be a string or string array")
    if any(not item.strip() for item in names):
        raise CliError(f"{source.view}: source option 'top' must not contain empty names")


def _validate_string_option(
    source: SourceConfig,
    options: dict[str, Any],
    name: str,
    *,
    length: int | None = None,
) -> None:
    if name not in options:
        return
    value = options[name]
    if not isinstance(value, str):
        raise CliError(f"{source.view}: source option {name!r} must be a string")
    if length is not None and len(value) != length:
        qualifier = "exactly one character" if length == 1 else f"exactly {length} characters"
        raise CliError(f"{source.view}: source option {name!r} must be {qualifier}")


def _validate_csv_delimiter(source: SourceConfig, options: dict[str, Any]) -> None:
    delimiter = options.get("delimiter")
    if delimiter is None:
        return
    try:
        csv.reader((), delimiter=delimiter)
    except (TypeError, ValueError) as error:
        raise CliError(
            f"{source.view}: source option 'delimiter' is not a valid CSV delimiter: {delimiter!r}"
        ) from error


def _load_observations(config: ProjectConfig) -> tuple[ViewObservation, ...]:
    return tuple(_parse_source(source) for source in config.sources)


def _run(config: ProjectConfig) -> EngineResult:
    return ComparisonEngine(config).run(_load_observations(config))


def _render(result: EngineResult, format_name: str, *, verbose: bool = False) -> str:
    if format_name == "json":
        return render_json(result)
    if format_name == "sarif":
        return render_sarif(result)
    if format_name == "markdown":
        return render_markdown(result)
    return render_text(result, verbose=verbose)


def _emit(text: str, output: str | None = None) -> None:
    if not output or output == "-":
        sys.stdout.write(text)
        return
    target = Path(output).expanduser().resolve()
    _write_text_file(target, text, description="output")


def _write_text_file(target: Path, text: str, *, description: str) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    except OSError as error:
        raise CliError(f"cannot write {description} {target}: {error}") from error


def _command_check(args: argparse.Namespace) -> int:
    config = load_config(_selected_config(args))
    if args.deny_warnings and not config.policy.deny_warnings:
        config = replace(config, policy=replace(config.policy, deny_warnings=True))
    result = _run(config)
    _emit(_render(result, args.format, verbose=args.verbose), args.output)
    return result.exit_code


def _command_demo(args: argparse.Namespace) -> int:
    try:
        if args.output_dir:
            root = write_demo(args.output_dir)
        else:
            root = write_demo(Path(tempfile.mkdtemp(prefix="opencollate-demo-")))
    except FileExistsError:
        raise
    except OSError as error:
        destination = Path(args.output_dir).expanduser().resolve() if args.output_dir else "demo"
        raise CliError(f"cannot write synthetic demo to {destination}: {error}") from error
    result = _run(load_config(root / "opencollate.toml"))
    if args.format == "text":
        sys.stdout.write(f"Synthetic demo: {root}\n")
    _emit(_render(result, args.format))
    return result.exit_code if args.strict_exit else 0


_INIT_TEMPLATE = """\
schema_version = 1

[project]
name = "my-soc"

[sources.rtl.default]
files = ["rtl/**/*.sv"]
include_dirs = ["rtl/include"]
defines = []

[sources.liberty.tt]
files = ["lib/**/*.lib"]

[sources.lef.abstract]
files = ["lef/**/*.lef"]

[sources.csv.package]
files = ["package/pins.csv"]
profile = "package_map"

[contract]
baseline = "rtl.default"

[policy]
rtl_power_pins = "optional"
max_boolean_inputs = 12
"""


def _command_init(args: argparse.Namespace) -> int:
    requested = Path(args.path).expanduser().resolve()
    target = requested if requested.suffix.lower() == ".toml" else requested / "opencollate.toml"
    if target.exists():
        raise CliError(f"refusing to overwrite existing configuration: {target}")
    _write_text_file(target, _INIT_TEMPLATE, description="configuration")
    print(f"Wrote {target}")
    return 0


def _capability_data() -> dict[str, Any]:
    try:
        import pyslang

        slang_version = getattr(pyslang, "__version__", "installed")
    except ImportError:  # pragma: no cover - package metadata requires pyslang
        slang_version = None
    return {
        "tool": {"name": "OpenCollate", "version": __version__},
        "formats": {
            "verilog_systemverilog": {
                "status": "supported",
                "backend": "pyslang",
                "version": slang_version,
            },
            "liberty": {"status": "supported", "backend": "native"},
            "lef": {"status": "supported", "backend": "native"},
            "csv_pin_maps": {"status": "supported", "backend": "stdlib"},
            "ip_xact": {"status": "planned"},
            "sdc": {"status": "planned"},
            "upf": {"status": "planned"},
        },
        "outputs": ["text", "json", "sarif", "markdown", "contract-json"],
        "rules": len(list(iter_rules())),
    }


def _command_capabilities(args: argparse.Namespace) -> int:
    data = _capability_data()
    if args.as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print(f"OpenCollate {__version__}")
    print("\nFormats:")
    for name, details in data["formats"].items():
        backend = f" ({details['backend']})" if "backend" in details else ""
        print(f"  {name.replace('_', '/'):24} {details['status']}{backend}")
    print(f"\nBuilt-in rules: {data['rules']}")
    print("Outputs: " + ", ".join(data["outputs"]))
    return 0


def _command_explain(args: argparse.Namespace) -> int:
    try:
        rule = get_rule(args.code)
    except KeyError as error:
        raise CliError(str(error)) from error
    print(f"{rule.code}  {rule.name}")
    print(f"Severity: {rule.default_severity.value}")
    print(f"\n{rule.summary}\n\nHelp: {rule.help}")
    return 0


def _schema_text(kind: str) -> str:
    filename = f"{kind}.schema.json"
    return resources.files("opencollate.schemas").joinpath(filename).read_text(encoding="utf-8")


def _command_schema(args: argparse.Namespace) -> int:
    text = _schema_text(args.kind)
    if not text.endswith("\n"):
        text += "\n"
    _emit(text, args.output)
    return 0


def _command_contract_build(args: argparse.Namespace) -> int:
    config = load_config(_selected_config(args))
    result = _run(config)
    if result.exit_code == 2:
        _emit(render_text(result))
        return 2
    try:
        target = write_contract(result.generated_contract, args.output)
    except OSError as error:
        destination = Path(args.output).expanduser().resolve()
        raise CliError(f"cannot write contract {destination}: {error}") from error
    print(f"Wrote {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return int(handler(args))
    except (ConfigError, CliError, FileExistsError) as error:
        code = getattr(error, "code", "OC1001")
        print(f"{code}: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


__all__ = ["build_parser", "main"]

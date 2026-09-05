"""Dependency-light command-line interface for OpenCollate."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from importlib import import_module, metadata, resources
from pathlib import Path
from typing import Any

from opencollate import __version__
from opencollate.baseline import (
    MAX_REPORT_JSON_BYTES,
    MAX_REPORT_JSON_NESTING,
    BaselineReportError,
    FindingState,
    ReportDiff,
    diff_reports,
)
from opencollate.catalog import get_rule, iter_rules
from opencollate.config import ConfigError, ProjectConfig, SourceConfig, load_config, load_contract
from opencollate.contracts import upgrade_contract
from opencollate.demo import write_demo
from opencollate.engine import ComparisonEngine, EngineResult, write_contract
from opencollate.execution import MAX_PARSE_JOBS, ordered_parallel_map, parse_job_count
from opencollate.model import ViewObservation
from opencollate.parsers import parser_inventory
from opencollate.plugins import plugin_inventory
from opencollate.reporters import (
    render_diff_json,
    render_diff_markdown,
    render_diff_sarif,
    render_diff_text,
    render_html,
    render_json,
    render_markdown,
    render_sarif,
    render_text,
)


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


def _job_count(value: str) -> int:
    try:
        return parse_job_count(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _jobs_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--jobs",
        type=_job_count,
        default=1,
        metavar="N",
        help=f"parse independent safe views with N workers, 1-{MAX_PARSE_JOBS} (default: 1)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencollate",
        description="Catch SoC design-collateral drift with explainable diagnostics.",
    )
    parser.add_argument("--version", action="version", version=f"OpenCollate {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser("check", help="compare every configured design view")
    _config_argument(check)
    _jobs_argument(check)
    check.add_argument(
        "--format",
        choices=("text", "json", "sarif", "markdown", "html"),
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

    review = subparsers.add_parser(
        "review", help="compare a live check against a committed report baseline"
    )
    _config_argument(review)
    _jobs_argument(review)
    review.add_argument("--baseline", required=True, help="prior OpenCollate JSON report")
    review.add_argument("--write-report", help="write the complete current JSON report")
    review.add_argument(
        "--fail-on",
        choices=("new", "changed", "all", "none"),
        default="changed",
        help="gate new; new-or-changed; all current; or no findings (default: changed)",
    )
    review.add_argument(
        "--format", choices=("text", "json", "sarif", "markdown", "html"), default="text"
    )
    review.add_argument("-o", "--output", help="write the diff artifact instead of stdout")
    review.add_argument("--include-unchanged", action="store_true")
    review.add_argument("--deny-warnings", action="store_true")
    review.set_defaults(handler=_command_review)

    report = subparsers.add_parser("report", help="inspect and compare saved reports")
    report_subparsers = report.add_subparsers(dest="report_command", required=True)
    report_diff = report_subparsers.add_parser(
        "diff", help="classify new, changed, unchanged, and resolved findings"
    )
    report_diff.add_argument("baseline", help="prior OpenCollate JSON report")
    report_diff.add_argument("current", help="current OpenCollate JSON report")
    report_diff.add_argument(
        "--format", choices=("text", "json", "sarif", "markdown", "html"), default="text"
    )
    report_diff.add_argument("-o", "--output")
    report_diff.add_argument("--include-unchanged", action="store_true")
    report_diff.add_argument("--fail-on", choices=("new", "changed", "all", "none"), default="none")
    report_diff.add_argument("--deny-warnings", action="store_true")
    report_diff.set_defaults(handler=_command_report_diff)

    demo = subparsers.add_parser("demo", help="run a self-contained synthetic demonstration")
    _jobs_argument(demo)
    demo.add_argument("--output-dir", help="keep generated sources in this directory")
    demo.add_argument(
        "--format",
        choices=("text", "json", "sarif", "markdown", "html"),
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
    schema.add_argument(
        "kind",
        nargs="?",
        choices=("report", "contract", "diff", "contract-diff", "formal-request", "formal-receipt"),
        default="report",
    )
    schema.add_argument("-o", "--output")
    schema.set_defaults(handler=_command_schema)

    contract = subparsers.add_parser("contract", help="manage canonical design contracts")
    contract_subparsers = contract.add_subparsers(dest="contract_command", required=True)
    build = contract_subparsers.add_parser("build", help="build a contract from configured views")
    _config_argument(build)
    _jobs_argument(build)
    build.add_argument("-o", "--output", default="contract.oc.json")
    build.set_defaults(handler=_command_contract_build)
    migrate = contract_subparsers.add_parser(
        "migrate",
        help="upgrade a v1 contract to the current schema without inventing unavailable facts",
    )
    migrate.add_argument("input", help="existing OpenCollate contract JSON")
    migrate.add_argument("-o", "--output", help="destination (default: INPUT stem plus .v2)")
    migrate.set_defaults(handler=_command_contract_migrate)
    contract_diff = contract_subparsers.add_parser(
        "diff", help="review all frozen observation families"
    )
    contract_diff.add_argument("baseline")
    contract_diff.add_argument("current")
    contract_diff.add_argument("-o", "--output")
    contract_diff.set_defaults(handler=_command_contract_diff)

    formal = subparsers.add_parser("formal", help="check explicit two-valued Boolean obligations")
    formal_commands = formal.add_subparsers(dest="formal_command", required=True)
    for command in ("check", "replay"):
        sub = formal_commands.add_parser(command)
        sub.add_argument("request")
        if command == "replay":
            sub.add_argument("receipt")
        sub.add_argument("-o", "--output")
        sub.add_argument("--max-variables", type=int, default=512)
        sub.add_argument("--timeout-ms", type=int, default=5000)
        sub.add_argument("--resource-limit", type=int, default=1000000)
        sub.set_defaults(handler=_command_formal)
    return parser


def _selected_config(args: argparse.Namespace) -> Path:
    positional = getattr(args, "config", None)
    optional = getattr(args, "config_option", None)
    if positional and optional:
        raise CliError("pass the configuration either positionally or with --config, not both")
    return Path(optional or positional or "opencollate.toml")


def _reject_inapplicable_source_fields(
    source: SourceConfig,
    *,
    allowed: frozenset[str],
) -> None:
    configured = {
        "include_dirs": bool(source.include_dirs),
        "defines": bool(source.defines),
        "profile": source.profile is not None,
        "columns": bool(source.columns),
    }
    inapplicable = sorted(
        name for name, present in configured.items() if present and name not in allowed
    )
    if inapplicable:
        raise CliError(
            f"{source.view}: source field(s) are not supported for this format: "
            + ", ".join(inapplicable)
        )


def _parse_source(source: SourceConfig) -> ViewObservation:
    paths = source.expand_files()
    kind = source.view.kind.lower()
    options = dict(source.options)
    from opencollate.parsers import UnsupportedFormatError, get_registration

    try:
        registration = get_registration(kind)
    except UnsupportedFormatError:
        registration = None
    plugin_generic_fields = (
        frozenset(("include_dirs", "defines", "profile", "columns"))
        if registration is not None and not registration.builtin
        else frozenset()
    )
    generic_fields = (
        frozenset(("include_dirs", "defines"))
        if kind in {"rtl", "sv", "systemverilog", "verilog"}
        else frozenset(("defines",))
        if kind in {"systemrdl", "system_rdl", "system-rdl", "rdl"}
        else frozenset(("profile", "columns"))
        if kind in {"csv", "pinmap", "pin_map"}
        else frozenset(("columns",))
        if kind in {"connectivity", "connectivity_spec", "connectivity-spec", "conn"}
        else plugin_generic_fields
    )
    _reject_inapplicable_source_fields(source, allowed=generic_fields)
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
    if kind in {"systemrdl", "system_rdl", "system-rdl", "rdl"}:
        _reject_unknown_source_options(source, options, {"top", "component_name"})
        _validate_string_option(source, options, "top", nonempty=True)
        _validate_string_option(source, options, "component_name", nonempty=True)
        from opencollate.parsers.systemrdl import parse_systemrdl

        return parse_systemrdl(
            paths,
            view_id=source.view,
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
        if source.profile is not None:
            profile = source.profile.strip().casefold().replace("-", "_")
            if profile not in {"auto", "component_pins", "package_map"}:
                raise CliError(
                    f"{source.view}: profile must be auto, component_pins, or package_map"
                )
            options["profile"] = profile
        from opencollate.parsers.csvpins import parse_pin_csv

        return parse_pin_csv(
            paths,
            view_id=source.view,
            column_map={
                source_name: canonical for canonical, source_name in source.columns.items()
            },
            **options,
        )
    if kind in {"connectivity", "connectivity_spec", "connectivity-spec", "conn"}:
        _reject_unknown_source_options(source, options, {"delimiter"})
        _validate_string_option(source, options, "delimiter", length=1)
        _validate_csv_delimiter(source, options)
        from opencollate.parsers.connectivity import parse_connectivity_csv

        return parse_connectivity_csv(
            paths,
            view_id=source.view,
            column_map={
                source_name: canonical for canonical, source_name in source.columns.items()
            },
            **options,
        )
    if kind in {"ipxact", "ip_xact", "ip-xact", "spirit"}:
        _reject_unknown_source_options(source, options, {"parameter_values"})
        _validate_integer_mapping_option(source, options, "parameter_values")
        from opencollate.parsers.ipxact import parse_ipxact

        return parse_ipxact(paths, view_id=source.view, **options)
    if kind == "sdc":
        _reject_unknown_source_options(source, options, set())
        from opencollate.parsers.sdc import parse_sdc

        return parse_sdc(paths, view_id=source.view)
    if kind == "upf":
        _reject_unknown_source_options(source, options, {"component_name"})
        _validate_string_option(
            source,
            options,
            "component_name",
            nonempty=True,
            maximum_length=16_384,
        )
        from opencollate.parsers.upf import parse_upf

        return parse_upf(paths, view_id=source.view, **options)
    if kind in {"header", "c_header", "c-header", "cheader", "software"}:
        supported = {"component_name", "macro_prefix", "default_register_width"}
        _reject_unknown_source_options(source, options, supported)
        _validate_string_option(source, options, "component_name", nonempty=True)
        _validate_string_option(source, options, "macro_prefix", nonempty=True)
        _validate_positive_integer_option(source, options, "default_register_width")
        from opencollate.parsers.cheader import parse_c_header

        return parse_c_header(paths, view_id=source.view, **options)
    if kind in {"cdl", "spice", "sp", "circuit"}:
        _reject_unknown_source_options(source, options, set())
        from opencollate.parsers.cdl import parse_cdl

        return parse_cdl(paths, view_id=source.view)
    if kind == "def":
        _reject_unknown_source_options(source, options, set())
        from opencollate.parsers.defparser import parse_def

        return parse_def(paths, view_id=source.view)
    if kind in {"gds", "gdsii", "gds2", "stream"}:
        supported = {"top_cells", "pin_text_layers", "pin_text_types"}
        _reject_unknown_source_options(source, options, supported)
        _validate_name_or_name_array_option(source, options, "top_cells")
        _validate_gds_top_cells(source, options)
        _validate_integer_or_integer_array_option(source, options, "pin_text_layers")
        _validate_integer_or_integer_array_option(source, options, "pin_text_types")
        from opencollate.parsers.gds import parse_gds

        return parse_gds(paths, view_id=source.view, **options)
    from opencollate.parsers import parse as parse_collateral

    plugin_options = dict(options)
    if source.include_dirs:
        plugin_options["include_dirs"] = source.include_dirs
    if source.defines:
        plugin_options["defines"] = dict(source.defines)
    if source.profile is not None:
        plugin_options["profile"] = source.profile
    if source.columns:
        plugin_options["columns"] = dict(source.columns)
    try:
        return parse_collateral(
            kind,
            paths,
            view_id=source.view,
            **plugin_options,
        )
    except UnsupportedFormatError as error:
        raise CliError(
            f"no parser is registered for source view {source.view}: {error}",
            code="OC1001",
        ) from error


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


def _validate_name_or_name_array_option(
    source: SourceConfig,
    options: dict[str, Any],
    name: str,
) -> None:
    if name not in options:
        return
    value = options[name]
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, list) or not names or not all(isinstance(item, str) for item in names):
        raise CliError(f"{source.view}: source option {name!r} must be a string or string array")
    if any(not item.strip() for item in names):
        raise CliError(f"{source.view}: source option {name!r} must not contain empty names")


def _validate_integer_or_integer_array_option(
    source: SourceConfig,
    options: dict[str, Any],
    name: str,
) -> None:
    if name not in options:
        return
    value = options[name]
    values = [value] if type(value) is int else value
    if not isinstance(values, list) or not all(type(item) is int for item in values):
        raise CliError(f"{source.view}: source option {name!r} must be an integer or integer array")
    if any(not 0 <= item <= 32_767 for item in values):
        raise CliError(f"{source.view}: source option {name!r} values must be between 0 and 32767")


def _validate_gds_top_cells(source: SourceConfig, options: dict[str, Any]) -> None:
    if "top_cells" not in options:
        return
    raw = options["top_cells"]
    names = [raw] if isinstance(raw, str) else raw
    # The generic validator has already established this shape.
    if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
        return
    seen: set[str] = set()
    for name in names:
        try:
            encoded = name.encode("ascii")
        except UnicodeEncodeError as error:
            raise CliError(
                f"{source.view}: source option 'top_cells' names must be 7-bit ASCII"
            ) from error
        if name in seen:
            raise CliError(
                f"{source.view}: source option 'top_cells' contains duplicate name {name!r}"
            )
        if len(encoded) > 16_384:
            raise CliError(
                f"{source.view}: source option 'top_cells' names must not exceed 16,384 bytes"
            )
        seen.add(name)


def _validate_string_option(
    source: SourceConfig,
    options: dict[str, Any],
    name: str,
    *,
    length: int | None = None,
    maximum_length: int | None = None,
    nonempty: bool = False,
) -> None:
    if name not in options:
        return
    value = options[name]
    if not isinstance(value, str):
        raise CliError(f"{source.view}: source option {name!r} must be a string")
    if nonempty and not value.strip():
        raise CliError(f"{source.view}: source option {name!r} must not be empty")
    if length is not None and len(value) != length:
        qualifier = "exactly one character" if length == 1 else f"exactly {length} characters"
        raise CliError(f"{source.view}: source option {name!r} must be {qualifier}")
    if maximum_length is not None and len(value) > maximum_length:
        raise CliError(
            f"{source.view}: source option {name!r} must not exceed {maximum_length:,} characters"
        )


def _validate_positive_integer_option(
    source: SourceConfig,
    options: dict[str, Any],
    name: str,
) -> None:
    if name not in options:
        return
    value = options[name]
    if type(value) is not int or value < 1:
        raise CliError(f"{source.view}: source option {name!r} must be a positive integer")


def _validate_integer_mapping_option(
    source: SourceConfig,
    options: dict[str, Any],
    name: str,
) -> None:
    if name not in options:
        return
    value = options[name]
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and type(item) is int for key, item in value.items()
    ):
        raise CliError(f"{source.view}: source option {name!r} must map strings to integers")


def _validate_csv_delimiter(source: SourceConfig, options: dict[str, Any]) -> None:
    delimiter = options.get("delimiter")
    if delimiter is None:
        return
    if delimiter in {"\r", "\n", '"'}:
        raise CliError(
            f"{source.view}: source option 'delimiter' is not a valid CSV delimiter: {delimiter!r}"
        )
    try:
        next(csv.reader(("",), delimiter=delimiter), None)
    except (TypeError, ValueError) as error:
        raise CliError(
            f"{source.view}: source option 'delimiter' is not a valid CSV delimiter: {delimiter!r}"
        ) from error


def _source_parallel_safe(source: SourceConfig) -> bool:
    from opencollate.parsers import UnsupportedFormatError, get_registration

    try:
        return get_registration(source.view.kind).parallel_safe
    except UnsupportedFormatError:
        return False


def _load_observations(
    config: ProjectConfig,
    *,
    jobs: int = 1,
) -> tuple[ViewObservation, ...]:
    return ordered_parallel_map(
        config.sources,
        _parse_source,
        jobs=jobs,
        parallel_safe=_source_parallel_safe,
    )


def _run(config: ProjectConfig, *, jobs: int = 1) -> EngineResult:
    return ComparisonEngine(config).run(_load_observations(config, jobs=jobs))


def _render(result: EngineResult, format_name: str, *, verbose: bool = False) -> str:
    if format_name == "html":
        return render_html(result)
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


def _json_nesting_overflow(text: str) -> tuple[int, int] | None:
    depth = 0
    line = 1
    column = 1
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_REPORT_JSON_NESTING:
                return line, column
        elif character in "]}" and depth:
            depth -= 1

        if character == "\n":
            line += 1
            column = 1
        else:
            column += 1
    return None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _read_json_report(path: str | Path, *, label: str) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as stream:
            data = stream.read(MAX_REPORT_JSON_BYTES + 1)
    except OSError as error:
        raise CliError(f"cannot read {label} report {source}: {error}") from error
    if len(data) > MAX_REPORT_JSON_BYTES:
        raise CliError(
            f"cannot read {label} report {source}: file exceeds the "
            f"{MAX_REPORT_JSON_BYTES:,}-byte limit"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise CliError(f"cannot decode {label} report {source} as UTF-8: {error}") from error

    overflow = _json_nesting_overflow(text)
    if overflow is not None:
        line, column = overflow
        raise CliError(
            f"cannot parse {label} report {source}: line {line}, column {column}: "
            f"JSON nesting exceeds the limit of {MAX_REPORT_JSON_NESTING}"
        )
    try:
        value = json.loads(text, object_pairs_hook=_unique_json_object)
    except json.JSONDecodeError as error:
        raise CliError(
            f"cannot parse {label} report {source}: line {error.lineno}, column {error.colno}: "
            f"{error.msg}"
        ) from error
    except RecursionError as error:
        raise CliError(
            f"cannot parse {label} report {source}: JSON nesting exceeds the supported limit"
        ) from error
    except ValueError as error:
        raise CliError(
            f"cannot parse {label} report {source}: invalid JSON value: {error}"
        ) from error
    if not isinstance(value, Mapping):
        raise CliError(f"{label} report {source} must contain a JSON object")
    return {str(key): item for key, item in value.items()}


def _paths_alias(first: str | Path, second: str | Path) -> bool:
    first_path = Path(first).expanduser().resolve()
    second_path = Path(second).expanduser().resolve()
    if first_path == second_path:
        return True
    try:
        return first_path.samefile(second_path)
    except OSError:
        return False


def _reject_path_alias(
    first: str | Path,
    first_label: str,
    second: str | Path | None,
    second_label: str,
) -> None:
    if second is not None and _paths_alias(first, second):
        raise CliError(f"{first_label} and {second_label} must not alias the same file")


def _saved_report_exit_code(report: Mapping[str, Any], *, label: str) -> int:
    value = report.get("exit_code")
    if type(value) is not int or value not in {0, 1, 2}:
        raise BaselineReportError(f"{label} report exit_code must be 0, 1, or 2")
    return value


def _render_diff(diff: ReportDiff, format_name: str, *, include_unchanged: bool = False) -> str:
    if format_name == "html":
        payload = diff.to_dict()
        if not include_unchanged:
            payload["findings"] = [
                row for row in payload["findings"] if row["state"] != "unchanged"
            ]
        return render_html(payload)
    if format_name == "json":
        return render_diff_json(diff)
    if format_name == "sarif":
        return render_diff_sarif(diff, include_unchanged=include_unchanged)
    if format_name == "markdown":
        return render_diff_markdown(diff, include_unchanged=include_unchanged)
    return render_diff_text(diff, include_unchanged=include_unchanged)


def _diff_exit_code(
    diff: ReportDiff,
    *,
    fail_on: str,
    deny_warnings: bool,
    current_exit_code: object | None = None,
) -> int:
    if current_exit_code == 2 or diff.summary.current_fatal:
        return 2
    states = {
        "none": frozenset(),
        "new": frozenset((FindingState.NEW,)),
        "changed": frozenset((FindingState.NEW, FindingState.CHANGED)),
        "all": frozenset((FindingState.NEW, FindingState.CHANGED, FindingState.UNCHANGED)),
    }[fail_on]
    for item in diff.findings:
        current = item.current
        if item.state not in states or current is None or item.current_suppressed:
            continue
        severity = current.get("severity")
        if severity in {"fatal", "error"} or (deny_warnings and severity == "warning"):
            return 1
    return 0


def _command_check(args: argparse.Namespace) -> int:
    config = load_config(_selected_config(args))
    if args.deny_warnings and not config.policy.deny_warnings:
        config = replace(config, policy=replace(config.policy, deny_warnings=True))
    result = _run(config, jobs=args.jobs)
    _emit(_render(result, args.format, verbose=args.verbose), args.output)
    return result.exit_code


def _command_review(args: argparse.Namespace) -> int:
    _reject_path_alias(args.baseline, "baseline report", args.write_report, "current report output")
    _reject_path_alias(args.baseline, "baseline report", args.output, "diff output")
    if args.write_report is not None:
        _reject_path_alias(args.write_report, "current report output", args.output, "diff output")
    baseline = _read_json_report(args.baseline, label="baseline")
    config = load_config(_selected_config(args))
    if args.deny_warnings and not config.policy.deny_warnings:
        config = replace(config, policy=replace(config.policy, deny_warnings=True))
    result = _run(config, jobs=args.jobs)
    current = result.to_dict()
    if args.write_report:
        _write_text_file(
            Path(args.write_report).expanduser().resolve(),
            render_json(result),
            description="current report",
        )
    diff = diff_reports(baseline, current)
    _emit(
        _render_diff(diff, args.format, include_unchanged=args.include_unchanged),
        args.output,
    )
    return _diff_exit_code(
        diff,
        fail_on=args.fail_on,
        deny_warnings=config.policy.deny_warnings,
        current_exit_code=result.exit_code,
    )


def _command_report_diff(args: argparse.Namespace) -> int:
    _reject_path_alias(args.baseline, "baseline report", args.output, "diff output")
    _reject_path_alias(args.current, "current report", args.output, "diff output")
    baseline = _read_json_report(args.baseline, label="baseline")
    current = _read_json_report(args.current, label="current")
    current_exit_code = _saved_report_exit_code(current, label="current")
    diff = diff_reports(baseline, current)
    _emit(
        _render_diff(diff, args.format, include_unchanged=args.include_unchanged),
        args.output,
    )
    return _diff_exit_code(
        diff,
        fail_on=args.fail_on,
        deny_warnings=args.deny_warnings,
        current_exit_code=current_exit_code,
    )


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
    result = _run(load_config(root / "opencollate.toml"), jobs=args.jobs)
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

# Enable the views your project owns:
# [sources.ipxact.component]
# files = ["ipxact/component.xml"]
#
# [sources.sdc.functional]
# files = ["constraints/**/*.sdc"]
#
# [sources.upf.low_power]
# files = ["power/**/*.upf"]
# component_name = "soc_top"
#
# [sources.header.firmware]
# files = ["software/include/registers.h"]
# component_name = "uart0"
# macro_prefix = "UART0"
# default_register_width = 32
#
# [sources.systemrdl.registers]
# files = ["registers/**/*.rdl"]
# top = "soc_registers"
# component_name = "soc_top"
#
# [sources.connectivity.intent]
# files = ["connectivity/requirements.csv"]
# # Required columns: id, source, sink, expect.
# # Optional columns: transform, through, exclude, description.
#
# [sources.cdl.extracted]
# files = ["netlist/**/*.cdl"]
#
# [sources.def.placed]
# files = ["physical/**/*.def"]
#
# [sources.gds.stream]
# files = ["physical/**/*.gds"]
# top_cells = ["soc_top"]
# # Text labels become candidate pins only with explicit selectors:
# pin_text_layers = [10]
# pin_text_types = [0]

[contract]
baseline = "rtl.default"

[policy]
rtl_power_pins = "optional"
max_boolean_inputs = 12
# Optional exact SMT backend: install opencollate[formal] first.
# boolean_backend = "z3"
# max_symbolic_inputs = 512
# symbolic_timeout_ms = 5000
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
        pyslang = import_module("pyslang")
        slang_version = getattr(pyslang, "__version__", "installed")
    except ImportError:  # pragma: no cover - package metadata requires pyslang
        slang_version = None
    try:
        systemrdl_version = metadata.version("systemrdl-compiler")
    except metadata.PackageNotFoundError:  # pragma: no cover - required package metadata
        systemrdl_version = None
    try:
        z3_version = metadata.version("z3-solver")
    except metadata.PackageNotFoundError:
        z3_version = None
    return {
        "tool": {"name": "OpenCollate", "version": __version__},
        "symbolic_boolean": {
            "backend": "z3",
            "installed_version": z3_version,
            "semantics": "two-valued-combinational",
            "opt_in": True,
        },
        "formats": {
            "verilog_systemverilog": {
                "status": "supported",
                "backend": "pyslang",
                "version": slang_version,
            },
            "liberty": {"status": "supported", "backend": "native"},
            "lef": {"status": "supported", "backend": "native"},
            "csv_pin_maps": {"status": "supported", "backend": "stdlib"},
            "ip_xact": {"status": "supported", "backend": "stdlib-expat"},
            "sdc": {"status": "supported", "backend": "native-static"},
            "upf": {"status": "supported", "backend": "native-static"},
            "c_register_headers": {"status": "supported", "backend": "stdlib"},
            "cdl_spice": {"status": "supported", "backend": "native-static"},
            "def": {"status": "supported", "backend": "native-structural"},
            "gdsii": {
                "status": "supported",
                "backend": "native-structural-streaming",
            },
            "systemrdl_2_0": {
                "status": "supported",
                "backend": "systemrdl-compiler",
                "version": systemrdl_version,
            },
            "connectivity_csv": {
                "status": "supported",
                "backend": "native-bounded-static",
            },
        },
        "outputs": [
            "html",
            "formal-receipt-json",
            "contract-diff-json",
            "text",
            "json",
            "sarif",
            "markdown",
            "contract-json",
            "report-diff-json",
        ],
        "rules": len(list(iter_rules())),
        "parser_registry": parser_inventory(),
        "plugins": plugin_inventory(),
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
    registry = data["parser_registry"]
    external = [item for item in registry["registrations"] if not item["builtin"]]
    print(f"\nBuilt-in rules: {data['rules']}")
    print(f"External parsers: {len(external)}")
    print(f"External checkers: {len(data['plugins']['checkers'])}")
    if registry["failures"] or data["plugins"]["failures"]:
        failures = {
            (
                item["group"],
                item["name"],
                item.get("provider"),
                item["error_type"],
                item["message"],
            )
            for item in [*registry["failures"], *data["plugins"]["failures"]]
        }
        print(f"Plugin failures: {len(failures)}")
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
    result = _run(config, jobs=args.jobs)
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


def _command_contract_migrate(args: argparse.Namespace) -> int:
    source = Path(args.input).expanduser().resolve()
    contract = load_contract(source)
    upgraded = upgrade_contract(contract)
    destination = (
        Path(args.output).expanduser().resolve()
        if args.output
        else source.with_name(f"{source.stem}.v2{source.suffix}")
    )
    try:
        target = write_contract(upgraded, destination)
    except OSError as error:
        raise CliError(f"cannot write contract {destination}: {error}") from error
    print(f"Wrote schema {upgraded.schema_version} contract to {target}")
    return 0


def _command_contract_diff(args: argparse.Namespace) -> int:
    from opencollate.contract_review import diff_contracts

    _reject_path_alias(args.baseline, "baseline contract", args.output, "diff output")
    _reject_path_alias(args.current, "current contract", args.output, "diff output")
    try:
        result = diff_contracts(load_contract(args.baseline), load_contract(args.current))
    except (ValueError, TypeError) as error:
        raise CliError(str(error)) from error
    _emit(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", args.output)
    return int(result["exit_code"])


def _command_formal(args: argparse.Namespace) -> int:
    from opencollate.formal import replay_receipt, run_obligations
    from opencollate.symbolic import SymbolicLimits

    _reject_path_alias(args.request, "obligation request", args.output, "formal output")
    if args.formal_command == "replay":
        _reject_path_alias(args.receipt, "prior receipt", args.output, "formal output")
    request = _read_json_report(args.request, label="obligation")
    try:
        limits = SymbolicLimits(
            max_variables=args.max_variables,
            timeout_ms=args.timeout_ms,
            resource_limit=args.resource_limit,
            max_queries=args.max_variables + 2,
        )
        result = (
            replay_receipt(request, _read_json_report(args.receipt, label="receipt"), limits=limits)
            if args.formal_command == "replay"
            else run_obligations(request, limits=limits)
        )
    except (ValueError, TypeError) as error:
        raise CliError(str(error)) from error
    _emit(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", args.output)
    return int(result["exit_code"])


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return int(handler(args))
    except (BaselineReportError, ConfigError, CliError, FileExistsError) as error:
        code = getattr(error, "code", "OC1001")
        print(f"{code}: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


__all__ = ["build_parser", "main"]

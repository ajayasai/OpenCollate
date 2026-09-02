"""Format inference, versioned plugin discovery, and uniform parser dispatch."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencollate.diagnostics import Severity
from opencollate.model import FactState, ViewId, ViewObservation
from opencollate.parsers.base import (
    Pathish,
    UnsupportedFormatError,
    ViewParser,
    coerce_paths,
    coerce_view,
    parser_diagnostic,
    provenance,
)
from opencollate.parsers.cdl import CdlParser
from opencollate.parsers.cheader import CHeaderParser
from opencollate.parsers.connectivity import ConnectivityCsvParser
from opencollate.parsers.csvpins import CsvPinMapParser
from opencollate.parsers.defparser import DefParser
from opencollate.parsers.gds import GdsParser
from opencollate.parsers.ipxact import IpxactParser
from opencollate.parsers.lef import LefParser
from opencollate.parsers.liberty import LibertyParser
from opencollate.parsers.sdc import SdcParser
from opencollate.parsers.systemrdl import SystemRdlParser
from opencollate.parsers.upf import UpfParser
from opencollate.parsers.verilog import VerilogParser
from opencollate.plugins import (
    PARSER_ENTRY_POINT_GROUP,
    ParserPluginSpec,
    PluginConflictError,
    PluginFailure,
    discover_parser_plugins,
    register_parser_plugin,
    reset_plugin_discovery,
    unregister_parser_plugin,
)

_BUILTIN_PARSERS: dict[str, ViewParser] = {
    "verilog": VerilogParser(),
    "connectivity": ConnectivityCsvParser(),
    "liberty": LibertyParser(),
    "lef": LefParser(),
    "csv": CsvPinMapParser(),
    "ipxact": IpxactParser(),
    "sdc": SdcParser(),
    "upf": UpfParser(),
    "header": CHeaderParser(),
    "cdl": CdlParser(),
    "def": DefParser(),
    "gds": GdsParser(),
    "systemrdl": SystemRdlParser(),
}

_BUILTIN_ALIASES = {
    "v": "verilog",
    "sv": "verilog",
    "systemverilog": "verilog",
    "rtl": "verilog",
    "lib": "liberty",
    "timing": "liberty",
    "pinmap": "csv",
    "pin_map": "csv",
    "package": "csv",
    "tsv": "csv",
    "ip_xact": "ipxact",
    "spirit": "ipxact",
    "c_header": "header",
    "cheader": "header",
    "software_header": "header",
    "spice": "cdl",
    "sp": "cdl",
    "circuit": "cdl",
    "design_exchange_format": "def",
    "gdsii": "gds",
    "gds2": "gds",
    "stream": "gds",
    "rdl": "systemrdl",
    "system_rdl": "systemrdl",
    "conn": "connectivity",
    "connectivity_spec": "connectivity",
}

_BUILTIN_EXTENSIONS = {
    ".v": "verilog",
    ".vh": "verilog",
    ".sv": "verilog",
    ".svh": "verilog",
    ".lib": "liberty",
    ".lef": "lef",
    ".csv": "csv",
    ".tsv": "csv",
    ".xml": "ipxact",
    ".ipxact": "ipxact",
    ".sdc": "sdc",
    ".upf": "upf",
    ".h": "header",
    ".hh": "header",
    ".hpp": "header",
    ".cdl": "cdl",
    ".cir": "cdl",
    ".ckt": "cdl",
    ".sp": "cdl",
    ".spi": "cdl",
    ".spice": "cdl",
    ".def": "def",
    ".gds": "gds",
    ".gdsii": "gds",
    ".rdl": "systemrdl",
    ".occonn": "connectivity",
}


def _format_token(value: str) -> str:
    return value.strip().lower().lstrip(".").replace("-", "_")


@dataclass(frozen=True, slots=True)
class ParserRegistration:
    """The resolved owner of one collateral format."""

    format_name: str
    parser: ViewParser
    aliases: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    provider: str = "OpenCollate"
    version: str | None = None
    plugin_name: str | None = None
    builtin: bool = False
    parallel_safe: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format_name,
            "aliases": list(self.aliases),
            "extensions": list(self.extensions),
            "provider": self.provider,
            "version": self.version,
            "plugin": self.plugin_name,
            "builtin": self.builtin,
            "parallel_safe": self.parallel_safe,
        }


@dataclass(frozen=True, slots=True)
class _RegistrySnapshot:
    registrations: Mapping[str, ParserRegistration]
    aliases: Mapping[str, str]
    extensions: Mapping[str, str]
    failures: tuple[PluginFailure, ...]


def _builtin_registration(format_name: str, parser: ViewParser) -> ParserRegistration:
    aliases = tuple(
        sorted(alias for alias, owner in _BUILTIN_ALIASES.items() if owner == format_name)
    )
    extensions = tuple(
        sorted(
            extension for extension, owner in _BUILTIN_EXTENSIONS.items() if owner == format_name
        )
    )
    return ParserRegistration(
        format_name=format_name,
        parser=parser,
        aliases=aliases,
        extensions=extensions,
        provider="OpenCollate",
        builtin=True,
        parallel_safe=True,
    )


def _conflict_failure(spec: ParserPluginSpec, conflicts: Sequence[str]) -> PluginFailure:
    error = PluginConflictError("; ".join(conflicts))
    return PluginFailure(
        group=PARSER_ENTRY_POINT_GROUP,
        name=str(spec.name),
        provider=spec.provider,
        version=spec.version,
        error_type=type(error).__name__,
        message=str(error),
    )


def _registry_snapshot() -> _RegistrySnapshot:
    registrations: dict[str, ParserRegistration] = {
        format_name: _builtin_registration(format_name, parser)
        for format_name, parser in _BUILTIN_PARSERS.items()
    }
    aliases = {_format_token(alias): owner for alias, owner in _BUILTIN_ALIASES.items()}
    extensions = dict(_BUILTIN_EXTENSIONS)
    specs, discovered_failures = discover_parser_plugins()
    failures = list(discovered_failures)

    for spec in sorted(
        specs,
        key=lambda item: (
            item.format_name,
            str(item.name),
            item.provider or "",
            item.version or "",
        ),
    ):
        format_name = spec.format_name
        conflicts: list[str] = []
        if format_name in registrations:
            format_owner = registrations[format_name]
            conflicts.append(f"format {format_name!r} is already owned by {format_owner.provider}")
        alias_owner = aliases.get(format_name)
        if alias_owner is not None and alias_owner != format_name:
            conflicts.append(f"format {format_name!r} conflicts with an alias for {alias_owner!r}")
        for alias in spec.aliases:
            token = _format_token(alias)
            if token in registrations and token != format_name:
                conflicts.append(f"alias {alias!r} conflicts with registered format {token!r}")
            token_owner = aliases.get(token)
            if token_owner is not None and token_owner != format_name:
                conflicts.append(f"alias {alias!r} is already owned by {token_owner!r}")
        for extension in spec.extensions:
            extension_owner = extensions.get(extension)
            if extension_owner is not None and extension_owner != format_name:
                conflicts.append(f"extension {extension!r} is already owned by {extension_owner!r}")
        if conflicts:
            failures.append(_conflict_failure(spec, tuple(sorted(set(conflicts)))))
            continue

        registration = ParserRegistration(
            format_name=format_name,
            parser=spec.parser,
            aliases=spec.aliases,
            extensions=spec.extensions,
            provider=spec.provider or "unknown distribution",
            version=spec.version,
            plugin_name=spec.name,
            builtin=False,
            parallel_safe=spec.parallel_safe,
        )
        registrations[format_name] = registration
        for alias in spec.aliases:
            aliases[_format_token(alias)] = format_name
        for extension in spec.extensions:
            extensions[extension] = format_name

    return _RegistrySnapshot(
        registrations=dict(sorted(registrations.items())),
        aliases=dict(sorted(aliases.items())),
        extensions=dict(sorted(extensions.items())),
        failures=tuple(
            sorted(
                failures,
                key=lambda item: (
                    item.name,
                    item.provider or "",
                    item.error_type,
                    item.message,
                ),
            )
        ),
    )


def normalize_format(format_name: str) -> str:
    snapshot = _registry_snapshot()
    normalized = _format_token(format_name)
    normalized = snapshot.aliases.get(normalized, normalized)
    if normalized not in snapshot.registrations:
        supported = ", ".join(snapshot.registrations)
        failure_note = (
            f"; {len(snapshot.failures)} parser plugin(s) failed to load "
            "(inspect `opencollate capabilities --json`)"
            if snapshot.failures
            else ""
        )
        raise UnsupportedFormatError(
            f"unsupported collateral format {format_name!r}; "
            f"supported formats: {supported}{failure_note}"
        )
    return normalized


def infer_format(paths: Sequence[Path]) -> str:
    snapshot = _registry_snapshot()
    formats: set[str] = set()
    unknown: list[str] = []
    for path in paths:
        detected = snapshot.extensions.get(path.suffix.lower())
        if detected is None:
            unknown.append(str(path))
        else:
            formats.add(detected)
    if unknown:
        raise UnsupportedFormatError("cannot infer collateral format for " + ", ".join(unknown))
    if len(formats) != 1:
        raise UnsupportedFormatError(
            "a single parser dispatch cannot mix formats: " + ", ".join(sorted(formats))
        )
    return formats.pop()


def get_registration(format_name: str) -> ParserRegistration:
    snapshot = _registry_snapshot()
    return snapshot.registrations[normalize_format(format_name)]


def get_parser(format_name: str) -> ViewParser:
    return get_registration(format_name).parser


def _plugin_failure_view(
    registration: ParserRegistration,
    source_paths: Sequence[Path],
    *,
    view_id: ViewId | str | None,
    view_name: str,
    error: BaseException,
) -> ViewObservation:
    view = coerce_view(view_id, kind=registration.format_name, name=view_name)
    message = " ".join(str(error).split()) or "no error message"
    message = message[:2_000]
    location = provenance(source_paths[0], view) if source_paths else None
    diagnostic = parser_diagnostic(
        "OC9001",
        Severity.FATAL,
        f"Parser plugin {registration.plugin_name!r} from {registration.provider} "
        f"failed: {type(error).__name__}: {message}.",
        location=location,
        metadata={
            "plugin": registration.plugin_name,
            "provider": registration.provider,
            "version": registration.version,
            "format": registration.format_name,
            "error_type": type(error).__name__,
        },
    )
    return ViewObservation(
        view=view,
        diagnostics=(diagnostic,),
        complete=False,
        tainted_scopes=frozenset(("*",)),
        attributes={
            "parser_state": FactState.UNSUPPORTED.value,
            "parser_plugin": registration.plugin_name,
            "parser_provider": registration.provider,
            "parser_version": registration.version,
        },
    )


def parse(
    format_or_paths: str | Path | Sequence[Pathish],
    paths: Pathish | Sequence[Pathish] | None = None,
    *,
    format: str | None = None,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    **options: Any,
) -> ViewObservation:
    """Parse collateral using an explicit or inferred built-in/plugin format.

    Third-party parser exceptions never escape as a false pass. They become a
    fatal, whole-view-tainted ``OC9001`` observation with plugin provenance.
    """

    if paths is None:
        source_paths = coerce_paths(format_or_paths)
        selected_format = normalize_format(format) if format else infer_format(source_paths)
    else:
        source_paths = coerce_paths(paths)
        selected_format = normalize_format(format or str(format_or_paths))
    if selected_format in {"csv", "connectivity"} and source_paths[0].suffix.lower() == ".tsv":
        options.setdefault("delimiter", "\t")
    registration = get_registration(selected_format)
    if registration.builtin:
        return registration.parser.parse(
            source_paths,
            view_id=view_id,
            view_name=view_name,
            **options,
        )
    plugin_view_id = view_id if view_id is not None else ViewId(selected_format, view_name)
    try:
        result = registration.parser.parse(
            source_paths,
            view_id=plugin_view_id,
            **options,
        )
        if not isinstance(result, ViewObservation):
            raise TypeError(f"parser returned {type(result).__name__}, expected ViewObservation")
        return result
    except Exception as error:
        return _plugin_failure_view(
            registration,
            source_paths,
            view_id=view_id,
            view_name=view_name,
            error=error,
        )


def registered_formats() -> tuple[str, ...]:
    return tuple(_registry_snapshot().registrations)


def parser_inventory() -> dict[str, Any]:
    snapshot = _registry_snapshot()
    return {
        "registrations": [
            item.to_dict()
            for item in sorted(
                snapshot.registrations.values(),
                key=lambda registration: registration.format_name,
            )
        ],
        "failures": [item.to_dict() for item in snapshot.failures],
    }


def reload_parser_plugins() -> None:
    """Refresh Python entry points for long-running host processes."""

    reset_plugin_discovery()


__all__ = [
    "ParserRegistration",
    "get_parser",
    "get_registration",
    "infer_format",
    "normalize_format",
    "parse",
    "parser_inventory",
    "register_parser_plugin",
    "registered_formats",
    "reload_parser_plugins",
    "unregister_parser_plugin",
]

"""Versioned third-party extension contracts for OpenCollate.

Plugins are discovered through Python package entry points. Discovery is lazy,
deterministic, and fail-closed: a broken checker becomes an OC9002 diagnostic,
while a broken parser is excluded from dispatch and reported by the capability
inventory. Runtime registration is also available for embedding and tests.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date
from importlib import metadata
from threading import RLock
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from opencollate.diagnostics import Diagnostic
from opencollate.model import CanonicalDesign, DesignContract, ViewObservation

if TYPE_CHECKING:
    from opencollate.config import ProjectConfig
    from opencollate.parsers.base import ViewParser

PLUGIN_API_VERSION = 1
PARSER_ENTRY_POINT_GROUP = "opencollate.parsers"
CHECKER_ENTRY_POINT_GROUP = "opencollate.checkers"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class PluginError(RuntimeError):
    """Base class for user-actionable extension failures."""


class PluginContractError(PluginError):
    """Raised when a plugin does not implement the advertised API."""


class PluginConflictError(PluginError):
    """Raised when runtime registration would replace an existing plugin."""


@runtime_checkable
class CheckerPlugin(Protocol):
    """Protocol implemented by semantic checker objects."""

    name: str

    def check(self, context: CheckerContext) -> Iterable[Diagnostic]: ...


CheckerCallable = Callable[["CheckerContext"], Iterable[Diagnostic]]


def _is_parser(value: Any) -> bool:
    return isinstance(getattr(value, "format_name", None), str) and callable(
        getattr(value, "parse", None)
    )


def _normalized_name(value: str, *, what: str) -> str:
    if not isinstance(value, str):
        raise PluginContractError(f"{what} must be a string")
    normalized = value.strip().lower().lstrip(".").replace("-", "_")
    if not normalized:
        raise PluginContractError(f"{what} must not be empty")
    if not normalized[0].isalpha() or any(
        not (character.isalnum() or character == "_") for character in normalized
    ):
        raise PluginContractError(
            f"{what} {value!r} must contain only letters, digits, underscores, or hyphens"
        )
    return normalized


def _normalized_extension(value: str) -> str:
    if not isinstance(value, str):
        raise PluginContractError("parser plugin extensions must be strings")
    normalized = value.strip().lower()
    if not normalized:
        raise PluginContractError("parser plugin extensions must not be empty")
    if not normalized.startswith("."):
        normalized = "." + normalized
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise PluginContractError(f"invalid parser plugin extension: {value!r}")
    return normalized


@dataclass(frozen=True, slots=True)
class ParserPluginSpec:
    """A parser plus the names and suffixes it owns.

    ``provider`` and ``version`` are normally filled from package metadata at
    discovery time. A parser plugin cannot silently override a built-in format,
    alias, or extension; the complete registration is rejected on any conflict.
    """

    parser: ViewParser
    aliases: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    api_version: int = PLUGIN_API_VERSION
    name: str | None = None
    provider: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        if type(self.api_version) is not int or self.api_version != PLUGIN_API_VERSION:
            raise PluginContractError(
                f"parser plugin API {self.api_version!r} is unsupported; "
                f"this release supports {PLUGIN_API_VERSION}"
            )
        if not _is_parser(self.parser):
            raise PluginContractError(
                "parser plugin must expose format_name and parse(paths, *, view_id, **options)"
            )
        format_name = _normalized_name(self.parser.format_name, what="parser format name")
        normalized_aliases = {_normalized_name(item, what="parser alias") for item in self.aliases}
        aliases = tuple(sorted(normalized_aliases - {format_name}))
        extensions = tuple(sorted({_normalized_extension(item) for item in self.extensions}))
        name = _normalized_name(self.name or format_name, what="parser plugin name")
        if self.provider is not None and not self.provider.strip():
            raise PluginContractError("parser plugin provider must not be empty")
        if self.version is not None and not self.version.strip():
            raise PluginContractError("parser plugin version must not be empty")
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "extensions", extensions)
        object.__setattr__(self, "name", name)

    @property
    def format_name(self) -> str:
        return _normalized_name(self.parser.format_name, what="parser format name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "format": self.format_name,
            "aliases": list(self.aliases),
            "extensions": list(self.extensions),
            "api_version": self.api_version,
            "provider": self.provider,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class CheckerContext:
    """Immutable inputs supplied to every semantic checker plugin."""

    config: ProjectConfig
    observations: tuple[ViewObservation, ...]
    design: CanonicalDesign
    contract: DesignContract | None
    generated_contract: DesignContract
    today: date


@dataclass(frozen=True, slots=True)
class CheckerPluginSpec:
    """A versioned semantic checker registration."""

    checker: CheckerPlugin | CheckerCallable
    name: str | None = None
    api_version: int = PLUGIN_API_VERSION
    provider: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        if type(self.api_version) is not int or self.api_version != PLUGIN_API_VERSION:
            raise PluginContractError(
                f"checker plugin API {self.api_version!r} is unsupported; "
                f"this release supports {PLUGIN_API_VERSION}"
            )
        checker_name = self.name or getattr(self.checker, "name", None)
        if not checker_name:
            checker_name = getattr(self.checker, "__name__", None)
        object.__setattr__(
            self,
            "name",
            _normalized_name(str(checker_name or ""), what="checker plugin name"),
        )
        if not callable(self.checker) and not callable(getattr(self.checker, "check", None)):
            raise PluginContractError(
                "checker plugin must be callable or expose check(CheckerContext)"
            )
        if self.provider is not None and not self.provider.strip():
            raise PluginContractError("checker plugin provider must not be empty")
        if self.version is not None and not self.version.strip():
            raise PluginContractError("checker plugin version must not be empty")

    def run(self, context: CheckerContext) -> tuple[Diagnostic, ...]:
        method = getattr(self.checker, "check", None)
        if callable(method):
            raw = method(context)
        elif callable(self.checker):
            raw = self.checker(context)
        else:  # guarded by __post_init__; retained for defensive embedding
            raise PluginContractError(f"checker plugin {self.name!r} is not callable")
        if raw is None:
            raise PluginContractError(
                f"checker plugin {self.name!r} returned None instead of diagnostics"
            )
        diagnostics = tuple(raw)
        invalid = next((item for item in diagnostics if not isinstance(item, Diagnostic)), None)
        if invalid is not None:
            raise PluginContractError(
                f"checker plugin {self.name!r} returned {type(invalid).__name__}, not a Diagnostic"
            )
        return diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "api_version": self.api_version,
            "provider": self.provider,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class PluginFailure:
    """A sanitized, serializable discovery or execution failure."""

    group: str
    name: str
    provider: str | None
    version: str | None
    error_type: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "name": self.name,
            "provider": self.provider,
            "version": self.version,
            "error_type": self.error_type,
            "message": self.message,
        }


_LOCK = RLock()
_DISCOVERED_PARSERS: tuple[ParserPluginSpec, ...] | None = None
_DISCOVERED_CHECKERS: tuple[CheckerPluginSpec, ...] | None = None
_PARSER_FAILURES: tuple[PluginFailure, ...] = ()
_CHECKER_FAILURES: tuple[PluginFailure, ...] = ()
_RUNTIME_PARSERS: dict[str, ParserPluginSpec] = {}
_RUNTIME_CHECKERS: dict[str, CheckerPluginSpec] = {}


def _plugins_disabled() -> bool:
    return os.environ.get("OPENCOLLATE_DISABLE_PLUGINS", "").strip().lower() in _TRUTHY


def _entry_points(group: str) -> tuple[metadata.EntryPoint, ...]:
    discovered = metadata.entry_points()
    selected = discovered.select(group=group)
    return tuple(sorted(selected, key=lambda item: (item.name, item.value)))


def _provider(entry_point: metadata.EntryPoint) -> tuple[str | None, str | None]:
    distribution = getattr(entry_point, "dist", None)
    name = getattr(distribution, "name", None)
    version = getattr(distribution, "version", None)
    return (
        str(name) if name is not None else None,
        str(version) if version is not None else None,
    )


def _safe_message(error: BaseException) -> str:
    text = " ".join(str(error).split())
    if not text:
        text = "no error message"
    return text[:2_000]


def _failure(
    group: str,
    name: str,
    provider: str | None,
    version: str | None,
    error: BaseException,
) -> PluginFailure:
    return PluginFailure(
        group=group,
        name=name,
        provider=provider,
        version=version,
        error_type=type(error).__name__,
        message=_safe_message(error),
    )


def _materialize_parser(
    entry_point: metadata.EntryPoint,
    loaded: Any,
) -> ParserPluginSpec:
    provider, version = _provider(entry_point)
    candidate = loaded
    if isinstance(candidate, type):
        candidate = candidate()
    elif (
        not isinstance(candidate, ParserPluginSpec)
        and not _is_parser(candidate)
        and callable(candidate)
    ):
        candidate = candidate()
    spec = (
        candidate
        if isinstance(candidate, ParserPluginSpec)
        else ParserPluginSpec(parser=candidate, name=entry_point.name)
    )
    return replace(
        spec,
        provider=spec.provider or provider,
        version=spec.version or version,
    )


def _materialize_checker(
    entry_point: metadata.EntryPoint,
    loaded: Any,
) -> CheckerPluginSpec:
    provider, version = _provider(entry_point)
    candidate = loaded() if isinstance(loaded, type) else loaded
    spec = (
        candidate
        if isinstance(candidate, CheckerPluginSpec)
        else CheckerPluginSpec(checker=candidate, name=entry_point.name)
    )
    return replace(
        spec,
        provider=spec.provider or provider,
        version=spec.version or version,
    )


def _discover_parsers() -> tuple[tuple[ParserPluginSpec, ...], tuple[PluginFailure, ...]]:
    if _plugins_disabled():
        return (), ()
    specs: list[ParserPluginSpec] = []
    failures: list[PluginFailure] = []
    names: set[str] = set()
    for entry_point in _entry_points(PARSER_ENTRY_POINT_GROUP):
        provider, version = _provider(entry_point)
        try:
            spec = _materialize_parser(entry_point, entry_point.load())
            if spec.name in names:
                raise PluginConflictError(
                    f"more than one parser entry point registered plugin name {spec.name!r}"
                )
            names.add(str(spec.name))
            specs.append(spec)
        except Exception as error:
            failures.append(
                _failure(
                    PARSER_ENTRY_POINT_GROUP,
                    entry_point.name,
                    provider,
                    version,
                    error,
                )
            )
    return tuple(sorted(specs, key=lambda item: (str(item.name), item.format_name))), tuple(
        sorted(failures, key=lambda item: (item.name, item.provider or "", item.message))
    )


def _discover_checkers() -> tuple[tuple[CheckerPluginSpec, ...], tuple[PluginFailure, ...]]:
    if _plugins_disabled():
        return (), ()
    specs: list[CheckerPluginSpec] = []
    failures: list[PluginFailure] = []
    names: set[str] = set()
    for entry_point in _entry_points(CHECKER_ENTRY_POINT_GROUP):
        provider, version = _provider(entry_point)
        try:
            spec = _materialize_checker(entry_point, entry_point.load())
            if spec.name in names:
                raise PluginConflictError(
                    f"more than one checker entry point registered plugin name {spec.name!r}"
                )
            names.add(str(spec.name))
            specs.append(spec)
        except Exception as error:
            failures.append(
                _failure(
                    CHECKER_ENTRY_POINT_GROUP,
                    entry_point.name,
                    provider,
                    version,
                    error,
                )
            )
    return tuple(sorted(specs, key=lambda item: str(item.name))), tuple(
        sorted(failures, key=lambda item: (item.name, item.provider or "", item.message))
    )


def discover_parser_plugins(
    *,
    refresh: bool = False,
) -> tuple[tuple[ParserPluginSpec, ...], tuple[PluginFailure, ...]]:
    """Return deterministic parser entry points plus runtime registrations."""

    global _DISCOVERED_PARSERS, _PARSER_FAILURES
    with _LOCK:
        if refresh or _DISCOVERED_PARSERS is None:
            _DISCOVERED_PARSERS, _PARSER_FAILURES = _discover_parsers()
        runtime = tuple(sorted(_RUNTIME_PARSERS.values(), key=lambda item: str(item.name)))
        return _DISCOVERED_PARSERS + runtime, _PARSER_FAILURES


def discover_checker_plugins(
    *,
    refresh: bool = False,
) -> tuple[tuple[CheckerPluginSpec, ...], tuple[PluginFailure, ...]]:
    """Return deterministic checker entry points plus runtime registrations."""

    global _DISCOVERED_CHECKERS, _CHECKER_FAILURES
    with _LOCK:
        if refresh or _DISCOVERED_CHECKERS is None:
            _DISCOVERED_CHECKERS, _CHECKER_FAILURES = _discover_checkers()
        runtime = tuple(sorted(_RUNTIME_CHECKERS.values(), key=lambda item: str(item.name)))
        return _DISCOVERED_CHECKERS + runtime, _CHECKER_FAILURES


def register_parser_plugin(spec: ParserPluginSpec, *, replace_existing: bool = False) -> None:
    """Register a parser for this Python process.

    Format, alias, and extension conflicts are still rejected by parser dispatch.
    ``replace_existing`` only controls replacement of the same runtime plugin name.
    """

    name = str(spec.name)
    with _LOCK:
        if name in _RUNTIME_PARSERS and not replace_existing:
            raise PluginConflictError(f"parser plugin {name!r} is already registered")
        _RUNTIME_PARSERS[name] = spec


def unregister_parser_plugin(name: str) -> None:
    with _LOCK:
        _RUNTIME_PARSERS.pop(_normalized_name(name, what="parser plugin name"), None)


def register_checker_plugin(spec: CheckerPluginSpec, *, replace_existing: bool = False) -> None:
    """Register a semantic checker for this Python process."""

    name = str(spec.name)
    with _LOCK:
        if name in _RUNTIME_CHECKERS and not replace_existing:
            raise PluginConflictError(f"checker plugin {name!r} is already registered")
        _RUNTIME_CHECKERS[name] = spec


def unregister_checker_plugin(name: str) -> None:
    with _LOCK:
        _RUNTIME_CHECKERS.pop(_normalized_name(name, what="checker plugin name"), None)


def reset_plugin_discovery(*, clear_runtime: bool = False) -> None:
    """Clear lazy entry-point caches, primarily for long-running hosts and tests."""

    global _DISCOVERED_PARSERS, _DISCOVERED_CHECKERS, _PARSER_FAILURES, _CHECKER_FAILURES
    with _LOCK:
        _DISCOVERED_PARSERS = None
        _DISCOVERED_CHECKERS = None
        _PARSER_FAILURES = ()
        _CHECKER_FAILURES = ()
        if clear_runtime:
            _RUNTIME_PARSERS.clear()
            _RUNTIME_CHECKERS.clear()


def _checker_failure_diagnostic(
    *,
    name: str,
    provider: str | None,
    version: str | None,
    error_type: str,
    message: str,
    phase: str,
) -> Diagnostic:
    source = provider or "unknown distribution"
    release = f" {version}" if version else ""
    return Diagnostic.from_rule(
        "OC9002",
        f"Checker plugin {name!r} from {source}{release} failed during {phase}: "
        f"{error_type}: {message}.",
        metadata={
            "plugin": name,
            "provider": provider,
            "version": version,
            "phase": phase,
            "error_type": error_type,
        },
    )


def run_checker_plugins(context: CheckerContext) -> tuple[Diagnostic, ...]:
    """Execute semantic checker plugins without allowing a crash to pass analysis."""

    specs, failures = discover_checker_plugins()
    diagnostics: list[Diagnostic] = [
        _checker_failure_diagnostic(
            name=failure.name,
            provider=failure.provider,
            version=failure.version,
            error_type=failure.error_type,
            message=failure.message,
            phase="discovery",
        )
        for failure in failures
    ]
    for spec in specs:
        try:
            diagnostics.extend(spec.run(context))
        except Exception as error:
            diagnostics.append(
                _checker_failure_diagnostic(
                    name=str(spec.name),
                    provider=spec.provider,
                    version=spec.version,
                    error_type=type(error).__name__,
                    message=_safe_message(error),
                    phase="execution",
                )
            )
    return tuple(diagnostics)


def plugin_inventory() -> dict[str, Any]:
    """Return machine-readable extension provenance for audits and support."""

    parsers, parser_failures = discover_parser_plugins()
    checkers, checker_failures = discover_checker_plugins()
    return {
        "api_version": PLUGIN_API_VERSION,
        "disabled": _plugins_disabled(),
        "entry_point_groups": {
            "parsers": PARSER_ENTRY_POINT_GROUP,
            "checkers": CHECKER_ENTRY_POINT_GROUP,
        },
        "parsers": [item.to_dict() for item in parsers],
        "checkers": [item.to_dict() for item in checkers],
        "failures": [
            item.to_dict()
            for item in sorted(
                (*parser_failures, *checker_failures),
                key=lambda failure: (
                    failure.group,
                    failure.name,
                    failure.provider or "",
                    failure.message,
                ),
            )
        ],
    }


__all__ = [
    "CHECKER_ENTRY_POINT_GROUP",
    "PARSER_ENTRY_POINT_GROUP",
    "PLUGIN_API_VERSION",
    "CheckerContext",
    "CheckerPlugin",
    "CheckerPluginSpec",
    "ParserPluginSpec",
    "PluginConflictError",
    "PluginContractError",
    "PluginError",
    "PluginFailure",
    "discover_checker_plugins",
    "discover_parser_plugins",
    "plugin_inventory",
    "register_checker_plugin",
    "register_parser_plugin",
    "reset_plugin_discovery",
    "run_checker_plugins",
    "unregister_checker_plugin",
    "unregister_parser_plugin",
]

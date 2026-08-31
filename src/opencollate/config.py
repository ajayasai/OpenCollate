"""Strict TOML project configuration for OpenCollate."""

from __future__ import annotations

import glob
import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from opencollate.catalog import RULES
from opencollate.diagnostics import Severity
from opencollate.model import DesignContract, PortRole, ViewId


class ConfigError(ValueError):
    """A user-actionable configuration problem with a stable rule code."""

    def __init__(self, message: str, *, code: str = "OC1001") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SourceConfig:
    view: ViewId
    files: tuple[Path, ...]
    include_dirs: tuple[Path, ...] = ()
    defines: Mapping[str, str | None] = field(default_factory=dict)
    profile: str | None = None
    columns: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)

    def expand_files(self, *, require_matches: bool = True) -> tuple[Path, ...]:
        """Expand configured patterns deterministically.

        Expansion is intentionally delayed until a parser is invoked, which
        keeps ``load_config`` useful to editors before generated inputs exist.
        """

        expanded: list[Path] = []
        for pattern in self.files:
            text = str(pattern)
            has_magic = glob.has_magic(text)
            matches = [Path(item).resolve() for item in glob.glob(text, recursive=True)]
            matches = [item for item in matches if item.is_file()]
            if has_magic and not matches and require_matches:
                raise ConfigError(
                    f"{self.view}: source pattern matched no files: {pattern}",
                    code="OC1003",
                )
            if not has_magic and not matches:
                if require_matches:
                    raise ConfigError(
                        f"{self.view}: source file does not exist: {pattern}",
                        code="OC1002",
                    )
                matches = [pattern]
            expanded.extend(matches)
        unique = {str(item): item for item in expanded}
        return tuple(
            sorted(unique.values(), key=lambda item: (item.as_posix().casefold(), item.as_posix()))
        )


@dataclass(frozen=True, slots=True)
class ContractSettings:
    baseline: ViewId | None = None
    file: Path | None = None
    authority: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicySettings:
    strict_inventory: bool = False
    rtl_power_pins: str = "optional"
    scalar_vector_equivalent: bool = False
    max_boolean_inputs: int = 12
    compare_functions: bool = True
    deny_warnings: bool = False
    report_unmatched_waivers: bool = True
    allow_multi_bond: bool = False
    severity_overrides: Mapping[str, Severity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        boolean_fields = (
            "strict_inventory",
            "scalar_vector_equivalent",
            "compare_functions",
            "deny_warnings",
            "report_unmatched_waivers",
            "allow_multi_bond",
        )
        for field_name in boolean_fields:
            if type(getattr(self, field_name)) is not bool:
                raise ConfigError(f"policy.{field_name} must be a boolean")
        if self.rtl_power_pins not in {"optional", "required", "ignore"}:
            raise ConfigError("policy.rtl_power_pins must be optional, required, or ignore")
        if type(self.max_boolean_inputs) is not int:
            raise ConfigError("policy.max_boolean_inputs must be an integer")
        if not 1 <= self.max_boolean_inputs <= 24:
            raise ConfigError("policy.max_boolean_inputs must be between 1 and 24")


@dataclass(frozen=True, slots=True)
class AliasRule:
    kind: str
    canonical: str
    view: str
    native: str
    component: str | None = None

    def __post_init__(self) -> None:
        kind = self.kind.strip().lower()
        if kind not in {"component", "port"}:
            raise ConfigError(f"alias kind must be 'component' or 'port', got {self.kind!r}")
        if not self.canonical or not self.native or not self.view:
            raise ConfigError("alias canonical, native, and view fields must not be empty")
        if kind == "port" and not self.component:
            raise ConfigError("a port alias requires its canonical component name")
        object.__setattr__(self, "kind", kind)


@dataclass(frozen=True, slots=True)
class ParticipationRule:
    component: str
    views: tuple[str, ...]
    optional_views: tuple[str, ...] = ()
    roles: tuple[PortRole, ...] = ()

    def __post_init__(self) -> None:
        if not self.component:
            raise ConfigError("participation component pattern must not be empty")
        if not self.views:
            raise ConfigError(f"participation rule {self.component!r} must name at least one view")
        if any(role == PortRole.UNKNOWN for role in self.roles):
            raise ConfigError("unknown participation role")


@dataclass(frozen=True, slots=True)
class Waiver:
    code: str
    reason: str
    object_pattern: str = "*"
    views: tuple[str, ...] = ()
    property_pattern: str | None = None
    fingerprint: str | None = None
    expires: date | None = None

    def __post_init__(self) -> None:
        code = self.code.strip().upper()
        if not code.startswith("OC"):
            raise ConfigError(f"invalid waiver code selector: {self.code!r}")
        has_pattern = any(character in code for character in "*?[")
        if not has_pattern and code not in RULES:
            raise ConfigError(f"unknown waiver code: {code}")
        if not has_pattern and RULES[code].default_severity == Severity.FATAL:
            raise ConfigError(f"fatal rule {code} cannot be waived")
        if not self.reason.strip():
            raise ConfigError(f"waiver {code} requires a nonempty reason")
        object.__setattr__(self, "code", code)


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    path: Path
    root: Path
    name: str
    sources: tuple[SourceConfig, ...]
    contract: ContractSettings = field(default_factory=ContractSettings)
    policy: PolicySettings = field(default_factory=PolicySettings)
    aliases: tuple[AliasRule, ...] = ()
    participation: tuple[ParticipationRule, ...] = ()
    waivers: tuple[Waiver, ...] = ()
    schema_version: int = 1

    @property
    def views(self) -> tuple[ViewId, ...]:
        return tuple(sorted(source.view for source in self.sources))

    def source(self, view: str | ViewId) -> SourceConfig:
        wanted = ViewId.parse(view)
        for source in self.sources:
            if source.view == wanted:
                return source
        raise KeyError(str(wanted))

    def load_contract(self) -> DesignContract | None:
        if self.contract.file is None:
            return None
        return load_contract(self.contract.file)


def _as_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a TOML table")
    return value


def _as_string_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where} must be a string or an array of strings")
    return tuple(value)


def _as_bool(value: Any, where: str, *, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise ConfigError(f"{where} must be a boolean")
    return value


def _as_int(value: Any, where: str, *, default: int) -> int:
    if value is None:
        return default
    if type(value) is not int:
        raise ConfigError(f"{where} must be an integer")
    return value


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _parse_defines(value: Any, where: str) -> dict[str, str | None]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        result: dict[str, str | None] = {}
        for key, item in value.items():
            if item is not None and not isinstance(item, (str, int, float, bool)):
                raise ConfigError(f"{where}.{key} must be a scalar")
            result[str(key)] = None if item is None else str(item)
        return result
    result = {}
    for item in _as_string_list(value, where):
        name, separator, raw_value = item.partition("=")
        if not name.strip():
            raise ConfigError(f"{where} contains an empty define")
        result[name.strip()] = raw_value if separator else None
    return result


def _looks_like_source_table(table: Mapping[str, Any]) -> bool:
    return "files" in table or "path" in table


def _parse_source(
    kind: str,
    name: str,
    raw: Mapping[str, Any],
    root: Path,
) -> SourceConfig:
    raw_files = raw.get("files", raw.get("path"))
    files = _as_string_list(raw_files, f"sources.{kind}.{name}.files")
    if not files:
        raise ConfigError(f"sources.{kind}.{name} requires files or path")
    include_dirs = _as_string_list(raw.get("include_dirs"), f"sources.{kind}.{name}.include_dirs")
    raw_columns = raw.get("columns", {})
    columns = {
        str(key): str(value)
        for key, value in _as_mapping(raw_columns, f"sources.{kind}.{name}.columns").items()
    }
    known = {"files", "path", "include_dirs", "defines", "profile", "columns"}
    options = {str(key): value for key, value in raw.items() if key not in known}
    return SourceConfig(
        view=ViewId(kind, name),
        files=tuple(_resolve_path(root, item) for item in files),
        include_dirs=tuple(_resolve_path(root, item) for item in include_dirs),
        defines=_parse_defines(raw.get("defines"), f"sources.{kind}.{name}.defines"),
        profile=str(raw["profile"]) if "profile" in raw else None,
        columns=columns,
        options=options,
    )


def _parse_sources(raw: Any, root: Path) -> tuple[SourceConfig, ...]:
    sources_table = _as_mapping(raw, "sources")
    parsed: list[SourceConfig] = []
    for raw_kind, raw_kind_table in sources_table.items():
        kind = str(raw_kind).strip().lower()
        kind_table = _as_mapping(raw_kind_table, f"sources.{kind}")
        if _looks_like_source_table(kind_table):
            parsed.append(_parse_source(kind, "default", kind_table, root))
            continue
        for raw_name, raw_view_table in kind_table.items():
            name = str(raw_name)
            parsed.append(
                _parse_source(
                    kind,
                    name,
                    _as_mapping(raw_view_table, f"sources.{kind}.{name}"),
                    root,
                )
            )
    views = [source.view for source in parsed]
    if not parsed:
        raise ConfigError("sources must contain at least one configured view")
    if len(views) != len(set(views)):
        raise ConfigError("source view IDs must be unique")
    return tuple(sorted(parsed, key=lambda item: item.view))


def _parse_alias_entry(kind: str, raw: Mapping[str, Any]) -> list[AliasRule]:
    canonical = str(raw.get("canonical", "")).strip()
    component = str(raw.get("component", "")).strip() or None
    if "names" in raw:
        names = _as_mapping(raw["names"], f"aliases.{kind}.names")
        return [
            AliasRule(kind, canonical, str(view), str(native), component)
            for view, native in names.items()
        ]
    return [
        AliasRule(
            kind=kind,
            canonical=canonical,
            view=str(raw.get("view", "")),
            native=str(raw.get("native", "")),
            component=component,
        )
    ]


def _parse_aliases(data: Mapping[str, Any]) -> tuple[AliasRule, ...]:
    parsed: list[AliasRule] = []
    if "aliases" in data:
        aliases = data["aliases"]
        aliases_table = _as_mapping(aliases, "aliases")
        for plural, kind in (("components", "component"), ("ports", "port")):
            entries = aliases_table.get(plural, [])
            if not isinstance(entries, list):
                raise ConfigError(f"aliases.{plural} must be an array of tables")
            for index, entry in enumerate(entries):
                parsed.extend(
                    _parse_alias_entry(kind, _as_mapping(entry, f"aliases.{plural}[{index}]"))
                )
    if "alias" in data:
        shorthand = data["alias"]
        if not isinstance(shorthand, list):
            raise ConfigError("alias must be an array of tables")
        for index, entry in enumerate(shorthand):
            raw = _as_mapping(entry, f"alias[{index}]")
            parsed.extend(_parse_alias_entry(str(raw.get("kind", "")), raw))

    by_native: dict[tuple[str, str | None, str, str], str] = {}
    by_canonical: dict[tuple[str, str | None, str, str], str] = {}
    for rule in parsed:
        native_key = (rule.kind, rule.component, rule.view.lower(), rule.native)
        previous = by_native.setdefault(native_key, rule.canonical)
        if previous != rule.canonical:
            raise ConfigError(
                f"alias collision: {rule.view}:{rule.native!r} maps to both "
                f"{previous!r} and {rule.canonical!r}"
            )
        canonical_key = (rule.kind, rule.component, rule.view.lower(), rule.canonical)
        previous_native = by_canonical.setdefault(canonical_key, rule.native)
        if previous_native != rule.native:
            raise ConfigError(
                f"alias collision: {rule.view}:{rule.canonical!r} names both "
                f"{previous_native!r} and {rule.native!r}"
            )
    return tuple(
        sorted(
            set(parsed),
            key=lambda item: (
                item.kind,
                item.component or "",
                item.canonical,
                item.view,
                item.native,
            ),
        )
    )


def _parse_participation(value: Any) -> tuple[ParticipationRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("participation must be an array of tables")
    parsed: list[ParticipationRule] = []
    for index, item in enumerate(value):
        raw = _as_mapping(item, f"participation[{index}]")
        role_names = _as_string_list(raw.get("roles"), f"participation[{index}].roles")
        roles = tuple(PortRole.parse(role) for role in role_names)
        unknown_roles = [
            role_name
            for role_name, role in zip(role_names, roles, strict=True)
            if role == PortRole.UNKNOWN
        ]
        if unknown_roles:
            raise ConfigError("unknown participation role: " + ", ".join(unknown_roles))
        parsed.append(
            ParticipationRule(
                component=str(raw.get("component", "")),
                views=_as_string_list(raw.get("views"), f"participation[{index}].views"),
                optional_views=_as_string_list(
                    raw.get("optional_views"),
                    f"participation[{index}].optional_views",
                ),
                roles=roles,
            )
        )
    return tuple(parsed)


def _parse_date(value: Any, where: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{where} must be an ISO date (YYYY-MM-DD)") from exc


def _parse_waivers(value: Any) -> tuple[Waiver, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("waivers must be an array of tables")
    parsed: list[Waiver] = []
    for index, item in enumerate(value):
        raw = _as_mapping(item, f"waivers[{index}]")
        parsed.append(
            Waiver(
                code=str(raw.get("code", "")),
                reason=str(raw.get("reason", "")),
                object_pattern=str(raw.get("object", "*")),
                views=_as_string_list(raw.get("views"), f"waivers[{index}].views"),
                property_pattern=(str(raw["property"]) if "property" in raw else None),
                fingerprint=(str(raw["fingerprint"]) if "fingerprint" in raw else None),
                expires=_parse_date(raw.get("expires"), f"waivers[{index}].expires"),
            )
        )
    return tuple(parsed)


def _parse_policy(raw: Any) -> PolicySettings:
    table = _as_mapping(raw or {}, "policy")
    rtl_power_pins = str(table.get("rtl_power_pins", "optional")).lower()
    if rtl_power_pins not in {"optional", "required", "ignore"}:
        raise ConfigError("policy.rtl_power_pins must be optional, required, or ignore")
    maximum = _as_int(
        table.get("max_boolean_inputs"),
        "policy.max_boolean_inputs",
        default=12,
    )
    if maximum < 1 or maximum > 24:
        raise ConfigError("policy.max_boolean_inputs must be between 1 and 24")
    raw_overrides = _as_mapping(table.get("severity_overrides", {}), "policy.severity_overrides")
    overrides: dict[str, Severity] = {}
    for raw_code, raw_severity in raw_overrides.items():
        code = str(raw_code).upper()
        if code not in RULES:
            raise ConfigError(f"unknown rule in severity override: {code}")
        try:
            parsed_severity = Severity.parse(str(raw_severity))
        except ValueError as exc:
            raise ConfigError(
                f"invalid severity for policy.severity_overrides.{code}: {raw_severity!r}"
            ) from exc
        if RULES[code].default_severity == Severity.FATAL and parsed_severity != Severity.FATAL:
            raise ConfigError(f"fatal rule {code} cannot be downgraded")
        overrides[code] = parsed_severity
    return PolicySettings(
        strict_inventory=_as_bool(
            table.get("strict_inventory"), "policy.strict_inventory", default=False
        ),
        rtl_power_pins=rtl_power_pins,
        scalar_vector_equivalent=_as_bool(
            table.get("scalar_vector_equivalent"),
            "policy.scalar_vector_equivalent",
            default=False,
        ),
        max_boolean_inputs=maximum,
        compare_functions=_as_bool(
            table.get("compare_functions"), "policy.compare_functions", default=True
        ),
        deny_warnings=_as_bool(table.get("deny_warnings"), "policy.deny_warnings", default=False),
        report_unmatched_waivers=_as_bool(
            table.get("report_unmatched_waivers"),
            "policy.report_unmatched_waivers",
            default=True,
        ),
        allow_multi_bond=_as_bool(
            table.get("allow_multi_bond"),
            "policy.allow_multi_bond",
            default=False,
        ),
        severity_overrides=overrides,
    )


def load_config(path: str | Path = "opencollate.toml") -> ProjectConfig:
    manifest = Path(path).expanduser().resolve()
    if not manifest.is_file():
        raise ConfigError(f"configuration file does not exist: {manifest}", code="OC1002")
    try:
        with manifest.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid TOML in {manifest}: {exc}") from exc

    schema_version = _as_int(data.get("schema_version"), "schema_version", default=1)
    if schema_version != 1:
        raise ConfigError(f"unsupported schema_version {schema_version}; this release supports 1")
    project = _as_mapping(data.get("project", {}), "project")
    raw_name = project.get("name", manifest.parent.name)
    if not isinstance(raw_name, str):
        raise ConfigError("project.name must be a string")
    name = raw_name.strip()
    if not name:
        raise ConfigError("project.name must not be empty")
    raw_root = project.get("root", ".")
    if not isinstance(raw_root, str):
        raise ConfigError("project.root must be a string path")
    root = _resolve_path(manifest.parent, raw_root)
    sources = _parse_sources(data.get("sources", {}), root)

    raw_contract = _as_mapping(data.get("contract", {}), "contract")
    baseline = None
    if "baseline" in raw_contract:
        raw_baseline = raw_contract["baseline"]
        if not isinstance(raw_baseline, str) or not raw_baseline.strip():
            raise ConfigError("contract.baseline must be a nonempty view name")
        try:
            baseline = ViewId.parse(raw_baseline)
        except ValueError as exc:
            raise ConfigError(f"invalid contract.baseline: {raw_baseline!r}") from exc
    if baseline is not None and sources and baseline not in {item.view for item in sources}:
        raise ConfigError(f"contract.baseline names an unknown source view: {baseline}")
    contract_file = (
        _resolve_path(root, str(raw_contract["file"])) if "file" in raw_contract else None
    )
    authority = {
        str(key): str(value)
        for key, value in _as_mapping(
            raw_contract.get("authority", {}), "contract.authority"
        ).items()
    }

    return ProjectConfig(
        path=manifest,
        root=root,
        name=name,
        sources=sources,
        contract=ContractSettings(baseline, contract_file, authority),
        policy=_parse_policy(data.get("policy")),
        aliases=_parse_aliases(data),
        participation=_parse_participation(data.get("participation")),
        waivers=_parse_waivers(data.get("waivers")),
        schema_version=schema_version,
    )


def load_contract(path: str | Path) -> DesignContract:
    contract_path = Path(path).expanduser().resolve()
    if not contract_path.is_file():
        raise ConfigError(f"contract file does not exist: {contract_path}", code="OC1002")
    try:
        data = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid contract JSON in {contract_path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ConfigError("contract root must be a JSON object")
    try:
        return DesignContract.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid contract schema in {contract_path}: {exc}") from exc


__all__ = [
    "AliasRule",
    "ConfigError",
    "ContractSettings",
    "ParticipationRule",
    "PolicySettings",
    "ProjectConfig",
    "SourceConfig",
    "Waiver",
    "load_config",
    "load_contract",
]

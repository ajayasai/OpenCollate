"""Apply the platform-foundation patch to files too large for atomic API editing.

The script is intentionally assertion-heavy. It refuses to write a partial upgrade
when its expected source context no longer matches the target branch.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one replacement target, found {count}")
    target.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


replace_once(
    "src/opencollate/engine.py",
    '''    decoded_identifier,
)

_VIEW_KIND_ALIASES = {
''',
    '''    decoded_identifier,
)
from opencollate.plugins import CheckerContext, run_checker_plugins

_VIEW_KIND_ALIASES = {
''',
)

replace_once(
    "src/opencollate/engine.py",
    '''        diagnostics.extend(self._check_registers(observed, contract))
        diagnostics.extend(self._check_connectivity(observed))
        diagnostics = self._apply_severity_overrides(diagnostics)
        diagnostics = self._apply_waivers(diagnostics, today=today or date.today())
        ordered = sort_diagnostics(diagnostics)
        generated = self.build_contract(reconciliation.design, observed)
        return EngineResult(
''',
    '''        diagnostics.extend(self._check_registers(observed, contract))
        diagnostics.extend(self._check_connectivity(observed))
        analysis_date = today or date.today()
        generated = self.build_contract(reconciliation.design, observed)
        diagnostics.extend(
            run_checker_plugins(
                CheckerContext(
                    config=self.config,
                    observations=observed,
                    design=reconciliation.design,
                    contract=contract,
                    generated_contract=generated,
                    today=analysis_date,
                )
            )
        )
        diagnostics = self._apply_severity_overrides(diagnostics)
        diagnostics = self._apply_waivers(diagnostics, today=analysis_date)
        ordered = sort_diagnostics(diagnostics)
        return EngineResult(
''',
)

replace_once(
    "src/opencollate/cli.py",
    '''from opencollate.model import ViewObservation
from opencollate.reporters import (
''',
    '''from opencollate.model import ViewObservation
from opencollate.parsers import parser_inventory
from opencollate.plugins import plugin_inventory
from opencollate.reporters import (
''',
)

replace_once(
    "src/opencollate/cli.py",
    '''    generic_fields = (
        frozenset(("include_dirs", "defines"))
        if kind in {"rtl", "sv", "systemverilog", "verilog"}
        else frozenset(("defines",))
        if kind in {"systemrdl", "system_rdl", "system-rdl", "rdl"}
        else frozenset(("profile", "columns"))
        if kind in {"csv", "pinmap", "pin_map"}
        else frozenset(("columns",))
        if kind in {"connectivity", "connectivity_spec", "connectivity-spec", "conn"}
        else frozenset()
    )
    _reject_inapplicable_source_fields(source, allowed=generic_fields)
''',
    '''    from opencollate.parsers import UnsupportedFormatError, get_registration

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
''',
)

replace_once(
    "src/opencollate/cli.py",
    '''    raise CliError(
        f"no parser is registered for source view {source.view}",
        code="OC1001",
    )


def _reject_unknown_source_options(
''',
    '''    from opencollate.parsers import parse as parse_collateral

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
''',
)

replace_once(
    "src/opencollate/cli.py",
    '''        "rules": len(list(iter_rules())),
    }
''',
    '''        "rules": len(list(iter_rules())),
        "parser_registry": parser_inventory(),
        "plugins": plugin_inventory(),
    }
''',
)

replace_once(
    "src/opencollate/cli.py",
    r'''    print(f"\nBuilt-in rules: {data['rules']}")
    print("Outputs: " + ", ".join(data["outputs"]))
    return 0
''',
    r'''    registry = data["parser_registry"]
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
''',
)

replace_once(
    "README.md",
    '''The frozen contract currently persists components, ports, and register maps. Clocks, interfaces,
hierarchical objects, constraints, and mappings remain first-class run observations but are not
all frozen-contract fields in schema version 1. Read the [architecture](docs/architecture.md),
[canonical contract](docs/canonical-contract.md), and [diagnostic model](docs/diagnostics.md).

## Security and privacy
''',
    '''The frozen contract currently persists components, ports, and register maps. Clocks, interfaces,
hierarchical objects, constraints, and mappings remain first-class run observations but are not
all frozen-contract fields in schema version 1. Read the [architecture](docs/architecture.md),
[canonical contract](docs/canonical-contract.md), and [diagnostic model](docs/diagnostics.md).

## Versioned extension platform

Installed packages can add collateral parsers through the `opencollate.parsers` entry-point group
and semantic checks through `opencollate.checkers`. Registrations declare extension API version 1,
provider/version provenance, aliases, and filename suffixes. They cannot silently shadow built-in
formats. Parser crashes become fatal, whole-view-tainted `OC9001` observations; checker discovery
or execution crashes become fatal `OC9002` diagnostics.

```console
opencollate capabilities --json  # exact built-in/plugin ownership and failures
```

See the [extension API](docs/extension-api.md) for packaging, runtime registration, compatibility,
configuration forwarding, deterministic conflict handling, and the plugin trust boundary. Set
`OPENCOLLATE_DISABLE_PLUGINS=1` when a hermetic run must ignore installed entry points.

## Security and privacy
''',
)

replace_once(
    "CHANGELOG.md",
    '''## [Unreleased]

## [0.3.0] - 2026-08-31
''',
    '''## [Unreleased]

### Added

- A versioned extension API for independently distributed parser and semantic-checker plugins,
  including Python entry-point discovery, runtime registration for embedding, provider/version
  provenance, deterministic capability inventory, and exact compatibility rejection.
- Generic configured-source dispatch for external collateral formats, including forwarding of
  parser-specific options and the standard include/define/profile/column fields.

### Security

- External parsers cannot shadow built-in formats, aliases, or filename extensions. Parser plugin
  exceptions become fatal whole-view-tainted `OC9001` observations, and checker discovery,
  contract, or execution failures become fatal `OC9002` diagnostics.
- Package plugin discovery can be disabled for hermetic execution with
  `OPENCOLLATE_DISABLE_PLUGINS=1`.

## [0.3.0] - 2026-08-31
''',
)

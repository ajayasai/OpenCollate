"""Restore mainline docs, then apply only focused contract-v2 edits."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one documentation anchor, found {count}")
    target.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


replace_once(
    "README.md",
    '''opencollate contract build [CONFIG] --output contract.oc.json
''',
    '''opencollate contract build [CONFIG] --output contract.oc.json
opencollate contract migrate LEGACY --output contract.v2.oc.json
''',
)

replace_once(
    "README.md",
    '''The frozen contract currently persists components, ports, and register maps. Clocks, interfaces,
hierarchical objects, constraints, and mappings remain first-class run observations but are not
all frozen-contract fields in schema version 1. Read the [architecture](docs/architecture.md),
[canonical contract](docs/canonical-contract.md), and [diagnostic model](docs/diagnostics.md).
''',
    '''Newly generated schema-version-2 contracts persist canonical components, ports, and registers plus
integrity-checked snapshots of every parser-neutral observation family: clocks, interfaces,
hierarchy and references, package mappings, constraint/power metadata, registers, connectivity,
view attributes, completeness, and tainted scopes. Version-1 contracts remain readable and can be
migrated without inventing facts they never stored. Read the [architecture](docs/architecture.md),
[canonical contract](docs/canonical-contract.md), and [diagnostic model](docs/diagnostics.md).
''',
)

replace_once(
    "README.md",
    '''OpenCollate has no telemetry, account, upload, or network-reporting feature. Reports still contain
design names, paths, connectivity, and expressions and must be protected like source collateral.
''',
    '''OpenCollate has no telemetry, account, upload, or network-reporting feature. Reports and contracts
still contain design names, paths, connectivity, expressions, and normalized parser metadata and
must be protected like source collateral.
''',
)

replace_once(
    "CHANGELOG.md",
    '''### Added

- A versioned extension API for independently distributed parser and semantic-checker plugins,
''',
    '''### Added

- Canonical contract schema version 2, with deterministic per-view snapshots covering components,
  package mappings, hierarchical objects, clocks, interfaces, registers, static connectivity, view
  attributes, completeness, and tainted scopes. Each snapshot is protected by a verified SHA-256
  content digest and contracts have a bounded, JSON-safe extension namespace.
- `opencollate contract migrate` upgrades schema-version-1 contracts without inventing observation
  families that legacy files did not persist. Version-1 contracts remain readable.
- A versioned extension API for independently distributed parser and semantic-checker plugins,
''',
)

replace_once(
    "CHANGELOG.md",
    '''- Generic configured-source dispatch for external collateral formats, including forwarding of
  parser-specific options and the standard include/define/profile/column fields.

### Security
''',
    '''- Generic configured-source dispatch for external collateral formats, including forwarding of
  parser-specific options and the standard include/define/profile/column fields.

### Changed

- Newly generated contracts use schema version 2. Semantic checker plugins can inspect durable
  frozen view snapshots through `CheckerContext.contract.views` rather than requiring source
  collateral to be reparsed.

### Security

- Contract loading recomputes every view-snapshot digest and rejects stale or modified content.
  Snapshot attributes and extension values reject non-finite numbers, non-string object keys,
  unsupported values, and excessive nesting.
''',
)

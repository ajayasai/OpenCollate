"""Apply exact final repairs to the semantic mutation benchmark."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "benchmarks/mutations.py"
text = SOURCE.read_text(encoding="utf-8")
replacements = (
    (
        '''ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from opencollate import __version__
''',
        '''from opencollate import __version__
''',
    ),
    (
        '''    ViewObservation,
)

SCHEMA_VERSION = 1
''',
        '''    ViewObservation,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
''',
    ),
    (
        '''            shape=BusShape(
                packed=(IndexRange(0, 7),) if mutated else (IndexRange(7, 0),)
            ),
''',
        '''            shape=BusShape(packed=(IndexRange(0, 7),) if mutated else (IndexRange(7, 0),)),
''',
    ),
    (
        '''    source, middle, sink = _endpoint("top/a"), _endpoint("top/n", line=2), _endpoint(
        "top/y", line=3
    )
''',
        '''    source, middle, sink = (
        _endpoint("top/a"),
        _endpoint("top/n", line=2),
        _endpoint("top/y", line=3),
    )
''',
    ),
    (
        '''    return tuple(
        item
        for item in result.diagnostics
        if not item.waived and item.severity != Severity.INFO
    )
''',
        '''    return tuple(
        item for item in result.diagnostics if not item.waived and item.severity != Severity.INFO
    )
''',
    ),
    (
        '''    summary["families"] = {
        family: _metrics(items) for family, items in sorted(by_family.items())
    }
''',
        '''    summary["families"] = {family: _metrics(items) for family, items in sorted(by_family.items())}
''',
    ),
    (
        '''        ("OC2004",),
        "Add a second same-name component definition with a different interface.",
''',
        '''        ("OC2004", "OC4001"),
        "Add a second same-name component definition with a contradictory pin direction.",
''',
    ),
)
for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"mutation source repair expected one target, found {count}")
    text = text.replace(old, new)
SOURCE.write_text(text, encoding="utf-8", newline="\n")

SCHEMA = ROOT / "benchmarks/mutation-results.schema.json"
schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
metrics = schema["$defs"]["metrics"]
if "families" in metrics["properties"]:
    raise RuntimeError("mutation metrics schema repair has already been applied")
metrics["properties"]["families"] = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": {"$ref": "#/$defs/metrics"},
}
SCHEMA.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8", newline="\n")

CHANGELOG = ROOT / "CHANGELOG.md"
changelog = CHANGELOG.read_text(encoding="utf-8")
old = '''### Added

- Canonical contract schema version 2, with deterministic per-view snapshots covering components,
'''
new = '''### Added

- An oracle-backed semantic mutation benchmark with 34 paired mutants and clean controls across
  inventory, interfaces, Boolean logic, package mappings, SDC, UPF, registers, DEF hierarchy, and
  static connectivity. CI publishes exact recall, clean-control specificity, false-negative,
  false-positive, inconclusive, overtrigger, and observation-order determinism metrics.
- Canonical contract schema version 2, with deterministic per-view snapshots covering components,
'''
if changelog.count(old) != 1:
    raise RuntimeError("changelog mutation benchmark insertion expected exactly one target")
CHANGELOG.write_text(changelog.replace(old, new), encoding="utf-8", newline="\n")

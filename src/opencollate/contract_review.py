"""Inventory-aware review of every frozen contract observation family.

Repeated identities retain multiset semantics: duplicates are never silently
collapsed and ambiguous unmatched groups are not paired by guesswork.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from opencollate.model import DesignContract

_FAMILIES = {
    "components": ("name",),
    "pin_mappings": ("component", "die_pad", "package_ball"),
    "objects": ("kind", "qualified_name", "relation"),
    "clocks": ("name",),
    "interfaces": ("component", "name"),
    "registers": ("component", "memory_map", "address_block", "name"),
    "connectivity_endpoints": ("key",),
    "connectivity_edges": ("source", "sink", "kind"),
    "connectivity_requirements": ("id",),
}
MAX_CHANGES = 10_000


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _groups(rows: Sequence[Mapping[str, Any]], keys: tuple[str, ...]) -> dict[str, Counter[str]]:
    result: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        identity = []
        for key in keys:
            item = row.get(key)
            if key in {"source", "sink"} and isinstance(item, Mapping):
                item = item.get("key", item)
            identity.append(item)
        result[_json(identity)][_json(row)] += 1
    return result


def diff_contracts(baseline: DesignContract, current: DesignContract) -> dict[str, Any]:
    """Compare canonical values and all snapshots, preserving fact-state drift.

    Schema-v1 and empty-snapshot contracts have incomplete snapshot coverage;
    an unchanged canonical subset is never mislabeled a complete comparison.
    In-memory snapshots are revalidated, including their content digests.
    """

    old = DesignContract.from_dict(baseline.to_dict()).to_dict()
    new = DesignContract.from_dict(current.to_dict()).to_dict()
    changes: list[dict[str, Any]] = []

    def emit(scope: str, family: str, identity: Any, before: Any, after: Any) -> None:
        if len(changes) >= MAX_CHANGES:
            raise ValueError(
                f"contract diff exceeds {MAX_CHANGES} changes; no partial pass is emitted"
            )
        changes.append(
            {
                "scope": scope,
                "family": family,
                "identity": identity,
                "state": "added" if before is None else "removed" if after is None else "changed",
                "before": before,
                "after": after,
            }
        )

    def compare(
        scope: str,
        family: str,
        before: Sequence[Mapping[str, Any]],
        after: Sequence[Mapping[str, Any]],
        keys: tuple[str, ...],
    ) -> None:
        left, right = _groups(before, keys), _groups(after, keys)
        for identity in sorted(set(left) | set(right)):
            a, b = left.get(identity, Counter()), right.get(identity, Counter())
            removed = [json.loads(item) for item in sorted((a - b).elements())]
            added = [json.loads(item) for item in sorted((b - a).elements())]
            if removed or added:
                # Whole unmatched groups are retained, not arbitrarily paired.
                emit(scope, family, json.loads(identity), removed or None, added or None)

    for family, keys in (
        ("components", ("canonical_name",)),
        ("registers", ("component", "memory_map", "address_block", "canonical_name")),
    ):
        compare("canonical", family, old.get(family, []), new.get(family, []), keys)
    for field in ("schema_version", "generated_by", "extensions"):
        if old.get(field) != new.get(field):
            emit("contract", field, [], old.get(field), new.get(field))
    left_views = {item["view"]: item for item in old.get("views", [])}
    right_views = {item["view"]: item for item in new.get("views", [])}
    for view in sorted(set(left_views) | set(right_views)):
        a, b = left_views.get(view), right_views.get(view)
        if a is None or b is None:
            emit(view, "view", [view], a, b)
            continue
        for family, family_keys in _FAMILIES.items():
            compare(view, family, a[family], b[family], family_keys)
        for field in ("snapshot_version", "complete", "tainted_scopes", "attributes", "extensions"):
            if a[field] != b[field]:
                emit(view, field, [], a[field], b[field])
    incomplete = (
        old["schema_version"] < 2
        or new["schema_version"] < 2
        or not left_views
        or not right_views
        or any(
            not item["complete"] or item["tainted_scopes"]
            for item in [*left_views.values(), *right_views.values()]
        )
    )
    counts = Counter(item["state"] for item in changes)
    return {
        "schema_version": 1,
        "kind": "opencollate-contract-diff",
        "baseline_sha256": _hash(old),
        "current_sha256": _hash(new),
        "snapshot_coverage": "incomplete" if incomplete else "complete",
        "status": "incomplete" if incomplete else "changed" if changes else "unchanged",
        "exit_code": 2 if incomplete else 1 if changes else 0,
        "summary": {
            "changes": len(changes),
            **{key: counts[key] for key in ("added", "removed", "changed")},
        },
        "changes": changes,
    }

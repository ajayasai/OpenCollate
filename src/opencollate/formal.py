"""Portable Boolean obligations and re-executable, content-bound receipts.

Receipts are not authenticated certificates: replay recomputes the answers.
Neither input nor receipt is a native solver script or executable Python.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from opencollate.symbolic import SymbolicLimits, check_symbolic_equivalence

SEMANTICS = "two-valued-combinational"
MAX_OBLIGATIONS = 128


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def validate_obligations(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate strict versioned data; unknown fields cannot hide assumptions."""

    if set(value) != {"schema_version", "semantics", "obligations"}:
        raise ValueError("obligations require exactly schema_version, semantics, and obligations")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported obligations schema_version")
    if value["semantics"] != SEMANTICS:
        raise ValueError("only two-valued-combinational semantics are supported")
    rows = value["obligations"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_OBLIGATIONS:
        raise ValueError(f"obligations must contain between 1 and {MAX_OBLIGATIONS} entries")
    normalized = []
    identifiers: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) - {"id", "left", "right", "assume"}:
            raise ValueError("obligation fields must be id, left, right, and optional assume")
        for key in ("id", "left", "right"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"obligation {key} must be a nonempty string")
        if len(row["id"]) > 256 or row["id"] in identifiers:
            raise ValueError("obligation ids must be unique and at most 256 characters")
        identifiers.add(row["id"])
        assume = row.get("assume", "1")
        if not isinstance(assume, str) or not assume.strip():
            raise ValueError("obligation assume must be a nonempty Boolean expression")
        if any(len(text) > 65_536 for text in (row["left"], row["right"], assume)):
            raise ValueError("obligation expression exceeds the 65536-character limit")
        normalized.append(
            {"id": row["id"], "left": row["left"], "right": row["right"], "assume": assume}
        )
    return {
        "schema_version": 1,
        "semantics": SEMANTICS,
        "obligations": sorted(normalized, key=lambda row: row["id"]),
    }


def run_obligations(
    value: Mapping[str, Any], *, limits: SymbolicLimits | None = None
) -> dict[str, Any]:
    """Run each declared obligation; vacuity or incompleteness takes precedence."""

    request = validate_obligations(value)
    results = []
    for obligation in request["obligations"]:
        result = check_symbolic_equivalence(
            obligation["left"], obligation["right"], assume=obligation["assume"], limits=limits
        )
        results.append({"id": obligation["id"], **result.to_dict()})
    states = {item["status"] for item in results}
    exit_code = 2 if states & {"inconclusive", "vacuous"} else 1 if "different" in states else 0
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "semantics": SEMANTICS,
        "request_sha256": _digest(request),
        "status": ("pass", "fail", "inconclusive")[exit_code],
        "exit_code": exit_code,
        "results": results,
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def replay_receipt(
    request: Mapping[str, Any], receipt: Mapping[str, Any], *, limits: SymbolicLimits | None = None
) -> dict[str, Any]:
    """Validate the binding, then re-solve; never trust an imported status flag.

    Stable semantic outputs must agree. Backend version and resource counters
    may differ after upgrades, so the returned receipt contains fresh metadata.
    An inconclusive or vacuous result never produces exit status zero.
    """

    normalized = validate_obligations(request)
    if set(receipt) != {
        "schema_version",
        "semantics",
        "request_sha256",
        "status",
        "exit_code",
        "results",
        "receipt_sha256",
    }:
        raise ValueError("invalid formal receipt fields")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise ValueError("unsupported receipt schema_version")
    if receipt["semantics"] != SEMANTICS or receipt["request_sha256"] != _digest(normalized):
        raise ValueError("receipt belongs to a different obligation request or semantics")
    if receipt["receipt_sha256"] != _digest(
        {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    ):
        raise ValueError("formal receipt content digest mismatch")
    rows = receipt["results"]
    if not isinstance(rows, list) or len(rows) != len(normalized["obligations"]):
        raise ValueError("receipt has missing or additional results")
    if not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("receipt results must be objects")
    if [row.get("id") for row in rows] != [row["id"] for row in normalized["obligations"]]:
        raise ValueError("receipt result identities are incomplete, duplicated, or out of order")
    fresh = run_obligations(normalized, limits=limits)
    if (
        type(receipt["exit_code"]) is not int
        or receipt["exit_code"] != fresh["exit_code"]
        or receipt["status"] != fresh["status"]
    ):
        raise ValueError("receipt outcome disagrees with independently rerun obligations")
    for old, current in zip(rows, fresh["results"], strict=True):
        for key in ("status", "variables", "counterexample", "obligation_sha256", "semantics"):
            if old.get(key) != current[key]:
                raise ValueError(
                    f"receipt {old['id']!r} {key} disagrees with independently rerun obligation"
                )
        witness = old.get("counterexample")
        if witness is not None and (
            not isinstance(witness, Mapping) or any(type(v) is not bool for v in witness.values())
        ):
            raise ValueError("receipt counterexample values must be Boolean")
    return fresh

"""Source-bound synchronous verification, receipts, and conservative CLI status.

A bounded clean result is not a proof. Only covered, successful k-induction
returns zero. Replay reads current RTL and recomputes all results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from opencollate.atomic_output import atomic_write_text
from opencollate.sequential_coi import cone_summary, lift_trace, select_cone
from opencollate.sequential_ir import SequentialError
from opencollate.sequential_rtl import load_circuit
from opencollate.sequential_smt import Budget, verify_property
from opencollate.sequential_spec import SEMANTICS, integer, normalize, read_json, validate_signals

ALGORITHM = "source-bound-hierarchical-k-induction-v2"
RECEIPT_VERSION = 2


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    ).hexdigest()


def implementation_digest() -> str:
    directory = Path(__file__).parent
    return digest(
        {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.glob("sequential*.py"))
        }
    )


def run_request(
    request: dict[str, Any],
    *,
    root: Path,
    timeout_ms: int = 10000,
    resource_limit: int = 1000000,
    cone_reduction: bool = True,
) -> dict[str, Any]:
    if type(cone_reduction) is not bool:
        raise SequentialError("cone_reduction must be a boolean")
    spec = normalize(request)
    integer(timeout_ms, 1, 120000, "timeout_ms")
    integer(resource_limit, 1, 100000000, "resource_limit")
    # Binding depends on exact source bytes, assumptions and lowering, not just formula text.
    c = load_circuit(spec["files"], root=root, top=spec["top"], clock=spec["clock"])
    validate_signals(c, spec)
    binding = {
        "request_sha256": digest(spec),
        "ir_sha256": digest(c.serialized()),
        "implementation_sha256": implementation_digest(),
        "sources": c.sources,
        "frontend": c.frontend,
        "algorithm": ALGORITHM,
    }
    selected = {
        p["id"]: select_cone(c, spec, p) if cone_reduction else c for p in spec["properties"]
    }
    cones = {
        identity: {**cone_summary(model), "ir_sha256": digest(model.serialized())}
        for identity, model in selected.items()
    }
    results: list[dict[str, Any]] = []
    backend = None
    try:
        b = Budget(timeout_ms, resource_limit)
        backend = b.z3.get_version_string()
        for prop in spec["properties"]:
            model = selected[prop["id"]]
            result = verify_property(model, spec, prop, b)
            if cone_reduction and result["status"] == "counterexample":
                result["trace"] = lift_trace(
                    c, model, spec, prop, result["trace"], checkpoint=b.remaining
                )
            results.append(result)
    except Exception as exc:
        # Includes native/backend import failures. User interrupts still propagate.
        for prop in spec["properties"][len(results) :]:
            results.append(
                {
                    "id": prop["id"],
                    "status": "inconclusive",
                    "checked_through": None,
                    "induction_depth": None,
                    "cover_cycle": None,
                    "trace": None,
                    "reason": f"backend failure: {type(exc).__name__}: {exc}",
                }
            )
    statuses = {r["status"] for r in results}
    code = (
        2 if statuses - {"proven", "counterexample"} else 1 if "counterexample" in statuses else 0
    )
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "semantics": SEMANTICS,
        "binding": binding,
        "status": ("proven", "counterexample", "inconclusive")[code],
        "exit_code": code,
        "limits": {
            "timeout_ms": timeout_ms,
            "resource_limit": resource_limit,
            "cone_reduction": cone_reduction,
        },
        "backend": {"name": "z3", "version": backend},
        "model": {
            "signals": len(c.signals) - 1,
            "state_bits": sum(c.signals[n][0] for n in c.next_state),
            "states": sorted(c.next_state),
            "inputs": sorted(c.inputs),
            "ir_nodes": len(c.nodes),
            "locations": c.locations,
            "property_cones": cones,
        },
        "results": results,
    }
    receipt["receipt_sha256"] = digest(receipt)
    return receipt


def replay(
    request: dict[str, Any],
    receipt: dict[str, Any],
    *,
    root: Path,
    timeout_ms: int = 10000,
    resource_limit: int = 1000000,
    cone_reduction: bool | None = None,
) -> dict[str, Any]:
    required = {
        "schema_version",
        "semantics",
        "binding",
        "status",
        "exit_code",
        "limits",
        "backend",
        "model",
        "results",
        "receipt_sha256",
    }
    if (
        set(receipt) != required
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != RECEIPT_VERSION
        or receipt["semantics"] != SEMANTICS
    ):
        raise SequentialError("invalid sequential receipt fields/version/semantics")
    if receipt["receipt_sha256"] != digest(
        {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    ):
        raise SequentialError("sequential receipt content digest mismatch")
    recorded_limits = receipt["limits"]
    if (
        not isinstance(recorded_limits, dict)
        or set(recorded_limits) != {"timeout_ms", "resource_limit", "cone_reduction"}
        or type(recorded_limits["cone_reduction"]) is not bool
    ):
        raise SequentialError("invalid sequential receipt limits")
    integer(recorded_limits["timeout_ms"], 1, 120000, "receipt timeout_ms")
    integer(recorded_limits["resource_limit"], 1, 100000000, "receipt resource_limit")
    mode = recorded_limits["cone_reduction"] if cone_reduction is None else cone_reduction
    fresh = run_request(
        request,
        root=root,
        timeout_ms=timeout_ms,
        resource_limit=resource_limit,
        cone_reduction=mode,
    )
    if digest(fresh["binding"]) != digest(receipt["binding"]):
        raise SequentialError(
            "source, request, frontend, IR, or implementation changed since verification"
        )

    # Reasons for a resource timeout can vary; semantic outcomes must still match.
    def stable(rows: Any) -> Any:
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise SequentialError("receipt results must be an array of objects")
        return [{k: v for k, v in row.items() if k != "reason"} for row in rows]

    if (
        type(receipt["exit_code"]) is not int
        or receipt["exit_code"] != fresh["exit_code"]
        or receipt["status"] != fresh["status"]
        or digest(stable(receipt["results"])) != digest(stable(fresh["results"]))
        or digest(receipt["model"]) != digest(fresh["model"])
    ):
        raise SequentialError("saved result disagrees with source-based reverification")
    return fresh


def add_commands(subparsers: Any) -> None:
    group = subparsers.add_parser(
        "sequential", help="source-bound synchronous RTL BMC and k-induction"
    )
    commands = group.add_subparsers(dest="sequential_command", required=True)
    for command in ("check", "replay"):
        sub = commands.add_parser(command)
        sub.add_argument("request", type=Path)
        if command == "replay":
            sub.add_argument("receipt", type=Path)
        sub.add_argument("--timeout-ms", type=int, default=10000)
        sub.add_argument("--resource-limit", type=int, default=1000000)
        sub.add_argument("-o", "--output", type=Path)
        sub.add_argument(
            "--no-cone-reduction",
            dest="cone_reduction",
            action="store_false",
            default=None,
            help="use the full model as an audit/reference path (replay otherwise follows receipt)",
        )
        sub.set_defaults(handler=command_handler)


def command_handler(args: argparse.Namespace) -> int:
    request_path = args.request.resolve()
    try:
        request = read_json(request_path)
        # Do not let publication overwrite a source, request, or input receipt.
        spec = normalize(request)
        protected = [request_path, *[(request_path.parent / f).resolve() for f in spec["files"]]]
        if args.sequential_command == "replay":
            protected.append(args.receipt.resolve())
        if args.output:
            out = args.output.resolve()
            for p in protected:
                if out == p or (out.exists() and p.exists() and out.samefile(p)):
                    raise SequentialError(
                        "output must not alias a request, source, or receipt input"
                    )
        options = {
            "root": request_path.parent,
            "timeout_ms": args.timeout_ms,
            "resource_limit": args.resource_limit,
        }
        if args.cone_reduction is not None:
            options["cone_reduction"] = args.cone_reduction
        result = (
            replay(request, read_json(args.receipt), **options)
            if args.sequential_command == "replay"
            else run_request(request, **options)
        )
    except Exception as error:
        result = {
            "schema_version": RECEIPT_VERSION,
            "semantics": SEMANTICS,
            "status": "invalid-or-unsupported",
            "exit_code": 2,
            "error": f"{type(error).__name__}: {error}",
        }
        # On input errors never write to the possibly aliased output path.
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    try:
        if args.output:
            atomic_write_text(args.output, text)
        else:
            print(text, end="")
    except OSError as error:
        print(
            json.dumps(
                {
                    "schema_version": RECEIPT_VERSION,
                    "semantics": SEMANTICS,
                    "status": "invalid-or-unsupported",
                    "exit_code": 2,
                    "error": f"artifact publication failed: {error}",
                },
                sort_keys=True,
            )
        )
        return 2
    return int(result["exit_code"])

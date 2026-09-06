"""Paired, source-derived full-versus-cone benchmarks with exact outcome checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from opencollate.sequential_cone import property_cone, verify_cone_property
from opencollate.sequential_rtl import load_circuit
from opencollate.sequential_smt import Budget, verify_property
from opencollate.sequential_spec import normalize, validate_signals


def source(islands: int, bug: bool, dense: bool) -> str:
    updates = []
    for i in range(islands):
        updates.append(
            f"logic [15:0] n{i}; always_ff @(posedge clk) "
            f"if (rst) n{i} <= 0; else n{i} <= n{i} + 1'b1;"
        )
    rhs = "~d" if bug else "d"
    if dense:
        rhs = "d ^ " + " ^ ".join(f"(n{i} ^ n{i})" for i in range(islands))
    return (
        "module pipe(input logic clk, rst, input logic [15:0] d, "
        "output logic [15:0] q);\n"
        + "\n".join(updates)
        + f"\nalways_ff @(posedge clk) if (rst) q <= 0; else q <= {rhs};\nendmodule\n"
    )


def run_case(root: Path, name: str, islands: int, bug: bool, dense: bool, repeat: int) -> dict:
    rtl = source(islands, bug, dense)
    (root / "design.sv").write_text(rtl)
    request = {
        "schema_version": 1,
        "semantics": "two-valued-synchronous",
        "files": ["design.sv"],
        "top": "pipe",
        "clock": "clk",
        "reset": {"signal": "rst", "active": 1},
        "depth": 8,
        "induction": 3,
        "properties": [{"id": "data", "source": "d", "sink": "q", "latency": 1}],
    }
    spec = normalize(request)
    start = time.perf_counter()
    full = load_circuit(spec["files"], root=root, top="pipe", clock="clk")
    validate_signals(full, spec)
    frontend_seconds = time.perf_counter() - start
    prop = spec["properties"][0]
    cone = property_cone(full, spec, prop)
    runs = {"full": [], "cone": []}
    reference = None
    for iteration in range(repeat):
        # Alternate order to avoid always giving one backend a warm-process advantage.
        order = ["full", "cone"] if iteration % 2 == 0 else ["cone", "full"]
        paired = {}
        for mode in order:
            fn = verify_property if mode == "full" else verify_cone_property
            start = time.perf_counter()
            result = fn(full, spec, prop, Budget(120000, 1000000))
            elapsed = time.perf_counter() - start
            if result["status"] not in {"proven", "counterexample"}:
                raise RuntimeError(f"{name}/{mode}: {result}")
            runs[mode].append(elapsed)
            paired[mode] = result
        if paired["full"] != paired["cone"]:
            raise RuntimeError(f"semantic mismatch in {name}: {paired}")
        if reference is not None and paired["full"] != reference:
            raise RuntimeError(f"non-deterministic result in {name}")
        reference = paired["full"]
    expected = "counterexample" if bug else "proven"
    if reference is None or reference["status"] != expected:
        raise RuntimeError(f"unexpected result for {name}: {reference}")
    full_median = statistics.median(runs["full"])
    cone_median = statistics.median(runs["cone"])
    return {
        "name": name,
        "source_sha256": hashlib.sha256(rtl.encode()).hexdigest(),
        "request": spec,
        "frontend_seconds": frontend_seconds,
        "full_state_bits": sum(full.signals[n][0] for n in full.next_state),
        "cone_state_bits": sum(cone.signals[n][0] for n in cone.next_state),
        "full_ir_nodes": len(full.nodes),
        "cone_ir_nodes": len(cone.nodes),
        "full_samples_seconds": runs["full"],
        "cone_samples_seconds": runs["cone"],
        "full_median_seconds": full_median,
        "cone_median_seconds": cone_median,
        "median_solve_speedup": full_median / cone_median,
        "status": reference["status"],
        "checked_through": reference["checked_through"],
        "exact_result_and_full_trace_match": True,
        "result_sha256": hashlib.sha256(
            json.dumps(reference, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json-output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20:
        parser.error("repeat must be between 1 and 20")
    cases = [
        ("proof-small", 1, False, False),
        ("proof-16-islands", 16, False, False),
        ("proof-128-islands", 128, False, False),
        ("bug-64-islands", 64, True, False),
        # Each self-XOR cancels but remains a syntactic dependency: a
        # deliberate no-reduction control without auxiliary invariant proofs.
        ("dense-no-reduction", 16, False, True),
    ]
    with tempfile.TemporaryDirectory() as directory:
        rows = [run_case(Path(directory), *case, args.repeat) for case in cases]
    import z3

    report = {
        "schema_version": 1,
        "benchmark": "source-derived-sequential-cone-v1",
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "z3": z3.get_version_string(),
        },
        "scope": (
            "Synthetic independent-counter circuits. Solver-stage timings include Budget "
            "creation, projection and full-trace replay, but exclude frontend parsing, "
            "receipt hashing and interpreter startup. Not a commercial-tool comparison."
        ),
        "repeat": args.repeat,
        "cases": rows,
    }
    args.json_output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    for row in rows:
        print(
            f"{row['name']}: {row['status']}; state bits "
            f"{row['full_state_bits']} -> {row['cone_state_bits']}; "
            f"solve speedup {row['median_solve_speedup']:.2f}x; exact match"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

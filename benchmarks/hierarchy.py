"""Actual hierarchical RTL: matched full/cone proofs, full traces, and timing tiers."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from opencollate.sequential import digest, run_request

CELLS = """module delay_cell #(parameter W=8, INVERT=0)(input wire clk,rst_n,en,
 input wire [W-1:0] d, output logic [W-1:0] q);
 always_ff @(posedge clk) if (!rst_n) q<=0;
 else if (en) q<=INVERT ? ~d : d;
endmodule
module counter_cell #(parameter W=32)(input wire clk,rst_n, input wire [W-1:0] d,
 output logic [W-1:0] count);
 always_ff @(posedge clk) if(!rst_n) count<=0; else count<=count+d+1'b1;
endmodule
"""


def source(background: int, *, mutant: bool = False) -> str:
    return f"""module top(input wire clk,rst_n,en, input wire [7:0] d, output wire [7:0] q);
 wire [7:0] middle;
 delay_cell #(.INVERT({int(mutant)})) first(clk,rst_n,en,d,middle);
 delay_cell second(clk,rst_n,en,middle,q);
 for(genvar i=0;i<{background};i++) begin: peripherals
   wire [31:0] result;
   counter_cell c(clk,rst_n,{{24'b0,d}},result);
 end
endmodule
"""


def request() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "semantics": "two-valued-synchronous",
        "files": ["top.sv", "cells.sv"],
        "top": "top",
        "clock": "clk",
        "reset": {"signal": "rst_n", "active": 0},
        "depth": 8,
        "induction": 4,
        "properties": [
            {
                "id": "pipeline",
                "source": "d",
                "sink": "q",
                "latency": 2,
                "when": [{"signal": "en", "equals": 1, "lag": lag} for lag in (1, 2)],
            }
        ],
    }


def run_suite(*, tiers: tuple[int, ...] = (0, 16, 64, 128), repeat: int = 3) -> dict[str, Any]:
    if type(repeat) is not int or not 1 <= repeat <= 10:
        raise ValueError("repeat must be an integer in 1..10")
    if not tiers or any(type(n) is not int or not 0 <= n <= 256 for n in tiers):
        raise ValueError("tiers must contain background instance counts in 0..256")
    rows = []
    with tempfile.TemporaryDirectory(prefix="opencollate-hierarchy-") as directory:
        root = Path(directory)
        (root / "cells.sv").write_text(CELLS, encoding="utf-8")
        p = request()
        for count in tiers:
            timings: dict[str, list[float]] = {"full": [], "cone": []}
            receipts = {}
            matches = True
            (root / "top.sv").write_text(source(count), encoding="utf-8")
            # Alternate measurement order to reduce systematic warmup bias.
            for i in range(repeat):
                modes = (False, True) if i % 2 == 0 else (True, False)
                for mode in modes:
                    start = time.perf_counter()
                    result = run_request(
                        p, root=root, cone_reduction=mode, timeout_ms=60000, resource_limit=10000000
                    )
                    timings["cone" if mode else "full"].append(time.perf_counter() - start)
                    prior = receipts.get(mode)
                    matches &= prior is None or prior == result
                    receipts[mode] = result
            a, b = receipts[False], receipts[True]
            matches &= a["results"] == b["results"] and a["binding"] == b["binding"]
            expected = a["exit_code"] == b["exit_code"] == 0
            (root / "top.sv").write_text(source(count, mutant=True), encoding="utf-8")
            mutant_full = run_request(
                p, root=root, cone_reduction=False, timeout_ms=60000, resource_limit=10000000
            )
            mutant_cone = run_request(p, root=root, timeout_ms=60000, resource_limit=10000000)
            mutant_match = mutant_full["results"] == mutant_cone["results"]
            mutant_ok = mutant_full["exit_code"] == mutant_cone["exit_code"] == 1
            medians = {k: statistics.median(v) for k, v in timings.items()}
            rows.append(
                {
                    "background_instances": count,
                    "module_instances": count + 2,
                    "source_bytes": len(CELLS.encode()) + len(source(count).encode()),
                    "full_state_bits": a["model"]["state_bits"],
                    "cone_state_bits": b["model"]["property_cones"]["pipeline"]["state_bits"],
                    "full_ir_nodes": a["model"]["ir_nodes"],
                    "cone_ir_nodes": b["model"]["property_cones"]["pipeline"]["ir_nodes"],
                    "samples_seconds": timings,
                    "median_seconds": medians,
                    "full_over_cone_speedup": medians["full"] / medians["cone"],
                    "exact_proof_results_and_bindings_match": matches,
                    "exact_lifted_counterexample_trace_matches": mutant_match,
                    "proof_status": b["results"][0]["status"],
                    "mutant_status": mutant_cone["results"][0]["status"],
                    "mutant_failure_cycle": mutant_cone["results"][0]["checked_through"],
                    "proof_results_sha256": digest(b["results"]),
                    "mutant_results_sha256": digest(mutant_cone["results"]),
                    "pass": bool(matches and expected and mutant_match and mutant_ok),
                }
            )
    return {
        "schema_version": 1,
        "suite": "source-hierarchy-and-closed-cones-v1",
        "status": "pass" if all(row["pass"] for row in rows) else "fail",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "repeat": repeat,
        "limits": {"timeout_ms": 60000, "resource_limit": 10000000},
        "cases": rows,
        "scope": (
            "Synthetic hierarchical RTL. Timings include frontend, lowering, cone construction "
            "and solving; exclude interpreter startup. Both modes use the new implementation; "
            "not a commercial-tool benchmark or production-SoC qualification."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--tiers", default="0,16,64,128")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = run_suite(tiers=tuple(map(int, args.tiers.split(","))), repeat=args.repeat)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return int(result["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())

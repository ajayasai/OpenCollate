"""Source-file hierarchy and cone-reduction benchmark; not a vendor comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from opencollate.sequential import run_request


def source(distractors: int, *, mutation: bool = False) -> str:
    return f"""module leaf #(parameter W=8)(input logic clk,rst,input logic [W-1:0] d,
    output logic [W-1:0] q);
    always_ff @(posedge clk) if(rst) q<='0; else q<={"~d" if mutation else "d"};
    endmodule
    module ballast(input logic clk,rst);
    logic [31:0] count;
    always_ff @(posedge clk) if(rst) count<=0; else count<=(count+1'b1)^(count<<1);
    endmodule
    module top(input logic clk,rst,input logic [7:0] d,output wire [7:0] q);
    wire [7:0] mid;
    leaf a(clk,rst,d,mid);
    leaf b(clk,rst,mid,q);
    for(genvar i=0;i<{distractors};i++) begin: irrelevant
      ballast u(.clk(clk),.rst(rst));
    end
    endmodule
    """


def run_suite(*, distractors: int = 128, repeat: int = 3) -> dict[str, Any]:
    if (
        type(distractors) is not int
        or not 0 <= distractors <= 256
        or type(repeat) is not int
        or not 1 <= repeat <= 10
    ):
        raise ValueError("distractors must be 0..256, repeat 1..10")
    samples: dict[str, list[float]] = {"full": [], "cone": []}
    rows = {}
    with tempfile.TemporaryDirectory(prefix="opencollate-hierarchy-") as folder:
        root = Path(folder)
        clean = source(distractors)
        (root / "design.sv").write_text(clean, encoding="utf-8")
        spec = {
            "schema_version": 1,
            "semantics": "two-valued-synchronous",
            "files": ["design.sv"],
            "top": "top",
            "clock": "clk",
            "reset": {"signal": "rst", "active": 1},
            "depth": 6,
            "induction": 4,
            "properties": [{"id": "pipeline", "sink": "q", "source": "d", "latency": 2}],
        }
        # Warm imports, not cached source or proof. Alternate order to reduce systematic bias.
        run_request(spec, root=root)
        for iteration in range(repeat):
            for mode in ["full", "cone"] if iteration % 2 == 0 else ["cone", "full"]:
                start = time.perf_counter()
                rows[mode] = run_request(
                    spec, root=root, cone_reduction=mode == "cone", timeout_ms=60000
                )
                samples[mode].append(time.perf_counter() - start)
        clean_match = (
            rows["full"]["results"] == rows["cone"]["results"] and rows["cone"]["exit_code"] == 0
        )
        # One-stage mutation (mutating both inversions would cancel).
        (root / "design.sv").write_text(
            clean.replace("leaf b(clk,rst,mid,q);", "leaf b(clk,rst,~mid,q);"), encoding="utf-8"
        )
        small = run_request(spec, root=root, timeout_ms=60000)
        # A shallow mutant sample bounds canonical witness work on larger full models.
        large = run_request(spec, root=root, cone_reduction=False, timeout_ms=60000)
        mutant_match = small["results"] == large["results"] and small["exit_code"] == 1
    full = statistics.median(samples["full"])
    reduced = statistics.median(samples["cone"])
    result = {
        "schema_version": 1,
        "suite": "opencollate-hierarchy-cone",
        "scope": (
            "synthetic source-file module hierarchy; full frontend/IR/solver timing "
            "excluding interpreter startup; no vendor or production-SoC comparison"
        ),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "inputs": {
            "distractors": distractors,
            "repeat": repeat,
            "source_sha256": hashlib.sha256(clean.encode()).hexdigest(),
        },
        "status": "pass" if clean_match and mutant_match else "fail",
        "verification": {
            "clean_results_identical": clean_match,
            "mutant_results_and_full_traces_identical": mutant_match,
            "mutant_first_failure": small["results"][0]["checked_through"],
        },
        "full_model": {
            "instances": len(rows["cone"]["model"]["hierarchy"]),
            "state_bits": rows["cone"]["model"]["state_bits"],
            "ir_nodes": rows["cone"]["model"]["ir_nodes"],
        },
        "property_cone": rows["cone"]["model"]["proof_cones"]["pipeline"],
        "seconds": samples,
        "median_seconds": {"full": full, "cone": reduced},
        "speedup": full / reduced,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distractors", type=int, default=128)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json-output", type=Path, required=True)
    args = parser.parse_args()
    result = run_suite(distractors=args.distractors, repeat=args.repeat)
    args.json_output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

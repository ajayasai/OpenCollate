"""Source-file sequential mutations, proof boundaries and width/depth scale tiers."""

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

from opencollate.sequential import digest, run_request
from opencollate.sequential_ir import SequentialError


def pipeline(width: int, stages: int) -> str:
    declarations = "\n".join(f"logic [{width - 1}:0] r{i};" for i in range(stages))
    resets = " ".join(f"r{i} <= '0;" for i in range(stages))
    updates = " ".join(f"r{i} <= {'d' if i == 0 else f'r{i - 1}'};" for i in range(stages))
    return (
        f"module top(input logic clk,rst_n,en,input logic [{width - 1}:0] d,"
        f"output wire [{width - 1}:0] q);\n{declarations}\n"
        f"always_ff @(posedge clk) if (!rst_n) begin {resets} end "
        f"else if (en) begin {updates} end\nassign q = r{stages - 1}; endmodule\n"
    )


def case_specs() -> list[tuple[str, str, dict[str, Any], str]]:
    cases = []

    def spec(stages: int) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "semantics": "two-valued-synchronous",
            "files": ["design.sv"],
            "top": "top",
            "clock": "clk",
            "reset": {"signal": "rst_n", "active": 0},
            "depth": max(12, stages + 3),
            "induction": 4,
            "assumptions": {"en": 1},
            "properties": [{"id": "data", "source": "d", "sink": "q", "latency": stages}],
        }

    for width, stages in [(8, 1), (64, 2), (256, 8), (256, 16)]:
        cases.append(
            (f"pipeline-{width}x{stages}", pipeline(width, stages), spec(stages), "proven")
        )
    cases.append(
        ("inverted-data", pipeline(8, 1).replace("r0 <= d", "r0 <= ~d"), spec(1), "counterexample")
    )
    p = spec(2)
    p["properties"][0]["latency"] = 1
    cases.append(("wrong-latency", pipeline(8, 2), p, "counterexample"))
    p = spec(1)
    p["assumptions"] = {}
    cases.append(("missing-enable-guard", pipeline(8, 1), p, "counterexample"))
    p = spec(1)
    p["induction"] = 0
    cases.append(("bmc-not-proof", pipeline(8, 1), p, "bounded"))
    p = spec(1)
    p["properties"][0]["when"] = [{"signal": "en", "equals": 0}]
    cases.append(("unreachable-guard", pipeline(8, 1), p, "uncovered"))
    late = (
        "module top(input logic clk,rst_n,en,d,output wire q);logic [3:0] count;"
        "always_ff @(posedge clk) if(!rst_n) count<=0; else count<=count+1'b1;"
        "assign q=count==4'd9; endmodule"
    )
    p = spec(1)
    p["properties"] = [{"id": "late", "sink": "q", "equals": 0}]
    p["depth"] = 6
    cases.append(("late-failure-shallow", late, p, "bounded"))
    p = json.loads(json.dumps(p))
    p["depth"] = 12
    cases.append(("late-failure-deep", late, p, "counterexample"))
    cases.append(
        (
            "reject-async-reset",
            pipeline(8, 1).replace("posedge clk", "posedge clk or negedge rst_n"),
            spec(1),
            "unsupported",
        )
    )
    return cases


def run_suite(*, repeat: int = 1) -> dict[str, Any]:
    if type(repeat) is not int or not 1 <= repeat <= 10:
        raise ValueError("repeat must be 1..10")
    results = []
    passed = True
    for name, rtl, spec, expected in case_specs():
        samples = []
        stable = []
        receipt = None
        with tempfile.TemporaryDirectory(prefix="oc-sequential-") as directory:
            root = Path(directory)
            (root / "design.sv").write_text(rtl, encoding="utf-8", newline="\n")
            for _ in range(repeat):
                start = time.perf_counter()
                try:
                    receipt = run_request(spec, root=root, timeout_ms=30000)
                    status = receipt["results"][0]["status"]
                    semantic = {
                        "status": status,
                        "results": receipt["results"],
                        "model": receipt["model"],
                        "binding": receipt["binding"],
                    }
                except SequentialError as exc:
                    status = "unsupported"
                    semantic = {"status": status, "error": str(exc)}
                samples.append(time.perf_counter() - start)
                stable.append(digest(semantic))
        ok = status == expected and len(set(stable)) == 1
        passed = passed and ok
        results.append(
            {
                "name": name,
                "expected": expected,
                "actual": status,
                "pass": ok,
                "source_sha256": hashlib.sha256(rtl.encode()).hexdigest(),
                "request_sha256": digest(spec),
                "semantic_sha256": stable[0],
                "elapsed_samples_seconds": samples,
                "median_seconds": statistics.median(samples),
                "state_bits": receipt["model"]["state_bits"] if receipt else None,
                "induction_depth": receipt["results"][0]["induction_depth"] if receipt else None,
                "counterexample_cycle": receipt["results"][0]["checked_through"]
                if receipt and status == "counterexample"
                else None,
            }
        )
    return {
        "suite": "opencollate-source-sequential-v1",
        "status": "pass" if passed else "fail",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "repeat": repeat,
        "cases": results,
        "result_sha256": digest(
            [
                {
                    k: v
                    for k, v in r.items()
                    if k not in {"elapsed_samples_seconds", "median_seconds"}
                }
                for r in results
            ]
        ),
        "scope": (
            "Synthetic RTL source corpus; no commercial-tool or production-SoC performance claim."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    r = run_suite(repeat=args.repeat)
    text = json.dumps(r, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return int(r["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())

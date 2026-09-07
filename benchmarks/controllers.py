"""Controller source corpus: reduced/full SMT, certificates, and explicit rejection oracles."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from opencollate.proof_kernel import ProofError
from opencollate.sequential import run_request
from opencollate.sequential_certificate import certify, verify_certificate
from opencollate.sequential_ir import SequentialError

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "controller"


def cases() -> list[tuple[str, str, dict[str, Any], str, str | None]]:
    rtl = (EXAMPLE / "controller.sv").read_text(encoding="utf-8")
    spec = json.loads((EXAMPLE / "request.json").read_text(encoding="utf-8"))
    spec["files"] = ["design.sv"]
    bank = (
        rtl[: rtl.index("module top")]
        + """
      module top(input logic clk,rst,input logic [1:0] op,input logic [7:0] d,
                 output wire [7:0] q);
        for(genvar i=0;i<16;i++) begin: channels
          wire [7:0] value;
          lane bank(.clk(clk),.rst(rst),.op(op),.d(d),.q(value));
        end
        assign q=channels[0].value;
      endmodule
    """
    )
    fsm = (EXAMPLE / "fsm.sv").read_text(encoding="utf-8")
    fspec = json.loads((EXAMPLE / "fsm-request.json").read_text(encoding="utf-8"))
    fspec["files"] = ["design.sv"]
    bounded = copy.deepcopy(spec)
    bounded["induction"] = 0
    uncovered = copy.deepcopy(spec)
    uncovered["assumptions"] = {"op": 1}
    return [
        ("hierarchical-load", rtl, spec, "proven", None),
        ("sixteen-controller-bank", bank, spec, "proven", None),
        ("enumerated-fsm", fsm, fspec, "proven", None),
        (
            "inverted-load",
            rtl.replace("0: n = d", "0: n = ~d"),
            spec,
            "counterexample",
            "base counterexample",
        ),
        (
            "wrong-mode",
            rtl.replace("0: n = d", "0: n = q + 1'b1"),
            spec,
            "counterexample",
            "base counterexample",
        ),
        (
            "first-match-priority",
            rtl.replace("0: n = d", "0,1: n = ~d; 0: n = d"),
            spec,
            "counterexample",
            "base counterexample",
        ),
        (
            "possible-latch",
            rtl.replace("n = q;", "").replace("default: n = 0;", ""),
            spec,
            "unsupported",
            "incomplete combinational",
        ),
        (
            "read-before-write",
            rtl.replace("n = q;", "n = n + q;"),
            spec,
            "unsupported",
            "read before definite",
        ),
        (
            "wildcard-case",
            rtl.replace("case (op)", "casez (op)"),
            spec,
            "unsupported",
            "ordinary case",
        ),
        (
            "blocking-clocked-update",
            rtl.replace("q <= n", "q = n"),
            spec,
            "unsupported",
            "nonblocking",
        ),
        ("guard-uncovered", rtl, uncovered, "uncovered", "guard is uncovered"),
        ("bounded-is-not-proof", rtl, bounded, "bounded", "insufficient induction/base depth"),
    ]


def run_suite(*, repeat: int = 1) -> dict[str, Any]:
    if type(repeat) is not int or not 1 <= repeat <= 5:
        raise ValueError("repeat must be 1..5")
    results = []
    for name, rtl, spec, expected, rejection in cases():
        times = []
        verify_times = []
        identical = []
        statuses = []
        certificate_outcomes = []
        sizes = []
        model = None
        error = None
        certificate_hashes = []
        with tempfile.TemporaryDirectory(prefix="oc-controllers-") as directory:
            root = Path(directory)
            (root / "design.sv").write_text(rtl, encoding="utf-8", newline="\n")
            for _ in range(repeat):
                started = time.perf_counter()
                try:
                    reduced = run_request(spec, root=root)
                    times.append(time.perf_counter() - started)
                    full = run_request(spec, root=root, cone_reduction=False)
                    identical.append(reduced["results"] == full["results"])
                    statuses.append(reduced["results"][0]["status"])
                    model = {k: reduced["model"][k] for k in ("state_bits", "proof_cones")}
                except SequentialError as exc:
                    times.append(time.perf_counter() - started)
                    error = str(exc)
                    statuses.append(
                        "unsupported"
                        if expected == "unsupported" and rejection in error
                        else "unexpected-error"
                    )
                    identical.append(expected == "unsupported")
                try:
                    proof = certify(spec, root=root)
                    started = time.perf_counter()
                    checked = verify_certificate(spec, proof, root=root)
                    verify_times.append(time.perf_counter() - started)
                    certificate_outcomes.append(checked["status"])
                    sizes.append(
                        len(json.dumps(proof, sort_keys=True, separators=(",", ":")).encode())
                    )
                    certificate_hashes.append(proof["certificate_sha256"])
                except (ProofError, SequentialError) as exc:
                    error = str(exc)
                    certificate_outcomes.append(
                        "expected-rejection"
                        if rejection and rejection in error
                        else "unexpected-error"
                    )
        cert_expected = "certificate-verified" if expected == "proven" else "expected-rejection"
        okay = (
            all(x == expected for x in statuses)
            and all(identical)
            and all(x == cert_expected for x in certificate_outcomes)
            and len(set(certificate_hashes)) <= 1
        )
        results.append(
            {
                "name": name,
                "expected": expected,
                "outcomes": statuses,
                "pass": okay,
                "source_sha256": hashlib.sha256(rtl.encode()).hexdigest(),
                "request_sha256": hashlib.sha256(
                    json.dumps(spec, sort_keys=True).encode()
                ).hexdigest(),
                "reduced_full_outcomes_and_traces_match": identical,
                "certificate_outcomes": certificate_outcomes,
                "certificate_bytes": sizes,
                "certificate_sha256": certificate_hashes,
                "check_seconds": times,
                "median_check_seconds": statistics.median(times),
                "certificate_verification_seconds": verify_times,
                "model": model,
                "rejection_reason": error,
            }
        )
    return {
        "suite": "opencollate-hierarchical-controllers-v1",
        "status": "pass" if all(x["pass"] for x in results) else "fail",
        "repeat": repeat,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": {
            p: importlib.metadata.version(p) for p in ("pyslang", "z3-solver", "python-sat")
        },
        "cases": results,
        "scope": (
            "Synthetic source fixtures, not production RTL qualification or a "
            "proprietary-tool comparison. Timings exclude interpreter startup."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = run_suite(repeat=args.repeat)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return int(result["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())

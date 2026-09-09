"""Manifest-bound multi-file source checks and explicit dependency failure oracles."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from opencollate.proof_kernel import ProofError
from opencollate.sequential import run_request
from opencollate.sequential_certificate import certify, verify_certificate
from opencollate.sequential_cnf import digest
from opencollate.sequential_ir import SequentialError

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/preprocessed"
CASES = [
    ("one-bit", 1, "proven", ""),
    ("byte", 8, "proven", ""),
    ("word64", 64, "proven", ""),
    ("word256", 256, "proven", ""),
    ("inverted-build", 8, "counterexample", ""),
    ("mutated-header", 8, "counterexample", ""),
    ("missing-header", 8, "rejected", "declared header"),
    ("ambiguous-header", 8, "rejected", "exactly one"),
    ("computed-include", 8, "rejected", "literal quoted"),
    ("inactive-undeclared-include", 8, "rejected", "declared header"),
    ("source-location-spoof", 8, "rejected", "unsupported directive"),
    ("inactive-header-drift", 8, "stale-rejected", ""),
]


def _prepare(root: Path, name: str, width: int) -> dict[str, Any]:
    shutil.copytree(EXAMPLE, root, dirs_exist_ok=True)
    request = json.loads((root / "request.json").read_text())
    request["preprocess"]["defines"]["DATA_WIDTH"] = str(width)
    source = root / "rtl/register.sv"
    if name == "inverted-build":
        request["preprocess"]["defines"]["INVERT_DATA"] = "1"
    elif name == "mutated-header":
        path = root / "include/ops.svh"
        path.write_text(
            path.read_text().replace(
                "`define TRANSFER(data) (data)", "`define TRANSFER(data) (~data)"
            )
        )
    elif name == "missing-header":
        request["preprocess"]["headers"] = []
    elif name == "ambiguous-header":
        shutil.copy(root / "include/ops.svh", root / "rtl/ops.svh")
        request["preprocess"]["headers"].append("rtl/ops.svh")
    elif name == "computed-include":
        source.write_text(source.read_text().replace('"ops.svh"', "`HEADER"))
    elif name == "inactive-undeclared-include":
        source.write_text('`ifdef NOT_SET\n`include "unlisted.svh"\n`endif\n' + source.read_text())
    elif name == "source-location-spoof":
        source.write_text('`line 10 "false.sv" 0\n' + source.read_text())
    return request


def run_suite(*, repeat: int = 1) -> dict[str, Any]:
    if type(repeat) is not int or not 1 <= repeat <= 10:
        raise ValueError("repeat must be in 1..10")
    rows = []
    for name, width, expected, reason in CASES:
        samples: list[float] = []
        outcomes: list[dict[str, Any]] = []
        source_hashes: dict[str, str] = {}
        for _ in range(repeat):
            with tempfile.TemporaryDirectory(prefix="oc-preprocess-") as directory:
                root = Path(directory)
                request = _prepare(root, name, width)
                source_hashes = {
                    p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(root.rglob("*"))
                    if p.is_file() and p.suffix in {".sv", ".svh"}
                }
                start = time.perf_counter()
                try:
                    result = run_request(request, root=root)
                    if expected == "stale-rejected":
                        cert = certify(request, root=root)
                        header = root / "include/ops.svh"
                        header.write_text(header.read_text().replace("(~(data))", "(data)"))
                        try:
                            verify_certificate(request, cert, root=root)
                        except (ProofError, SequentialError) as exc:
                            if "binding" not in str(exc) and "source" not in str(exc):
                                raise
                            outcome = {
                                "status": "stale-rejected",
                                "logical_result": run_request(request, root=root)["status"],
                            }
                        else:
                            outcome = {"status": "unexpectedly-accepted-stale-proof"}
                    elif result["status"] == "proven":
                        cert = certify(request, root=root)
                        verified = verify_certificate(request, cert, root=root)
                        outcome = {
                            "status": "proven",
                            "certificate": verified["status"],
                            "source_count": len(result["binding"]["sources"]),
                            "binding": result["binding"],
                        }
                    else:
                        try:
                            certify(request, root=root)
                        except ProofError as exc:
                            if "base counterexample" not in str(exc):
                                raise
                            outcome = {
                                "status": result["status"],
                                "certificate": "rejected-counterexample",
                                "trace": result["results"][0]["trace"],
                            }
                        else:
                            outcome = {"status": "incorrectly-certified"}
                except SequentialError as exc:
                    outcome = {
                        "status": "rejected"
                        if reason and reason in str(exc)
                        else "unexpected-failure",
                        "reason": str(exc),
                    }
                samples.append(time.perf_counter() - start)
                outcomes.append(outcome)
        hashes = [digest(outcome) for outcome in outcomes]
        passed = all(o["status"] == expected for o in outcomes) and len(set(hashes)) == 1
        if expected == "proven":
            passed = passed and all(
                o.get("certificate") == "certificate-verified" for o in outcomes
            )
        if expected == "stale-rejected":
            passed = passed and all(o.get("logical_result") == "proven" for o in outcomes)
        if expected == "counterexample":
            passed = passed and all(
                o.get("certificate") == "rejected-counterexample" and o.get("trace")
                for o in outcomes
            )
        rows.append(
            {
                "name": name,
                "expected": expected,
                "pass": passed,
                "outcomes": outcomes,
                "outcome_sha256": hashes[0],
                "source_sha256": source_hashes,
                "seconds": samples,
                "median_seconds": statistics.median(samples),
            }
        )
    return {
        "suite": "manifest-preprocessing-v1",
        "status": "pass" if all(r["pass"] for r in rows) else "fail",
        "repeat": repeat,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": {
            p: importlib.metadata.version(p) for p in ("pyslang", "z3-solver", "python-sat")
        },
        "scope": (
            "Structured source-file regression corpus, not a commercial comparison, "
            "production SoC or all-build-configurations proof."
        ),
        "cases": rows,
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

"""Source-bound certificate creation and solver-free checking, with explicit negative cases."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from benchmarks.sequential import case_specs
from opencollate.proof_kernel import ProofError
from opencollate.sequential_certificate import certify, verify_certificate
from opencollate.sequential_cnf import digest
from opencollate.sequential_ir import SequentialError

EXPECTED_REJECTION = {
    "inverted-data": "base counterexample",
    "wrong-latency": "base counterexample",
    "missing-enable-guard": "base counterexample",
    "bmc-not-proof": "insufficient induction/base depth",
    "unreachable-guard": "guard is uncovered",
    "late-failure-shallow": "bounded only",
    "late-failure-deep": "base counterexample",
    "reject-async-reset": "unsupported",
}


def run_suite(*, repeat: int = 1) -> dict[str, Any]:
    if type(repeat) is not int or not 1 <= repeat <= 10:
        raise ValueError("repeat must be 1..10")
    results = []
    for name, rtl, request, expected in case_specs():
        creation, verification, outcomes, hashes = [], [], [], []
        certificate = None
        error = None
        with tempfile.TemporaryDirectory(prefix="oc-certificates-") as directory:
            root = Path(directory)
            (root / "design.sv").write_text(rtl, encoding="utf-8", newline="\n")
            for _ in range(repeat):
                start = time.perf_counter()
                try:
                    certificate = certify(request, root=root)
                    creation.append(time.perf_counter() - start)
                    start = time.perf_counter()
                    checked = verify_certificate(request, certificate, root=root)
                    verification.append(time.perf_counter() - start)
                    outcomes.append(checked["status"])
                    hashes.append(certificate["certificate_sha256"])
                except (ProofError, SequentialError) as exc:
                    creation.append(time.perf_counter() - start)
                    error = str(exc)
                    if expected == "unsupported" and isinstance(exc, SequentialError):
                        outcomes.append("expected-rejection")
                    elif name in EXPECTED_REJECTION and EXPECTED_REJECTION[name] in error:
                        outcomes.append("expected-rejection")
                    else:
                        outcomes.append("unexpected-failure")
                    hashes.append(digest({"error": error}))
        target = "certificate-verified" if expected == "proven" else "expected-rejection"
        results.append(
            {
                "name": name,
                "expected": target,
                "outcomes": outcomes,
                "pass": all(o == target for o in outcomes) and len(set(hashes)) == 1,
                "source_sha256": hashlib.sha256(rtl.encode()).hexdigest(),
                "request_sha256": digest(request),
                "certificate_sha256": certificate["certificate_sha256"] if certificate else None,
                "certificate_bytes": len(
                    json.dumps(certificate, sort_keys=True, separators=(",", ":")).encode()
                )
                if certificate
                else None,
                "creation_or_rejection_seconds": creation,
                "verification_seconds": verification,
                "median_creation_seconds": statistics.median(creation),
                "median_verification_seconds": statistics.median(verification)
                if verification
                else None,
                "proof_steps": sum(
                    len(row[k]["proof"]) for row in certificate["claims"] for k in ("base", "step")
                )
                if certificate
                else None,
                "reason": error,
            }
        )
    return {
        "suite": "opencollate-source-certificates-v1",
        "status": "pass" if all(r["pass"] for r in results) else "fail",
        "repeat": repeat,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": {p: importlib.metadata.version(p) for p in ("pyslang", "python-sat")},
        "cases": results,
        "scope": (
            "Synthetic RTL, not a commercial-tool comparison or production-SoC scalability claim. "
            "Timings include source parsing, encoding and evidence checking; "
            "interpreter startup is excluded."
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

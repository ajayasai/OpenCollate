"""Reproducible symbolic-versus-legacy Boolean conformance measurements.

Synthetic formulas, not commercial-product or production-SoC benchmarks.
Timing is informational. Correct outcomes and deterministic evidence are gated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import time
from pathlib import Path
from typing import Any

from opencollate.boolean import check_equivalence
from opencollate.symbolic import check_symbolic_equivalence


def run_suite(repeat: int = 3) -> dict[str, Any]:
    if type(repeat) is not int or not 1 <= repeat <= 20:
        raise ValueError("repeat must be an integer between 1 and 20")
    cases = []
    for count in (12, 64, 128):
        names = [f"A{index:03}" for index in range(count)]
        left = " & ".join(names)
        for mutant in (False, True):
            selected = names[:-1] if mutant else names
            right = "!(" + " | ".join("!" + name for name in selected) + ")"
            samples, old_samples, results = [], [], []
            legacy = None
            for _ in range(repeat):
                start = time.perf_counter()
                result = check_symbolic_equivalence(left, right)
                samples.append(time.perf_counter() - start)
                results.append(result.to_dict())
                start = time.perf_counter()
                legacy = check_equivalence(left, right, max_variables=12)
                old_samples.append(time.perf_counter() - start)
            expected = "different" if mutant else "equivalent"
            valid_witness = not mutant or results[0]["counterexample"] == {
                name: name != names[-1] for name in names
            }
            deterministic = all(item == results[0] for item in results)
            passed = results[0]["status"] == expected and valid_witness and deterministic
            cases.append(
                {
                    "name": f"and-demorgan-{count}-{'mutant' if mutant else 'control'}",
                    "variables": count,
                    "expected": expected,
                    "status": "pass" if passed else "fail",
                    "deterministic": deterministic,
                    "result": results[0],
                    "symbolic_median_seconds": statistics.median(samples),
                    "legacy_median_seconds": statistics.median(old_samples),
                    "legacy_equivalent": legacy.equivalent if legacy is not None else None,
                    "legacy_checked_assignments": legacy.checked_assignments
                    if legacy is not None
                    else 0,
                }
            )
    guard_cases = [
        ("selected-mux", "(S&A)|(!S&B)", "A", "S", "equivalent"),
        ("contradictory-mode", "A", "A", "S&!S", "vacuous"),
    ]
    for name, left, right, guard, expected in guard_cases:
        result = check_symbolic_equivalence(left, right, assume=guard)
        cases.append(
            {
                "name": name,
                "expected": expected,
                "result": result.to_dict(),
                "status": "pass" if result.status == expected else "fail",
            }
        )
    evidence = [{k: v for k, v in case.items() if not k.endswith("seconds")} for case in cases]
    return {
        "schema_version": 1,
        "suite": "opencollate-symbolic-conformance",
        "status": "pass" if all(case["status"] == "pass" for case in cases) else "fail",
        "scope": "synthetic two-valued formulas; not a commercial comparison",
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "repeat": repeat,
        "result_sha256": hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_suite(args.repeat)
        text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if args.json_output is None:
            print(text, end="")
        else:
            args.json_output.write_text(text, encoding="utf-8")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

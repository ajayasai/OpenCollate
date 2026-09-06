"""Public, deterministic Boolean certificate conformance and timing corpus.

Synthetic formulas only. No commercial comparisons or production-SoC claims.
The optional DIMACS/RUP export supports independent DRAT-trim validation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import platform
import statistics
import time
from pathlib import Path
from typing import Any

from opencollate.boolean_proof_kernel import ProofLimits
from opencollate.certificates import certify_obligations, verify_certificate
from opencollate.formal import SEMANTICS, _digest
from opencollate.proof_cnf import compile_boolean


def request(left: str, right: str, guard: str = "1") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "semantics": SEMANTICS,
        "obligations": [{"id": "route", "left": left, "right": right, "assume": guard}],
    }


def corpus() -> list[tuple[str, dict[str, Any], str, ProofLimits | None]]:
    cases = []
    for width in (12, 64, 128, 512):
        names = [f"A{i:04}" for i in range(width)]
        for mutant in (False, True):
            selected = names[:-1] if mutant else names
            cases.append(
                (
                    f"demorgan-{width}-{'mutant' if mutant else 'control'}",
                    request("&".join(names), "!(" + "|".join("!" + x for x in selected) + ")"),
                    "different" if mutant else "equivalent",
                    None,
                )
            )
    cases.extend(
        [
            ("selected-mux", request("(S&A)|(!S&B)", "A", "S"), "equivalent", None),
            ("inactive-mux", request("(S&A)|(!S&B)", "A", "!S"), "different", None),
            ("contradictory-mode", request("A", "A", "S&!S"), "vacuous", None),
            ("constant-mismatch", request("0", "1"), "different", None),
            ("parity-permutation", request("A^B^C^D^E^F", "F^E^D^C^B^A"), "equivalent", None),
            ("unsupported-ternary", request("A?B:C", "A"), "inconclusive", None),
            ("verification-budget", request("A&B", "B&A"), "inconclusive", ProofLimits(max_work=1)),
        ]
    )
    return cases


def _export(directory: Path, name: str, source: dict[str, Any], bundle: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.request.json").write_text(
        json.dumps(source, sort_keys=True) + "\n", encoding="utf-8"
    )
    (directory / f"{name}.certificate.json").write_text(
        json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8"
    )
    row = bundle["results"][0]
    if row["status"] not in {"equivalent", "vacuous"}:
        return
    item = source["obligations"][0]
    compiled = compile_boolean(item["left"], item["right"], item["assume"])
    cnf = compiled.cnf("guard" if row["status"] == "vacuous" else "difference")
    text = f"p cnf {compiled.encoder.variables} {len(cnf)}\n"
    text += "".join(" ".join(map(str, (*clause, 0))) + "\n" for clause in cnf)
    (directory / f"{name}.cnf").write_text(text, encoding="ascii")
    proof = "".join(" ".join(map(str, (*clause, 0))) + "\n" for clause in row["proof"])
    (directory / f"{name}.rup").write_text(proof, encoding="ascii")


def poison_controls() -> list[dict[str, Any]]:
    source = request("A", "!A")
    different = certify_obligations(source)
    true_source = request("A", "A", "S")
    equivalent = certify_obligations(true_source)
    cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    forged = copy.deepcopy(different)
    forged.update(status="pass", exit_code=0)
    forged["results"][0].update(status="equivalent", counterexample=None, proof=[[]])
    cases.append(("rehashed-false-unsat", source, forged))
    for name, change in (
        ("missing-proof", {"proof": []}),
        ("wrong-cnf-binding", {"cnf_sha256": "0" * 64}),
        ("false-guard-witness", {"guard_witness": {"A": False, "S": False}}),
        ("integer-witness", {"guard_witness": {"A": 0, "S": 1}}),
        ("extra-variable", {"variables": ["A", "S", "T"]}),
        ("trailing-proof-data", {"proof": [[], []]}),
    ):
        forged = copy.deepcopy(equivalent)
        forged["results"][0].update(change)
        cases.append((name, true_source, forged))
    cases.append(("changed-request", request("A", "B"), different))
    results = []
    for name, value, certificate in cases:
        certificate["certificate_sha256"] = _digest(
            {k: v for k, v in certificate.items() if k != "certificate_sha256"}
        )
        try:
            verify_certificate(value, certificate)
        except ValueError:
            rejected = True
        else:
            rejected = False
        results.append({"name": name, "rejected": rejected})
    return results


def run_suite(repeat: int = 3, *, export_dir: Path | None = None) -> dict[str, Any]:
    if type(repeat) is not int or not 1 <= repeat <= 20:
        raise ValueError("repeat must be an integer between 1 and 20")
    results = []
    for name, source, expected, limits in corpus():
        bundles, generation_samples, verification_samples = [], [], []
        checked = None
        for _ in range(repeat):
            start = time.perf_counter()
            bundle = certify_obligations(source, limits=limits)
            generation_samples.append(time.perf_counter() - start)
            start = time.perf_counter()
            checked = verify_certificate(source, bundle)
            verification_samples.append(time.perf_counter() - start)
            bundles.append(bundle)
        bundle = bundles[0]
        row = bundle["results"][0]
        deterministic = all(item == bundle for item in bundles)
        passed = row["status"] == expected and deterministic and checked is not None
        expected_code = (
            2 if expected in {"vacuous", "inconclusive"} else int(expected == "different")
        )
        passed = (
            passed
            and bundle["exit_code"] == expected_code
            and checked["exit_code"] == expected_code
        )
        results.append(
            {
                "name": name,
                "status": "pass" if passed else "fail",
                "expected": expected,
                "observed": row["status"],
                "deterministic": deterministic,
                "request_sha256": bundle["request_sha256"],
                "certificate_sha256": bundle["certificate_sha256"],
                "source_variables": len(row["variables"]),
                "proof_steps": len(row["proof"]) if row["proof"] is not None else None,
                "certificate_bytes": len(json.dumps(bundle, sort_keys=True).encode()),
                "generation_seconds": generation_samples,
                "verification_seconds": verification_samples,
                "generation_median_seconds": statistics.median(generation_samples),
                "verification_median_seconds": statistics.median(verification_samples),
            }
        )
        if export_dir is not None:
            _export(export_dir, name, source, bundle)
    poisons = poison_controls()
    evidence = [{k: v for k, v in row.items() if not k.endswith("seconds")} for row in results]
    return {
        "schema_version": 1,
        "suite": "opencollate-independent-boolean-certificates",
        "scope": (
            "synthetic two-valued combinational formulas; "
            "no commercial or production-SoC comparison"
        ),
        "status": "pass"
        if all(row["status"] == "pass" for row in results)
        and all(row["rejected"] for row in poisons)
        else "fail",
        "repeat": repeat,
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "producer": "Glucose3",
            "python-sat": importlib.metadata.version("python-sat"),
        },
        "implementation_sha256": _digest(
            {
                name: hashlib.sha256(
                    (
                        Path(__file__).resolve().parents[1] / "src" / "opencollate" / name
                    ).read_bytes()
                ).hexdigest()
                for name in (
                    "boolean.py",
                    "symbolic.py",
                    "formal.py",
                    "proof_cnf.py",
                    "boolean_proof_kernel.py",
                    "certificates.py",
                    "certificate_process.py",
                    "_certificate_worker.py",
                )
            }
        ),
        "manifest_sha256": _digest(
            [
                {"name": name, "request": value, "expected": expected, "limits": repr(limits)}
                for name, value, expected, limits in corpus()
            ]
        ),
        "result_sha256": _digest({"cases": evidence, "poison_controls": poisons}),
        "cases": results,
        "poison_controls": poisons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--export-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_suite(args.repeat, export_dir=args.export_dir)
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

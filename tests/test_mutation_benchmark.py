from __future__ import annotations

import hashlib
import json
from pathlib import Path

from benchmarks.mutations import CASES, main, run_suite


def _digest_without_result(report: dict[str, object]) -> str:
    payload = dict(report)
    payload.pop("result_sha256")
    rendered = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def test_full_mutation_suite_is_exact_clean_and_deterministic() -> None:
    report = run_suite()
    summary = report["summary"]

    assert report["status"] == "pass"
    assert summary["mutation_cases"] == len(CASES) == 34
    assert summary["clean_controls"] == len(CASES)
    assert summary["target_detections"] == len(CASES)
    assert summary["exact_mutation_detections"] == len(CASES)
    assert summary["false_negatives"] == 0
    assert summary["overtriggered_mutations"] == 0
    assert summary["inconclusive_mutations"] == 0
    assert summary["true_negative_controls"] == len(CASES)
    assert summary["false_positive_controls"] == 0
    assert summary["deterministic_pairs"] == len(CASES)
    assert summary["passed_pairs"] == len(CASES)
    assert summary["recall"] == 1.0
    assert summary["clean_control_specificity"] == 1.0
    assert summary["exact_pair_accuracy"] == 1.0
    assert all(item["status"] == "pass" for item in report["cases"])
    assert report["result_sha256"] == _digest_without_result(report)


def test_selection_order_cannot_change_report_digest() -> None:
    identifiers = [item.identifier for item in CASES]
    forward = run_suite(identifiers=identifiers)
    reverse = run_suite(identifiers=list(reversed(identifiers)))

    assert forward == reverse


def test_family_selection_reports_only_that_oracle_partition() -> None:
    report = run_suite(families=("connectivity",))

    assert report["status"] == "pass"
    assert report["summary"]["mutation_cases"] == 3
    assert report["oracle"]["selection"]["families"] == ["connectivity"]
    assert {item["family"] for item in report["cases"]} == {"connectivity"}


def test_cli_writes_schema_valid_deterministic_report(tmp_path: Path) -> None:
    target = tmp_path / "mutation-results.json"

    assert main(["--family", "registers", "--enforce-perfect", "--json-output", str(target)]) == 0
    first = json.loads(target.read_text(encoding="utf-8"))
    assert first["status"] == "pass"
    assert first["summary"]["mutation_cases"] == 6

    assert main(["--family", "registers", "--enforce-perfect", "--json-output", str(target)]) == 0
    second = json.loads(target.read_text(encoding="utf-8"))
    assert first == second

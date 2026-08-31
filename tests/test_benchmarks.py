from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmarks.run import (
    _connectivity_operation,
    _diff_operation,
    _diff_verifier,
    _systemrdl_operation,
    _verify_connectivity,
    _verify_systemrdl,
    build_diff_workload,
    main,
    render_json,
    run_suite,
)
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "benchmarks" / "benchmark-results.schema.json"


def _validate_report(report: object) -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(report)


class StepTimer:
    def __init__(self, step: float = 0.25) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def test_report_diff_workload_is_adversarial_and_order_invariant() -> None:
    workload = build_diff_workload(8)
    result = _diff_operation(workload)
    verification = _diff_verifier(workload)(result)

    assert verification["input_order_invariant"]
    assert verification["states_exercised"] == ["changed", "new", "resolved", "unchanged"]
    assert verification["summary"] == {
        "baseline": 32,
        "current": 32,
        "new": 2,
        "changed": 2,
        "unchanged": 28,
        "resolved": 2,
        "current_active": 32,
        "current_suppressed": 0,
        "current_fatal": 0,
        "current_active_fatal": 0,
    }


def test_machine_report_is_byte_deterministic_with_a_controlled_timer() -> None:
    first = run_suite(
        cases=("report-diff",),
        repeat=2,
        warmup=1,
        diff_groups=4,
        timer=StepTimer(),
    )
    second = run_suite(
        cases=("report-diff",),
        repeat=2,
        warmup=1,
        diff_groups=4,
        timer=StepTimer(),
    )

    assert render_json(first) == render_json(second)
    _validate_report(first)
    assert first["status"] == "pass"
    assert first["cases"][0]["timing"]["samples_seconds"] == [0.25, 0.25]


def test_enforced_budget_failure_is_machine_readable() -> None:
    report = run_suite(
        cases=("report-diff",),
        repeat=1,
        warmup=0,
        diff_groups=4,
        diff_budget_seconds=0.1,
        enforce_budgets=True,
        timer=StepTimer(),
    )

    assert report["conformance_status"] == "pass"
    assert report["budget_status"] == "fail"
    assert report["status"] == "fail"
    assert report["cases"][0]["budget"]["within_budget"] is False
    _validate_report(report)


def test_benchmark_results_schema_is_valid_draft_2020_12() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


def test_schema_accepts_captured_conformance_failure(tmp_path: Path) -> None:
    report = run_suite(
        cases=("systemrdl-import",),
        root=tmp_path,
        repeat=1,
        warmup=0,
        diff_groups=4,
        timer=StepTimer(),
    )

    assert report["status"] == "fail"
    assert report["cases"][0]["error"]["type"] == "ConformanceFailure"
    _validate_report(report)


def test_cli_writes_json_and_concise_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "nested" / "benchmark-results.json"

    assert (
        main(
            [
                "--case",
                "report-diff",
                "--repeat",
                "1",
                "--warmup",
                "0",
                "--diff-groups",
                "4",
                "--json-output",
                str(output),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert "OpenCollate public benchmark suite: PASS" in captured.out
    assert "report-diff: PASS" in captured.out
    assert report["schema_version"] == 1
    assert report["suite"] == "opencollate-public-benchmarks"
    _validate_report(report)


def test_uart_case_checks_the_complete_public_example() -> None:
    report = run_suite(
        cases=("uart-check",),
        root=ROOT,
        repeat=1,
        warmup=0,
        diff_groups=4,
        timer=StepTimer(),
    )

    case = report["cases"][0]
    assert report["status"] == "pass"
    assert case["verification"]["diagnostic_codes"] == ["OC4001", "OC4301", "OC5003"]
    assert case["verification"]["summary"]["views"] == 13


def test_systemrdl_case_checks_exact_top_register_field_and_address_oracle() -> None:
    result = _systemrdl_operation(ROOT)
    verification = _verify_systemrdl(result)

    assert verification == {
        "selected_top": "benchmark_regs",
        "register_names": ["CTRL", "DATA[0]", "DATA[1]"],
        "absolute_addresses": [0, 0x110, 0x114],
        "field_layouts": {
            "CTRL": [["ENABLE", 0, 1], ["READY", 1, 1]],
            "DATA[0]": [["VALUE", 0, 8]],
            "DATA[1]": [["VALUE", 0, 8]],
        },
    }


def test_connectivity_case_checks_pass_witness_and_tainted_frontier() -> None:
    result = _connectivity_operation(ROOT)
    verification = _verify_connectivity(result)

    assert verification["required_pass"] is True
    assert verification["forbidden_witness"] == [
        {
            "source": "top/middle",
            "sink": "top/y",
            "kind": "assign",
            "inverted": False,
            "status": "known",
        }
    ]
    assert verification["tainted_frontier"] == {
        "source": "top/a",
        "sink": "top/selected",
        "kind": "unsupported_assign",
        "inverted": None,
        "status": "tainted",
    }


def test_new_semantic_cases_have_deterministic_digests_and_validate() -> None:
    arguments = {
        "cases": ("systemrdl-import", "rtl-connectivity"),
        "root": ROOT,
        "repeat": 1,
        "warmup": 0,
        "diff_groups": 4,
    }
    first = run_suite(**arguments, timer=StepTimer())
    second = run_suite(**arguments, timer=StepTimer())

    assert render_json(first) == render_json(second)
    assert [item["name"] for item in first["cases"]] == [
        "systemrdl-import",
        "rtl-connectivity",
    ]
    assert all(len(item["result_sha256"]) == 64 for item in first["cases"])
    _validate_report(first)

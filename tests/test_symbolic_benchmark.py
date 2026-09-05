from __future__ import annotations

import pytest
from benchmarks.symbolic import run_suite


def test_symbolic_corpus_has_exact_oracles() -> None:
    report = run_suite(repeat=1)
    assert report["status"] == "pass"
    assert len(report["cases"]) == 8
    assert all(case["status"] == "pass" for case in report["cases"])
    large = next(case for case in report["cases"] if case["name"] == "and-demorgan-128-control")
    assert large["legacy_equivalent"] is None
    assert large["result"]["status"] == "equivalent"


def test_symbolic_benchmark_rejects_unbounded_repeats() -> None:
    with pytest.raises(ValueError):
        run_suite(repeat=0)

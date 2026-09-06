from __future__ import annotations

import pytest
from benchmarks.hierarchy import run_suite


def test_actual_source_tier_and_full_counterexample_equivalence() -> None:
    result = run_suite(tiers=(0, 3), repeat=1)
    assert result["status"] == "pass"
    for row in result["cases"]:
        assert row["full_state_bits"] == 16 + 32 * row["background_instances"]
        assert row["cone_state_bits"] == 16
        assert row["exact_proof_results_and_bindings_match"]
        assert row["exact_lifted_counterexample_trace_matches"]
        assert row["mutant_failure_cycle"] == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"repeat": True},
        {"repeat": 0},
        {"tiers": ()},
        {"tiers": (-1,)},
        {"tiers": (257,)},
        {"tiers": (True,)},
    ],
)
def test_benchmark_limits(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        run_suite(**kwargs)

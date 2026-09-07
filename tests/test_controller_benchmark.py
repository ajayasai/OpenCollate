from __future__ import annotations

import pytest
from benchmarks.controllers import run_suite


def test_controller_corpus_uses_explicit_positive_and_negative_oracles() -> None:
    result = run_suite()
    assert result["status"] == "pass"
    assert len(result["cases"]) == 12
    assert all(x["pass"] for x in result["cases"])
    assert sum(x["expected"] == "proven" for x in result["cases"]) == 3
    banks = next(x for x in result["cases"] if x["name"] == "sixteen-controller-bank")
    assert banks["model"]["state_bits"] == 128
    assert banks["model"]["proof_cones"]["load"]["state_bits"] == 8


@pytest.mark.parametrize("repeat", [True, 0, 6, 1.0])
def test_controller_benchmark_rejects_invalid_repeats(repeat: object) -> None:
    with pytest.raises(ValueError):
        run_suite(repeat=repeat)

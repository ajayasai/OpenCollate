from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from benchmarks import incremental, upstream_cells


def test_upstream_controls_mutants_and_repeatability() -> None:
    first = upstream_cells.run_suite()
    second = upstream_cells.run_suite()
    assert first == second
    assert first["status"] == "pass"
    assert len(first["cases"]) == 8
    assert all(
        item["independent_inventory_oracle"]
        for item in first["cases"]
        if item["mutation"] == "control"
    )
    assert all(item["observed_codes"] == item["expected_codes"] for item in first["cases"])


def test_upstream_tampered_input_rejected(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    shutil.copytree(upstream_cells.FIXTURES, root)
    source = root / next(iter(upstream_cells.SHA256))
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="integrity mismatch"):
        upstream_cells.verify_fixtures(root)


def test_incremental_actual_file_and_full_rule_pipeline() -> None:
    result = incremental.run_suite(cells=4, table_rows=2, repeat=2)
    assert result["status"] == "pass"
    assert result["verification"]["all_reports_identical"]
    assert result["verification"]["single_file_mutation_detected"]
    assert result["verification"]["mutant_matches_fresh_check"]
    assert result["incremental_cache_stats"]["hits"] == 1
    assert result["incremental_cache_stats"]["misses"] == 1


@pytest.mark.parametrize("kwargs", [dict(cells=0), dict(table_rows=-1), dict(repeat=True)])
def test_incremental_input_bounds(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        incremental.run_suite(**kwargs)


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_upstream_root_aliases_have_identical_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, separator: str
) -> None:
    original = tmp_path / "RUNNER~1" / "case"
    resolved = tmp_path / "runner admin" / "case"
    monkeypatch.setattr(Path, "resolve", lambda self: resolved)
    raw = str(original).replace("\\", "/").replace("/", separator)
    canonical = str(resolved).replace("\\", "/").replace("/", separator)
    payload = {
        "source": canonical + separator + "cell.v",
        "evidence": [raw + separator + "cell.lef", canonical, 3, None],
        "similar_directory": canonical + "-other" + separator + "keep.v",
        "expression": r"\escaped.name & A",
    }
    expected = {
        "source": "$CASE/cell.v",
        "evidence": ["$CASE/cell.lef", "$CASE", 3, None],
        "similar_directory": payload["similar_directory"],
        "expression": payload["expression"],
    }
    assert upstream_cells._normalize(payload, original) == expected


def test_upstream_relative_frontend_paths_are_normalized(tmp_path: Path) -> None:
    # Use a child of cwd so this also works when Windows TEMP is on another drive.
    root = Path.cwd() / "temporary-oracle-case"
    relative = os.path.relpath(root)
    payload = {"location": {"path": relative + "/cell.v", "line": 39, "column": 12}}
    assert upstream_cells._normalize(payload, root) == {
        "location": {"path": "$CASE/cell.v", "line": 39, "column": 12}
    }

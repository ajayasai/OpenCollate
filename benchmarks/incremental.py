"""Measure full-check caching on synthetic multi-view characterization collateral.

All measured paths hash inputs, reconcile and run rules. This is an in-process
comparison with OpenCollate's uncached path, not with a commercial product.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from opencollate.cache import ObservationCache
from opencollate.cli import _load_observations
from opencollate.config import load_config
from opencollate.engine import ComparisonEngine


def _normalize(value: Any, root: Path) -> Any:
    if isinstance(value, str):
        return value.replace(str(root), "$FIXTURE").replace(root.as_posix(), "$FIXTURE")
    if isinstance(value, dict):
        return {key: _normalize(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, root) for item in value]
    return value


def generate(root: Path, cells: int, table_rows: int) -> Path:
    row = '"' + ", ".join("0.012345" for _ in range(10)) + '"'
    table = ",\n".join(row for _ in range(table_rows))
    liberty, physical = ["library(L) {\n"], []
    for index in range(cells):
        name = f"cell_{index:05}"
        liberty.append(
            f"cell({name}) {{\n"
            "pin(A) { direction: input; }\n"
            'pin(Y) { direction: output; function: "A";\n'
            f'timing() {{ related_pin: "A"; cell_rise(table) {{ values({table}); }} }}\n'
            "}\n}\n"
        )
        physical.append(
            f"MACRO {name}\n  PIN A\n    DIRECTION INPUT ;\n    USE SIGNAL ;\n  END A\n"
            f"  PIN Y\n    DIRECTION OUTPUT ;\n    USE SIGNAL ;\n  END Y\nEND {name}\n"
        )
    liberty.append("}\n")
    (root / "cells.lib").write_text("".join(liberty), encoding="utf-8")
    (root / "cells.lef").write_text("".join(physical), encoding="utf-8")
    config = root / "opencollate.toml"
    config.write_text(
        '[project]\nname = "incremental-synthetic"\n'
        '[sources.liberty.tt]\nfiles = ["cells.lib"]\n'
        '[sources.lef.abstract]\nfiles = ["cells.lef"]\n'
        "[policy]\nstrict_inventory = true\ncompare_functions = false\n",
        encoding="utf-8",
    )
    return config


def run_suite(cells: int = 128, table_rows: int = 256, repeat: int = 3) -> dict[str, Any]:
    for name, value, maximum in (
        ("cells", cells, 512),
        ("table_rows", table_rows, 1024),
        ("repeat", repeat, 10),
    ):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    with tempfile.TemporaryDirectory(prefix="opencollate-incremental-") as directory:
        root = Path(directory)
        config = load_config(generate(root, cells, table_rows))
        total_bytes = sum((root / path).stat().st_size for path in ("cells.lib", "cells.lef"))

        def check(cache_dir: Path | None) -> tuple[str, float, dict[str, int]]:
            start = time.perf_counter()
            cache = ObservationCache(cache_dir) if cache_dir else None
            observations = _load_observations(config, cache=cache)
            result = ComparisonEngine(config).run(observations)
            text = json.dumps(_normalize(result.to_dict(), root), sort_keys=True)
            elapsed = time.perf_counter() - start
            return text, elapsed, cache.stats() if cache else {}

        baseline, _, _ = check(None)  # Untimed parser import/initialization warmup.
        decoded = json.loads(baseline)
        if decoded["exit_code"] != 0 or decoded["diagnostics"]:
            raise ValueError("synthetic clean fixture is not clean")
        if decoded["summary"]["components"] != cells or decoded["summary"]["ports"] != cells * 2:
            raise ValueError("synthetic fixture inventory differs from its oracle")
        samples: dict[str, list[float]] = {"uncached": [], "cold_cache": [], "warm_cache": []}
        exact = True
        hits = []
        for sample in range(repeat):
            # Every iteration measures the same files, freshly constructed cache object,
            # and full rule run. Cache setup/hashing/decoding are in the timed interval.
            cache_dir = root / f"cache-{sample}"
            for mode, path in (
                ("uncached", None),
                ("cold_cache", cache_dir),
                ("warm_cache", cache_dir),
            ):
                output, elapsed, stats = check(path)
                exact = exact and output == baseline
                samples[mode].append(elapsed)
                if mode == "warm_cache":
                    hits.append(stats)
        # Change one real interface direction, not an ignored timing-table value.
        target = root / "cells.lib"
        content = target.read_text(encoding="utf-8")
        content = content.replace(
            "pin(A) { direction: input; }", "pin(A) { direction: output; }", 1
        )
        target.write_text(content, encoding="utf-8")
        fresh_mutant, _, _ = check(None)
        cached_mutant, incremental_time, incremental_stats = check(root / f"cache-{repeat - 1}")
        mutant = json.loads(fresh_mutant)
        changed_exact = cached_mutant == fresh_mutant
        mutation_detected = (
            mutant["exit_code"] == 1
            and [x["code"] for x in mutant["diagnostics"]] == ["OC4001"]
            and incremental_stats["hits"] == 1
            and incremental_stats["misses"] == 1
        )
        medians = {name: statistics.median(values) for name, values in samples.items()}
        stable = {
            "cells": cells,
            "pins": 2 * cells,
            "views": 2,
            "all_reports_identical": exact,
            "mutant_matches_fresh_check": changed_exact,
            "single_file_mutation_detected": mutation_detected,
            "all_warm_views_reused": all(item["hits"] == 2 for item in hits),
            "baseline_report_sha256": hashlib.sha256(baseline.encode()).hexdigest(),
            "mutant_report_sha256": hashlib.sha256(fresh_mutant.encode()).hexdigest(),
        }
        passed = exact and changed_exact and mutation_detected and stable["all_warm_views_reused"]
        return {
            "schema_version": 1,
            "suite": "opencollate-incremental-full-check",
            "status": "pass" if passed else "fail",
            "scope": "synthetic Liberty/LEF corpus with skipped characterization tables; "
            "in-process full-check timings, not commercial or production-SoC comparison",
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "processor": platform.processor(),
            },
            "workload": {
                "cells": cells,
                "table_rows_per_cell": table_rows,
                "source_bytes": total_bytes,
                "repeat": repeat,
            },
            "verification": stable,
            "seconds": {
                "samples": samples,
                "medians": medians,
                "one_file_changed": incremental_time,
            },
            "warm_vs_uncached_speedup": medians["uncached"] / medians["warm_cache"],
            "incremental_cache_stats": incremental_stats,
            "result_sha256": hashlib.sha256(
                json.dumps(stable, sort_keys=True).encode()
            ).hexdigest(),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cells", type=int, default=128)
    parser.add_argument("--table-rows", type=int, default=256)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_suite(args.cells, args.table_rows, args.repeat)
        text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if args.json_output:
            args.json_output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

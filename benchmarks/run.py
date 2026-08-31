"""Run reproducible OpenCollate conformance and scalability benchmarks.

The runner deliberately depends only on the Python standard library plus the
installed OpenCollate package.  Result payloads are serialized with stable key
ordering and contain no wall-clock timestamp.  Elapsed samples remain
machine-dependent measurements; deterministic result digests and verification
summaries are reported separately.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import platform
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencollate import __version__
from opencollate.baseline import diff_reports
from opencollate.cli import main as opencollate_main
from opencollate.config import ProjectConfig, SourceConfig
from opencollate.engine import ComparisonEngine
from opencollate.model import ViewId
from opencollate.parsers.connectivity import parse_connectivity_csv
from opencollate.parsers.systemrdl import parse_systemrdl
from opencollate.parsers.verilog import parse_verilog

SUITE_NAME = "opencollate-public-benchmarks"
SCHEMA_VERSION = 1
CASE_NAMES = ("report-diff", "systemrdl-import", "rtl-connectivity", "uart-check")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

JsonObject = dict[str, Any]
Operation = Callable[[], JsonObject]
Verifier = Callable[[Mapping[str, Any]], JsonObject]
Timer = Callable[[], float]


class ConformanceFailure(RuntimeError):
    """A benchmark produced an incorrect or nondeterministic result."""


@dataclass(frozen=True, slots=True)
class CaseSpec:
    """One measured operation with an independent correctness oracle."""

    name: str
    description: str
    workload: JsonObject
    unit_name: str
    units_per_run: int
    budget_seconds: float
    operation: Operation
    verifier: Verifier


@dataclass(frozen=True, slots=True)
class DiffWorkload:
    """Synthetic multiset reports and their independently derived oracle."""

    baseline: JsonObject
    current: JsonObject
    reversed_baseline: JsonObject
    reversed_current: JsonObject
    expected_summary: JsonObject
    groups: int

    @property
    def finding_occurrences(self) -> int:
        return len(self.baseline["diagnostics"]) + len(self.current["diagnostics"])


def _canonical_json(value: object, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=True,
    )


def _result_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _finding(fingerprint: str, group: int, variant: int) -> JsonObject:
    return {
        "code": "OC4101",
        "severity": "error",
        "message": "Synthetic port-width disagreement.",
        "fingerprint": fingerprint,
        "waived": False,
        "evidence": [
            {
                "view": "rtl.synthetic",
                "value": {"group": group, "variant": variant},
            }
        ],
    }


def _report(findings: Sequence[JsonObject]) -> JsonObject:
    return {
        "schema_version": 1,
        "tool": {"name": "OpenCollate", "version": __version__},
        "project": "benchmark-report-diff",
        "status": "fail" if findings else "pass",
        "exit_code": 1 if findings else 0,
        "summary": {
            "errors": len(findings),
            "warnings": 0,
            "notes": 0,
            "suppressed": 0,
            "views": 1,
            "components": 1,
            "ports": 1,
            "registers": 0,
        },
        "diagnostics": list(findings),
    }


def _adversarial_order(findings: Sequence[JsonObject]) -> list[JsonObject]:
    """Interleave ascending and descending halves without using randomness."""

    return [*findings[::2], *reversed(findings[1::2])]


def build_diff_workload(groups: int) -> DiffWorkload:
    """Build duplicate-fingerprint reports with every diff state represented."""

    if groups < 4:
        raise ValueError("report-diff groups must be at least 4")

    baseline: list[JsonObject] = []
    current: list[JsonObject] = []
    expected = {
        "baseline": 0,
        "current": 0,
        "new": 0,
        "changed": 0,
        "unchanged": 0,
        "resolved": 0,
        "current_active": 0,
        "current_suppressed": 0,
        "current_fatal": 0,
        "current_active_fatal": 0,
    }

    for group in range(groups):
        fingerprint = f"duplicate-{group:08d}"
        old_group = [_finding(fingerprint, group, variant) for variant in range(4)]
        mode = group % 4
        if mode == 0:
            new_group = [_finding(fingerprint, group, variant) for variant in range(4)]
            expected["unchanged"] += 4
        elif mode == 1:
            new_group = [_finding(fingerprint, group, variant) for variant in (0, 1, 2, 30)]
            expected["unchanged"] += 3
            expected["changed"] += 1
        elif mode == 2:
            new_group = [_finding(fingerprint, group, variant) for variant in range(3)]
            expected["unchanged"] += 3
            expected["resolved"] += 1
        else:
            new_group = [_finding(fingerprint, group, variant) for variant in range(5)]
            expected["unchanged"] += 4
            expected["new"] += 1
        baseline.extend(old_group)
        current.extend(new_group)

    expected["baseline"] = len(baseline)
    expected["current"] = len(current)
    expected["current_active"] = len(current)
    ordered_baseline = _adversarial_order(baseline)
    ordered_current = _adversarial_order(current)
    return DiffWorkload(
        baseline=_report(ordered_baseline),
        current=_report(ordered_current),
        reversed_baseline=_report(tuple(reversed(ordered_baseline))),
        reversed_current=_report(tuple(reversed(ordered_current))),
        expected_summary=expected,
        groups=groups,
    )


def _diff_operation(workload: DiffWorkload) -> JsonObject:
    return diff_reports(workload.baseline, workload.current).to_dict()


def _diff_verifier(workload: DiffWorkload) -> Verifier:
    def verify(result: Mapping[str, Any]) -> JsonObject:
        summary = result.get("summary")
        if summary != workload.expected_summary:
            raise ConformanceFailure(
                "report-diff summary disagrees with the independently derived oracle"
            )
        reverse = diff_reports(workload.reversed_baseline, workload.reversed_current).to_dict()
        if result != reverse:
            raise ConformanceFailure("report-diff output changes when input order is reversed")
        states = {str(item.get("state")) for item in result.get("findings", ())}
        if states != {"new", "changed", "unchanged", "resolved"}:
            raise ConformanceFailure(f"report-diff did not exercise every state: {sorted(states)}")
        return {
            "input_order_invariant": True,
            "states_exercised": sorted(states),
            "summary": dict(summary),
        }

    return verify


def _normalized_path(value: str, root: Path) -> str:
    normalized = value.replace("\\", "/")
    root_text = str(root.resolve()).replace("\\", "/").rstrip("/")
    prefix = f"{root_text}/"
    if normalized.casefold().startswith(prefix.casefold()):
        normalized = normalized[len(prefix) :]
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _normalize_report(value: Any, root: Path, *, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {
            str(item_key): _normalize_report(item, root, key=str(item_key))
            for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_normalize_report(item, root) for item in value]
    if key == "path" and isinstance(value, str):
        return _normalized_path(value, root)
    return value


def _uart_operation(root: Path) -> JsonObject:
    config = root / "examples" / "uart" / "opencollate.toml"
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = opencollate_main(["check", str(config), "--format", "json"])
    if stderr.getvalue():
        raise ConformanceFailure(f"UART check wrote to stderr: {stderr.getvalue().strip()}")
    try:
        report = json.loads(stdout.getvalue())
    except json.JSONDecodeError as error:
        raise ConformanceFailure(f"UART check did not emit valid JSON: {error}") from error
    return {
        "exit_code": exit_code,
        "report": _normalize_report(report, root),
    }


def _verify_uart(result: Mapping[str, Any]) -> JsonObject:
    if result.get("exit_code") != 1:
        raise ConformanceFailure(f"UART check returned {result.get('exit_code')!r}, expected 1")
    report = result.get("report")
    if not isinstance(report, Mapping):
        raise ConformanceFailure("UART check result is missing its report object")
    summary = report.get("summary")
    diagnostics = report.get("diagnostics")
    if not isinstance(summary, Mapping) or not isinstance(diagnostics, list):
        raise ConformanceFailure("UART report is missing summary or diagnostic data")
    codes = [str(item.get("code")) for item in diagnostics if isinstance(item, Mapping)]
    expected_codes = ["OC4001", "OC4301", "OC5003"]
    if codes != expected_codes:
        raise ConformanceFailure(
            f"UART diagnostic codes are {codes!r}, expected {expected_codes!r}"
        )
    if report.get("schema_version") != 1:
        raise ConformanceFailure("UART report schema_version is not 1")
    if summary.get("components") != 1 or summary.get("views") != 13:
        raise ConformanceFailure("UART inventory summary is incomplete")
    return {
        "diagnostic_codes": codes,
        "report_schema_version": 1,
        "summary": dict(summary),
    }


def _systemrdl_operation(root: Path) -> JsonObject:
    source = root / "benchmarks" / "fixtures" / "systemrdl" / "registers.rdl"
    view = parse_systemrdl(
        source,
        view_id="systemrdl.benchmark",
        top="benchmark_regs",
        component_name="uart0",
    )
    return {
        "view": str(view.view),
        "complete": view.complete,
        "diagnostic_codes": [item.code for item in view.diagnostics],
        "selected_top": view.attributes.get("selected_top"),
        "component_name": view.attributes.get("component_name"),
        "registers": [
            {
                "name": register.native_name,
                "component": register.component,
                "memory_map": register.memory_map,
                "address_block": register.address_block,
                "address_offset": register.address_offset,
                "absolute_address": register.absolute_address,
                "local_address_offset": register.attributes.get("local_address_offset"),
                "address_block_absolute_address": register.attributes.get(
                    "address_block_absolute_address"
                ),
                "register_files": register.attributes.get("register_files"),
                "size_bits": register.size_bits,
                "access": register.access,
                "status": register.status.value,
                "systemrdl_path": register.attributes.get("systemrdl_path"),
                "array_indices": register.attributes.get("array_indices"),
                "fields": [
                    {
                        "name": field.native_name,
                        "bit_offset": field.bit_offset,
                        "bit_width": field.bit_width,
                        "access": field.access,
                        "reset_value": field.reset_value,
                        "status": field.status.value,
                    }
                    for field in register.fields
                ],
            }
            for register in view.registers
        ],
    }


def _expected_systemrdl_result() -> JsonObject:
    value_field = {
        "name": "VALUE",
        "bit_offset": 0,
        "bit_width": 8,
        "access": "read-write",
        "reset_value": 0x5A,
        "status": "known",
    }
    return {
        "view": "systemrdl.benchmark",
        "complete": True,
        "diagnostic_codes": [],
        "selected_top": "benchmark_regs",
        "component_name": "uart0",
        "registers": [
            {
                "name": "CTRL",
                "component": "uart0",
                "memory_map": "benchmark_regs",
                "address_block": "benchmark_regs",
                "address_offset": 0,
                "absolute_address": 0,
                "local_address_offset": 0,
                "address_block_absolute_address": 0,
                "register_files": [],
                "size_bits": 32,
                "access": None,
                "status": "known",
                "systemrdl_path": "benchmark_regs/CTRL",
                "array_indices": None,
                "fields": [
                    {
                        "name": "ENABLE",
                        "bit_offset": 0,
                        "bit_width": 1,
                        "access": "read-write",
                        "reset_value": 0,
                        "status": "known",
                    },
                    {
                        "name": "READY",
                        "bit_offset": 1,
                        "bit_width": 1,
                        "access": "read-only",
                        "reset_value": 1,
                        "status": "known",
                    },
                ],
            },
            {
                "name": "DATA[0]",
                "component": "uart0",
                "memory_map": "benchmark_regs",
                "address_block": "benchmark_regs",
                "address_offset": 0x110,
                "absolute_address": 0x110,
                "local_address_offset": 0x10,
                "address_block_absolute_address": 0,
                "register_files": ["channel"],
                "size_bits": 16,
                "access": "read-write",
                "status": "known",
                "systemrdl_path": "benchmark_regs/channel/DATA[0]",
                "array_indices": [0],
                "fields": [dict(value_field)],
            },
            {
                "name": "DATA[1]",
                "component": "uart0",
                "memory_map": "benchmark_regs",
                "address_block": "benchmark_regs",
                "address_offset": 0x114,
                "absolute_address": 0x114,
                "local_address_offset": 0x14,
                "address_block_absolute_address": 0,
                "register_files": ["channel"],
                "size_bits": 16,
                "access": "read-write",
                "status": "known",
                "systemrdl_path": "benchmark_regs/channel/DATA[1]",
                "array_indices": [1],
                "fields": [dict(value_field)],
            },
        ],
    }


def _verify_systemrdl(result: Mapping[str, Any]) -> JsonObject:
    expected = _expected_systemrdl_result()
    if result != expected:
        raise ConformanceFailure(
            "SystemRDL import disagrees with the exact register/address oracle "
            f"(actual SHA-256 {_result_digest(result)})"
        )
    return {
        "selected_top": "benchmark_regs",
        "register_names": ["CTRL", "DATA[0]", "DATA[1]"],
        "absolute_addresses": [0, 0x110, 0x114],
        "field_layouts": {
            "CTRL": [["ENABLE", 0, 1], ["READY", 1, 1]],
            "DATA[0]": [["VALUE", 0, 8]],
            "DATA[1]": [["VALUE", 0, 8]],
        },
    }


def _connectivity_operation(root: Path) -> JsonObject:
    fixture = root / "benchmarks" / "fixtures" / "connectivity"
    rtl_path = fixture / "top.sv"
    intent_path = fixture / "requirements.csv"
    rtl_view = ViewId("rtl", "benchmark")
    intent_view = ViewId("connectivity", "benchmark")
    rtl = parse_verilog(rtl_path, view_id=rtl_view, top="top")
    intent = parse_connectivity_csv(intent_path, view_id=intent_view)
    config = ProjectConfig(
        path=fixture / "benchmark.toml",
        root=fixture,
        name="connectivity-benchmark",
        sources=(
            SourceConfig(rtl_view, (rtl_path,)),
            SourceConfig(intent_view, (intent_path,)),
        ),
    )
    checked = ComparisonEngine(config).run((rtl, intent))

    edges: list[JsonObject] = []
    for edge in rtl.connectivity_edges:
        serialized = edge.to_dict()
        reason = edge.attributes.get("reason")
        if reason is not None:
            serialized["reason"] = reason
        edges.append(serialized)
    diagnostics: list[JsonObject] = []
    for diagnostic in checked.diagnostics:
        diagnostics.append(
            {
                "code": diagnostic.code,
                "severity": diagnostic.severity.value,
                "requirement": diagnostic.object.display if diagnostic.object is not None else None,
                "witness_path": diagnostic.metadata.get("witness_path"),
                "frontier": diagnostic.metadata.get("frontier"),
            }
        )
    return {
        "rtl": {
            "complete": rtl.complete,
            "connectivity_complete": rtl.attributes.get("connectivity_complete"),
            "diagnostic_codes": [item.code for item in rtl.diagnostics],
            "endpoints": [item.to_dict() for item in rtl.connectivity_endpoints],
            "edges": edges,
        },
        "intent": {
            "complete": intent.complete,
            "diagnostic_codes": [item.code for item in intent.diagnostics],
            "requirements": [item.to_dict() for item in intent.connectivity_requirements],
        },
        "result": {
            "exit_code": checked.exit_code,
            "diagnostics": diagnostics,
        },
    }


def _expected_connectivity_result() -> JsonObject:
    reason = "BinaryExpression is outside the transparent expression subset"

    def endpoint(name: str) -> JsonObject:
        return {
            "name": name,
            "bit_index": None,
            "ordinal": 0,
            "width": 1,
            "key": name,
            "status": "known",
        }

    return {
        "rtl": {
            "complete": True,
            "connectivity_complete": True,
            "diagnostic_codes": [],
            "endpoints": [
                endpoint("top/a"),
                endpoint("top/middle"),
                endpoint("top/selected"),
                endpoint("top/y"),
            ],
            "edges": [
                {
                    "source": "top/a",
                    "sink": "top/middle",
                    "kind": "assign",
                    "inverted": False,
                    "status": "known",
                },
                {
                    "source": "top/a",
                    "sink": "top/selected",
                    "kind": "unsupported_assign",
                    "inverted": None,
                    "status": "tainted",
                    "reason": reason,
                },
                {
                    "source": "top/middle",
                    "sink": "top/y",
                    "kind": "assign",
                    "inverted": False,
                    "status": "known",
                },
                {
                    "source": "top/y",
                    "sink": "top/selected",
                    "kind": "unsupported_assign",
                    "inverted": None,
                    "status": "tainted",
                    "reason": reason,
                },
            ],
        },
        "intent": {
            "complete": True,
            "diagnostic_codes": [],
            "requirements": [
                {
                    "id": "REQUIRED_PASS",
                    "source": "top/a",
                    "sink": "top/y",
                    "expectation": "reachable",
                    "transform": "identity",
                    "through": [],
                    "exclude": [],
                    "description": "Known two-edge path must pass",
                    "status": "known",
                },
                {
                    "id": "FORBIDDEN_WITNESS",
                    "source": "top/middle",
                    "sink": "top/y",
                    "expectation": "unreachable",
                    "transform": "any",
                    "through": [],
                    "exclude": [],
                    "description": "Known path must produce a witness",
                    "status": "known",
                },
                {
                    "id": "TAINTED_FRONTIER",
                    "source": "top/a",
                    "sink": "top/selected",
                    "expectation": "reachable",
                    "transform": "any",
                    "through": [],
                    "exclude": [],
                    "description": "Unsupported binary logic must be inconclusive",
                    "status": "known",
                },
            ],
        },
        "result": {
            "exit_code": 1,
            "diagnostics": [
                {
                    "code": "OC6504",
                    "severity": "error",
                    "requirement": "FORBIDDEN_WITNESS",
                    "witness_path": [
                        {
                            "source": "top/middle",
                            "sink": "top/y",
                            "kind": "assign",
                            "inverted": False,
                            "status": "known",
                        }
                    ],
                    "frontier": None,
                },
                {
                    "code": "OC6505",
                    "severity": "warning",
                    "requirement": "TAINTED_FRONTIER",
                    "witness_path": None,
                    "frontier": {
                        "source": "top/a",
                        "sink": "top/selected",
                        "kind": "unsupported_assign",
                        "inverted": None,
                        "status": "tainted",
                    },
                },
            ],
        },
    }


def _verify_connectivity(result: Mapping[str, Any]) -> JsonObject:
    expected = _expected_connectivity_result()
    if result != expected:
        raise ConformanceFailure(
            "bounded RTL connectivity disagrees with the exact graph/evidence oracle "
            f"(actual SHA-256 {_result_digest(result)})"
        )
    diagnostics = expected["result"]["diagnostics"]
    diagnosed_requirements = [item["requirement"] for item in diagnostics]
    if "REQUIRED_PASS" in diagnosed_requirements:
        raise ConformanceFailure("known required connectivity unexpectedly produced a diagnostic")
    return {
        "required_pass": True,
        "forbidden_witness": diagnostics[0]["witness_path"],
        "tainted_frontier": diagnostics[1]["frontier"],
    }


def _case_specs(
    *,
    selected: Sequence[str],
    root: Path,
    diff_groups: int,
    diff_budget_seconds: float,
    uart_budget_seconds: float,
    systemrdl_budget_seconds: float,
    connectivity_budget_seconds: float,
) -> tuple[CaseSpec, ...]:
    unknown = sorted(set(selected) - set(CASE_NAMES))
    if unknown:
        raise ValueError(f"unknown benchmark case(s): {', '.join(unknown)}")
    selected_set = set(selected)
    workload = build_diff_workload(diff_groups) if "report-diff" in selected_set else None
    specs: list[CaseSpec] = []
    if workload is not None:
        specs.append(
            CaseSpec(
                name="report-diff",
                description="Adversarial duplicate-fingerprint report multiset comparison",
                workload={
                    "groups": workload.groups,
                    "baseline_findings": len(workload.baseline["diagnostics"]),
                    "current_findings": len(workload.current["diagnostics"]),
                },
                unit_name="finding_occurrences",
                units_per_run=workload.finding_occurrences,
                budget_seconds=diff_budget_seconds,
                operation=lambda: _diff_operation(workload),
                verifier=_diff_verifier(workload),
            )
        )
    if "systemrdl-import" in selected_set:
        specs.append(
            CaseSpec(
                name="systemrdl-import",
                description="SystemRDL top, hierarchy, register array, field, and address import",
                workload={"source": "benchmarks/fixtures/systemrdl/registers.rdl"},
                unit_name="imports",
                units_per_run=1,
                budget_seconds=systemrdl_budget_seconds,
                operation=lambda: _systemrdl_operation(root),
                verifier=_verify_systemrdl,
            )
        )
    if "rtl-connectivity" in selected_set:
        specs.append(
            CaseSpec(
                name="rtl-connectivity",
                description="Bounded static RTL graph extraction and connectivity checking",
                workload={
                    "rtl": "benchmarks/fixtures/connectivity/top.sv",
                    "intent": "benchmarks/fixtures/connectivity/requirements.csv",
                    "requirements": 3,
                },
                unit_name="checks",
                units_per_run=3,
                budget_seconds=connectivity_budget_seconds,
                operation=lambda: _connectivity_operation(root),
                verifier=_verify_connectivity,
            )
        )
    if "uart-check" in selected_set:
        specs.append(
            CaseSpec(
                name="uart-check",
                description="Complete public UART example parse, reconcile, check, and JSON render",
                workload={"configuration": "examples/uart/opencollate.toml"},
                unit_name="checks",
                units_per_run=1,
                budget_seconds=uart_budget_seconds,
                operation=lambda: _uart_operation(root),
                verifier=_verify_uart,
            )
        )
    return tuple(specs)


def _round_seconds(value: float) -> float:
    return float(f"{value:.9f}")


def _measure_case(
    spec: CaseSpec,
    *,
    repeat: int,
    warmup: int,
    enforce_budgets: bool,
    timer: Timer,
) -> JsonObject:
    base: JsonObject = {
        "name": spec.name,
        "description": spec.description,
        "workload": spec.workload,
        "unit_name": spec.unit_name,
        "units_per_run": spec.units_per_run,
    }
    try:
        reference = spec.operation()
        verification = spec.verifier(reference)
        digest = _result_digest(reference)
        for _ in range(warmup):
            if _result_digest(spec.operation()) != digest:
                raise ConformanceFailure("warm-up result digest was nondeterministic")

        samples: list[float] = []
        for _ in range(repeat):
            started = timer()
            result = spec.operation()
            elapsed = timer() - started
            if not math.isfinite(elapsed) or elapsed < 0:
                raise ConformanceFailure("benchmark timer returned an invalid elapsed value")
            if _result_digest(result) != digest:
                raise ConformanceFailure("measured result digest was nondeterministic")
            samples.append(elapsed)

        median = statistics.median(samples)
        within_budget = median <= spec.budget_seconds
        rate = spec.units_per_run / median if median > 0 else None
        if rate is not None and not math.isfinite(rate):
            raise ConformanceFailure("benchmark throughput is not finite")
        base.update(
            {
                "status": "pass" if within_budget or not enforce_budgets else "fail",
                "conformance_status": "pass",
                "budget_status": "pass" if within_budget else "fail",
                "result_sha256": digest,
                "verification": verification,
                "timing": {
                    "samples_seconds": [_round_seconds(item) for item in samples],
                    "minimum_seconds": _round_seconds(min(samples)),
                    "median_seconds": _round_seconds(median),
                    "maximum_seconds": _round_seconds(max(samples)),
                    "units_per_second": None if rate is None else float(f"{rate:.3f}"),
                },
                "budget": {
                    "enforced": enforce_budgets,
                    "maximum_median_seconds": spec.budget_seconds,
                    "within_budget": within_budget,
                },
            }
        )
    except Exception as error:
        message = str(error).strip() or "case failed without a diagnostic message"
        base.update(
            {
                "status": "fail",
                "conformance_status": "fail",
                "budget_status": "not-run",
                "error": {"type": type(error).__name__, "message": message},
                "budget": {
                    "enforced": enforce_budgets,
                    "maximum_median_seconds": spec.budget_seconds,
                    "within_budget": None,
                },
            }
        )
    return base


def run_suite(
    *,
    cases: Sequence[str] = CASE_NAMES,
    root: Path = REPOSITORY_ROOT,
    repeat: int = 3,
    warmup: int = 1,
    diff_groups: int = 1_000,
    diff_budget_seconds: float = 30.0,
    uart_budget_seconds: float = 30.0,
    systemrdl_budget_seconds: float = 30.0,
    connectivity_budget_seconds: float = 30.0,
    enforce_budgets: bool = False,
    timer: Timer = time.perf_counter,
) -> JsonObject:
    """Run selected cases and return a stable, JSON-serializable result."""

    if not cases:
        raise ValueError("at least one benchmark case must be selected")
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    if warmup < 0:
        raise ValueError("warmup must not be negative")
    if diff_groups < 4:
        raise ValueError("diff_groups must be at least 4")
    budgets = (
        diff_budget_seconds,
        uart_budget_seconds,
        systemrdl_budget_seconds,
        connectivity_budget_seconds,
    )
    if any(not math.isfinite(item) or item <= 0 for item in budgets):
        raise ValueError("benchmark budgets must be positive")

    specs = _case_specs(
        selected=cases,
        root=root.resolve(),
        diff_groups=diff_groups,
        diff_budget_seconds=diff_budget_seconds,
        uart_budget_seconds=uart_budget_seconds,
        systemrdl_budget_seconds=systemrdl_budget_seconds,
        connectivity_budget_seconds=connectivity_budget_seconds,
    )
    results = [
        _measure_case(
            spec,
            repeat=repeat,
            warmup=warmup,
            enforce_budgets=enforce_budgets,
            timer=timer,
        )
        for spec in specs
    ]
    conformance_status = (
        "pass" if all(item["conformance_status"] == "pass" for item in results) else "fail"
    )
    budget_status = "pass" if all(item["budget_status"] == "pass" for item in results) else "fail"
    status = (
        "pass"
        if conformance_status == "pass" and (budget_status == "pass" or not enforce_budgets)
        else "fail"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": SUITE_NAME,
        "status": status,
        "conformance_status": conformance_status,
        "budget_status": budget_status,
        "environment": {
            "opencollate_version": __version__,
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "parameters": {
            "repeat": repeat,
            "warmup": warmup,
            "diff_groups": diff_groups,
            "enforce_budgets": enforce_budgets,
        },
        "cases": results,
    }


def render_json(report: Mapping[str, Any]) -> str:
    """Render stable machine-readable output."""

    return _canonical_json(report, pretty=True) + "\n"


def render_text(report: Mapping[str, Any]) -> str:
    """Render a concise human-readable summary."""

    lines = [f"OpenCollate public benchmark suite: {str(report['status']).upper()}"]
    for case in report["cases"]:
        if case["conformance_status"] != "pass":
            error = case.get("error", {})
            lines.append(
                f"  {case['name']}: FAIL {error.get('type', 'Error')}: "
                f"{error.get('message', 'unknown conformance failure')}"
            )
            continue
        timing = case["timing"]
        budget = case["budget"]
        lines.append(
            f"  {case['name']}: {str(case['status']).upper()} "
            f"median={timing['median_seconds']:.6f}s "
            f"rate={timing['units_per_second']} {case['unit_name']}/s "
            f"budget<={budget['maximum_median_seconds']:.1f}s "
            f"({str(case['budget_status']).upper()})"
        )
    return "\n".join(lines) + "\n"


def _bounded_int(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return parsed

    return parse


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run public OpenCollate conformance and scalability benchmarks."
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=CASE_NAMES,
        help="case to run; repeat the option to select multiple (default: all)",
    )
    parser.add_argument("--repeat", type=_bounded_int(1, 100), default=3)
    parser.add_argument("--warmup", type=_bounded_int(0, 20), default=1)
    parser.add_argument(
        "--diff-groups",
        type=_bounded_int(4, 100_000),
        default=1_000,
        help="duplicate-fingerprint groups; each baseline group contains four findings",
    )
    parser.add_argument(
        "--diff-budget-seconds",
        type=_positive_float,
        default=30.0,
        help="maximum allowed report-diff median when budgets are enforced",
    )
    parser.add_argument(
        "--uart-budget-seconds",
        type=_positive_float,
        default=30.0,
        help="maximum allowed UART-check median when budgets are enforced",
    )
    parser.add_argument(
        "--systemrdl-budget-seconds",
        type=_positive_float,
        default=30.0,
        help="maximum allowed SystemRDL-import median when budgets are enforced",
    )
    parser.add_argument(
        "--connectivity-budget-seconds",
        type=_positive_float,
        default=30.0,
        help="maximum allowed RTL-connectivity median when budgets are enforced",
    )
    parser.add_argument(
        "--enforce-budgets",
        action="store_true",
        help="return failure when a median exceeds its generous regression ceiling",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="write deterministic machine-readable results to this path",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_suite(
        cases=tuple(args.case or CASE_NAMES),
        root=args.repository_root,
        repeat=args.repeat,
        warmup=args.warmup,
        diff_groups=args.diff_groups,
        diff_budget_seconds=args.diff_budget_seconds,
        uart_budget_seconds=args.uart_budget_seconds,
        systemrdl_budget_seconds=args.systemrdl_budget_seconds,
        connectivity_budget_seconds=args.connectivity_budget_seconds,
        enforce_budgets=args.enforce_budgets,
    )
    sys.stdout.write(render_text(report))
    if args.json_output is not None:
        target = args.json_output.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_json(report), encoding="utf-8", newline="\n")
        sys.stdout.write(f"Machine-readable results: {target}\n")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

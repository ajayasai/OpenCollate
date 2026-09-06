"""Offline interface conformance on pinned, unmodified upstream SkyWater cells.

Two cell-interface fixtures are not production-SoC or signoff qualification.
Mutations are applied to temporary source files, before either parser runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from opencollate.config import (
    AliasRule,
    ContractSettings,
    PolicySettings,
    ProjectConfig,
    SourceConfig,
)
from opencollate.engine import ComparisonEngine
from opencollate.model import ViewId
from opencollate.parsers.lef import parse_lef
from opencollate.parsers.verilog import parse_verilog

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sky130"
COMMIT = "ac7fb61f06e6470b94e8afdf7c25268f62fbd7b1"
# Expected pins are a hand-reviewed oracle, not copied from the parser output.
SIGNALS = {
    "inv": {"A": "input", "Y": "output"},
    "nand2": {"A": "input", "B": "input", "Y": "output"},
}
SHA256 = {
    "cells/inv/sky130_fd_sc_hd__inv.blackbox.v": (
        "007f3160cf5db97243c9aa6e13969c80dad643e68ef639928460654cc2f57721"
    ),
    "cells/inv/sky130_fd_sc_hd__inv_1.lef": (
        "271694f8d1223b72fd14af21c5666b28649c39083d60e9525b208a7fa3cef5ea"
    ),
    "cells/nand2/sky130_fd_sc_hd__nand2.blackbox.v": (
        "41b22ec9ee224b497ee5e01897799623899cbdbdf12facff0bec2eab7227bd71"
    ),
    "cells/nand2/sky130_fd_sc_hd__nand2_1.lef": (
        "f3b99f7946a7e667aa21041a989c0c7b46dfa25596f80b9cea56edfb60363e0e"
    ),
}


def _normalize(value: Any, root: Path) -> Any:
    # Native frontends may canonicalize Windows 8.3 names or symlink aliases,
    # while Python-based parsers retain the spelling supplied by the caller.
    # Some frontends also return paths relative to the working directory.
    # Normalize separators only inside matching paths, leaving HDL text intact.
    roots = [root, root.resolve()]
    for path in tuple(roots):
        try:
            roots.append(Path(os.path.relpath(path)))
        except ValueError:
            # Windows paths on different drives have no relative spelling.
            continue
    spellings = {
        spelling
        for path in roots
        for spelling in (str(path), path.as_posix(), path.as_posix().replace("/", "\\"))
    }
    prefixes = sorted(spellings, key=lambda item: (-len(item), item))

    def visit(item: Any) -> Any:
        if isinstance(item, str):
            for prefix in prefixes:
                if item == prefix:
                    return "$CASE"
                if item.startswith((prefix + "/", prefix + "\\")):
                    return "$CASE/" + item[len(prefix) + 1 :].replace("\\", "/")
            return item
        if isinstance(item, dict):
            return {key: visit(child) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    return visit(value)


def verify_fixtures(root: Path = FIXTURES) -> dict[str, Any]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["commit"] != COMMIT or {x["path"] for x in manifest["files"]} != set(SHA256):
        raise ValueError("upstream fixture manifest differs from the pinned corpus")
    for item in manifest["files"]:
        data = (root / item["path"]).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != SHA256[item["path"]] or digest != item["sha256"] or len(data) != item["bytes"]:
            raise ValueError(f"upstream fixture integrity mismatch: {item['path']}")
        blob = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False
        ).hexdigest()
        if blob != item["git_blob_sha"]:
            raise ValueError("upstream Git blob identity mismatch")
    return manifest


def _case(cell: str, mutation: str, root: Path) -> dict[str, Any]:
    name = f"sky130_fd_sc_hd__{cell}"
    rtl_text = (FIXTURES / f"cells/{cell}/{name}.blackbox.v").read_text(encoding="utf-8")
    lef_text = (FIXTURES / f"cells/{cell}/{name}_1.lef").read_text(encoding="utf-8")
    expected: list[str] = []
    if mutation == "direction":
        if rtl_text.count("output Y;") != 1:
            raise ValueError("direction mutation anchor changed")
        rtl_text = rtl_text.replace("output Y;", "input  Y;")
        expected = ["OC4001"]
    elif mutation == "width":
        if rtl_text.count("input  A;") != 1:
            raise ValueError("width mutation anchor changed")
        rtl_text = rtl_text.replace("input  A;", "input [1:0] A;")
        expected = ["OC4101"]
    elif mutation == "missing-pin":
        lef_text, count = re.subn(r"  PIN A\n.*?  END A\n", "", lef_text, flags=re.DOTALL)
        if count != 1:
            raise ValueError("missing-pin mutation anchor changed")
        expected = ["OC3101"]
    elif mutation != "control":
        raise ValueError("unknown mutation")
    rtl_path, lef_path = root / "cell.v", root / "cell.lef"
    rtl_path.write_text(rtl_text, encoding="utf-8")
    lef_path.write_text(lef_text, encoding="utf-8")
    rtl = parse_verilog(rtl_path, view_id=ViewId("rtl"))
    lef = parse_lef(lef_path, view_id=ViewId("lef"))
    config = ProjectConfig(
        root=root,
        path=root / "opencollate.toml",
        name=f"sky130-{cell}",
        sources=(
            SourceConfig(ViewId("rtl"), (rtl_path,)),
            SourceConfig(ViewId("lef"), (lef_path,)),
        ),
        aliases=(AliasRule("component", name, "lef", name + "_1"),),
        contract=ContractSettings(baseline=ViewId("rtl")),
        policy=PolicySettings(
            strict_inventory=True, rtl_power_pins="optional", compare_functions=False
        ),
    )
    engine = ComparisonEngine(config)
    first = engine.run((rtl, lef))
    second = engine.run((lef, rtl))
    observed = [x.code for x in first.diagnostics]
    deterministic = first.to_dict() == second.to_dict()
    inventory_valid = True
    if mutation == "control":
        rtl_pins = {p.native_name: p.direction.value for c in rtl.components for p in c.ports}
        lef_pins = {p.native_name: p.direction.value for c in lef.components for p in c.ports}
        roles = {p.native_name: p.role.value for c in lef.components for p in c.ports}
        inventory_valid = (
            rtl_pins == SIGNALS[cell]
            and lef_pins == {**SIGNALS[cell], "VGND": "inout", "VPWR": "inout"}
            and roles["VPWR"] == "power"
            and roles["VGND"] == "ground"
            and rtl.complete
            and lef.complete
            and all(p.shape.width == 1 for c in rtl.components for p in c.ports)
        )
    expected_exit = 0 if mutation == "control" else 1
    passed = (
        observed == expected
        and first.exit_code == expected_exit
        and deterministic
        and inventory_valid
    )
    # Normalize only the temporary root, retaining complete source-backed diagnostics.
    evidence = _normalize(first.to_dict(), root)
    return {
        "cell": cell,
        "mutation": mutation,
        "expected_codes": expected,
        "observed_codes": observed,
        "status": "pass" if passed else "fail",
        "independent_inventory_oracle": inventory_valid if mutation == "control" else None,
        "deterministic": deterministic,
        "report": evidence,
    }


def run_suite() -> dict[str, Any]:
    manifest = verify_fixtures()
    cases = []
    with tempfile.TemporaryDirectory(prefix="opencollate-upstream-") as directory:
        for cell in SIGNALS:
            for mutation in ("control", "direction", "width", "missing-pin"):
                cases.append(_case(cell, mutation, Path(directory).resolve()))
    return {
        "schema_version": 1,
        "suite": "opencollate-pinned-upstream-cell-interfaces",
        "scope": "two unmodified cell-interface controls and six source-level mutations; "
        "not functional, geometric, foundry, SoC or commercial qualification",
        "status": "pass" if all(c["status"] == "pass" for c in cases) else "fail",
        "provenance": manifest,
        "result_sha256": hashlib.sha256(
            json.dumps(cases, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_suite()
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.json_output:
            args.json_output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

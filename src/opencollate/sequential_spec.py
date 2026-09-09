"""Strict, bounded, declarative source-linked synchronous property requests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opencollate.sequential_ir import Circuit, SequentialError
from opencollate.sequential_rtl import IDENTIFIER, SIGNAL_PATH
from opencollate.sequential_sources import normalize_preprocess

SEMANTICS = "two-valued-synchronous"


def integer(value: Any, lo: int, hi: int, label: str) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise SequentialError(f"{label} must be an integer in {lo}..{hi}")
    return value


def name(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 256 or not IDENTIFIER.fullmatch(value):
        raise SequentialError(f"{label} must be a simple ASCII identifier")
    return value


def signal_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 256 or not SIGNAL_PATH.fullmatch(value):
        raise SequentialError(f"{label} must be a top-relative elaborated ASCII signal path")
    return value


def obj(value: Any, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - required - optional or required - set(value):
        raise SequentialError(f"expected fields {sorted(required)}; optional {sorted(optional)}")
    return value


def read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = stream.read(8 * 1048576 + 1)
    if len(data) > 8 * 1048576:
        raise SequentialError("sequential JSON exceeds 8 MiB")
    text = data.decode("utf-8")
    # Check depth before json.loads, ignoring braces inside quoted strings.
    depth, quoted, escaped = 0, False, False
    for ch in text:
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch in "[{":
            depth += 1
            if depth > 64:
                raise SequentialError("sequential JSON exceeds 64 nesting levels")
        elif ch in "]}":
            depth -= 1

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, val in items:
            if key in result:
                raise SequentialError(f"duplicate JSON key: {key}")
            result[key] = val
        return result

    def nonfinite(value: str) -> Any:
        raise SequentialError(f"non-finite JSON value: {value}")

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    if not isinstance(value, dict):
        raise SequentialError("request/receipt must be a JSON object")
    return value


def normalize(value: Any) -> dict[str, Any]:
    r = obj(
        value,
        {"schema_version", "semantics", "files", "top", "clock", "properties"},
        {"reset", "assumptions", "depth", "induction", "preprocess"},
    )
    integer(r["schema_version"], 1, 1, "schema_version")
    if r["semantics"] != SEMANTICS:
        raise SequentialError("semantics must explicitly be two-valued-synchronous")
    files = r["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 32:
        raise SequentialError("files must be an explicit array of 1..32 source paths")
    for f in files:
        if (
            not isinstance(f, str)
            or not f
            or len(f) > 1024
            or "\x00" in f
            or "\\" in f
            or Path(f).is_absolute()
            or ".." in Path(f).parts
        ):
            raise SequentialError("files must be relative POSIX paths without '..'")
    if len(set(files)) != len(files):
        raise SequentialError("duplicate source path")
    reset = None
    if r.get("reset") is not None:
        q = obj(r["reset"], {"signal", "active"}, {"cycles"})
        reset = {
            "signal": name(q["signal"], "reset signal"),
            "active": integer(q["active"], 0, 1, "reset active"),
            "cycles": integer(q.get("cycles", 1), 1, 16, "reset cycles"),
        }
    assumptions = r.get("assumptions", {})
    if not isinstance(assumptions, dict) or len(assumptions) > 256:
        raise SequentialError("assumptions must map at most 256 primary inputs to fixed values")
    assumptions = {
        name(k, "assumption"): integer(v, 0, (1 << 256) - 1, "assumption value")
        for k, v in assumptions.items()
    }
    rows = r["properties"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise SequentialError("properties must contain 1..32 entries")
    props, ids = [], set()
    for row in rows:
        q = obj(row, {"id", "sink"}, {"source", "equals", "latency", "when", "start_cycle"})
        identity = q["id"]
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or len(identity) > 256
            or identity in ids
        ):
            raise SequentialError(
                "property ids must be nonempty, unique, and at most 256 characters"
            )
        ids.add(identity)
        if ("source" in q) == ("equals" in q):
            raise SequentialError("property requires exactly one of source or equals")
        prop: dict[str, Any] = {
            "id": identity,
            "sink": signal_name(q["sink"], "sink"),
            "latency": integer(q.get("latency", 0), 0, 16, "latency"),
        }
        if "source" in q:
            prop["source"] = signal_name(q["source"], "source")
        else:
            prop["equals"] = integer(q["equals"], 0, (1 << 256) - 1, "equals")
            if prop["latency"]:
                raise SequentialError("constant properties must have latency zero")
        when = q.get("when", [])
        if not isinstance(when, list) or len(when) > 64:
            raise SequentialError("when must contain at most 64 signal/value/lag guards")
        guards = []
        for g in when:
            g = obj(g, {"signal", "equals"}, {"lag"})
            guards.append(
                {
                    "signal": signal_name(g["signal"], "guard signal"),
                    "equals": integer(g["equals"], 0, (1 << 256) - 1, "guard equals"),
                    "lag": integer(g.get("lag", 0), 0, 16, "guard lag"),
                }
            )
        prop["when"] = sorted(guards, key=lambda g: (g["lag"], g["signal"], g["equals"]))
        history = max([prop["latency"], *(g["lag"] for g in guards)])
        prop["start_cycle"] = integer(
            q.get("start_cycle", (reset["cycles"] if reset else 0) + history),
            history,
            128,
            "start_cycle",
        )
        props.append(prop)
    result = {
        "schema_version": 1,
        "semantics": SEMANTICS,
        "files": list(files),
        "top": name(r["top"], "top"),
        "clock": name(r["clock"], "clock"),
        "reset": reset,
        "assumptions": assumptions,
        "depth": integer(r.get("depth", 12), 1, 128, "depth"),
        "induction": integer(r.get("induction", 4), 0, 16, "induction"),
        "properties": sorted(props, key=lambda p: p["id"]),
    }

    if "preprocess" in r:
        result["preprocess"] = normalize_preprocess(r["preprocess"], files)
    return result


def validate_signals(c: Circuit, spec: dict[str, Any]) -> None:
    def signal(n: str, value: int | None = None) -> int:
        if n == c.clock or n not in set(c.inputs) | set(c.next_state) | set(c.combinational):
            raise SequentialError(f"unknown or undriven property signal: {n}")
        width = c.signals[n][0]
        if value is not None and value >= (1 << width):
            raise SequentialError(f"value does not fit signal {n} ({width} bits)")
        return width

    for n, v in spec["assumptions"].items():
        if n not in c.inputs:
            raise SequentialError("assumptions may constrain only primary data inputs")
        signal(n, v)
    reset = spec["reset"]
    if reset:
        if reset["signal"] not in c.inputs or signal(reset["signal"]) != 1:
            raise SequentialError("reset must be a scalar input")
        if reset["signal"] in spec["assumptions"]:
            raise SequentialError("reset protocol conflicts with a fixed input assumption")
    for p in spec["properties"]:
        width = signal(p["sink"], p.get("equals"))
        if "source" in p and signal(p["source"]) != width:
            raise SequentialError("connection endpoints must have equal widths")
        for g in p["when"]:
            signal(g["signal"], g["equals"])
        if p["start_cycle"] > spec["depth"]:
            raise SequentialError("depth precedes a property's first checked cycle")


def holds(prop: dict[str, Any], frames: list[dict[str, int]], cycle: int) -> tuple[bool, bool]:
    enabled = all(frames[cycle - g["lag"]][g["signal"]] == g["equals"] for g in prop["when"])
    expected = (
        frames[cycle - prop["latency"]][prop["source"]] if "source" in prop else prop["equals"]
    )
    return enabled, (not enabled or frames[cycle][prop["sink"]] == expected)

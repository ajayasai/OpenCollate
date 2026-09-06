"""Independent cycle enumeration and original-RTL Icarus hierarchy replays."""

from __future__ import annotations

import itertools
import shutil
import subprocess
from pathlib import Path

import pytest

from opencollate.sequential import run_request
from tests.test_sequential_hierarchy import LEAF, TOP, circuit, request


@pytest.mark.parametrize("invert_first", [False, True])
@pytest.mark.parametrize("guards", ["both", "last-only", "contradictory"])
def test_exhaustive_two_stage_hardware_oracle(
    tmp_path: Path, invert_first: bool, guards: str
) -> None:
    # This oracle never calls the lowered model, SMT evaluator or IR interpreter.
    # It enumerates the two registers and every one-bit input/enable sequence.
    top = TOP.replace("[7:0]", "[0:0]").replace("delay_cell first", "first_cell first")
    cells = LEAF.replace("W=8", "W=1")
    first = cells.replace("delay_cell", "first_cell")
    if invert_first:
        first = first.replace("q <= d", "q <= ~d")
    p = request(tmp_path, top, cells + first, depth=3, induction=0)
    if guards == "last-only":
        p["properties"][0]["when"] = [{"signal": "en", "equals": 1, "lag": 1}]
    elif guards == "contradictory":
        p["properties"][0]["when"] = [
            {"signal": "en", "equals": 1, "lag": 1},
            {"signal": "en", "equals": 0, "lag": 1},
        ]
    expected = "uncovered"
    failures = []
    sequences = list(itertools.product(itertools.product(range(2), repeat=2), repeat=4))
    for initial in itertools.product(range(2), repeat=2):
        for sequence in sequences:
            a, b = initial
            before = []
            for t, (d, en) in enumerate(sequence):
                before.append({"d": d, "en": en, "q": b})
                if t == 0:
                    a, b = 0, 0
                elif en:
                    a, b = d ^ invert_first, a
            enabled = (
                (before[2]["en"] and before[1]["en"])
                if guards == "both"
                else before[2]["en"]
                if guards == "last-only"
                else False
            )
            if enabled:
                if before[3]["q"] != before[1]["d"]:
                    failures.append((initial, sequence))
                expected = "bounded"
    if failures:
        expected = "counterexample"
    reduced = run_request(p, root=tmp_path)
    full = run_request(p, root=tmp_path, cone_reduction=False)
    assert reduced["results"] == full["results"]
    row = reduced["results"][0]
    assert row["status"] == expected
    if failures:
        # The solver must pick the same lexicographically first free assignment.
        initial, sequence = min(failures)
        trace = row["trace"]
        assert (trace[0]["values"]["first.q"], trace[0]["values"]["second.q"]) == initial
        assert [(r["values"]["d"], r["values"]["en"]) for r in trace] == list(sequence)


ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None


@pytest.mark.skipif(not ICARUS, reason="Icarus hierarchy oracle runs in dedicated CI")
@pytest.mark.parametrize(
    "mutation",
    [
        "first-invert",
        "last-invert",
        "first-disable",
        "wrong-reset",
        "generated-invert",
        "width-truncate",
    ],
)
def test_full_hierarchical_counterexample_in_independent_simulator(
    tmp_path: Path, mutation: str
) -> None:
    first = LEAF.replace("delay_cell", "first_cell")
    second = LEAF.replace("delay_cell", "last_cell")
    top = TOP.replace("delay_cell first", "first_cell first").replace(
        "delay_cell second", "last_cell second"
    )
    if mutation in {"first-invert", "generated-invert"}:
        first = first.replace("q <= d", "q <= ~d")
    elif mutation == "last-invert":
        second = second.replace("q <= d", "q <= ~d")
    elif mutation == "first-disable":
        first = first.replace("else if (en)", "else if (!en)")
    elif mutation == "wrong-reset":
        first = first.replace("q <= 0", "q <= 1")
    elif mutation == "width-truncate":
        top = top.replace("wire [7:0] middle", "wire [3:0] middle").replace(
            "first_cell first", "first_cell #(.W(4)) first"
        )
    if mutation == "generated-invert":
        top = top.replace(
            "first_cell first(.clock_in(clk),.rst_n(rst_n),.en(en),.d(d),.q(middle));",
            "for(genvar i=0;i<2;i++) begin: lane wire [7:0] w; first_cell u(clk,rst_n,en,d,w); "
            "end assign middle=lane[0].w;",
        )
    # This register is intentionally outside the failing property's dependency cone.
    top = top.replace(
        "endmodule",
        "logic [7:0] unrelated; always @(posedge clk) if(!rst_n) unrelated<=0; "
        "else unrelated<=unrelated+3; endmodule",
    )
    p = request(tmp_path, top, first + second)
    if mutation == "wrong-reset":
        p["properties"] = [
            {
                "id": "reset",
                "sink": "first.q",
                "equals": 0,
                "start_cycle": 1,
                "when": [{"signal": "rst_n", "equals": 0, "lag": 1}],
            }
        ]
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 1
    trace = r["results"][0]["trace"]
    c = circuit(tmp_path)
    names = sorted(set(c.inputs) | set(c.next_state) | set(c.combinational))
    bench = [
        "module tb; reg clk=0,rst_n,en; reg [7:0] d; wire [7:0] q;",
        "top dut(clk,rst_n,en,d,q); initial begin",
    ]
    for n in sorted(c.next_state):
        bench.append(f"dut.{n}={c.signals[n][0]}'d{trace[0]['values'][n]};")
    fields = " ".join("%d" for n in names)
    for row in trace:
        v = row["values"]
        bench.append(f"rst_n=1'b{v['rst_n']}; en=1'b{v['en']}; d=8'd{v['d']};")
        bench.append(
            f'#1; $display("FRAME {row["cycle"]} {fields}", '
            + ", ".join("dut." + n for n in names)
            + "); #1; clk=1; #1; clk=0;"
        )
    bench.append("$finish; end endmodule")
    (tmp_path / "tb.sv").write_text("\n".join(bench))
    compile_result = subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-s",
            "tb",
            "-o",
            str(tmp_path / "sim"),
            str(tmp_path / "top.sv"),
            str(tmp_path / "cells.sv"),
            str(tmp_path / "tb.sv"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    output = subprocess.run(
        ["vvp", str(tmp_path / "sim")], capture_output=True, text=True, check=True, timeout=15
    ).stdout
    actual = [
        list(map(int, line.split()[2:]))
        for line in output.splitlines()
        if line.startswith("FRAME ")
    ]
    expected = [[row["values"][n] for n in names] for row in trace]
    assert actual == expected  # Every retained AND omitted signal, not just the endpoint.

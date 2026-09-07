"""Optional independent simulation of generated and instantiated original RTL."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from opencollate.sequential import run_request
from tests.test_sequential_hierarchy import PIPE, generated, request


@pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="Icarus hierarchy oracle requires the independent simulator",
)
@pytest.mark.parametrize("design", ["two-modules", "generate-loop", "generate-bits"])
def test_hierarchical_counterexamples_in_iverilog(tmp_path: Path, design: str) -> None:
    if design == "two-modules":
        text = PIPE.replace(".d(mid)", ".d(~mid)")
        p = request(tmp_path, text)
    elif design == "generate-loop":
        text = generated(4).replace("assign q=g[3].value", "assign q=~g[3].value")
        p = request(tmp_path, text, depth=7, assumptions={"en": 1})
        p["properties"][0].update(latency=4, when=[])
    else:
        from tests.test_sequential_hierarchy import LEAF

        text = (
            LEAF
            + """module top(input clk,rst,en,input [3:0] d,output wire [3:0] q);
        for(genvar i=0;i<4;i++) begin:g
          leaf #(.W(1)) ff(.clk(clk),.rst(rst),.en(en),.d(~d[i]),.q(q[i]));
        end endmodule"""
        )
        p = request(tmp_path, text)
        p["properties"][0].update(latency=1, when=[{"signal": "en", "equals": 1, "lag": 1}])
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 1
    trace = r["results"][0]["trace"]
    names = ["q", *r["model"]["states"]]
    bench = [
        "module tb; reg clk=0; reg rst,en; reg [3:0] d; wire [3:0] q;",
        "top dut(clk,rst,en,d,q); initial begin",
    ]
    for n in r["model"]["states"]:
        bench.append(f"dut.{n} = {trace[0]['values'][n]};")
    for row in trace:
        v = row["values"]
        bench.append(f"rst={v['rst']}; en={v['en']}; d={v['d']}; #1;")
        fmt = ",".join(["%0d"] * len(names))
        bench.append(f'$display("FRAME {fmt}",' + ",".join(f"dut.{n}" for n in names) + ");")
        bench.append("clk=1; #1; clk=0; #1;")
    bench.append("$finish; end endmodule")
    tb = tmp_path / "tb.sv"
    tb.write_text("\n".join(bench))
    exe = tmp_path / "trace.vvp"
    subprocess.run(
        ["iverilog", "-g2012", "-s", "tb", "-o", str(exe), str(tmp_path / "design.sv"), str(tb)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    got = subprocess.run(["vvp", str(exe)], check=True, capture_output=True, text=True, timeout=30)
    values = [
        [int(x) for x in line.removeprefix("FRAME ").split(",")]
        for line in got.stdout.splitlines()
        if line.startswith("FRAME ")
    ]
    assert values == [[row["values"][n] for n in names] for row in trace]

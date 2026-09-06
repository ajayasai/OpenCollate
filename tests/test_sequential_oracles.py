"""Independent native constant evaluation and optional Icarus cycle replay."""

from __future__ import annotations

import itertools
import shutil
import subprocess
from pathlib import Path

import pytest

from opencollate.sequential import run_request
from opencollate.sequential_ir import simulate_frame
from opencollate.sequential_rtl import load_circuit
from opencollate.sequential_smt import Budget, Unroller
from tests.test_sequential import PIPE, project


@pytest.mark.parametrize(
    "expression",
    [
        "a + b",
        "a - b",
        "a * b",
        "a & b",
        "a | b",
        "a ^ b",
        "a ~^ b",
        "a == b",
        "a != b",
        "a < b",
        "a <= b",
        "a > b",
        "a >= b",
        "a && b",
        "a || b",
        "a << b",
        "a >> b",
        "a >>> b",
        "a <<< b",
        "+a",
        "-a",
        "~a",
        "!a",
        "&a",
        "~&a",
        "|a",
        "~|a",
        "^a",
        "~^a",
        "a ? b : ~b",
        "{a[0],b[3:1]}",
    ],
)
@pytest.mark.parametrize("signed", [False, True])
def test_native_sv_constant_oracle(tmp_path: Path, expression: str, signed: bool) -> None:
    import pyslang as sv

    sign = "signed " if signed else ""
    text = (
        f"module pipe(input logic clk, input logic {sign}[3:0] a,b,"
        f"output wire [3:0] q); assign q = {expression}; endmodule"
    )
    (tmp_path / "design.sv").write_text(text)
    circuit = load_circuit(["design.sv"], root=tmp_path, top="pipe", clock="clk")
    constants = []
    pairs = list(itertools.product(range(16), repeat=2))
    for i, (a, b) in enumerate(pairs):
        constants.append(f"localparam logic {sign}[3:0] a{i} = 4'd{a}, b{i} = 4'd{b};")
        expr = expression.replace("a", f"a{i}").replace("b", f"b{i}")
        constants.append(f"localparam logic [3:0] result{i} = {expr};")
    compilation = sv.ast.Compilation()
    compilation.addSyntaxTree(
        sv.syntax.SyntaxTree.fromText("module oracle;\n" + "\n".join(constants) + "\nendmodule")
    )
    root = compilation.getRoot()
    assert not [d for d in compilation.getAllDiagnostics() if d.isError()]
    expected = {
        m.name: int(m.value.value)
        for m in root.topInstances[0].body
        if m.kind.name == "Parameter" and m.name.startswith("result")
    }
    budget = Budget(30000, 1000000)
    unroller = Unroller(circuit, {"assumptions": {}, "reset": None}, budget, "native")
    unroller.append()
    for i, (a, b) in enumerate(pairs):
        values, _ = simulate_frame(circuit, {}, {"a": a, "b": b})
        unroller.solver.push()
        unroller.solver.add(unroller.frames[0]["a"] == a, unroller.frames[0]["b"] == b)
        assert budget.query(unroller.solver)
        actual = unroller.solver.model().eval(unroller.frames[0]["q"]).as_long()
        assert actual == values["q"] == expected[f"result{i}"], (
            expression,
            signed,
            a,
            b,
            actual,
            expected[f"result{i}"],
        )
        unroller.solver.pop()


@pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="Icarus cycle oracle runs in dedicated CI",
)
@pytest.mark.parametrize("mutation", ["inversion", "bypass", "enable", "reset"])
def test_counterexample_replays_in_iverilog(tmp_path: Path, mutation: str) -> None:
    rtl = PIPE
    if mutation == "inversion":
        rtl = rtl.replace("q <= d", "q <= ~d")
    elif mutation == "bypass":
        rtl = rtl.replace("q <= d", "q <= 0")
    elif mutation == "enable":
        rtl = rtl.replace("else if (en)", "else if (!en)")
    else:
        rtl = rtl.replace("q <= '0", "q <= '1")
    request = project(tmp_path, rtl)
    if mutation == "reset":
        request["properties"] = [
            {
                "id": "reset",
                "sink": "q",
                "equals": 0,
                "start_cycle": 1,
                "when": [{"signal": "rst_n", "equals": 0, "lag": 1}],
            }
        ]
    report = run_request(request, root=tmp_path)
    assert report["exit_code"] == 1
    trace = report["results"][0]["trace"]
    testbench = [
        "module tb; reg clk=0; reg rst_n,en; reg [7:0] d; wire [7:0] q;",
        "pipe dut(.clk(clk),.rst_n(rst_n),.en(en),.d(d),.q(q)); initial begin",
    ]
    # Initial state is arbitrary in the two-valued model. Force precisely the
    # selected initial witness in the TESTBENCH, never modify source RTL.
    testbench.append(f"dut.q=8'd{trace[0]['values']['q']};")
    for frame in trace:
        v = frame["values"]
        testbench.append(f"d=8'd{v['d']}; en=1'b{v['en']}; rst_n=1'b{v['rst_n']};")
        testbench.append(f'#1; $display("FRAME {frame["cycle"]} %d", q); #1; clk=1; #1; clk=0;')
    testbench.append("$finish; end endmodule")
    (tmp_path / "tb.sv").write_text("\n".join(testbench))
    compiled = subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-s",
            "tb",
            "-o",
            str(tmp_path / "sim"),
            str(tmp_path / "design.sv"),
            str(tmp_path / "tb.sv"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert compiled.returncode == 0, compiled.stderr
    output = subprocess.run(
        ["vvp", str(tmp_path / "sim")], capture_output=True, text=True, check=True, timeout=15
    ).stdout
    lines = [line.split() for line in output.splitlines() if line.startswith("FRAME ")]
    assert [int(line[2]) for line in lines] == [frame["values"]["q"] for frame in trace]

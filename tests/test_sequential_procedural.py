"""Procedural hardware semantics, strict rejection, and certificate integration."""

from __future__ import annotations

import copy
import importlib
import itertools
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from opencollate.proof_kernel import ProofError
from opencollate.sequential import replay, run_request
from opencollate.sequential_certificate import _binding, _load, certify, verify_certificate
from opencollate.sequential_cnf import digest
from opencollate.sequential_ir import SequentialError, simulate_frame
from opencollate.sequential_rtl import load_circuit
from opencollate.sequential_spec import SEMANTICS

CONTROLLER = """
module lane #(parameter W=8)(input logic clk, rst, input logic [1:0] op,
    input logic [W-1:0] d, output logic [W-1:0] q);
    logic [W-1:0] n;
    always_comb begin
        n = q;
        case (op)
            0: n = d;
            1: n = q + 1'b1;
            2,3: n = q ^ d;
            default: n = 0;
        endcase
    end
    always_ff @(posedge clk) begin
        if (rst) q <= 0;
        else q <= n;
    end
endmodule
module top(input logic clk, rst, input logic [1:0] op,
    input logic [7:0] d, output wire [7:0] q);
    lane #(.W(8)) unit(.clk(clk), .rst(rst), .op(op), .d(d), .q(q));
endmodule
"""


def request(tmp_path: Path, rtl: str = CONTROLLER) -> dict[str, Any]:
    (tmp_path / "design.sv").write_text(rtl, encoding="utf-8", newline="\n")
    return {
        "schema_version": 1,
        "semantics": SEMANTICS,
        "files": ["design.sv"],
        "top": "top",
        "clock": "clk",
        "depth": 6,
        "induction": 3,
        "reset": {"signal": "rst", "active": 1, "cycles": 1},
        "properties": [
            {
                "id": "load",
                "sink": "q",
                "source": "d",
                "latency": 1,
                "when": [{"signal": "op", "equals": 0, "lag": 1}],
            }
        ],
    }


def circuit(tmp_path: Path, block: str, *, width: int = 4, signed: bool = False):
    sign = "signed " if signed else ""
    rtl = (
        f"module top(input logic clk, input logic [1:0] s, "
        f"input logic {sign}[{width - 1}:0] a,b, output logic {sign}[{width - 1}:0] q); "
        f"logic {sign}[{width - 1}:0] x,y; " + block + " endmodule"
    )
    (tmp_path / "design.sv").write_text(rtl)
    return load_circuit(["design.sv"], root=tmp_path, top="top", clock="clk")


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("width", [1, 4, 8])
def test_blocking_order_and_case_priority(tmp_path: Path, signed: bool, width: int) -> None:
    c = circuit(
        tmp_path,
        """
      always_comb begin
        x=a; y=x+1'b1; x=b; q=y;
        case(s)
          default: q=x;
          0,1: q=y;
          1,2: q=x ^ y;
        endcase
      end
    """,
        width=width,
        signed=signed,
    )
    mask = (1 << width) - 1
    for a, b, s in itertools.product(range(min(mask + 1, 16)), range(min(mask + 1, 16)), range(4)):
        values, updated = simulate_frame(c, {}, {"a": a, "b": b, "s": s})
        y = (a + 1) & mask
        expected = y if s in (0, 1) else (b ^ y if s == 2 else b)
        assert values["x"] == b and values["y"] == y
        assert values["q"] == expected and updated == {}


def test_branch_environment_and_later_overwrite(tmp_path: Path) -> None:
    c = circuit(
        tmp_path,
        """
      always_comb begin
        if (s) x=a; else x=b;
        y=x;
        if (s) q=b;
        q=y;
      end
    """,
    )
    for s in range(4):
        values, _ = simulate_frame(c, {}, {"a": 3, "b": 9, "s": s})
        assert values["q"] == (3 if s else 9)


def test_blocking_signed_target_reinterprets_rhs(tmp_path: Path) -> None:
    rtl = """module top(input clk, input logic [3:0] a, output logic [7:0] q);
      logic signed [3:0] x;
      always_comb begin x=a; q=x; end
    endmodule"""
    request(tmp_path, rtl)
    c = load_circuit(["design.sv"], root=tmp_path, top="top", clock="clk")
    for a in range(16):
        values, _ = simulate_frame(c, {}, {"a": a})
        assert values["q"] == (a if a < 8 else a + 240)


def test_sequential_case_holds_and_uses_old_state(tmp_path: Path) -> None:
    c = circuit(
        tmp_path,
        """
      always_ff @(posedge clk) begin
        q<=a;
        case(s)
          0,1: begin q<=b; x<=q; end
          1,2: q<=q+1'b1;
        endcase
      end
    """,
    )
    for s in range(4):
        _, updated = simulate_frame(c, {"q": 3, "x": 5}, {"a": 8, "b": 9, "s": s})
        assert updated["q"] == (9 if s in (0, 1) else (4 if s == 2 else 8))
        assert updated["x"] == (3 if s in (0, 1) else 5)


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("always_comb if(s) q=a;", "incomplete combinational"),
        ("always_comb case(s) 0:q=a;endcase", "incomplete combinational"),
        ("always_comb begin q=x; x=a; end", "read before definite"),
        ("always_comb begin if(s) x=a; q=x; x=b; end", "read before definite"),
        ("always_comb begin x=x+1; q=x;end", "read before definite"),
        ("always_comb q<=a;", "blocking"),
        ("always_ff @(posedge clk) q=a;", "nonblocking"),
        ("always_comb casez(s) 0:q=a;default:q=b;endcase", "ordinary case"),
        ("always_comb casex(s) 0:q=a;default:q=b;endcase", "ordinary case"),
        ("always_comb unique case(s) 0:q=a;default:q=b;endcase", "ordinary case"),
        ("always_comb priority case(s) 0:q=a;default:q=b;endcase", "ordinary case"),
        ("always_comb begin q=a; q[0]=b[0];end", "whole-signal"),
        (
            "always_comb begin q=a; for(int i=0;i<2;i++) q=q+1;end",
            "unsupported instantiated module member: StatementBlock",
        ),
        ("always_comb q=a; always_comb q=b;", "multiple drivers"),
        ("always_comb q=x; always_comb x=q;", "combinational cycle"),
        ("always_comb q=clk;", "clock-as-data"),
    ],
)
def test_unsupported_controller_fails_closed(tmp_path: Path, block: str, message: str) -> None:
    with pytest.raises(SequentialError, match=message):
        circuit(tmp_path, block)


def test_oversized_case_is_bounded(tmp_path: Path) -> None:
    cases = " ".join(f"{i}:q=a;" for i in range(257))
    with pytest.raises(SequentialError, match="case exceeds"):
        circuit(tmp_path, "always_comb case(s) " + cases + " default:q=b;endcase")


ENUM_FSM = """
module top(input logic clk,rst,go, output logic busy);
  typedef enum logic [1:0] {IDLE=0, WORK=1, DONE=2} phase_t;
  phase_t state, next_state;
  always_comb begin
    next_state=IDLE;
    busy=0;
    case(state)
      IDLE: if(go) next_state=WORK;
      WORK: begin busy=1; next_state=DONE; end
      DONE: next_state=IDLE;
      default: next_state=IDLE;
    endcase
  end
  always_ff @(posedge clk) if(rst) state<=IDLE; else state<=next_state;
endmodule
"""


def test_enum_fsm_nonblocking_reset_and_corrupt_encoding(tmp_path: Path) -> None:
    request(tmp_path, ENUM_FSM)
    c = load_circuit(["design.sv"], root=tmp_path, top="top", clock="clk")
    for state, rst, go in itertools.product(range(4), range(2), range(2)):
        values, updated = simulate_frame(c, {"state": state}, {"rst": rst, "go": go})
        assert values["busy"] == (state == 1)
        assert updated["state"] == (0 if rst else (go if state == 0 else (2 if state == 1 else 0)))


@pytest.mark.parametrize("full", [False, True])
def test_hierarchical_controller_proof_replay_and_mutant(tmp_path: Path, full: bool) -> None:
    spec = request(tmp_path)
    good = run_request(spec, root=tmp_path, cone_reduction=not full)
    assert good["status"] == "proven"
    assert replay(spec, good, root=tmp_path, cone_reduction=not full)["status"] == "proven"
    request(tmp_path, CONTROLLER.replace("0: n = d", "0: n = ~d"))
    bad = run_request(spec, root=tmp_path, cone_reduction=not full)
    assert bad["status"] == "counterexample"
    assert bad["results"][0]["trace"][-1]["cycle"] == 2
    with pytest.raises(SequentialError, match="changed"):
        replay(spec, good, root=tmp_path, cone_reduction=not full)


def test_solver_free_certificate_for_hierarchical_controller(tmp_path: Path, monkeypatch) -> None:
    spec = request(tmp_path)
    certificate = certify(spec, root=tmp_path)
    original = importlib.import_module

    def forbidden(name, *args, **kwargs):
        if name == "z3" or name.startswith("pysat"):
            raise AssertionError("receiver imported a solver")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", forbidden)
    assert verify_certificate(spec, certificate, root=tmp_path)["exit_code"] == 0


def test_rebound_bad_controller_cannot_reuse_old_proof(tmp_path: Path) -> None:
    spec = request(tmp_path)
    certificate = certify(spec, root=tmp_path)
    request(tmp_path, CONTROLLER.replace("0: n = d", "0: n = ~d"))
    c, normalized = _load(spec, tmp_path)
    forged = copy.deepcopy(certificate)
    forged["binding"] = _binding(c, normalized)
    forged["certificate_sha256"] = digest(
        {k: v for k, v in forged.items() if k != "certificate_sha256"}
    )
    with pytest.raises(ProofError):
        verify_certificate(spec, forged, root=tmp_path)
    with pytest.raises(ProofError, match="base counterexample"):
        certify(spec, root=tmp_path)


@pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="independent controller oracle requires Icarus",
)
@pytest.mark.parametrize("signed", [False, True])
def test_blocking_case_exhaustive_icarus_oracle(tmp_path: Path, signed: bool) -> None:
    block = """always_comb begin
        x=a; y=x+1'b1; x=b; q=y;
        case(s)
          default: q=x;
          0,1: q=y;
          1,2: q=x ^ y;
        endcase
      end"""
    c = circuit(tmp_path, block, signed=signed)
    triples = list(itertools.product(range(16), range(16), range(4)))
    tb = [
        "module tb; reg clk=0;reg [1:0] s;reg [3:0] a,b;wire [3:0] q;",
        "top dut(.*); initial begin",
    ]
    for a, b, s in triples:
        tb.append(f'a={a}; b={b}; s={s}; #1; $display("ROW %h %h %h",dut.x,dut.y,q);')
    tb += ["$finish;end endmodule"]
    (tmp_path / "tb.sv").write_text("\n".join(tb))
    p = subprocess.run(
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
    assert p.returncode == 0, p.stderr
    output = subprocess.run(
        ["vvp", str(tmp_path / "sim")], capture_output=True, text=True, timeout=15, check=True
    ).stdout
    rows = [
        [int(x, 16) for x in line.split()[1:]]
        for line in output.splitlines()
        if line.startswith("ROW ")
    ]
    assert len(rows) == len(triples)
    for (a, b, s), row in zip(triples, rows, strict=True):
        values, _ = simulate_frame(c, {}, {"a": a, "b": b, "s": s})
        assert row == [values[n] for n in ("x", "y", "q")]


def test_previously_rejected_clocked_case_proves(tmp_path: Path) -> None:
    from tests.test_sequential import PIPE, project

    spec = project(tmp_path, PIPE.replace("q <= d", "case(en) 1: q <= d; default:q<=0; endcase"))
    assert run_request(spec, root=tmp_path)["status"] == "proven"
    proof = certify(spec, root=tmp_path)
    assert verify_certificate(spec, proof, root=tmp_path)["status"] == "certificate-verified"


def test_enum_controller_certificate_and_activation(tmp_path: Path) -> None:
    spec = request(tmp_path, ENUM_FSM)
    spec["properties"] = [
        {"id": "idle", "sink": "busy", "equals": 0, "when": [{"signal": "state", "equals": 0}]},
        {"id": "work", "sink": "busy", "equals": 1, "when": [{"signal": "state", "equals": 1}]},
    ]
    assert run_request(spec, root=tmp_path)["status"] == "proven"
    proof = certify(spec, root=tmp_path)
    result = verify_certificate(spec, proof, root=tmp_path)
    assert result["status"] == "certificate-verified" and len(result["results"]) == 2
    assert all(row["cover_cycle"] >= 1 for row in result["results"])


@pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="independent controller oracle requires Icarus",
)
@pytest.mark.parametrize("mutation", ["inverted-load", "wrong-mode", "priority"])
def test_controller_counterexample_in_independent_simulator(tmp_path: Path, mutation: str) -> None:
    rtl = CONTROLLER
    if mutation == "inverted-load":
        rtl = rtl.replace("0: n = d", "0: n = ~d")
    elif mutation == "wrong-mode":
        rtl = rtl.replace("0: n = d", "0: n = q + 1'b1")
    else:
        rtl = rtl.replace("0: n = d", "0,1: n = ~d;\n            0: n = d")
    spec = request(tmp_path, rtl)
    reduced = run_request(spec, root=tmp_path)
    full = run_request(spec, root=tmp_path, cone_reduction=False)
    assert reduced["status"] == full["status"] == "counterexample"
    trace = reduced["results"][0]["trace"]
    assert trace == full["results"][0]["trace"]
    observed_names = sorted(trace[0]["values"])
    bench = [
        "module tb; reg clk=0;reg rst;reg [1:0] op;reg [7:0] d;wire [7:0] q;",
        "top dut(.*); initial begin",
    ]
    for name in reduced["model"]["states"]:
        bench.append(f"dut.{name}={trace[0]['values'][name]};")
    for frame in trace:
        v = frame["values"]
        bench.append(f"rst={v['rst']};op={v['op']};d={v['d']};#1;")
        fmt = ",".join("%0d" for _ in observed_names)
        bench.append(
            f'$display("FRAME {fmt}",' + ",".join(f"dut.{n}" for n in observed_names) + ");"
        )
        bench.append("clk=1;#1;clk=0;#1;")
    bench.append("$finish;end endmodule")
    (tmp_path / "tb.sv").write_text("\n".join(bench))
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
        ["vvp", str(tmp_path / "sim")], capture_output=True, text=True, timeout=15, check=True
    ).stdout
    rows = [
        [int(x) for x in line.removeprefix("FRAME ").split(",")]
        for line in output.splitlines()
        if line.startswith("FRAME ")
    ]
    assert rows == [[row["values"][n] for n in observed_names] for row in trace]

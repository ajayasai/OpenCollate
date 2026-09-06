from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from opencollate.cli import main
from opencollate.sequential import digest, replay, run_request
from opencollate.sequential_ir import SequentialError, simulate_frame
from opencollate.sequential_rtl import load_circuit
from opencollate.sequential_smt import Budget, Unroller, replay_trace
from opencollate.sequential_spec import normalize, read_json

PIPE = """module pipe #(parameter W=8)(
 input logic clk, rst_n, en, input logic [W-1:0] d,
 output logic [W-1:0] q);
 always_ff @(posedge clk) begin
   if (!rst_n) q <= '0;
   else if (en) q <= d;
 end
endmodule
"""


def project(tmp: Path, rtl: str = PIPE, **extra: object) -> dict:
    (tmp / "design.sv").write_text(rtl)
    return {
        "schema_version": 1,
        "semantics": "two-valued-synchronous",
        "files": ["design.sv"],
        "top": "pipe",
        "clock": "clk",
        "depth": 6,
        "induction": 4,
        "reset": {"signal": "rst_n", "active": 0},
        "properties": [
            {
                "id": "data",
                "source": "d",
                "sink": "q",
                "latency": 1,
                "when": [{"signal": "en", "equals": 1, "lag": 1}],
            }
        ],
        **extra,
    }


def test_pipeline_inductively_proven(tmp_path: Path) -> None:
    r = run_request(project(tmp_path), root=tmp_path)
    assert r["exit_code"] == 0
    assert r["results"][0]["status"] == "proven"
    assert r["results"][0]["induction_depth"] == 1
    assert r["results"][0]["cover_cycle"] == 2
    assert r["model"]["state_bits"] == 8
    assert r["binding"]["sources"][0]["sha256"]


def test_counterexample_is_earliest_replayed_and_deterministic(tmp_path: Path) -> None:
    p = project(tmp_path)
    p["properties"][0]["when"] = []
    r = run_request(p, root=tmp_path)
    assert r == run_request(p, root=tmp_path)
    assert r["exit_code"] == 1
    row = r["results"][0]
    assert row["checked_through"] == 2
    frames = [f["values"] for f in row["trace"]]
    c = load_circuit(p["files"], root=tmp_path, top="pipe", clock="clk")
    spec = normalize(p)
    assert replay_trace(c, spec, spec["properties"][0], frames)
    assert frames[1]["d"] == 1 and frames[1]["en"] == 0 and frames[2]["q"] == 0
    frames[2]["q"] = 1
    assert not replay_trace(c, spec, spec["properties"][0], frames)


@pytest.mark.parametrize("width", [1, 8, 64, 128, 256])
def test_two_stage_latency_and_nonblocking_old_values(tmp_path: Path, width: int) -> None:
    rtl = PIPE.replace("W=8", f"W={width}").replace(
        " always_ff", " logic [W-1:0] first;\n always_ff"
    )
    rtl = rtl.replace("if (!rst_n) q <= '0;", "if (!rst_n) begin q <= '0; first <= '0; end")
    rtl = rtl.replace("else if (en) q <= d;", "else if (en) begin first <= d; q <= first; end")
    p = project(tmp_path, rtl)
    p["properties"][0].update(
        latency=2,
        when=[{"signal": "en", "equals": 1, "lag": 1}, {"signal": "en", "equals": 1, "lag": 2}],
    )
    assert run_request(p, root=tmp_path)["exit_code"] == 0
    p["properties"][0]["latency"] = 1
    assert run_request(p, root=tmp_path)["exit_code"] == 1


def test_sequential_assignment_priority_reads_old_state(tmp_path: Path) -> None:
    rtl = PIPE.replace("else if (en) q <= d;", "else begin q <= d; if (en) q <= q + 1'b1; end")
    p = project(tmp_path, rtl)
    c = load_circuit(p["files"], root=tmp_path, top="pipe", clock="clk")
    _, next_state = simulate_frame(c, {"q": 4}, {"d": 100, "en": 1, "rst_n": 1})
    assert next_state["q"] == 5
    _, next_state = simulate_frame(c, {"q": 4}, {"d": 100, "en": 0, "rst_n": 1})
    assert next_state["q"] == 100


def test_bounded_is_not_proven(tmp_path: Path) -> None:
    p = project(tmp_path, induction=0)
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 2 and r["results"][0]["status"] == "bounded"


def test_late_counterexample_beyond_shallow_bound(tmp_path: Path) -> None:
    rtl = """module pipe(input logic clk,rst_n,en,input logic d,output logic q);
 logic [3:0] count;
 always_ff @(posedge clk) if (!rst_n) count <= 0; else count <= count + 1'b1;
 assign q = count == 4'd9;
 endmodule"""
    p = project(tmp_path, rtl, properties=[{"id": "late", "sink": "q", "equals": 0}])
    r = run_request(p, root=tmp_path)
    assert r["results"][0]["status"] == "bounded" and r["exit_code"] == 2
    p["depth"] = 12
    r = run_request(p, root=tmp_path)
    assert r["results"][0]["status"] == "counterexample"
    assert r["results"][0]["checked_through"] == 10


def test_initial_state_not_invented(tmp_path: Path) -> None:
    p = project(
        tmp_path,
        reset=None,
        properties=[{"id": "initial", "sink": "q", "equals": 0, "start_cycle": 0}],
    )
    r = run_request(p, root=tmp_path)
    assert r["results"][0]["checked_through"] == 0 and r["exit_code"] == 1


def test_no_vacuous_success(tmp_path: Path) -> None:
    p = project(tmp_path)
    p["properties"][0]["when"] = [{"signal": "en", "equals": 0}, {"signal": "en", "equals": 1}]
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 2 and r["results"][0]["status"] == "uncovered"
    p["properties"][0]["when"] = [{"signal": "en", "equals": 1}]
    p["assumptions"] = {"en": 0}
    assert run_request(p, root=tmp_path)["results"][0]["status"] == "uncovered"


def test_assumptions_can_justify_unconditional_transfer(tmp_path: Path) -> None:
    p = project(tmp_path, assumptions={"en": 1})
    p["properties"][0]["when"] = []
    assert run_request(p, root=tmp_path)["exit_code"] == 0


def test_short_reset_property_with_explicit_start(tmp_path: Path) -> None:
    p = project(
        tmp_path,
        properties=[
            {
                "id": "reset",
                "sink": "q",
                "equals": 0,
                "start_cycle": 1,
                "when": [{"signal": "rst_n", "equals": 0, "lag": 1}],
            }
        ],
    )
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 0 and r["results"][0]["cover_cycle"] == 1
    p = project(tmp_path, PIPE.replace("q <= '0", "q <= '1"), properties=p["properties"])
    assert run_request(p, root=tmp_path)["exit_code"] == 1


def test_receipt_replay_rechecks_rtl_and_does_not_trust_status(tmp_path: Path) -> None:
    p = project(tmp_path)
    r = run_request(p, root=tmp_path)
    assert replay(p, r, root=tmp_path) == r
    (tmp_path / "design.sv").write_text(PIPE.replace("q <= d", "q <= ~d"))
    with pytest.raises(SequentialError, match="changed"):
        replay(p, r, root=tmp_path)
    forged = run_request(p, root=tmp_path)
    forged["results"][0]["status"] = "proven"
    forged["status"] = "proven"
    forged["exit_code"] = 0
    forged["receipt_sha256"] = digest({k: v for k, v in forged.items() if k != "receipt_sha256"})
    with pytest.raises(SequentialError, match="reverification"):
        replay(p, forged, root=tmp_path)


@pytest.mark.parametrize("change", ["request", "digest", "model", "rows", "exit-bool"])
def test_receipt_tampering(tmp_path: Path, change: str) -> None:
    p = project(tmp_path)
    r = run_request(p, root=tmp_path)
    if change == "request":
        p["properties"][0]["latency"] = 0
    elif change == "digest":
        r["receipt_sha256"] = "0" * 64
    elif change == "model":
        r["model"]["state_bits"] = 1
    elif change == "rows":
        r["results"] = []
    else:
        r["exit_code"] = False
    if change not in {"request", "digest"}:
        r["receipt_sha256"] = digest({k: v for k, v in r.items() if k != "receipt_sha256"})
    with pytest.raises(SequentialError):
        replay(p, r, root=tmp_path)


@pytest.mark.parametrize(
    "rtl",
    [
        PIPE.replace("@(posedge clk)", "@(posedge clk or negedge rst_n)"),
        PIPE.replace("@(posedge clk)", "@(negedge clk)"),
        PIPE.replace("@(posedge clk)", "@(posedge en)"),
        PIPE.replace("q <= d", "q = d"),
        PIPE.replace("q <= d", "q[0] <= d[0]"),
        PIPE.replace("q <= d", "q <= #1 d"),
        PIPE.replace("q <= d", "q <= 8'bx"),
        PIPE.replace("q <= d", "q <= 8'bz"),
        PIPE.replace("q <= d", "q <= d / en"),
        PIPE.replace("q <= d", "q <= clk"),
        PIPE.replace("endmodule", "initial q = 0; endmodule"),
        PIPE.replace("endmodule", "always_comb q = d; endmodule"),
        PIPE.replace("endmodule", "always_ff @(posedge clk) q <= d; endmodule"),
        PIPE.replace("endmodule", "assign q = d; endmodule"),
        PIPE.replace("q <= d", "for(int i=0;i<8;i++) q[i] <= d[i]"),
        PIPE.replace("q <= d", "case(en) 1: q <= d; default: q<=0; endcase"),
        PIPE.replace("W=8", "W=257"),
        PIPE.replace("[W-1:0]", "[0:W-1]"),
        PIPE.replace("input logic [W-1:0] d", "inout wire [W-1:0] d"),
        '`include "missing.svh"\n' + PIPE,
        PIPE.replace("q <= d", "q <= ghost"),
        PIPE.replace("endmodule", "leaf u(); endmodule module leaf; endmodule"),
        "module pipe(input logic clk,rst_n,en,d,output wire q); "
        "wire a,b; assign a=b; assign b=a; assign q=b; endmodule",
        "module pipe(input logic clk,rst_n,en,d,output wire q); endmodule",
    ],
)
def test_unsupported_never_proves(tmp_path: Path, rtl: str) -> None:
    p = project(tmp_path, rtl)
    with pytest.raises((SequentialError, ValueError)):
        run_request(p, root=tmp_path)


@pytest.mark.parametrize(
    "update",
    [
        {"depth": True},
        {"depth": 0},
        {"depth": 129},
        {"induction": 17},
        {"semantics": "four-state"},
        {"schema_version": True},
        {"unknown": 1},
        {"files": []},
        {"files": ["../outside.sv"]},
        {"files": ["design.sv", "design.sv"]},
        {"assumptions": {"q": 0}},
        {"assumptions": {"rst_n": 1}},
        {"assumptions": {"d": 256}},
        {"reset": {"signal": "d", "active": 0}},
        {"properties": []},
        {"properties": [{"id": "p", "sink": "q", "source": "en"}]},
        {"properties": [{"id": "p", "sink": "q", "equals": 256}]},
        {"properties": [{"id": "p", "sink": "q", "equals": 0, "latency": 1}]},
        {"properties": [{"id": "p", "sink": "q", "source": "d", "equals": 0}]},
        {"properties": [{"id": "p", "sink": "q", "source": "d", "start_cycle": 7}]},
        {
            "properties": [
                {"id": "p", "sink": "q", "source": "d", "when": [{"signal": "ghost", "equals": 0}]}
            ]
        },
    ],
)
def test_invalid_requests(tmp_path: Path, update: dict) -> None:
    p = project(tmp_path, **update)
    with pytest.raises(SequentialError):
        run_request(p, root=tmp_path)


@pytest.mark.parametrize(
    "text", ['{"x":1,"x":2}', '{"x":NaN}', "[" * 65 + "0" + "]" * 65, "[]", "{bad"]
)
def test_bounded_strict_json(tmp_path: Path, text: str) -> None:
    f = tmp_path / "q.json"
    f.write_text(text)
    with pytest.raises(ValueError):
        read_json(f)


def test_cli_and_input_protection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = project(tmp_path)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(p))
    result = tmp_path / "result.json"
    assert main(["sequential", "check", str(path), "--output", str(result)]) == 0
    assert main(["sequential", "replay", str(path), str(result)]) == 0
    assert json.loads(capsys.readouterr().out)["exit_code"] == 0
    assert main(["sequential", "check", str(path), "--output", str(tmp_path / "design.sv")]) == 2
    assert (tmp_path / "design.sv").read_text() == PIPE
    capsys.readouterr()
    p["files"] = ["not-there.sv"]
    path.write_text(json.dumps(p))
    assert main(["sequential", "check", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid-or-unsupported"


def test_solver_exhaustion_is_inconclusive(tmp_path: Path) -> None:
    p = project(tmp_path)
    r = run_request(p, root=tmp_path, resource_limit=1)
    assert r["exit_code"] == 2 and r["results"][0]["status"] == "inconclusive"


def test_deadline_and_query_caps() -> None:
    b = Budget(1000, 1000)
    b.deadline = 0
    with pytest.raises(SequentialError, match="deadline"):
        b.remaining()
    b = Budget(1000, 1000)
    b.queries = 8192
    with pytest.raises(SequentialError, match="query bound"):
        b.query(b.solver())


def test_independent_exhaustive_trace_oracle(tmp_path: Path) -> None:
    # Independent scalar pipeline simulator, not the imported IR interpreter.
    # All 2^(2*4) data/enable sequences after reset are enumerated.
    p = project(tmp_path, PIPE.replace("W=8", "W=1"), depth=4)
    p["properties"][0]["when"] = []
    earliest = None
    for sequence in itertools.product(range(4), repeat=4):
        q = 0
        previous_d = 0
        for t, pair in enumerate(sequence, start=1):
            d, en = pair & 1, pair >> 1
            if t >= 2 and q != previous_d:
                earliest = t if earliest is None else min(t, earliest)
            q = d if en else q
            previous_d = d
    r = run_request(p, root=tmp_path)
    assert earliest == r["results"][0]["checked_through"] == 2


@pytest.mark.parametrize(
    "operator",
    [
        "+",
        "-",
        "*",
        "&",
        "|",
        "^",
        "~^",
        "==",
        "!=",
        "<",
        "<=",
        ">",
        ">=",
        "&&",
        "||",
        "<<",
        ">>",
        ">>>",
    ],
)
@pytest.mark.parametrize("signed", [False, True])
def test_smt_lowering_differential_against_python(
    tmp_path: Path, operator: str, signed: bool
) -> None:
    sign = "signed " if signed else ""
    rtl = (
        f"module pipe(input logic clk,rst_n,en,input logic {sign}[3:0] a,b,"
        f"output wire [3:0] q); assign q = a {operator} b; endmodule"
    )
    (tmp_path / "design.sv").write_text(rtl)
    c = load_circuit(["design.sv"], root=tmp_path, top="pipe", clock="clk")
    spec = {"assumptions": {}, "reset": None}
    budget = Budget(30000, 1000000)
    u = Unroller(c, spec, budget, "oracle")
    u.append()
    for a, b in itertools.product(range(16), repeat=2):
        env = {"a": a, "b": b, "rst_n": 1, "en": 1}
        expected, _ = simulate_frame(c, {}, env)
        u.solver.push()
        u.solver.add(*[u.frames[0][n] == v for n, v in env.items()])
        assert budget.query(u.solver)
        actual = u.solver.model().eval(u.frames[0]["q"]).as_long()
        assert actual == expected["q"], (operator, signed, a, b, actual, expected)
        u.solver.pop()


def test_conditional_concat_and_slices(tmp_path: Path) -> None:
    rtl = (
        "module pipe(input logic clk,rst_n,en,input logic [3:0] d,output wire [3:0] q); "
        "assign q = en ? {d[0],d[3:1]} : ~d; endmodule"
    )
    (tmp_path / "design.sv").write_text(rtl)
    c = load_circuit(["design.sv"], root=tmp_path, top="pipe", clock="clk")
    for d, en in itertools.product(range(16), range(2)):
        env, _ = simulate_frame(c, {}, {"d": d, "en": en, "rst_n": 1})
        assert env["q"] == (((d & 1) << 3) | (d >> 1) if en else (~d & 15))


def test_inconclusive_dominates_counterexample(tmp_path: Path) -> None:
    p = project(tmp_path)
    p["properties"] = [
        {"id": "a", "sink": "q", "equals": 255},
        {
            "id": "b",
            "sink": "q",
            "source": "d",
            "latency": 1,
            "when": [{"signal": "en", "equals": 0}, {"signal": "en", "equals": 1}],
        },
    ]
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 2
    assert [r["status"] for r in r["results"]] == ["counterexample", "uncovered"]


def test_receipt_bool_trace_tamper_rejected(tmp_path: Path) -> None:
    p = project(tmp_path)
    p["properties"][0]["when"] = []
    r = run_request(p, root=tmp_path)
    r["results"][0]["trace"][0]["values"]["en"] = False
    r["receipt_sha256"] = digest({k: v for k, v in r.items() if k != "receipt_sha256"})
    with pytest.raises(SequentialError, match="reverification"):
        replay(p, r, root=tmp_path)


def test_schema_conformance(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from opencollate.cli import _schema_text

    p = project(tmp_path)
    req_schema = json.loads(_schema_text("sequential-request"))
    receipt_schema = json.loads(_schema_text("sequential-receipt"))
    Draft202012Validator.check_schema(req_schema)
    Draft202012Validator.check_schema(receipt_schema)
    Draft202012Validator(req_schema).validate(p)
    for changes in ({}, {"induction": 0}, {"assumptions": {"en": 0}}):
        spec = {**p, **changes}
        Draft202012Validator(receipt_schema).validate(run_request(spec, root=tmp_path))
    p["properties"][0]["when"] = []
    Draft202012Validator(receipt_schema).validate(run_request(p, root=tmp_path))


def test_portable_receipts_under_directory_move(tmp_path: Path) -> None:
    p = project(tmp_path)
    r = run_request(p, root=tmp_path)
    moved = tmp_path / "new"
    moved.mkdir()
    (moved / "design.sv").write_text(PIPE)
    assert replay(p, r, root=moved) == r


def test_guard_executes_sequential_checker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = project(tmp_path)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(p))
    assert main(["guard", "--wall-seconds", "30", "--", "sequential", "check", str(path)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "proven"


def test_missing_solver_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opencollate import sequential_smt

    real = sequential_smt.importlib.import_module

    def missing(module: str) -> object:
        if module == "z3":
            raise ModuleNotFoundError("missing optional solver")
        return real(module)

    monkeypatch.setattr(sequential_smt.importlib, "import_module", missing)
    result = run_request(project(tmp_path), root=tmp_path)
    assert result["exit_code"] == 2
    assert "missing optional solver" in result["results"][0]["reason"]


def test_output_failure_is_status_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = project(tmp_path)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(p))
    out = tmp_path / "directory"
    out.mkdir()
    assert main(["sequential", "check", str(path), "--output", str(out)]) == 2
    assert "publication failed" in capsys.readouterr().out


def test_benchmark_proof_boundaries() -> None:
    from benchmarks.sequential import run_suite

    report = run_suite(repeat=1)
    assert report["status"] == "pass"
    assert len(report["cases"]) == 12
    cases = {row["name"]: row for row in report["cases"]}
    assert cases["late-failure-deep"]["counterexample_cycle"] == 10
    assert cases["late-failure-shallow"]["actual"] == "bounded"
    assert cases["pipeline-256x16"]["state_bits"] == 4096

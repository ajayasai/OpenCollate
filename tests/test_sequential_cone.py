from __future__ import annotations

import copy
import itertools
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from opencollate.sequential import digest, replay, run_request
from opencollate.sequential_cone import cone_summary, expand_trace, property_cone
from opencollate.sequential_ir import Circuit, SequentialError, simulate_frame
from opencollate.sequential_rtl import load_circuit
from opencollate.sequential_smt import Budget, replay_trace
from opencollate.sequential_spec import normalize


def setup(tmp: Path, *, mutation: bool = False, guard_state: bool = False) -> dict:
    src = f"""module top(input logic clk,rst,en,d,output logic q);
    logic a,b,hold;
    logic [3:0] ballast;
    always_ff @(posedge clk) begin
      if(rst) begin a<=0; b<=0; q<=0; ballast<=0; hold<=0; end
      else begin
        a <= d; b <= a; q <= {"~b" if mutation else "b"};
        ballast <= ballast+1'b1;
        hold <= !hold;
      end
    end
    endmodule"""
    (tmp / "design.sv").write_text(src)
    return {
        "schema_version": 1,
        "semantics": "two-valued-synchronous",
        "top": "top",
        "clock": "clk",
        "files": ["design.sv"],
        "reset": {"signal": "rst", "active": 1},
        "depth": 7,
        "induction": 4,
        "properties": [
            {
                "id": "data",
                "sink": "q",
                "source": "d",
                "latency": 3,
                "when": [{"signal": "hold", "equals": 1}] if guard_state else [],
            }
        ],
    }


@pytest.mark.parametrize(
    "mutation,guard_state,induction", list(itertools.product([False, True], [False, True], [0, 4]))
)
def test_reduced_and_full_verification_outcomes_and_complete_witnesses(
    tmp_path: Path, mutation: bool, guard_state: bool, induction: int
) -> None:
    p = setup(tmp_path, mutation=mutation, guard_state=guard_state)
    p["induction"] = induction
    small = run_request(p, root=tmp_path)
    full = run_request(p, root=tmp_path, cone_reduction=False)
    assert small["results"] == full["results"]
    assert small["exit_code"] == full["exit_code"]
    assert small["binding"] == full["binding"]
    assert small["model"]["state_bits"] == 8
    assert small["model"]["proof_cones"]["data"]["state_bits"] == (4 if guard_state else 3)
    if mutation:
        trace = small["results"][0]["trace"]
        assert "ballast" in trace[0]["values"]
        assert trace[2]["values"]["ballast"] == 1
        c = load_circuit(p["files"], root=tmp_path, top="top", clock="clk")
        spec = normalize(p)
        assert replay_trace(c, spec, spec["properties"][0], [r["values"] for r in trace])


def test_nonlocal_feedback_and_guard_history_in_cone(tmp_path: Path) -> None:
    p = setup(tmp_path)
    file = tmp_path / "design.sv"
    file.write_text(file.read_text().replace("a <= d", "a <= d ^ hold"))
    c = load_circuit(p["files"], root=tmp_path, top="top", clock="clk")
    prop = normalize(p)["properties"][0]
    reduced = property_cone(c, prop)
    assert set(reduced.next_state) == {"a", "b", "q", "hold"}
    # Feedback must be closed through ALL retained state transitions, not only one edge.
    assert cone_summary(reduced)["state_bits"] == 4
    prop["when"] = [{"signal": "ballast", "equals": 1, "lag": 4}]
    assert property_cone(c, prop).next_state.keys() == c.next_state.keys()


def test_reset_and_fixed_unused_primary_inputs_preserved(tmp_path: Path) -> None:
    p = setup(tmp_path)
    p["assumptions"] = {"en": 1}
    p["properties"][0]["when"] = [{"signal": "en", "equals": 0}]
    a = run_request(p, root=tmp_path)
    b = run_request(p, root=tmp_path, cone_reduction=False)
    assert a["results"] == b["results"]
    assert a["results"][0]["status"] == "uncovered"


def test_reduction_does_not_skip_unsupported_irrelevant_logic(tmp_path: Path) -> None:
    p = setup(tmp_path)
    f = tmp_path / "design.sv"
    f.write_text(f.read_text().replace("endmodule", "initial ballast=0; endmodule"))
    with pytest.raises(SequentialError):
        run_request(p, root=tmp_path)


def test_each_property_gets_its_own_closed_cone(tmp_path: Path) -> None:
    p = setup(tmp_path)
    p["properties"].append({"id": "constant", "sink": "ballast", "equals": 0, "start_cycle": 1})
    r = run_request(p, root=tmp_path)
    assert r["model"]["proof_cones"]["data"]["states"] == ["a", "b", "q"]
    assert r["model"]["proof_cones"]["constant"]["states"] == ["ballast"]
    assert r["exit_code"] == 1


def test_false_or_inconsistent_cone_metadata_cannot_replay(tmp_path: Path) -> None:
    p = setup(tmp_path)
    r = run_request(p, root=tmp_path)
    assert r == replay(p, r, root=tmp_path)
    r["model"]["proof_cones"]["data"]["state_bits"] = 0
    r["receipt_sha256"] = digest({k: v for k, v in r.items() if k != "receipt_sha256"})
    with pytest.raises(SequentialError, match="reverification"):
        replay(p, r, root=tmp_path)


def test_corrupt_cone_witness_never_exported(tmp_path: Path) -> None:
    p = setup(tmp_path, mutation=True)
    c = load_circuit(p["files"], root=tmp_path, top="top", clock="clk")
    spec = normalize(p)
    r = run_request(p, root=tmp_path)
    row = copy.deepcopy(r["results"][0])
    row["trace"][1]["values"]["q"] ^= 1
    with pytest.raises(SequentialError):
        expand_trace(c, spec, spec["properties"][0], row, Budget(10000, 1000000))


def test_finalize_order_is_idempotent_and_has_no_cycles(tmp_path: Path) -> None:
    p = setup(tmp_path)
    c = load_circuit(p["files"], root=tmp_path, top="top", clock="clk")
    first = list(c.comb_order)
    c.finalize()
    assert c.comb_order == first
    c.combinational["cycle"] = c.add("ref", 1, value="cycle")
    c.signals["cycle"] = (1, False)
    with pytest.raises(SequentialError, match="cycle"):
        c.finalize()


@given(st.lists(st.integers(min_value=0, max_value=15), min_size=1, max_size=12))
@settings(max_examples=75, deadline=None)
def test_projection_preserves_all_future_transitions(inputs: list[int]) -> None:
    c = Circuit("top", "clk")
    c.signals = {
        "clk": (1, False),
        "d": (4, False),
        "a": (4, False),
        "b": (4, False),
        "noise": (4, False),
    }
    c.inputs = ["d"]
    c.outputs = ["b"]
    c.next_state = {
        "a": c.ref("d"),
        "b": c.ref("a"),
        "noise": c.add("Add", 4, args=(c.ref("noise"), c.add("const", 4, value=1))),
    }
    c.finalize()
    p = {"sink": "b", "source": "d", "when": [], "latency": 2}
    small = property_cone(c, p)
    for initial in (0, 7, 15):
        full_state = {"a": initial, "b": 15 - initial, "noise": initial}
        small_state = {n: full_state[n] for n in small.next_state}
        for d in inputs:
            full_frame, full_state = simulate_frame(c, full_state, {"d": d})
            small_frame, small_state = simulate_frame(small, small_state, {"d": d})
            assert small_frame == {n: full_frame[n] for n in small_frame}
            assert small_state == {n: full_state[n] for n in small_state}


def test_no_cone_cli_and_replay(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from opencollate.cli import main

    p = setup(tmp_path)
    request = tmp_path / "request.json"
    receipt = tmp_path / "receipt.json"
    request.write_text(json.dumps(p))
    assert main(["sequential", "check", str(request), "--no-cone", "--output", str(receipt)]) == 0
    assert main(["sequential", "replay", str(request), str(receipt), "--no-cone"]) == 0
    assert json.loads(capsys.readouterr().out)["limits"]["cone_reduction"] is False
    assert main(["sequential", "replay", str(request), str(receipt)]) == 2


def test_invalid_reduction_option(tmp_path: Path) -> None:
    with pytest.raises(SequentialError, match="boolean"):
        run_request(setup(tmp_path), root=tmp_path, cone_reduction=1)  # type: ignore[arg-type]


def test_replayed_mode_cannot_be_forged(tmp_path: Path) -> None:
    p = setup(tmp_path)
    r = run_request(p, root=tmp_path)
    r["limits"]["cone_reduction"] = False
    r["receipt_sha256"] = digest({k: v for k, v in r.items() if k != "receipt_sha256"})
    with pytest.raises(SequentialError, match="reverification"):
        replay(p, r, root=tmp_path)


def test_source_hierarchy_benchmark_matches_unreduced_and_mutant(tmp_path: Path) -> None:
    from benchmarks.hierarchy import run_suite

    r = run_suite(distractors=4, repeat=1)
    assert r["status"] == "pass"
    assert r["full_model"]["state_bits"] == 144
    assert r["property_cone"]["state_bits"] == 16
    assert r["verification"]["mutant_results_and_full_traces_identical"]

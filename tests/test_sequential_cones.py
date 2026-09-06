from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from opencollate.cli import main
from opencollate.sequential import digest, replay, run_request
from opencollate.sequential_coi import lift_trace, select_cone
from opencollate.sequential_ir import Circuit, SequentialError, simulate_frame
from opencollate.sequential_smt import replay_trace
from opencollate.sequential_spec import normalize
from tests.test_sequential_hierarchy import LEAF, TOP, circuit, request


def noisy(tmp_path: Path, *, mutate: bool = False) -> dict:
    extra = """module counter(input wire clk,rst_n, input wire [7:0] feed,
      output logic [7:0] q);
      always_ff @(posedge clk) if (!rst_n) q<=0; else q<=q*3+feed;
    endmodule"""
    text = TOP.replace(
        "endmodule",
        """
      for(genvar j=0;j<12;j++) begin: unrelated
        wire [7:0] data;
        counter c(clk,rst_n,d,data);
      end
      endmodule""",
    )
    leaf = LEAF.replace("q <= d", "q <= ~d") if mutate else LEAF
    p = request(tmp_path, text, leaf + extra)
    if mutate:
        # Two inversions cancel, so use the first-stage property to expose it.
        p["properties"] = [
            {
                "id": "data",
                "sink": "first.q",
                "source": "d",
                "latency": 1,
                "when": [{"signal": "en", "equals": 1, "lag": 1}],
            }
        ]
    return p


@pytest.mark.parametrize("mutate", [False, True])
def test_reduction_preserves_outcomes_and_full_traces(tmp_path: Path, mutate: bool) -> None:
    p = noisy(tmp_path, mutate=mutate)
    reduced = run_request(p, root=tmp_path)
    full = run_request(p, root=tmp_path, cone_reduction=False)
    assert reduced["binding"] == full["binding"]
    assert reduced["results"] == full["results"]
    assert reduced["model"]["state_bits"] == full["model"]["state_bits"] == 112
    identity = p["properties"][0]["id"]
    assert reduced["model"]["property_cones"][identity]["state_bits"] == (8 if mutate else 16)
    assert full["model"]["property_cones"][identity]["state_bits"] == 112
    assert reduced["exit_code"] == full["exit_code"] == int(mutate)
    assert replay(p, reduced, root=tmp_path) == reduced
    assert replay(p, full, root=tmp_path) == full
    if mutate:
        c = circuit(tmp_path)
        frames = [row["values"] for row in reduced["results"][0]["trace"]]
        assert replay_trace(c, normalize(p), normalize(p)["properties"][0], frames)
        assert "unrelated[11].c.q" in frames[-1]


def test_late_temporal_dependency_is_not_discarded(tmp_path: Path) -> None:
    leaf = """module delayed(input wire ck,rst_n,output wire alarm);
      logic [3:0] count; logic a,b;
      always_ff @(posedge ck) begin
       if (!rst_n) begin count<=0; a<=0; b<=0; end
       else begin count<=count+1'b1; a<=count==9; b<=a; end
      end
      assign alarm=b;
    endmodule"""
    text = """module top(input wire clk,rst_n,output wire q);
     delayed u(clk,rst_n,q); endmodule"""
    p = request(
        tmp_path, text, leaf, properties=[{"id": "late", "sink": "q", "equals": 0}], depth=14
    )
    full = run_request(p, root=tmp_path, cone_reduction=False)
    reduced = run_request(p, root=tmp_path)
    assert full["results"] == reduced["results"]
    assert reduced["results"][0]["checked_through"] == 12
    assert reduced["model"]["property_cones"]["late"]["state_bits"] == 6
    p["depth"] = 6
    assert run_request(p, root=tmp_path)["results"][0]["status"] == "bounded"


def test_guard_only_state_is_kept(tmp_path: Path) -> None:
    p = noisy(tmp_path)
    p["properties"][0]["when"].append({"signal": "unrelated[7].c.q", "equals": 3})
    a = run_request(p, root=tmp_path)
    b = run_request(p, root=tmp_path, cone_reduction=False)
    assert a["results"] == b["results"]
    assert a["model"]["property_cones"]["pipeline"]["state_bits"] == 24


def test_reset_and_fixed_inputs_kept_even_when_property_independent(tmp_path: Path) -> None:
    text = """module top(input wire clk,rst_n,en,d,output wire q); assign q=d; endmodule"""
    p = request(
        tmp_path,
        text,
        "",
        assumptions={"en": 1},
        properties=[{"id": "constant", "sink": "q", "equals": 0}],
    )
    a = run_request(p, root=tmp_path)
    b = run_request(p, root=tmp_path, cone_reduction=False)
    assert a["results"] == b["results"] and a["exit_code"] == 1
    frames = a["results"][0]["trace"]
    assert all(row["values"]["en"] == 1 for row in frames)
    assert frames[0]["values"]["rst_n"] == 0 and frames[-1]["values"]["rst_n"] == 1


def test_invalid_ir_outside_property_cone_still_rejected(tmp_path: Path) -> None:
    text = TOP.replace("endmodule", "async_cell bad(clk,rst_n,d); endmodule")
    leaf = (
        LEAF
        + """module async_cell(input wire clk,rst_n,input wire[7:0] d);
      logic [7:0] internal_q;
      always @(posedge clk or negedge rst_n) if(!rst_n) internal_q<=0; else internal_q<=d;
    endmodule"""
    )
    p = request(tmp_path, text, leaf)
    with pytest.raises(SequentialError, match="asynchronous"):
        run_request(p, root=tmp_path)


def test_unrelated_combinational_cycle_still_rejected(tmp_path: Path) -> None:
    text = TOP.replace("endmodule", "wire x,y; assign x=y; assign y=x; endmodule")
    p = request(tmp_path, text)
    with pytest.raises(SequentialError, match="cycle"):
        run_request(p, root=tmp_path)


def test_cones_are_property_specific_and_order_independent(tmp_path: Path) -> None:
    p = noisy(tmp_path)
    p["properties"].append(
        {
            "id": "first-only",
            "source": "d",
            "sink": "first.q",
            "latency": 1,
            "when": [{"signal": "en", "equals": 1, "lag": 1}],
        }
    )
    a = run_request(p, root=tmp_path)
    p["properties"].reverse()
    b = run_request(p, root=tmp_path)
    assert a == b and a["exit_code"] == 0
    assert a["model"]["property_cones"]["first-only"]["state_bits"] == 8
    assert a["model"]["property_cones"]["pipeline"]["state_bits"] == 16


@pytest.mark.parametrize("value", [0, 1, "yes", None])
def test_reduction_mode_requires_boolean(tmp_path: Path, value: object) -> None:
    with pytest.raises(SequentialError, match="boolean"):
        run_request(request(tmp_path), root=tmp_path, cone_reduction=value)


@pytest.mark.parametrize("field", ["state_bits", "ir_sha256"])
def test_tampered_cone_metadata_rejected_even_with_new_receipt_hash(
    tmp_path: Path, field: str
) -> None:
    p = noisy(tmp_path)
    r = run_request(p, root=tmp_path)
    r["model"]["property_cones"]["pipeline"][field] = 0 if field == "state_bits" else "0" * 64
    r["receipt_sha256"] = digest({k: v for k, v in r.items() if k != "receipt_sha256"})
    with pytest.raises(SequentialError, match="disagrees"):
        replay(p, r, root=tmp_path)


def test_source_change_in_omitted_child_invalidates_receipt(tmp_path: Path) -> None:
    p = noisy(tmp_path)
    r = run_request(p, root=tmp_path)
    path = tmp_path / "cells.sv"
    path.write_text(path.read_text().replace("q*3+feed", "q*5+feed"))
    assert run_request(p, root=tmp_path)["exit_code"] == 0
    with pytest.raises(SequentialError, match="changed"):
        replay(p, r, root=tmp_path)


@pytest.mark.parametrize("change", ["cycle", "value", "empty"])
def test_lift_rejects_bad_projected_witness(tmp_path: Path, change: str) -> None:
    p = noisy(tmp_path, mutate=True)
    c = circuit(tmp_path)
    spec = normalize(p)
    prop = spec["properties"][0]
    reduced = select_cone(c, spec, prop)
    r = run_request(p, root=tmp_path)
    trace = copy.deepcopy(r["results"][0]["trace"])
    keep = set(reduced.inputs) | set(reduced.next_state) | set(reduced.combinational)
    for row in trace:
        row["values"] = {n: v for n, v in row["values"].items() if n in keep}
    if change == "cycle":
        trace[0]["cycle"] = 2
    elif change == "value":
        trace[-1]["values"]["first.q"] ^= 1
    else:
        trace = []
    with pytest.raises(SequentialError):
        lift_trace(c, reduced, spec, prop, trace)


def test_lift_disagreement_fails_closed(tmp_path: Path, monkeypatch) -> None:
    import opencollate.sequential as seq

    p = noisy(tmp_path, mutate=True)

    def bad(*args, **kwargs):
        raise SequentialError("injected lift disagreement")

    monkeypatch.setattr(seq, "lift_trace", bad)
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 2 and r["results"][0]["trace"] is None
    assert "lift disagreement" in r["results"][0]["reason"]


def test_cli_reference_path_and_replay_mode(tmp_path: Path, capsys) -> None:
    p = noisy(tmp_path)
    path = tmp_path / "request.json"
    receipt = tmp_path / "receipt.json"
    path.write_text(json.dumps(p))
    assert main(["sequential", "check", str(path), "--no-cone-reduction", "-o", str(receipt)]) == 0
    r = json.loads(receipt.read_text())
    assert r["limits"]["cone_reduction"] is False
    assert main(["sequential", "replay", str(path), str(receipt)]) == 0
    assert json.loads(capsys.readouterr().out)["model"] == r["model"]


def test_old_receipt_version_is_not_silently_upgraded(tmp_path: Path) -> None:
    p = request(tmp_path)
    r = run_request(p, root=tmp_path)
    r["schema_version"] = 1
    r["receipt_sha256"] = digest({k: v for k, v in r.items() if k != "receipt_sha256"})
    with pytest.raises(SequentialError, match="version"):
        replay(p, r, root=tmp_path)


def test_dependency_sort_is_idempotent_and_detects_cycles() -> None:
    c = Circuit("top", "clk")
    c.signals = {"clk": (1, False), "d": (1, False), **{f"n{i}": (1, False) for i in range(200)}}
    c.inputs = ["d"]
    c.outputs = ["n199"]
    previous = "d"
    for i in range(200):
        c.combinational[f"n{i}"] = c.ref(previous)
        previous = f"n{i}"
    c.finalize()
    order = list(c.comb_order)
    c.finalize()
    assert c.comb_order == order and len(order) == 200
    assert simulate_frame(c, {}, {"d": 1})[0]["n199"] == 1
    c.combinational["n0"] = c.ref("n199")
    with pytest.raises(SequentialError, match="cycle"):
        c.finalize()

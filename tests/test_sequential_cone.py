from __future__ import annotations

import copy
import itertools
from pathlib import Path

import pytest

from opencollate.sequential_cone import expand_trace, property_cone
from opencollate.sequential_ir import Circuit, SequentialError, simulate_frame


def model(width: int = 2) -> Circuit:
    c = Circuit(top="pipe", clock="clk")
    c.signals = {n: (width, False) for n in ("d", "q", "stage", "side", "out", "side_out")}
    c.signals.update({n: (1, False) for n in ("clk", "gate", "enable", "rst", "fixed", "spare")})
    c.inputs = ["d", "gate", "rst", "fixed", "spare"]
    c.outputs = ["out", "side_out"]
    c.next_state = {"stage": c.ref("d"), "q": c.ref("stage")}
    c.next_state["side"] = c.add("Add", width, args=(c.ref("side"), c.add("const", width, value=1)))
    c.combinational = {
        "out": c.ref("q"),
        "enable": c.ref("gate"),
        "side_out": c.add("BinaryXor", width, args=(c.ref("side"), c.ref("q"))),
    }
    c.locations = {n: {"line": i + 1} for i, n in enumerate(c.signals)}
    c.sources = [{"path": "design.sv", "sha256": "source-bound-placeholder"}]
    c.finalize()
    return c


def setup() -> tuple[dict, dict]:
    return (
        {"assumptions": {"fixed": 1}, "reset": {"signal": "rst", "active": 1, "cycles": 1}},
        {
            "id": "transfer",
            "source": "d",
            "sink": "out",
            "latency": 2,
            "when": [{"signal": "enable", "equals": 1, "lag": 2}],
        },
    )


def test_closure_compaction_provenance_and_no_mutation() -> None:
    c = model()
    before = copy.deepcopy(c.serialized())
    spec, prop = setup()
    cone = property_cone(c, spec, prop)
    assert set(cone.next_state) == {"stage", "q"}
    assert set(cone.combinational) == {"out", "enable"}
    assert set(cone.inputs) == {"d", "gate", "rst", "fixed"}
    assert set(cone.outputs) == {"out"}
    assert len(cone.nodes) < len(c.nodes)
    assert cone.sources == c.sources and cone.sources is not c.sources
    assert cone.locations["q"] == c.locations["q"]
    assert c.serialized() == before
    assert property_cone(cone, spec, prop).serialized() == cone.serialized()
    assert all(a < i for i, n in enumerate(cone.nodes) for a in n.args)


def test_state_feedback_closure_is_not_bounded_by_property_latency() -> None:
    c = model()
    c.next_state["stage"] = c.add("BinaryXor", 2, args=(c.ref("d"), c.ref("side")))
    spec, prop = setup()
    prop["latency"] = 0
    cone = property_cone(c, spec, prop)
    assert set(cone.next_state) == {"stage", "q", "side"}
    assert "side_out" not in cone.combinational


def test_guard_only_state_dependency_is_retained() -> None:
    c = model()
    spec, prop = setup()
    prop["when"] = [{"signal": "side", "equals": 1, "lag": 16}]
    cone = property_cone(c, spec, prop)
    assert "side" in cone.next_state
    assert "gate" not in cone.inputs


def test_constant_property_without_reset_or_guards() -> None:
    c = model()
    cone = property_cone(
        c, {"assumptions": {}, "reset": None}, {"sink": "side", "equals": 0, "when": []}
    )
    assert set(cone.next_state) == {"side"}
    assert not cone.inputs and not cone.combinational


def test_exhaustive_projection_commutes_with_transition() -> None:
    c = model()
    spec, prop = setup()
    cone = property_cone(c, spec, prop)
    for q, stage, side, d, gate in itertools.product(
        range(4), range(4), range(4), range(4), range(2)
    ):
        states = {"q": q, "stage": stage, "side": side}
        inputs = {"d": d, "gate": gate, "rst": 1, "fixed": 1, "spare": side % 2}
        full_frame, full_next = simulate_frame(c, states, inputs)
        projected, projected_next = simulate_frame(
            cone, {n: states[n] for n in cone.next_state}, {n: inputs[n] for n in cone.inputs}
        )
        assert projected == {n: full_frame[n] for n in projected}
        assert projected_next == {n: full_next[n] for n in projected_next}


def sample_trace(c: Circuit, frames: int = 4) -> list[dict]:
    state = {n: 0 for n in c.next_state}
    trace = []
    for t in range(frames):
        inputs = {n: 0 for n in c.inputs}
        inputs.update(d=t % 4, gate=1, rst=int(t == 0), fixed=1)
        observed, state = simulate_frame(c, state, inputs)
        trace.append({"cycle": t, "values": observed})
    return trace


def test_expansion_computes_removed_later_state_and_downstream_logic() -> None:
    full = model()
    cone = property_cone(full, *setup())
    trace = sample_trace(cone)
    expanded = expand_trace(full, cone, trace)
    assert [row["values"]["side"] for row in expanded] == [0, 1, 2, 3]
    for small, large in zip(trace, expanded, strict=True):
        assert {n: large["values"][n] for n in small["values"]} == small["values"]
        assert large["values"]["side_out"] == large["values"]["side"] ^ large["values"]["q"]
        assert large["values"]["spare"] == 0


@pytest.mark.parametrize(
    "mutation",
    ["empty", "cycle", "boolcycle", "missing", "extra", "bool", "range", "state", "comb"],
)
def test_expansion_rejects_corruption(mutation: str) -> None:
    full = model()
    cone = property_cone(full, *setup())
    trace = sample_trace(cone)
    if mutation == "empty":
        trace = []
    elif mutation == "cycle":
        trace[1]["cycle"] = 8
    elif mutation == "boolcycle":
        trace[0]["cycle"] = False
    elif mutation == "missing":
        del trace[1]["values"]["q"]
    elif mutation == "extra":
        trace[1]["values"]["ghost"] = 0
    elif mutation == "bool":
        trace[1]["values"]["gate"] = True
    elif mutation == "range":
        trace[1]["values"]["q"] = 4
    elif mutation == "state":
        trace[1]["values"]["q"] ^= 1
    else:
        trace[1]["values"]["out"] ^= 1
    with pytest.raises(SequentialError):
        expand_trace(full, cone, trace)


@pytest.mark.parametrize("signal", ["ghost", "clk"])
def test_missing_or_clock_seed_is_rejected(signal: str) -> None:
    spec, prop = setup()
    prop["sink"] = signal
    with pytest.raises(SequentialError, match="unresolved"):
        property_cone(model(), spec, prop)


def test_unfinalized_full_circuit_is_rejected() -> None:
    c = model()
    c.comb_order = []
    with pytest.raises(SequentialError, match="finalized"):
        property_cone(c, *setup())


def rtl_case(width: int, variant: str, islands: int = 6) -> tuple[str, dict]:
    noise = "\n".join(
        f"logic [{width - 1}:0] noise{i}; always_ff @(posedge clk) "
        f"if (rst) noise{i} <= '0; else noise{i} <= noise{i} + 1'b1;"
        for i in range(islands)
    )
    update = "q <= d;" if variant != "inversion" else "q <= ~d;"
    rtl = f"""module pipe(input logic clk, rst, en, fixed, spare,
        input logic [{width - 1}:0] d, output logic [{width - 1}:0] q);
        {noise}
        always_ff @(posedge clk) if (rst) q <= '0; else if (en) {update}
        endmodule"""
    prop = {
        "id": "data",
        "source": "d",
        "sink": "q",
        "latency": 1,
        "when": [{"signal": "en", "equals": 1, "lag": 1}],
    }
    request = {
        "schema_version": 1,
        "semantics": "two-valued-synchronous",
        "files": ["design.sv"],
        "top": "pipe",
        "clock": "clk",
        "depth": 5,
        "induction": 3,
        "reset": {"signal": "rst", "active": 1},
        "assumptions": {"fixed": 1},
        "properties": [prop],
    }
    if variant == "unguarded":
        prop["when"] = []
    elif variant == "uncovered":
        request["assumptions"]["en"] = 0
    elif variant == "bounded":
        request["induction"] = 0
    elif variant == "initial":
        prop.clear()
        prop.update(id="initial", sink="q", equals=0, start_cycle=0)
    elif variant == "guard-state":
        prop["when"] = [{"signal": "noise0", "equals": 1, "lag": 1}]
    elif variant == "feedback":
        rtl = rtl.replace("q <= d;", "q <= d ^ noise0;")
    return rtl, request


@pytest.mark.parametrize("width", [1, 4, 16, 64, 256])
@pytest.mark.parametrize(
    "variant",
    [
        "correct",
        "inversion",
        "unguarded",
        "uncovered",
        "bounded",
        "initial",
        "guard-state",
        "feedback",
    ],
)
def test_source_derived_sliced_and_full_results_match(
    tmp_path: Path, width: int, variant: str
) -> None:
    pytest.importorskip("z3")
    pytest.importorskip("pyslang")
    from opencollate.sequential_cone import verify_cone_property
    from opencollate.sequential_rtl import load_circuit
    from opencollate.sequential_smt import Budget, replay_trace, verify_property
    from opencollate.sequential_spec import normalize, validate_signals

    rtl, request = rtl_case(width, variant)
    (tmp_path / "design.sv").write_text(rtl)
    spec = normalize(request)
    full = load_circuit(spec["files"], root=tmp_path, top="pipe", clock="clk")
    validate_signals(full, spec)
    prop = spec["properties"][0]
    baseline = verify_property(full, spec, prop, Budget(60000, 1000000))
    optimized = verify_cone_property(full, spec, prop, Budget(60000, 1000000))
    assert baseline["status"] != "inconclusive", baseline
    assert optimized == baseline
    if optimized["trace"]:
        frames = [row["values"] for row in optimized["trace"]]
        assert replay_trace(full, spec, prop, frames)
        assert all("noise5" in frame and "fixed" in frame and "spare" in frame for frame in frames)


def test_full_rtl_validation_cannot_be_hidden_by_slicing(tmp_path: Path) -> None:
    pytest.importorskip("z3")
    pytest.importorskip("pyslang")
    from opencollate.sequential import run_request

    rtl, request = rtl_case(4, "correct")
    rtl = rtl.replace("endmodule", "logic hidden; always @(negedge clk) hidden <= en; endmodule")
    (tmp_path / "design.sv").write_text(rtl)
    with pytest.raises(SequentialError):
        run_request(request, root=tmp_path)


def test_full_source_binding_detects_changed_unrelated_logic(tmp_path: Path) -> None:
    pytest.importorskip("z3")
    pytest.importorskip("pyslang")
    from opencollate.sequential import replay, run_request

    rtl, request = rtl_case(4, "correct")
    path = tmp_path / "design.sv"
    path.write_text(rtl)
    receipt = run_request(request, root=tmp_path)
    assert receipt["exit_code"] == 0
    path.write_text(rtl.replace("noise5 + 1'b1", "noise5 + 2'd2"))
    with pytest.raises(SequentialError, match="changed"):
        replay(request, receipt, root=tmp_path)


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5, None])
def test_wrapper_never_publishes_failed_replay_or_expired_witness(
    monkeypatch: pytest.MonkeyPatch, fail_at: int | None
) -> None:
    from opencollate import sequential_smt
    from opencollate.sequential_cone import verify_cone_property

    full = model()
    spec, prop = setup()
    cone = property_cone(full, spec, prop)
    claimed = {
        "id": prop["id"],
        "status": "counterexample",
        "checked_through": 3,
        "induction_depth": None,
        "cover_cycle": 2,
        "trace": sample_trace(cone),
        "reason": None,
    }
    monkeypatch.setattr(sequential_smt, "verify_property", lambda *args: copy.deepcopy(claimed))
    monkeypatch.setattr(sequential_smt, "replay_trace", lambda *args: fail_at is not None)

    class Deadline:
        calls = 0

        def remaining(self) -> int:
            self.calls += 1
            if self.calls == fail_at:
                raise SequentialError("test deadline exhausted")
            return 1000

    result = verify_cone_property(full, spec, prop, Deadline())
    assert result["status"] == "inconclusive"
    assert result["trace"] is None
    expected = "deadline" if fail_at else "full-circuit replay"
    assert expected in result["reason"]

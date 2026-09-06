from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from opencollate.sequential import replay, run_request
from opencollate.sequential_ir import SequentialError, simulate_frame
from opencollate.sequential_rtl import load_circuit
from opencollate.sequential_spec import normalize, validate_signals

LEAF = """module delay_cell #(parameter W=8)(
 input wire clock_in, input wire rst_n, en, input wire [W-1:0] d,
 output logic [W-1:0] q);
 always_ff @(posedge clock_in) if (!rst_n) q <= 0; else if (en) q <= d;
endmodule
"""
TOP = """module top(input wire clk,rst_n,en,input wire [7:0] d,output wire [7:0] q);
 wire [7:0] middle;
 delay_cell first(.clock_in(clk),.rst_n(rst_n),.en(en),.d(d),.q(middle));
 delay_cell second(clk,rst_n,en,middle,q);
endmodule
"""


def request(tmp_path: Path, text: str = TOP, leaf: str = LEAF, **kwargs: object) -> dict:
    (tmp_path / "top.sv").write_text(text, encoding="utf-8")
    (tmp_path / "cells.sv").write_text(leaf, encoding="utf-8")
    return {
        "schema_version": 1,
        "semantics": "two-valued-synchronous",
        "files": ["top.sv", "cells.sv"],
        "top": "top",
        "clock": "clk",
        "reset": {"signal": "rst_n", "active": 0},
        "depth": 6,
        "induction": 4,
        "properties": [
            {
                "id": "pipeline",
                "sink": "q",
                "source": "d",
                "latency": 2,
                "when": [{"signal": "en", "equals": 1, "lag": n} for n in (1, 2)],
            }
        ],
        **kwargs,
    }


def circuit(tmp_path: Path):
    return load_circuit(["top.sv", "cells.sv"], root=tmp_path, top="top", clock="clk")


def test_hierarchy_proven_without_flattened_sources(tmp_path: Path) -> None:
    p = request(tmp_path)
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 0 and r["results"][0]["status"] == "proven"
    assert r["model"]["states"] == ["first.q", "second.q"]
    assert r["model"]["state_bits"] == 16
    assert r["model"]["locations"]["first.q"]["source"] == "cells.sv"
    assert r["model"]["locations"]["second.q"]["source"] == "cells.sv"
    assert replay(p, r, root=tmp_path) == r
    assert r == run_request(p, root=tmp_path)


def test_instances_do_not_share_register_state(tmp_path: Path) -> None:
    request(tmp_path)
    c = circuit(tmp_path)
    observed, states = simulate_frame(
        c, {"first.q": 13, "second.q": 90}, {"rst_n": 1, "en": 1, "d": 57}
    )
    assert observed["q"] == 90 and observed["middle"] == 13
    assert states == {"first.q": 57, "second.q": 13}


@pytest.mark.parametrize("width", [1, 4, 16, 64, 128, 256])
def test_named_parameter_overrides(tmp_path: Path, width: int) -> None:
    text = TOP.replace("[7:0]", f"[{width - 1}:0]")
    text = text.replace("delay_cell first", f"delay_cell #(.W({width})) first")
    text = text.replace("delay_cell second", f"delay_cell #({width}) second")
    p = request(tmp_path, text)
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 0 and r["model"]["state_bits"] == 2 * width


def test_different_instance_parameter_values(tmp_path: Path) -> None:
    text = TOP.replace("wire [7:0] middle;", "wire [3:0] middle;")
    text = text.replace("delay_cell first", "delay_cell #(.W(4)) first")
    p = request(tmp_path, text)
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 1  # Truncation really changes the checked interface.
    assert r["model"]["state_bits"] == 12
    assert r["results"][0]["trace"][-1]["values"]["q"] != 16


def test_nested_named_instances_and_clock_forwarding(tmp_path: Path) -> None:
    wrapper = """module wrapper(input wire ck,rst_n,en,input wire [7:0] d,output wire [7:0] q);
      delay_cell inner(ck,rst_n,en,d,q); endmodule\n"""
    p = request(
        tmp_path, TOP.replace("delay_cell", "wrapper").replace(".clock_in(", ".ck("), LEAF + wrapper
    )
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 0
    assert r["model"]["states"] == ["first.inner.q", "second.inner.q"]
    assert "first.ck" not in r["model"]["locations"]


def test_generate_instances_and_internal_signal_properties(tmp_path: Path) -> None:
    text = """module top(input wire clk,rst_n,en,input wire [7:0] d, output wire [7:0] q);
      for (genvar i=0; i<3; i++) begin: lanes
        wire [7:0] data;
        delay_cell #(.W(8)) u(clk,rst_n,en,d,data);
      end
      assign q=lanes[1].data;
    endmodule"""
    p = request(tmp_path, text)
    p["properties"] = [
        {
            "id": str(i),
            "sink": f"lanes[{i}].u.q",
            "source": "d",
            "latency": 1,
            "when": [{"signal": f"lanes[{i}].u.en", "equals": 1, "lag": 1}],
        }
        for i in range(3)
    ]
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 0 and r["model"]["state_bits"] == 24
    assert len(set(r["model"]["states"])) == 3
    assert all(x["state_bits"] == 8 for x in r["model"]["property_cones"].values())


@pytest.mark.parametrize("start", [-2, 0, 3])
def test_generate_indices_and_parameter_constants(tmp_path: Path, start: int) -> None:
    text = f"""module top(input wire clk,rst_n,en,input wire [7:0] d,output wire [7:0] q);
      for(genvar i={start};i<{start + 2};i++) begin: lanes
        wire [7:0] data;
        delay_cell u(clk,rst_n,en,d ^ 8'(i),data);
      end
      assign q=lanes[{start}].data;
    endmodule"""
    request(tmp_path, text)
    c = circuit(tmp_path)
    _, states = simulate_frame(c, dict.fromkeys(c.next_state, 0), {"d": 25, "en": 1, "rst_n": 1})
    assert states == {f"lanes[{i}].u.q": 25 ^ (i & 255) for i in range(start, start + 2)}


@pytest.mark.parametrize("active", [0, 1])
def test_only_selected_generate_branch_is_lowered(tmp_path: Path, active: int) -> None:
    text = TOP.replace(
        "endmodule", f"if({active}) begin: unsupported real x; initial $finish; end endmodule"
    )
    p = request(tmp_path, text)
    if active:
        with pytest.raises(SequentialError):
            run_request(p, root=tmp_path)
    else:
        assert run_request(p, root=tmp_path)["exit_code"] == 0


def test_empty_child_and_open_driven_output_supported(tmp_path: Path) -> None:
    text = TOP.replace("endmodule", "empty unused(); delay_cell extra(clk,rst_n,en,d,); endmodule")
    p = request(tmp_path, text, LEAF + "\nmodule empty; endmodule")
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 0 and r["model"]["state_bits"] == 24
    assert r["model"]["property_cones"]["pipeline"]["state_bits"] == 16


@pytest.mark.parametrize(
    "text,leaf",
    [
        (TOP.replace(".d(d)", ".d()"), LEAF),
        (TOP.replace(".clock_in(clk)", ".clock_in(en)"), LEAF),
        (TOP.replace(".clock_in(clk)", ".clock_in(clk & en)"), LEAF),
        (
            TOP.replace("endmodule", "wire ck; assign ck=clk; endmodule").replace(
                ".clock_in(clk)", ".clock_in(ck)"
            ),
            LEAF,
        ),
        (TOP, LEAF.replace("posedge clock_in", "posedge clock_in or negedge rst_n")),
        (TOP, LEAF.replace("posedge clock_in", "negedge clock_in")),
        (TOP, LEAF.replace("q <= d", "q <= clock_in")),
        (TOP.replace(".q(middle)", ".q(q)"), LEAF),
        (TOP.replace(".q(middle)", ".q(middle[3:0])"), LEAF),
        (TOP, LEAF.replace("input wire [W-1:0] d", "inout wire [W-1:0] d")),
        (TOP, LEAF.replace("q <= d", "q <= 1'bx")),
        (TOP, LEAF.replace("always_ff", "initial $finish; always_ff")),
        (TOP.replace("delay_cell first", "missing_cell first"), LEAF),
        (
            TOP,
            LEAF.replace(
                "always_ff @(posedge clock_in) if (!rst_n) q <= 0; else if (en) q <= d;", ""
            ),
        ),
        (TOP.replace("endmodule", "interface foo; logic a; endinterface foo x(); endmodule"), LEAF),
    ],
)
def test_unsupported_hierarchy_never_becomes_proof(tmp_path: Path, text: str, leaf: str) -> None:
    p = request(tmp_path, text, leaf)
    with pytest.raises(SequentialError):
        run_request(p, root=tmp_path)


@pytest.mark.parametrize("spelling", ["ghost.q", "first.clock_in", "q[0]", "first.q[0]"])
def test_unresolved_or_clock_property_paths_rejected(tmp_path: Path, spelling: str) -> None:
    p = request(tmp_path)
    p["properties"][0]["sink"] = spelling
    with pytest.raises(SequentialError):
        run_request(p, root=tmp_path)


@pytest.mark.parametrize(
    "spelling",
    ["first..q", "first[-a].q", "first[+1].q", ".q", "\\q", "u/../q", "a." * 513 + "q", 2],
)
def test_malformed_paths_rejected(spelling: object, tmp_path: Path) -> None:
    p = request(tmp_path)
    p["properties"][0]["sink"] = spelling
    with pytest.raises(SequentialError):
        normalize(p)


def test_internal_assumptions_are_still_forbidden(tmp_path: Path) -> None:
    p = request(tmp_path, assumptions={"first.en": 1})
    with pytest.raises(SequentialError):
        normalize(p)


@pytest.mark.parametrize(
    "limit,value", [("MAX_MEMBERS", 1), ("MAX_INSTANCES", 1), ("MAX_DEPTH", 0)]
)
def test_global_hierarchy_budgets(tmp_path: Path, monkeypatch, limit: str, value: int) -> None:
    import opencollate.sequential_hierarchy as h

    monkeypatch.setattr(h, limit, value)
    p = request(tmp_path)
    with pytest.raises(SequentialError, match="exceeds"):
        run_request(p, root=tmp_path)


@pytest.mark.parametrize(
    "in_width,child_width,out_width", [(4, 8, 12), (8, 4, 16), (8, 4, 2), (1, 8, 16), (8, 1, 16)]
)
@pytest.mark.parametrize("signed_in,signed_child", list(itertools.product([False, True], repeat=2)))
def test_port_width_and_sign_conversions(
    tmp_path: Path,
    in_width: int,
    child_width: int,
    out_width: int,
    signed_in: bool,
    signed_child: bool,
) -> None:
    sign_in, sign_child = "signed " if signed_in else "", "signed " if signed_child else ""
    text = f"""module top(input wire clk, input wire {sign_in}[{in_width - 1}:0] d,
     output wire [{out_width - 1}:0] q); convert u(clk,d,q); endmodule"""
    leaf = f"""module convert(input wire clk, input wire {sign_child}[{child_width - 1}:0] d,
     output logic {sign_child}[{child_width - 1}:0] q); always_ff @(posedge clk) q<=d; endmodule"""
    request(tmp_path, text, leaf)
    c = circuit(tmp_path)
    for d in range(1 << in_width):
        val = d - (1 << in_width) if signed_in and d & (1 << (in_width - 1)) else d
        child = val & ((1 << child_width) - 1)
        extended = (
            child - (1 << child_width)
            if signed_child and child & (1 << (child_width - 1))
            else child
        )
        _, updated = simulate_frame(c, {"u.q": 0}, {"d": d})
        observed, _ = simulate_frame(c, updated, {"d": 0})
        assert observed["q"] == extended & ((1 << out_width) - 1)


def test_request_and_receipt_schemas_cover_hierarchical_paths(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    import opencollate

    p = request(tmp_path)
    p["properties"][0].update(sink="second.q", source="first.d")
    schemas = Path(opencollate.__file__).parent / "schemas"
    Draft202012Validator(
        json.loads((schemas / "sequential-request.schema.json").read_text())
    ).validate(p)
    spec = normalize(p)
    validate_signals(circuit(tmp_path), spec)
    r = run_request(p, root=tmp_path)
    assert r["schema_version"] == 2
    Draft202012Validator(
        json.loads((schemas / "sequential-receipt.schema.json").read_text())
    ).validate(r)


@pytest.mark.parametrize("choice", [0, 1, 2])
def test_selected_case_generate_branches(tmp_path: Path, choice: int) -> None:
    text = f"""module top #(parameter CHOICE={choice})(input wire clk,rst_n,en,
      input wire [7:0] d,output wire [7:0] q);
      case(CHOICE)
       0: begin: selected0 delay_cell u(clk,rst_n,en,d,q); end
       1: begin: selected1 delay_cell u(clk,rst_n,en,d,q); end
       default: begin: selected2 delay_cell u(clk,rst_n,en,d,q); end
      endcase
    endmodule"""
    p = request(tmp_path, text)
    p["properties"][0].update(latency=1, when=[{"signal": "en", "equals": 1, "lag": 1}])
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 0
    assert r["model"]["states"] == [f"selected{choice}.u.q"]

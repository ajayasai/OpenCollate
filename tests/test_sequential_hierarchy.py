from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from opencollate.sequential import replay, run_request
from opencollate.sequential_ir import SequentialError, simulate_frame
from opencollate.sequential_rtl import load_circuit
from opencollate.sequential_spec import normalize

LEAF = """module leaf #(parameter W=4)(input logic clk,rst,en,
input logic [W-1:0] d, output logic [W-1:0] q);
always_ff @(posedge clk) if(rst) q <= '0; else if(en) q <= d;
endmodule
"""
PIPE = (
    LEAF
    + """module top(input logic clk,rst,en,input logic [3:0] d,
output wire [3:0] q);
wire [3:0] mid;
leaf a(clk,rst,en,d,mid);
leaf b(.clk(clk),.rst(rst),.en(en),.d(mid),.q(q));
endmodule
"""
)


def request(tmp: Path, text: str = PIPE, **kwargs: object) -> dict:
    (tmp / "design.sv").write_text(text, encoding="utf-8")
    return {
        "schema_version": 1,
        "semantics": "two-valued-synchronous",
        "files": ["design.sv"],
        "top": "top",
        "clock": "clk",
        "reset": {"signal": "rst", "active": 1},
        "depth": 6,
        "induction": 4,
        "properties": [
            {
                "id": "data",
                "source": "d",
                "sink": "q",
                "latency": 2,
                "when": [{"signal": "en", "equals": 1, "lag": i} for i in (1, 2)],
            }
        ],
        **kwargs,
    }


def circuit(tmp: Path, text: str = PIPE):
    request(tmp, text)
    return load_circuit(["design.sv"], root=tmp, top="top", clock="clk")


def test_actual_modules_proof_schema_and_replay(tmp_path: Path) -> None:
    spec = request(tmp_path)
    receipt = run_request(spec, root=tmp_path)
    assert receipt["exit_code"] == 0
    assert receipt == replay(spec, receipt, root=tmp_path)
    assert receipt["model"]["states"] == ["a.q", "b.q"]
    assert receipt["model"]["clock_aliases"] == ["a.clk", "b.clk", "clk"]
    assert [r["path"] for r in receipt["model"]["hierarchy"]] == ["top", "top.a", "top.b"]
    root = Path(__file__).parents[1] / "src/opencollate/schemas"
    for value, schema in [(spec, "sequential-request"), (receipt, "sequential-receipt")]:
        Draft202012Validator(json.loads((root / f"{schema}.schema.json").read_text())).validate(
            value
        )


def test_empty_child_is_now_supported_not_blackboxed_logic(tmp_path: Path) -> None:
    spec = request(
        tmp_path,
        PIPE.replace("wire [3:0] mid;", "wire [3:0] mid; empty u();") + "module empty; endmodule",
    )
    assert run_request(spec, root=tmp_path)["exit_code"] == 0


def test_two_instances_same_definition_never_share_state(tmp_path: Path) -> None:
    c = circuit(tmp_path)
    for a, b, d, en, rst in itertools.product(range(4), range(4), range(4), range(2), range(2)):
        frame, state = simulate_frame(c, {"a.q": a, "b.q": b}, {"d": d, "en": en, "rst": rst})
        assert frame["q"] == b
        assert state == {"a.q": 0 if rst else d if en else a, "b.q": 0 if rst else a if en else b}


@pytest.mark.parametrize("width", [1, 4, 8, 64, 128, 256])
def test_parameter_overrides_and_deep_wrapper(tmp_path: Path, width: int) -> None:
    wrapper = """module wrapper #(parameter W=4)(input logic clk,rst,en,
    input logic [W-1:0] d, output wire [W-1:0] q);
    leaf #(.W(W)) inner(.*); endmodule
    """
    text = PIPE.replace("leaf a(", f"wrapper #(.W({width})) a(").replace(
        "leaf b(", f"wrapper #(.W({width})) b("
    )
    # Only the top module has literal [3:0] widths.
    text = text.replace("[3:0]", f"[{width - 1}:0]") + wrapper
    r = run_request(request(tmp_path, text), root=tmp_path)
    assert r["exit_code"] == 0 and r["model"]["state_bits"] == 2 * width
    assert r["model"]["states"] == ["a.inner.q", "b.inner.q"]


def generated(stages: int = 4, width: int = 4) -> str:
    return (
        LEAF
        + f"""module top(input logic clk,rst,en,input logic [{width - 1}:0] d,
    output wire [{width - 1}:0] q);
    for (genvar i=0;i<{stages};i++) begin: g
      wire [{width - 1}:0] din, value;
      if(i==0) begin: first
        assign din=d;
      end else begin: later
        assign din=g[i-1].value;
      end
      leaf #(.W({width})) u(.clk(clk),.rst(rst),.en(en),.d(din),.q(value));
    end
    assign q=g[{stages - 1}].value;
    endmodule
    """
    )


@pytest.mark.parametrize("stages,width", [(1, 1), (2, 8), (4, 4), (8, 16), (16, 256)])
def test_generate_loop_branch_selection_and_cross_scope_references(
    tmp_path: Path, stages: int, width: int
) -> None:
    p = request(
        tmp_path, generated(stages, width), depth=stages + 2, induction=16, assumptions={"en": 1}
    )
    p["properties"][0].update(latency=stages, when=[])
    r = run_request(p, root=tmp_path, timeout_ms=60000)
    assert r["exit_code"] == 0
    assert r["model"]["state_bits"] == stages * width
    assert len(r["model"]["hierarchy"]) == stages + 1


def test_hierarchical_property_guard_and_source_names(tmp_path: Path) -> None:
    p = request(tmp_path, generated(2))
    p["properties"] = [
        {
            "id": "internal",
            "source": "g[0].u.d",
            "sink": "g[0].u.q",
            "latency": 1,
            "when": [{"signal": "g[0].u.en", "equals": 1, "lag": 1}],
        }
    ]
    assert run_request(p, root=tmp_path)["exit_code"] == 0
    p["properties"][0]["source"] = "g[0].u.clk"
    with pytest.raises(SequentialError):
        run_request(p, root=tmp_path)


def test_child_byte_changes_invalidate_receipt(tmp_path: Path) -> None:
    p = request(tmp_path)
    r = run_request(p, root=tmp_path)
    (tmp_path / "design.sv").write_text(PIPE.replace("q <= d", "q <= ~d"))
    with pytest.raises(SequentialError, match="changed"):
        replay(p, r, root=tmp_path)


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("small,large", [(1, 4), (3, 8), (4, 4), (8, 4)])
def test_frontend_port_casts_match_integer_width_oracle(
    tmp_path: Path, direction: str, signed: bool, small: int, large: int
) -> None:
    # Output widening uses the child's signedness, not the parent's declared type.
    iw, ow = (large, small) if direction == "input" else (small, large)
    sign = "signed " if signed else ""
    text = f"""module cast_leaf(input wire {sign}[{small - 1}:0] d,
    output wire {sign}[{small - 1}:0] q);
    assign q=d; endmodule
    module top(input logic clk, input wire {sign}[{iw - 1}:0] d,output wire [{ow - 1}:0] q);
    cast_leaf u(.d(d),.q(q)); endmodule"""
    c = circuit(tmp_path, text)
    for d in range(1 << iw):
        f, _ = simulate_frame(c, {}, {"d": d})
        signed_input = d - (1 << iw) if signed and d & (1 << (iw - 1)) else d
        child = signed_input & ((1 << small) - 1)
        value = child - (1 << small) if signed and child & (1 << (small - 1)) else child
        assert f["q"] == value & ((1 << ow) - 1)


@pytest.mark.parametrize(
    "wire", ["wire ck; assign ck=clk;", "wire ck, ck0; assign ck=ck0; assign ck0=clk;"]
)
def test_clock_wire_alias_chain(tmp_path: Path, wire: str) -> None:
    text = (
        PIPE.replace("wire [3:0] mid;", "wire [3:0] mid;" + wire)
        .replace("leaf a(clk,", "leaf a(ck,")
        .replace(".clk(clk)", ".clk(ck)")
    )
    assert run_request(request(tmp_path, text), root=tmp_path)["exit_code"] == 0


@pytest.mark.parametrize(
    "text",
    [
        PIPE.replace(".clk(clk)", ".clk(en)"),
        PIPE.replace(".clk(clk)", ".clk(clk & en)"),
        PIPE.replace(".clk(clk)", ".clk(~clk)"),
        PIPE.replace(".d(mid)", ".d()"),
        PIPE.replace("wire [3:0] mid;", "wire [3:0] mid; assign mid=d;"),
        PIPE.replace("q <= d", "q <= clk"),
        PIPE.replace("posedge clk", "posedge clk or posedge rst"),
        PIPE.replace("endmodule\nmodule top", "initial q=0; endmodule\nmodule top"),
        PIPE.replace(
            "wire [3:0] mid;", "wire [3:0] mid; wire ck; assign ck=clk; assign ck=en;"
        ).replace("leaf a(clk,", "leaf a(ck,"),
        PIPE.replace(".q(q)", ".q(q[0])"),
    ],
)
def test_unsupported_descendant_never_blackboxed(tmp_path: Path, text: str) -> None:
    with pytest.raises((SequentialError, ValueError)):
        run_request(request(tmp_path, text), root=tmp_path)


def test_inactive_generate_unsupported_body_is_not_instantiated(tmp_path: Path) -> None:
    text = PIPE.replace(
        "wire [3:0] mid;", 'wire [3:0] mid; if (0) begin: absent initial $display("inactive"); end'
    )
    assert run_request(request(tmp_path, text), root=tmp_path)["exit_code"] == 0
    with pytest.raises(SequentialError):
        run_request(request(tmp_path, text.replace("if (0)", "if (1)")), root=tmp_path)


@pytest.mark.parametrize(
    "path", [".u.q", "u..q", "u[01].q", "u[*].q", "u[1+2].q", "u/q", "u[-].q", "u[0].q\n", "α.q"]
)
def test_noncanonical_paths_rejected(tmp_path: Path, path: str) -> None:
    p = request(tmp_path)
    p["properties"][0]["sink"] = path
    with pytest.raises(SequentialError):
        normalize(p)


def test_unknown_hierarchical_path_not_guessed(tmp_path: Path) -> None:
    p = request(tmp_path)
    p["properties"][0]["sink"] = "a.typo"
    with pytest.raises(SequentialError, match="unknown"):
        run_request(p, root=tmp_path)


def test_child_input_cannot_be_constrained_as_primary(tmp_path: Path) -> None:
    p = request(tmp_path, assumptions={"a.en": 1})
    with pytest.raises(SequentialError):
        run_request(p, root=tmp_path)


def test_generated_bit_lane_outputs_and_replica_state(tmp_path: Path) -> None:
    text = (
        LEAF
        + """module top(input logic clk,rst,en,input logic [3:0] d,output wire [3:0] q);
    for(genvar i=0;i<4;i++) begin: lanes
      leaf #(.W(1)) ff(.clk(clk),.rst(rst),.en(en),.d(d[i]),.q(q[i]));
    end
    endmodule"""
    )
    p = request(tmp_path, text)
    p["properties"][0].update(latency=1, when=[{"signal": "en", "equals": 1, "lag": 1}])
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 0
    assert r["model"]["states"] == [f"lanes[{i}].ff.q" for i in range(4)]
    p["properties"][0]["source"] = "lanes[0].ff.q"
    with pytest.raises(SequentialError, match="width"):
        run_request(p, root=tmp_path)


def test_output_concat_and_part_select_bit_order(tmp_path: Path) -> None:
    text = """module leaf(input logic [3:0] d, output wire [3:0] q);
    assign q=d; endmodule
    module top(input logic clk, input logic [3:0] d, output wire [3:0] q);
    leaf u(.d(d),.q({q[1:0], q[3:2]})); endmodule"""
    c = circuit(tmp_path, text)
    for d in range(16):
        frame, _ = simulate_frame(c, {}, {"d": d})
        assert frame["q"] == (d << 2 | d >> 2) & 15


def test_disjoint_continuous_parts_are_assembled(tmp_path: Path) -> None:
    c = circuit(
        tmp_path,
        """module top(input logic clk,input logic [3:0] d, output wire [3:0] q);
    assign q[3:2]=d[1:0]; assign q[1:0]=d[3:2]; endmodule""",
    )
    for d in range(16):
        assert simulate_frame(c, {}, {"d": d})[0]["q"] == (d << 2 | d >> 2) & 15


@pytest.mark.parametrize(
    "body",
    [
        "assign q[0]=d[0];",  # gaps cannot become arbitrary symbolic wires
        "assign q[2:0]=d[2:0]; assign q[3:2]=d[3:2];",  # overlap
        "assign {q[2:0],q[0]}=d;",  # duplicate concatenation destination
        "assign q[d]=1'b1;",  # dynamic writes
        "assign d=q;",  # primary input write
    ],
)
def test_partial_driver_errors_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(SequentialError):
        circuit(
            tmp_path,
            f"module top(input logic clk,input wire [3:0] d,output wire [3:0] q); {body} endmodule",
        )


def test_instance_array_port_slicing_from_elaborator(tmp_path: Path) -> None:
    text = (
        LEAF
        + """module top(input logic clk,rst,en,input logic [3:0] d,output wire [3:0] q);
    leaf #(.W(1)) ff[3:0](.clk(clk),.rst(rst),.en(en),.d(d),.q(q));
    endmodule"""
    )
    p = request(tmp_path, text)
    p["properties"][0].update(latency=1, when=[{"signal": "en", "equals": 1, "lag": 1}])
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 0
    assert r["model"]["states"] == [f"ff[{i}].q" for i in range(4)]


def test_deep_hierarchy_limit(tmp_path: Path) -> None:
    wrappers = []
    for i in range(34):
        child = f"m{i + 1} u(clk,rst,en,d,q);" if i < 33 else "leaf u(clk,rst,en,d,q);"
        wrappers.append(
            f"module m{i}(input clk,rst,en,input [3:0] d,output [3:0] q); {child} endmodule"
        )
    text = (
        LEAF + "\n".join(wrappers) + "\nmodule top(input clk,rst,en,input [3:0] d,output [3:0] q);"
        "m0 u(clk,rst,en,d,q);endmodule"
    )
    with pytest.raises(SequentialError, match="levels"):
        run_request(request(tmp_path, text), root=tmp_path)


def test_instance_count_limit_not_silent_cut(tmp_path: Path) -> None:
    text = (
        "module empty; endmodule module top(input clk,output wire q); assign q=1'b0; "
        "for(genvar i=0;i<1024;i++) begin:g empty u(); end endmodule"
    )
    with pytest.raises(SequentialError, match="1024 instances"):
        circuit(tmp_path, text)


def test_clock_forwarded_through_module_output(tmp_path: Path) -> None:
    text = (
        PIPE.replace("wire [3:0] mid;", "wire [3:0] mid; wire ck; clkbuf buf0(clk,ck);")
        .replace("leaf a(clk,", "leaf a(ck,")
        .replace(".clk(clk)", ".clk(ck)")
    )
    text += "module clkbuf(input wire i,output wire o);assign o=i; endmodule"
    r = run_request(request(tmp_path, text), root=tmp_path)
    assert r["exit_code"] == 0
    assert set(r["model"]["clock_aliases"]) == {"clk", "ck", "a.clk", "b.clk", "buf0.i", "buf0.o"}


def test_output_disconnected_is_not_an_unconnected_input(tmp_path: Path) -> None:
    text = PIPE.replace("wire [3:0] mid;", "wire [3:0] mid; leaf unused(clk,rst,en,d,);")
    assert run_request(request(tmp_path, text), root=tmp_path)["exit_code"] == 0


def test_negative_generate_indices(tmp_path: Path) -> None:
    text = (
        LEAF
        + """module top(input logic clk,rst,en,input logic [3:0] d,output wire [3:0] q);
    for(genvar i=-2;i<0;i++) begin:g
      wire [3:0] v;
      leaf ff(clk,rst,en,d,v);
    end
    assign q=g[-1].v; endmodule"""
    )
    p = request(tmp_path, text)
    p["properties"][0].update(latency=1, when=[{"signal": "g[-1].ff.en", "equals": 1, "lag": 1}])
    r = run_request(p, root=tmp_path)
    assert r["exit_code"] == 0
    assert "g[-1].ff.q" in r["model"]["states"]


def test_elaborated_generate_case_not_procedural_case(tmp_path: Path) -> None:
    text = PIPE.replace(
        "leaf a(clk,rst,en,d,mid);",
        'case (2) 1: begin: off initial $display("uninstantiated"); end '
        "2: begin: on_path leaf a(clk,rst,en,d,mid); end "
        'default: begin: unused initial $display("default"); end endcase',
    )
    r = run_request(request(tmp_path, text), root=tmp_path)
    assert r["exit_code"] == 0
    assert "on_path.a.q" in r["model"]["states"]

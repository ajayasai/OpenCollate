from __future__ import annotations

import itertools

import pytest

from opencollate.proof_kernel import Limits, Meter, ProofError
from opencollate.sequential_cnf import CNF, Unroller
from opencollate.sequential_ir import Circuit, evaluate
from opencollate.sequential_rtl import BINARY, UNARY


def satisfies(clauses: list[list[int]], values: tuple[bool, ...]) -> bool:
    return all(any(values[abs(v) - 1] == (v > 0) for v in clause) for clause in clauses)


@pytest.mark.parametrize("operation", ["and", "xor", "or", "mux"])
def test_tseitin_truth_tables_without_any_solver(operation: str) -> None:
    for sign_a, sign_b in itertools.product((-1, 1), repeat=2):
        q = CNF(Meter())
        a, b = q.variable() * sign_a, q.variable() * sign_b
        c = q.variable()
        out = {"and": q.and_, "xor": q.xor, "or": q.or_}.get(operation, q.and_)(a, b)
        if operation == "mux":
            out = q.mux(c, a, b)
        models = 0
        for values in itertools.product((False, True), repeat=q.variables):
            if not satisfies(q.clauses, values):
                continue
            x = values[abs(a) - 1] == (a > 0)
            y = values[abs(b) - 1] == (b > 0)
            z = values[c - 1]
            expected = {"and": x and y, "xor": x != y, "or": x or y, "mux": x if z else y}[
                operation
            ]
            assert (values[abs(out) - 1] == (out > 0)) == expected
            models += 1
        assert models == 8  # Every assignment of the three free inputs extends uniquely.


@pytest.mark.parametrize("op", sorted(BINARY | UNARY))
@pytest.mark.parametrize("signed", [False, True])
def test_bitblast_matches_integer_oracle_for_all_four_bit_inputs(op: str, signed: bool) -> None:
    from pysat.solvers import Solver

    c = Circuit("dut", "clk")
    c.signals = {"a": (4, signed), "b": (4, signed), "clk": (1, False)}
    c.inputs = ["a", "b"]
    args = (c.ref("a"), c.ref("b")) if op in BINARY else (c.ref("a"),)
    boolean = op in {
        "Equality",
        "Inequality",
        "LessThan",
        "LessThanEqual",
        "GreaterThan",
        "GreaterThanEqual",
        "LogicalAnd",
        "LogicalOr",
        "LogicalNot",
        "BitwiseAnd",
        "BitwiseNand",
        "BitwiseOr",
        "BitwiseNor",
        "BitwiseXor",
        "BitwiseXnor",
    }
    width = 1 if boolean else 4
    root = c.add(op, width, signed and not boolean, args)
    c.signals["y"] = (width, signed and not boolean)
    c.outputs = ["y"]
    c.combinational = {"y": root}
    c.finalize()
    unroll = Unroller(c, {"assumptions": {}, "reset": None}, Meter())
    unroll.append()
    f = unroll.frames[0]
    with Solver(name="g3", bootstrap_with=unroll.cnf.clauses) as solver:
        for a, b in itertools.product(range(16), repeat=2):
            expected = evaluate(c, root, {"a": a, "b": b})
            assignments = [
                v if value & (1 << bit) else -v
                for name, value in (("a", a), ("b", b))
                for bit, v in enumerate(f[name])
            ]
            assert solver.solve(assumptions=assignments)
            for bit, literal in enumerate(f["y"]):
                wrong = -literal if expected & (1 << bit) else literal
                assert not solver.solve(assumptions=[*assignments, wrong]), (op, signed, a, b, bit)


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("width", [1, 4, 8, 32, 256])
def test_cast_concat_slice_and_mux_edge_widths(signed: bool, width: int) -> None:
    from pysat.solvers import Solver

    c = Circuit("dut", "clk")
    c.signals = {"a": (4, signed), "b": (4, False), "select": (4, False), "clk": (1, False)}
    c.inputs = ["a", "b", "select"]
    a, b, select = c.ref("a"), c.ref("b"), c.ref("select")
    cast = c.add("cast", width, signed, (a,))
    concat = c.add("concat", 8, False, (a, b))
    selected = c.add("slice", 4, False, (concat,), 2)
    mux = c.add("mux", 4, False, (select, a, selected))
    for name, root in {"cast": cast, "slice": selected, "mux": mux}.items():
        c.signals[name] = (c.nodes[root].width, False)
        c.combinational[name] = root
    c.finalize()
    unroll = Unroller(c, {"assumptions": {}, "reset": None}, Meter())
    unroll.append()
    f = unroll.frames[0]
    with Solver(name="g3", bootstrap_with=unroll.cnf.clauses) as solver:
        for x, y, s in itertools.product((0, 1, 7, 8, 15), (0, 9, 15), (0, 1, 8)):
            env = {"a": x, "b": y, "select": s}
            inputs = [v if env[n] & (1 << i) else -v for n in c.inputs for i, v in enumerate(f[n])]
            assert solver.solve(assumptions=inputs)
            for name, root in c.combinational.items():
                expected = evaluate(c, root, env)
                for bit, v in enumerate(f[name]):
                    assert not solver.solve(
                        assumptions=[*inputs, -v if expected & (1 << bit) else v]
                    )


def test_constant_identities_cache_and_bounds() -> None:
    q = CNF(Meter())
    a, b = q.variable(), q.variable()
    assert q.and_(a, a) == a
    assert q.and_(a, -a) == -1
    assert q.and_(1, a) == a
    assert q.xor(a, -a) == 1
    assert q.xor(1, a) == -a
    assert q.xor(a, -1) == a
    assert q.and_(a, b) == q.and_(b, a)
    assert q.xor(a, b) == q.xor(b, a)
    assert q.mux(b, a, a) == a
    for limits, action in [
        (Limits(variables=1), lambda v: v.variable()),
        (Limits(clauses=1), lambda v: v.clause(-1)),
        (Limits(literals=1), lambda v: v.clause(v.variable())),
    ]:
        with pytest.raises(ProofError):
            action(CNF(Meter(limits)))


def test_unknown_ir_operator_and_width_mismatch_are_rejected() -> None:
    for op, width in [("unsupported", 1), ("Plus", 2)]:
        c = Circuit("dut", "clk", signals={"a": (1, False), "clk": (1, False)}, inputs=["a"])
        r = c.add(op, width, args=(c.ref("a"),))
        c.signals["y"] = (width, False)
        c.combinational["y"] = r
        c.finalize()
        with pytest.raises(ProofError):
            Unroller(c, {"assumptions": {}, "reset": None}, Meter()).append()

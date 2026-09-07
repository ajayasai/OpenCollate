"""Exact property-local projection of an already validated transition system.

This is dependency closure, not a black-box abstraction: every retained state
keeps its complete update function. The full circuit must be validated first.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from opencollate.sequential_ir import Circuit, SequentialError, simulate_frame


def property_cone(c: Circuit, spec: dict[str, Any], prop: dict[str, Any]) -> Circuit:
    """Retain all dependencies of observations, guards, and input constraints.

    Temporal dependencies close to a fixed point across state updates, regardless
    of the requested BMC depth. No semantic constant folding is used. Construction
    visits each retained signal/node once, then compacts nodes topologically.
    """
    if len(c.comb_order) != len(c.combinational) or set(c.comb_order) != set(c.combinational):
        raise SequentialError("property projection requires a finalized full circuit")
    roots = {prop["sink"], *spec["assumptions"]}
    if "source" in prop:
        roots.add(prop["source"])
    roots.update(g["signal"] for g in prop["when"])
    if spec["reset"]:
        roots.add(spec["reset"]["signal"])
    inputs = set(c.inputs)
    driven = inputs | set(c.next_state) | set(c.combinational)
    signals: set[str] = set()
    nodes: set[int] = set()
    pending = list(sorted(roots))
    while pending:
        name = pending.pop()
        if name in signals:
            continue
        if name not in driven or name == c.clock:
            raise SequentialError(f"unresolved property-cone signal: {name}")
        signals.add(name)
        if name in inputs:
            continue
        root = c.next_state[name] if name in c.next_state else c.combinational[name]
        stack = [root]
        while stack:
            i = stack.pop()
            if i in nodes:
                continue
            nodes.add(i)
            node = c.nodes[i]
            if node.op == "ref":
                pending.append(str(node.value))
            stack.extend(node.args)
    remap = {old: new for new, old in enumerate(sorted(nodes))}
    compact = [
        replace(c.nodes[i], args=tuple(remap[a] for a in c.nodes[i].args)) for i in sorted(nodes)
    ]
    cone = Circuit(
        top=c.top,
        clock=c.clock,
        nodes=compact,
        signals={n: c.signals[n] for n in sorted(signals | {c.clock})},
        inputs=[n for n in c.inputs if n in signals],
        outputs=[n for n in c.outputs if n in signals],
        next_state={n: remap[i] for n, i in c.next_state.items() if n in signals},
        combinational={n: remap[i] for n, i in c.combinational.items() if n in signals},
        locations={n: dict(v) for n, v in c.locations.items() if n in signals},
        sources=[dict(row) for row in c.sources],
        frontend=c.frontend,
        _intern={node: i for i, node in enumerate(compact)},
    )
    # Catch a broken closure rather than introducing unconstrained internal wires.
    cone.finalize()
    return cone


def expand_trace(full: Circuit, cone: Circuit, trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lift a cone witness to a complete full-circuit witness by integer execution.

    Removed initial state and primary inputs are independent of the property;
    their canonical unsigned minimum is zero. Removed *later* state is computed,
    never filled with zeros. Every retained observation must match at every sample.
    The caller must additionally replay assumptions and the property on the full
    trace before publishing any counterexample.
    """
    if not trace:
        raise SequentialError("cannot expand an empty counterexample")
    expected = set(cone.inputs) | set(cone.next_state) | set(cone.combinational)
    states: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    for t, row in enumerate(trace):
        if set(row) != {"cycle", "values"} or type(row["cycle"]) is not int or row["cycle"] != t:
            raise SequentialError("invalid cone trace cycle")
        values = row["values"]
        if not isinstance(values, dict) or set(values) != expected:
            raise SequentialError("incomplete cone trace frame")
        if any(
            type(v) is not int or not 0 <= v < (1 << cone.signals[n][0]) for n, v in values.items()
        ):
            raise SequentialError("invalid cone trace value")
        if t == 0:
            states = {n: values.get(n, 0) for n in full.next_state}
        inputs = {n: values.get(n, 0) for n in full.inputs}
        observed, states = simulate_frame(full, states, inputs)
        if any(observed.get(n) != v for n, v in values.items()):
            raise SequentialError("cone counterexample disagrees with full-circuit execution")
        result.append({"cycle": t, "values": observed})
    return result


def verify_cone_property(
    c: Circuit, spec: dict[str, Any], prop: dict[str, Any], budget: Any
) -> dict[str, Any]:
    """Solve a closed projection, then validate any witness on the original IR.

    Receipt creation stays in sequential.run_request and binds the *full* source
    and IR. The original unsliced solver remains available as a regression oracle.
    """
    from opencollate.sequential_smt import replay_trace, verify_property

    result: dict[str, Any] = {
        "id": prop["id"],
        "status": "inconclusive",
        "checked_through": None,
        "induction_depth": None,
        "cover_cycle": None,
        "trace": None,
        "reason": None,
    }
    try:
        budget.remaining()
        cone = property_cone(c, spec, prop)
        budget.remaining()
        result = verify_property(cone, spec, prop, budget)
        if result["status"] == "counterexample":
            budget.remaining()
            trace = expand_trace(c, cone, result["trace"])
            budget.remaining()
            if not replay_trace(c, spec, prop, [row["values"] for row in trace]):
                raise SequentialError("expanded counterexample failed full-circuit replay")
            budget.remaining()
            result["trace"] = trace
        return result
    except SequentialError as error:
        result.update(status="inconclusive", trace=None, reason=str(error))
        return result

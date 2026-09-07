"""Exact structural property-cone reduction for a validated total transition IR.

All primary inputs are retained, including reset and fixed-input assumptions.
State feedback and guard/history dependencies are closed to a fixed point.
No unsupported RTL is discarded: full elaboration and validation happen first.
"""

from __future__ import annotations

from typing import Any

from opencollate.sequential_ir import Circuit, SequentialError, simulate_frame
from opencollate.sequential_smt import Budget, replay_trace


def property_cone(c: Circuit, prop: dict[str, Any]) -> Circuit:
    required = {prop["sink"], *(g["signal"] for g in prop["when"])}
    if "source" in prop:
        required.add(prop["source"])
    signals: set[str] = set()
    nodes: set[int] = set()
    pending = list(required)
    while pending:
        name = pending.pop()
        if name in signals:
            continue
        signals.add(name)
        root = c.next_state.get(name, c.combinational.get(name))
        if root is None:
            if name not in c.inputs:
                raise SequentialError(f"unresolved signal in property cone: {name}")
            continue
        todo = [root]
        while todo:
            i = todo.pop()
            if i in nodes:
                continue
            nodes.add(i)
            node = c.nodes[i]
            todo.extend(node.args)
            if node.op == "ref":
                pending.append(str(node.value))

    # Retaining all primary inputs keeps environmental restrictions unchanged.
    signals.update(c.inputs)
    signals.add(c.clock)
    selected = Circuit(c.top, c.clock, frontend=c.frontend)
    selected.signals = {n: c.signals[n] for n in sorted(signals)}
    selected.inputs = list(c.inputs)
    selected.outputs = sorted(set(c.outputs) & signals)
    selected.sources = list(c.sources)
    selected.locations = {n: c.locations[n] for n in sorted(signals) if n in c.locations}
    selected.hierarchy = list(c.hierarchy)
    selected.clock_aliases = [c.clock]
    remap: dict[int, int] = {}
    for i in sorted(nodes):
        n = c.nodes[i]
        remap[i] = selected.add(n.op, n.width, n.signed, tuple(remap[j] for j in n.args), n.value)
    selected.next_state = {n: remap[c.next_state[n]] for n in sorted(signals & c.next_state.keys())}
    selected.combinational = {
        n: remap[c.combinational[n]] for n in sorted(signals & c.combinational.keys())
    }
    selected.finalize()
    return selected


def cone_summary(c: Circuit) -> dict[str, Any]:
    return {
        "state_bits": sum(c.signals[n][0] for n in c.next_state),
        "states": sorted(c.next_state),
        "combinational_signals": len(c.combinational),
        "ir_nodes": len(c.nodes),
    }


def expand_trace(
    c: Circuit, spec: dict[str, Any], prop: dict[str, Any], result: dict[str, Any], budget: Budget
) -> dict[str, Any]:
    """Lift a reduced witness to the complete model, then replay the full trace.

    Omitted state is independent of retained transitions. Choosing its initial value
    as zero preserves canonicality; subsequent values come from the original model,
    not from invented per-sample zeros. This function never changes a proof result.
    """
    if result["trace"] is None:
        return result
    trace = result["trace"]
    states = {n: trace[0]["values"].get(n, 0) for n in c.next_state}
    full: list[dict[str, int]] = []
    for row in trace:
        budget.remaining()
        inputs = {n: row["values"][n] for n in c.inputs}
        frame, states = simulate_frame(c, states, inputs)
        if any(frame.get(n) != v for n, v in row["values"].items()):
            raise SequentialError("reduced counterexample cannot be lifted to the full model")
        full.append(frame)
    if not replay_trace(c, spec, prop, full):
        raise SequentialError("lifted counterexample failed full-model replay")
    return {**result, "trace": [{"cycle": t, "values": f} for t, f in enumerate(full)]}

"""Exact syntactic cone reduction and full-design counterexample lifting.

The selected circuit is a dependency-closed projection of an already validated
single-clock, deterministic transition system. No black-box abstraction, don't-
care assumption, or source filtering is performed here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from opencollate.sequential_ir import Circuit, SequentialError, simulate_frame
from opencollate.sequential_smt import replay_trace


def select_cone(c: Circuit, spec: dict[str, Any], prop: dict[str, Any]) -> Circuit:
    """Retain property/history guards, reset and fixed inputs, closing over time."""
    wanted = {prop["sink"], *spec["assumptions"], *(g["signal"] for g in prop["when"])}
    if "source" in prop:
        wanted.add(prop["source"])
    if spec["reset"]:
        wanted.add(spec["reset"]["signal"])
    todo = sorted(wanted)
    seen: set[str] = set()
    nodes: set[int] = set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        root = c.next_state.get(name, c.combinational.get(name))
        if root is None:
            if name not in c.inputs:
                raise SequentialError("cone contains an undriven signal: " + name)
            continue
        pending = [root]
        while pending:
            idx = pending.pop()
            if idx in nodes:
                continue
            nodes.add(idx)
            node = c.nodes[idx]
            if node.op == "ref":
                todo.append(str(node.value))
            pending.extend(node.args)
    # Including each retained state's update, not merely its current value, is
    # essential: indirect feedback may first affect a property many cycles later.
    reduced = Circuit(c.top, c.clock, frontend=c.frontend)
    reduced.signals = {n: shape for n, shape in c.signals.items() if n in seen or n == c.clock}
    reduced.inputs = [n for n in c.inputs if n in seen]
    reduced.outputs = [n for n in c.outputs if n in seen]
    reduced.locations = {n: loc for n, loc in c.locations.items() if n in reduced.signals}
    reduced.sources = list(c.sources)
    mapping: dict[int, int] = {}
    for idx in sorted(nodes):
        node = c.nodes[idx]
        mapping[idx] = reduced.add(
            node.op, node.width, node.signed, tuple(mapping[a] for a in node.args), node.value
        )
    reduced.next_state = {n: mapping[r] for n, r in c.next_state.items() if n in seen}
    reduced.combinational = {n: mapping[r] for n, r in c.combinational.items() if n in seen}
    reduced.finalize()
    return reduced


def cone_summary(c: Circuit) -> dict[str, int]:
    return {
        "signals": len(c.signals) - 1,
        "state_bits": sum(c.signals[n][0] for n in c.next_state),
        "inputs": len(c.inputs),
        "ir_nodes": len(c.nodes),
    }


def lift_trace(
    full: Circuit,
    reduced: Circuit,
    spec: dict[str, Any],
    prop: dict[str, Any],
    trace: list[dict[str, Any]],
    *,
    checkpoint: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Complete a projected witness and independently replay the entire design.

    Omitted initial registers and unconstrained inputs are zero. The dependency
    closure guarantees they cannot affect the retained signals. A disagreement is
    an analysis failure, not a fabricated full-design counterexample.
    """
    if not trace:
        raise SequentialError("cannot lift an empty counterexample")
    projected = [row["values"] for row in trace]
    if not replay_trace(reduced, spec, prop, projected):
        raise SequentialError("projected counterexample failed replay before lifting")
    states = {n: projected[0].get(n, 0) for n in full.next_state}
    frames: list[dict[str, int]] = []
    for t, expected in enumerate(projected):
        if checkpoint is not None:
            checkpoint()
        if trace[t]["cycle"] != t:
            raise SequentialError("counterexample cycles are not contiguous")
        inputs = {n: expected.get(n, 0) for n in full.inputs}
        observed, states = simulate_frame(full, states, inputs)
        if any(observed.get(n) != v for n, v in expected.items()):
            raise SequentialError("full-design trace disagrees with the property cone")
        frames.append(observed)
    if not replay_trace(full, spec, prop, frames):
        raise SequentialError("lifted counterexample failed full-design replay")
    return [{"cycle": t, "values": f} for t, f in enumerate(frames)]

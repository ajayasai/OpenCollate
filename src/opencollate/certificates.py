"""Proof-producing Boolean checks with solver-free certificate verification.

Generation optionally uses Glucose3 through python-sat. Verification does not
import a SAT/SMT solver: it reconstructs the CNF and checks RUP evidence plus
complete Python-replayed witnesses. Standalone requests bind submitted Boolean
formulas, not the source RTL or a user's translation of that RTL.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from importlib import import_module
from threading import Timer
from typing import Any

from opencollate.boolean import BoolExpr
from opencollate.boolean_proof_kernel import ProofError, ProofLimits, clause, verify_rup
from opencollate.formal import SEMANTICS, _digest, validate_obligations
from opencollate.proof_cnf import ENCODING, CompiledBoolean, compile_boolean

ROW_FIELDS = {
    "status",
    "variables",
    "obligation_sha256",
    "cnf_sha256",
    "guard_witness",
    "counterexample",
    "proof",
    "reason",
}


def _remaining(limits: ProofLimits, deadline: float) -> ProofLimits:
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining < 1:
        raise ProofError("certificate total time limit exceeded")
    return replace(limits, timeout_ms=min(limits.timeout_ms, remaining))


def _proof_lines(raw: Any, variables: int, limits: ProofLimits) -> list[list[int]]:
    """Normalize DRUP to addition-only RUP, retaining all deleted clauses.

    Deletion is only a storage optimization. Keeping already justified clauses
    cannot invalidate a RUP inference. RAT/non-RUP additions are NOT accepted.
    """
    if not isinstance(raw, list) or len(raw) > limits.max_steps:
        raise ProofError("native proof trace exceeds the step limit or is missing")
    result: list[list[int]] = []
    literals = 0
    for text in raw:
        if not isinstance(text, str) or len(text) > limits.max_literals * 12:
            raise ProofError("invalid native proof trace")
        parts = text.split()
        deletion = bool(parts and parts[0] == "d")
        if deletion:
            parts = parts[1:]
        if not parts or parts[-1] != "0":
            raise ProofError("unterminated native proof clause")
        if any(
            not part.removeprefix("-").isascii()
            or not part.removeprefix("-").isdigit()
            or len(part) > 10
            for part in parts
        ):
            raise ProofError("non-integer native proof literal")
        values = [int(part) for part in parts[:-1]]
        literals += len(values)
        if literals > limits.max_literals:
            raise ProofError("native proof literal limit exceeded")
        if any(not 1 <= abs(value) <= variables for value in values):
            raise ProofError("native proof literal is out of range")
        values = sorted(set(values))
        if any(-value in values for value in values):
            continue  # A tautology contributes no inference.
        clause(values, variables)
        if not deletion:
            result.append(values)
    # Some solvers return no trace when input unit propagation finds a conflict.
    # Appending the empty clause is safe ONLY because the kernel checks it.
    if not result or result[-1]:
        result.append([])
    if len(result) > limits.max_steps:
        raise ProofError("normalized proof exceeds the step limit")
    return result


def _solve(
    compiled: CompiledBoolean,
    query: str,
    limits: ProofLimits,
    deadline: float,
) -> tuple[dict[str, bool] | None, list[list[int]] | None]:
    try:
        module = import_module("pysat.solvers")
    except ModuleNotFoundError as error:
        raise ProofError(
            "optional proof producer unavailable; install opencollate[certificates]"
        ) from error
    cnf = compiled.cnf(query)
    if _needs_process():
        from opencollate.certificate_process import solve_isolated

        return solve_isolated(
            cnf, compiled.encoder.inputs, compiled.encoder.variables, limits, deadline
        )
    return _solve_native(
        cnf, compiled.encoder.inputs, compiled.encoder.variables, limits, deadline, module.Glucose3
    )


def _needs_process() -> bool:
    # MSVC PySAT builds leave native FILE* proof output fully buffered. Never
    # flush the host application's CRT or retain these streams in that process.
    return sys.platform == "win32"


def _solve_native(
    cnf: list[tuple[int, ...]],
    inputs: Mapping[str, int],
    variables: int,
    limits: ProofLimits,
    deadline: float,
    solver_type: Callable[..., Any],
    *,
    flush_proof: Callable[[], None] | None = None,
) -> tuple[dict[str, bool] | None, list[list[int]] | None]:
    """One native instance; Windows callers invoke this only in a fresh worker."""
    _remaining(limits, deadline)
    # A fresh solver per base formula avoids assumptions leaking into proofs.
    with solver_type(bootstrap_with=cnf, with_proof=True) as solver:
        remaining = _remaining(limits, deadline)
        timer = Timer(remaining.timeout_ms / 1000, solver.interrupt)
        timer.daemon = True
        timer.start()
        try:

            def solve(assumptions: list[int]) -> bool:
                _remaining(limits, deadline)
                solver.conf_budget(limits.conflict_limit)
                answer = solver.solve_limited(assumptions=assumptions, expect_interrupt=True)
                if type(answer) is not bool:
                    raise ProofError("proof producer was interrupted or exhausted its budget")
                return answer

            if not solve([]):
                if flush_proof is not None:
                    flush_proof()
                return None, _proof_lines(solver.get_proof(), variables, limits)
            # Canonical, complete SOURCE witness: False before True in name order.
            prefix: list[int] = []
            witness: dict[str, bool] = {}
            for name in sorted(inputs):
                variable = inputs[name]
                value = not solve([*prefix, -variable])
                witness[name] = value
                prefix.append(variable if value else -variable)
            if not solve(prefix):
                raise ProofError("producer contradicted its own witness refinement")
            model = solver.get_model()
            if not isinstance(model, list):
                raise ProofError("proof producer returned no SAT model")
            if any(type(x) is not int or not 1 <= abs(x) <= variables for x in model):
                raise ProofError("proof producer returned invalid SAT literals")
            values = set(model)
            if any(-x in values for x in values) or len(values) != len(model):
                raise ProofError("proof producer returned contradictory SAT literals")
            if any(not any(literal in values for literal in item) for item in cnf):
                raise ProofError("producer SAT model does not satisfy the reconstructed CNF")
            if any(literal not in values for literal in prefix):
                raise ProofError("producer SAT model disagrees with the source witness")
            return witness, None
        finally:
            timer.cancel()
            timer.join()  # Never interrupt a deleted solver from a late callback.
            if flush_proof is not None:
                # A SAT query's assumption refinements can also write proof data.
                # Flush while the Python-owned file descriptor is still open.
                flush_proof()


def _verify_boolean(compiled: CompiledBoolean, row: Mapping[str, Any], limits: ProofLimits) -> None:
    if set(row) != ROW_FIELDS:
        raise ProofError("invalid Boolean certificate fields")
    status = row["status"]
    if status not in {"equivalent", "different", "vacuous"}:
        raise ProofError("no checkable Boolean certificate outcome")
    if (
        row["variables"] != list(compiled.names)
        or row["obligation_sha256"] != compiled.obligation_sha256
    ):
        raise ProofError("certificate variable inventory or obligation binding differs")
    if not isinstance(row["reason"], str) or len(row["reason"]) > 4096:
        raise ProofError("invalid certificate reason")
    query = "guard" if status == "vacuous" else "difference"
    if row["cnf_sha256"] != compiled.digest(query):
        raise ProofError("certificate CNF binding differs from the reconstructed formula")
    if status == "vacuous":
        if row["guard_witness"] is not None or row["counterexample"] is not None:
            raise ProofError("a vacuous certificate cannot contain satisfying witnesses")
    else:
        if not compiled.evaluate(row["guard_witness"])[2]:
            raise ProofError("certificate assumptions witness failed Python replay")
    if status == "different":
        if row["proof"] is not None:
            raise ProofError("a counterexample must not carry an UNSAT proof")
        left, right, guard = compiled.evaluate(row["counterexample"])
        if not guard or left == right:
            raise ProofError("certificate counterexample failed Python replay")
    else:
        if row["counterexample"] is not None:
            raise ProofError("an UNSAT certificate cannot contain a counterexample")
        verify_rup(compiled.cnf(query), row["proof"], compiled.encoder.variables, limits=limits)


def certify_boolean(
    left: BoolExpr | str,
    right: BoolExpr | str,
    *,
    assume: BoolExpr | str = "1",
    aliases: Mapping[str, str] | None = None,
    limits: ProofLimits | None = None,
) -> dict[str, Any]:
    """Generate and independently check evidence; failure never becomes a pass."""
    budget = limits or ProofLimits()
    deadline = time.monotonic() + budget.timeout_ms / 1000
    row: dict[str, Any] = {
        "status": "inconclusive",
        "variables": [],
        "obligation_sha256": None,
        "cnf_sha256": None,
        "guard_witness": None,
        "counterexample": None,
        "proof": None,
        "reason": "",
    }
    try:
        compiled = compile_boolean(left, right, assume, aliases=aliases, limits=budget)
        row["variables"] = list(compiled.names)
        row["obligation_sha256"] = compiled.obligation_sha256
        witness, proof = _solve(compiled, "guard", budget, deadline)
        if witness is None:
            row.update(
                status="vacuous",
                cnf_sha256=compiled.digest("guard"),
                proof=proof,
                reason="assumptions are unsatisfiable; no equivalence claim is made",
            )
        else:
            counterexample, proof = _solve(compiled, "difference", budget, deadline)
            row.update(guard_witness=witness, cnf_sha256=compiled.digest("difference"))
            if counterexample is None:
                row.update(
                    status="equivalent",
                    proof=proof,
                    reason="nonvacuous equivalence has a checked RUP proof",
                )
            else:
                row.update(
                    status="different",
                    counterexample=counterexample,
                    reason="functions differ for a Python-replayed complete witness",
                )
        _verify_boolean(compiled, row, _remaining(budget, deadline))
        _remaining(budget, deadline)
    except (Exception, SystemExit) as error:
        # Optional native libraries are untrusted proof PRODUCERS. They cannot
        # establish a pass by crashing, returning a bogus model, or claiming UNSAT.
        row.update(
            status="inconclusive",
            cnf_sha256=None,
            guard_witness=None,
            counterexample=None,
            proof=None,
            reason=f"{type(error).__name__}: {error}"[:4096],
        )
    return row


def _outcome(rows: list[dict[str, Any]]) -> tuple[str, int]:
    states = {row["status"] for row in rows}
    code = 2 if states & {"inconclusive", "vacuous"} else 1 if "different" in states else 0
    return ("pass", "fail", "inconclusive")[code], code


def certify_obligations(
    value: Mapping[str, Any], *, limits: ProofLimits | None = None
) -> dict[str, Any]:
    request = validate_obligations(value)
    rows = [
        {
            "id": item["id"],
            **certify_boolean(item["left"], item["right"], assume=item["assume"], limits=limits),
        }
        for item in request["obligations"]
    ]
    status, code = _outcome(rows)
    result = {
        "schema_version": 1,
        "semantics": SEMANTICS,
        "encoding": ENCODING,
        "request_sha256": _digest(request),
        "status": status,
        "exit_code": code,
        "results": rows,
    }
    result["certificate_sha256"] = _digest(result)
    return result


def verify_certificate(
    value: Mapping[str, Any],
    certificate: Mapping[str, Any],
    *,
    limits: ProofLimits | None = None,
) -> dict[str, Any]:
    """Verify against the supplied request WITHOUT importing or invoking a solver.

    An honest inconclusive row remains exit 2, not certified. Invalid evidence
    raises ProofError; caller must treat it as incomplete analysis, also exit 2.
    """
    budget = limits or ProofLimits()
    request = validate_obligations(value)
    fields = {
        "schema_version",
        "semantics",
        "encoding",
        "request_sha256",
        "status",
        "exit_code",
        "results",
        "certificate_sha256",
    }
    if not isinstance(certificate, Mapping) or set(certificate) != fields:
        raise ProofError("invalid certificate bundle fields")
    if type(certificate["schema_version"]) is not int or certificate["schema_version"] != 1:
        raise ProofError("unsupported certificate version")
    if certificate["semantics"] != SEMANTICS or certificate["encoding"] != ENCODING:
        raise ProofError("unsupported certificate semantics or encoding")
    if certificate["request_sha256"] != _digest(request):
        raise ProofError("certificate belongs to a different request")
    rows = certificate["results"]
    if not isinstance(rows, list) or len(rows) != len(request["obligations"]):
        raise ProofError("missing or additional certificate results")
    checked = 0
    for item, row in zip(request["obligations"], rows, strict=True):
        if (
            not isinstance(row, Mapping)
            or set(row) != ROW_FIELDS | {"id"}
            or row["id"] != item["id"]
        ):
            raise ProofError("invalid certificate result fields or identities")
        if not isinstance(row["status"], str):
            raise ProofError("invalid certificate status")
        if row["status"] == "inconclusive":
            # No evidence is accepted for this row, and aggregation cannot pass.
            if any(
                row[key] is not None
                for key in ("proof", "guard_witness", "counterexample", "cnf_sha256")
            ):
                raise ProofError("inconclusive certificate must not assert evidence")
            if not isinstance(row["reason"], str) or not row["reason"] or len(row["reason"]) > 4096:
                raise ProofError("inconclusive certificate requires a bounded reason")
            if (
                not isinstance(row["variables"], list)
                or len(row["variables"]) > budget.max_variables
            ):
                raise ProofError("invalid inconclusive variable inventory")
            if any(not isinstance(name, str) or len(name) > 65536 for name in row["variables"]):
                raise ProofError("invalid inconclusive variable name")
            binding = row["obligation_sha256"]
            if binding is not None and (not isinstance(binding, str) or len(binding) != 64):
                raise ProofError("invalid inconclusive binding")
            continue
        deadline = time.monotonic() + budget.timeout_ms / 1000
        compiled = compile_boolean(item["left"], item["right"], item["assume"], limits=budget)
        _verify_boolean(
            compiled, {key: row[key] for key in ROW_FIELDS}, _remaining(budget, deadline)
        )
        _remaining(budget, deadline)
        checked += 1
    status, code = _outcome(rows)
    if (
        type(certificate["exit_code"]) is not int
        or certificate["exit_code"] != code
        or certificate["status"] != status
    ):
        raise ProofError("certificate aggregate outcome disagrees with checked results")
    if certificate["certificate_sha256"] != _digest(
        {key: certificate[key] for key in fields - {"certificate_sha256"}}
    ):
        raise ProofError("certificate content digest mismatch")
    return {
        "schema_version": 1,
        "status": status,
        "exit_code": code,
        "request_sha256": certificate["request_sha256"],
        "certificate_sha256": certificate["certificate_sha256"],
        "checked_results": checked,
        "inconclusive_results": len(rows) - checked,
        "proof_system": "RUP",
        "solver_invoked": False,
    }

"""Source-bound safety certificates with a solver-free verification path.

Creation uses optional PySAT/Glucose3 only as an untrusted proof producer.
Verification regenerates CNF from current RTL and checks RUP refutations and
reachable guard witnesses. It never imports or calls a SAT/SMT solver.
"""

from __future__ import annotations

import argparse
import importlib
import json
import threading
from pathlib import Path
from typing import Any

from opencollate.atomic_output import atomic_write_text
from opencollate.proof_kernel import Limits, Meter, ProofError, verify_rup
from opencollate.proof_producer_io import flush_producer_output, read_producer_proof
from opencollate.sequential_cnf import ENCODING, Unroller, base_problem, digest, step_problem
from opencollate.sequential_ir import Circuit, simulate_frame
from opencollate.sequential_rtl import load_circuit
from opencollate.sequential_sources import declared_files
from opencollate.sequential_spec import (
    SEMANTICS,
    holds,
    integer,
    normalize,
    obj,
    read_json,
    validate_signals,
)


def _binding(c: Circuit, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_sha256": digest(spec),
        "ir_sha256": digest(c.serialized()),
        "sources": c.sources,
        "frontend": c.frontend,
    }


def _load(request: dict[str, Any], root: Path) -> tuple[Circuit, dict[str, Any]]:
    spec = normalize(request)
    circuit = load_circuit(
        spec["files"],
        root=root,
        top=spec["top"],
        clock=spec["clock"],
        preprocess=spec.get("preprocess"),
    )
    validate_signals(circuit, spec)
    return circuit, spec


def _proof_rows(lines: Any, variables: int, meter: Meter) -> list[list[int]]:
    if type(lines) is not list or len(lines) > meter.limits.steps:
        raise ProofError("proof producer returned no bounded DRUP evidence")
    result: list[list[int]] = []
    count = 0
    for line in lines:
        if type(line) is not str:
            raise ProofError("invalid DRUP producer output")
        meter.spend(len(line) + 1)
        if len(line) > 8 * 1048576:
            raise ProofError("DRUP line exceeds size bound")
        parts = line.split()
        deletion = bool(parts and parts[0] == "d")
        if deletion:
            parts = parts[1:]
        if not parts or parts[-1] != "0":
            raise ProofError("unterminated DRUP clause")
        clause = [int(p) for p in parts[:-1]]
        count += len(clause)
        if count > meter.limits.literals or any(lit == 0 or abs(lit) > variables for lit in clause):
            raise ProofError("DRUP literal bound exhausted or invalid literal")
        # Retaining proved clauses when a producer deletes them is sound.
        if not deletion:
            result.append(clause)
    # Some solvers discover contradiction while loading clauses and log no
    # final step. An explicit empty RUP step must STILL pass our checker.
    if not result or result[-1] != []:
        result.append([])
    return result


def _solve(
    problem: dict[str, Any], meter: Meter, *, proof: bool
) -> tuple[bool, list[int] | list[list[int]]]:
    try:
        solver_class = importlib.import_module("pysat.solvers").Solver
    except ImportError as error:
        raise ProofError(
            "certificate creation requires pip install 'opencollate[certificates]'"
        ) from error
    meter.remaining()
    with solver_class(name="g3", bootstrap_with=problem["clauses"], with_proof=proof) as solver:
        timer = threading.Timer(meter.remaining(), solver.interrupt)
        timer.daemon = True
        timer.start()
        try:
            outcome = solver.solve_limited(expect_interrupt=True)
        finally:
            timer.cancel()
            timer.join()
            if proof:
                flush_producer_output()
        meter.remaining()
        if outcome is None:
            raise ProofError("certificate proof producer was interrupted or inconclusive")
        if outcome:
            model = solver.get_model()
            if type(model) is not list or any(type(v) is not int or v == 0 for v in model):
                raise ProofError("invalid SAT witness from producer")
            positive = {v for v in model if v > 0}
            for clause in problem["clauses"]:
                meter.spend(len(clause) + 1)
                if not any((abs(v) in positive) == (v > 0) for v in clause):
                    raise ProofError("producer SAT witness does not satisfy the generated CNF")
            return True, model
        if not proof:
            return False, []
        rows = _proof_rows(read_producer_proof(solver), problem["variables"], meter)
        verify_rup(problem["clauses"], problem["variables"], rows, meter=meter)
        return False, rows


def _evidence(problem: dict[str, Any], proof: list[Any]) -> dict[str, Any]:
    return {
        "problem_sha256": digest(problem),
        "variables": problem["variables"],
        "clauses": len(problem["clauses"]),
        "proof": proof,
    }


def _check_evidence(problem: dict[str, Any], value: Any, meter: Meter) -> dict[str, int]:
    row = obj(value, {"problem_sha256", "variables", "clauses", "proof"}, set())
    if (
        type(row["variables"]) is not int
        or type(row["clauses"]) is not int
        or row["variables"] != problem["variables"]
        or row["clauses"] != len(problem["clauses"])
        or row["problem_sha256"] != digest(problem)
    ):
        raise ProofError("proof obligation differs from the current RTL-derived CNF")
    return verify_rup(problem["clauses"], problem["variables"], row["proof"], meter=meter)


def _cover(unroll: Unroller, model: list[int], prop: dict[str, Any]) -> list[dict[str, Any]]:
    positive = {v for v in model if v > 0}
    frames: list[dict[str, Any]] = []
    for t, frame in enumerate(unroll.frames):
        values = {
            name: sum(
                (1 << bit) for bit, lit in enumerate(bits) if (abs(lit) in positive) == (lit > 0)
            )
            for name, bits in frame.items()
        }
        frames.append({"cycle": t, "values": values})
        if t >= prop["start_cycle"] and holds(prop, [f["values"] for f in frames], t)[0]:
            return frames
    raise ProofError("producer model did not activate a property guard")


def _check_cover(
    c: Circuit, spec: dict[str, Any], prop: dict[str, Any], value: Any, meter: Meter
) -> int:
    if type(value) is not list or not 1 <= len(value) <= spec["depth"] + 1:
        raise ProofError("invalid guard witness length")
    if len(value) - 1 < prop["start_cycle"]:
        raise ProofError("guard witness ends before the first checked cycle")
    driven = set(c.inputs) | set(c.next_state) | set(c.combinational)
    frames = []
    updated: dict[str, int] | None = None
    for t, raw in enumerate(value):
        row = obj(raw, {"cycle", "values"}, set())
        f = row["values"]
        if type(row["cycle"]) is not int or row["cycle"] != t or type(f) is not dict:
            raise ProofError("invalid guard witness cycle or frame")
        if set(f) != driven:
            raise ProofError("guard witness signal inventory mismatch")
        for name, val in f.items():
            meter.spend()
            if type(val) is not int or not 0 <= val < (1 << c.signals[name][0]):
                raise ProofError("guard witness value is not a finite-width bit pattern")
        if any(f[n] != v for n, v in spec["assumptions"].items()):
            raise ProofError("guard witness violates fixed input assumptions")
        reset = spec["reset"]
        if reset and f[reset["signal"]] != (
            reset["active"] if t < reset["cycles"] else 1 - reset["active"]
        ):
            raise ProofError("guard witness violates the reset protocol")
        if updated is not None and any(f[n] != v for n, v in updated.items()):
            raise ProofError("guard witness violates a state transition")
        meter.spend(len(c.nodes) + 1)
        observed, updated = simulate_frame(
            c, {n: f[n] for n in c.next_state}, {n: f[n] for n in c.inputs}
        )
        if observed != f:
            raise ProofError("guard witness violates a combinational equation")
        frames.append(f)
    if not holds(prop, frames, len(frames) - 1)[0]:
        raise ProofError("guard witness does not activate the requested guard")
    meter.remaining()
    return len(frames) - 1


def _max_k(spec: dict[str, Any], prop: dict[str, Any]) -> int:
    history = max([prop["latency"], *(g["lag"] for g in prop["when"])])
    reset_cycles = spec["reset"]["cycles"] if spec["reset"] else 0
    prefix = max(prop["start_cycle"], reset_cycles + history)
    return min(spec["induction"], spec["depth"] - prefix)


def certify(request: dict[str, Any], *, root: Path, limits: Limits | None = None) -> dict[str, Any]:
    meter = Meter(limits)
    c, spec = _load(request, root)
    claims = []
    for prop in spec["properties"]:
        if _max_k(spec, prop) < 1:
            raise ProofError(f"property {prop['id']}: insufficient induction/base depth")
        unroll, base, cover = base_problem(c, spec, prop, meter)
        reachable, model = _solve(cover, meter, proof=False)
        if not reachable:
            raise ProofError(f"property {prop['id']}: guard is uncovered within the bound")
        witness = _cover(unroll, model, prop)  # type: ignore[arg-type]
        _check_cover(c, spec, prop, witness, meter)
        failed, base_proof = _solve(base, meter, proof=True)
        if failed:
            raise ProofError(f"property {prop['id']}: base counterexample; no certificate issued")
        step_evidence = None
        for k in range(1, _max_k(spec, prop) + 1):
            step = step_problem(c, spec, prop, k, meter)
            failed, step_proof = _solve(step, meter, proof=True)
            if not failed:
                step_evidence = _evidence(step, step_proof)
                break
        if step_evidence is None:
            raise ProofError(f"property {prop['id']}: bounded only; no induction certificate")
        claims.append(
            {
                "id": prop["id"],
                "induction_depth": k,
                "cover": witness,
                "base": _evidence(base, base_proof),
                "step": step_evidence,
            }
        )
    certificate = {
        "schema_version": 1,
        "semantics": SEMANTICS,
        "encoding": ENCODING,
        "binding": _binding(c, spec),
        "claims": claims,
    }
    certificate["certificate_sha256"] = digest(certificate)
    meter.remaining()
    return certificate


def verify_certificate(
    request: dict[str, Any],
    certificate: dict[str, Any],
    *,
    root: Path,
    limits: Limits | None = None,
) -> dict[str, Any]:
    """Verify from current source without importing any SAT/SMT solver."""
    meter = Meter(limits)
    doc = obj(
        certificate,
        {
            "schema_version",
            "semantics",
            "encoding",
            "binding",
            "claims",
            "certificate_sha256",
        },
        set(),
    )
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise ProofError("invalid certificate version")
    if doc["semantics"] != SEMANTICS or doc["encoding"] != ENCODING:
        raise ProofError("unsupported certificate semantics or encoding")
    if doc["certificate_sha256"] != digest(
        {k: v for k, v in doc.items() if k != "certificate_sha256"}
    ):
        raise ProofError("certificate content digest mismatch")
    c, spec = _load(request, root)
    if digest(doc["binding"]) != digest(_binding(c, spec)):
        raise ProofError("certificate source/request/IR/frontend binding mismatch")
    claims = doc["claims"]
    if type(claims) is not list or len(claims) != len(spec["properties"]):
        raise ProofError("certificate must cover every requested property exactly once")
    results = []
    for prop, raw in zip(spec["properties"], claims, strict=True):
        row = obj(raw, {"id", "induction_depth", "cover", "base", "step"}, set())
        if row["id"] != prop["id"]:
            raise ProofError("certificate property identity/order mismatch")
        k = integer(row["induction_depth"], 1, _max_k(spec, prop), "certificate induction depth")
        cover_cycle = _check_cover(c, spec, prop, row["cover"], meter)
        _, base, _ = base_problem(c, spec, prop, meter)
        base_stats = _check_evidence(base, row["base"], meter)
        step = step_problem(c, spec, prop, k, meter)
        step_stats = _check_evidence(step, row["step"], meter)
        results.append(
            {
                "id": prop["id"],
                "status": "certificate-verified",
                "induction_depth": k,
                "cover_cycle": cover_cycle,
                "base": base_stats,
                "step": step_stats,
            }
        )
    meter.remaining()
    return {
        "schema_version": 1,
        "status": "certificate-verified",
        "exit_code": 0,
        "encoding": ENCODING,
        "binding": doc["binding"],
        "certificate_sha256": doc["certificate_sha256"],
        "results": results,
    }


def add_commands(commands: Any) -> None:
    for name in ("certify", "verify-certificate"):
        sub = commands.add_parser(name, help="source-bound safety proof certificates")
        sub.add_argument("request", type=Path)
        if name == "verify-certificate":
            sub.add_argument("certificate", type=Path)
        sub.add_argument("--timeout-ms", type=int, default=30_000)
        sub.add_argument("--proof-work", type=int, default=50_000_000)
        sub.add_argument("-o", "--output", type=Path)
        sub.set_defaults(handler=command_handler)


def command_handler(args: argparse.Namespace) -> int:
    try:
        timeout = integer(args.timeout_ms, 1, 120_000, "timeout_ms")
        work = integer(args.proof_work, 1, 100_000_000, "proof_work")
        request_path = args.request.resolve()
        request = read_json(request_path)
        spec = normalize(request)
        protected = [
            request_path,
            *[(request_path.parent / f).resolve() for f in declared_files(spec)],
        ]
        checking = args.sequential_command == "verify-certificate"
        if checking:
            protected.append(args.certificate.resolve())
        if args.output:
            out = args.output.resolve()
            if any(
                out == p or (out.exists() and p.exists() and out.samefile(p)) for p in protected
            ):
                raise ProofError("output must not alias a request, source, or certificate input")
        limits = Limits(timeout_ms=timeout, work=work)
        result = (
            verify_certificate(
                request, read_json(args.certificate), root=request_path.parent, limits=limits
            )
            if checking
            else certify(request, root=request_path.parent, limits=limits)
        )
        text = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        if len(text.encode()) > 8 * 1048576:
            raise ProofError("certificate output exceeds the 8 MiB JSON bound")
        if args.output:
            atomic_write_text(args.output, text)
        else:
            print(text, end="")
        return 0
    except Exception as error:
        # Never publish failure text over a potentially aliased or prior artifact.
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "not-certified",
                    "exit_code": 2,
                    "error": f"{type(error).__name__}: {error}",
                },
                sort_keys=True,
            )
        )
        return 2

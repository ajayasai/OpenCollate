from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from opencollate.cli import main
from opencollate.proof_kernel import ProofError
from opencollate.sequential import replay, run_request
from opencollate.sequential_certificate import certify, verify_certificate
from opencollate.sequential_ir import SequentialError
from opencollate.sequential_rtl import load_circuit
from opencollate.sequential_sources import _logical_end, _path, _resolve, normalize_preprocess
from opencollate.sequential_spec import normalize

RTL = """`timescale 1ns/1ps
`default_nettype none
`include "defs.svh"
module top(input logic clk, input logic [`WIDTH-1:0] a, output logic [`WIDTH-1:0] q);
  always_ff @(posedge clk) q <= `VALUE(a);
endmodule
`default_nettype wire
"""
HEADER = """`ifndef DEFS_SVH
`define DEFS_SVH
`ifdef INVERT
`define VALUE(a) (~(a))
`else
`define VALUE(a) (a)
`endif
`endif
"""


def project(root: Path) -> dict[str, Any]:
    (root / "rtl").mkdir(parents=True, exist_ok=True)
    (root / "inc").mkdir(exist_ok=True)
    (root / "rtl/top.sv").write_text(RTL, encoding="utf-8", newline="\n")
    (root / "inc/defs.svh").write_text(HEADER, encoding="utf-8", newline="\n")
    return {
        "schema_version": 1,
        "semantics": "two-valued-synchronous",
        "files": ["rtl/top.sv"],
        "top": "top",
        "clock": "clk",
        "preprocess": {
            "headers": ["inc/defs.svh"],
            "include_dirs": ["inc"],
            "defines": {"WIDTH": "8"},
        },
        "depth": 4,
        "induction": 2,
        "properties": [{"id": "copy", "sink": "q", "source": "a", "latency": 1}],
    }


def test_native_macros_headers_receipts_and_certificates(tmp_path: Path) -> None:
    request = project(tmp_path)
    receipt = run_request(request, root=tmp_path)
    assert receipt["status"] == "proven"
    assert replay(request, receipt, root=tmp_path) == receipt
    certificate = certify(request, root=tmp_path)
    assert (
        verify_certificate(request, certificate, root=tmp_path)["status"] == "certificate-verified"
    )
    assert [s["path"] for s in receipt["binding"]["sources"]] == ["rtl/top.sv", "inc/defs.svh"]
    assert receipt["model"]["locations"]["q"] == {"source": "rtl/top.sv", "line": 5, "column": 28}
    base = Path(__file__).parents[1] / "src/opencollate/schemas"
    for value, schema in [
        (request, "sequential-request"),
        (receipt, "sequential-receipt"),
        (certificate, "sequential-certificate"),
    ]:
        jsonschema.validate(value, json.loads((base / f"{schema}.schema.json").read_text()))


def test_build_define_changes_property_and_invalidates_saved_proof(tmp_path: Path) -> None:
    request = project(tmp_path)
    receipt = run_request(request, root=tmp_path)
    certificate = certify(request, root=tmp_path)
    request["preprocess"]["defines"]["INVERT"] = "1"
    changed = run_request(request, root=tmp_path)
    assert changed["status"] == "counterexample"
    assert changed["results"][0]["trace"]
    with pytest.raises(SequentialError, match="changed"):
        replay(request, receipt, root=tmp_path)
    with pytest.raises((ProofError, SequentialError), match="binding|changed|source"):
        verify_certificate(request, certificate, root=tmp_path)
    with pytest.raises(ProofError, match="base counterexample"):
        certify(request, root=tmp_path)


@pytest.mark.parametrize("edit", ["active", "inactive", "unused"])
def test_every_declared_header_is_bound(tmp_path: Path, edit: str) -> None:
    request = project(tmp_path)
    if edit == "unused":
        (tmp_path / "inc/unused.svh").write_text("// unused\n")
        request["preprocess"]["headers"].append("inc/unused.svh")
    receipt = run_request(request, root=tmp_path)
    certificate = certify(request, root=tmp_path)
    path = tmp_path / ("inc/unused.svh" if edit == "unused" else "inc/defs.svh")
    original = path.read_text()
    altered = original.replace("(~(a))", "(a)") if edit == "inactive" else original + "// changed\n"
    path.write_text(altered)
    with pytest.raises(SequentialError, match="changed"):
        replay(request, receipt, root=tmp_path)
    with pytest.raises((ProofError, SequentialError)):
        verify_certificate(request, certificate, root=tmp_path)


def test_snapshot_precedes_native_frontend_no_header_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencollate import sequential_sources as sources

    request = project(tmp_path)
    saved = sources._tokens

    def change_after_snapshot(s: Any, data: bytes, *, remaining: int) -> Any:
        (tmp_path / "inc/defs.svh").write_text("`define VALUE(a) (~a)\n")
        return saved(s, data, remaining=remaining)

    monkeypatch.setattr(sources, "_tokens", change_after_snapshot)
    receipt = run_request(request, root=tmp_path)
    assert receipt["status"] == "proven"
    assert receipt["binding"]["sources"][1]["sha256"] == hashlib.sha256(HEADER.encode()).hexdigest()
    monkeypatch.setattr(sources, "_tokens", saved)
    assert run_request(request, root=tmp_path)["status"] == "counterexample"


def test_nested_headers_guarded_cycle_and_shared_compilation_unit(tmp_path: Path) -> None:
    request = project(tmp_path)
    (tmp_path / "inc/defs.svh").write_text(
        '`ifndef X\n`define X\n`include "nested.svh"\n`define VALUE(a) `PASS(a)\n`endif\n'
    )
    (tmp_path / "inc/nested.svh").write_text('`include "defs.svh"\n`define PASS(a) (a)\n')
    request["preprocess"]["headers"].append("inc/nested.svh")
    (tmp_path / "setup.sv").write_text("`define FROM_SETUP 1\n")
    request["files"].insert(0, "setup.sv")
    (tmp_path / "rtl/top.sv").write_text(
        RTL.replace("`timescale", "`ifdef FROM_SETUP\n`timescale") + "`endif\n"
    )
    assert run_request(request, root=tmp_path)["status"] == "proven"
    request["files"].reverse()
    with pytest.raises(SequentialError):
        run_request(request, root=tmp_path)


@pytest.mark.parametrize(
    "text, expected",
    [
        ('`include "missing.svh"\n', "declared header"),
        ('`ifdef NOT_DEFINED\n`include "missing.svh"\n`endif\n', "declared header"),
        ('`include "../outside.svh"\n', "portable"),
        ('`include "/etc/passwd"\n', "portable"),
        ('`include "C:/outside.svh"\n', "portable"),
        ('`include "inc\\\\defs.svh"\n', "literal"),
        ("`include `FILE\n", "literal"),
        ("`include <defs.svh>\n", "literal"),
        ('`include "defs.svh" junk\n', "unexpected tokens"),
        ('`define BAD `include "defs.svh"\n', "inside macro"),
        ('`define BAD \\\n`include "defs.svh"\n', "inside macro"),
        ("`define JOIN(a,b) a``b\n", "token-pasting"),
        ('`define STR(a) `"a`"\n', "stringification"),
        ('`line 1 "pretend.sv" 0\n', "unsupported directive"),
        ("`pragma protect begin_protected\n", "unsupported directive"),
        ('`begin_keywords "1800-2017"\n', "unsupported directive"),
        ("`MISSING_MACRO\n", "undeclared macro"),
        ("`define include anything\n", "definition name"),
        ("`define\n", "definition name"),
        ("`include", "literal"),
    ],
)
def test_unsafe_or_unsupported_dependency_syntax(tmp_path: Path, text: str, expected: str) -> None:
    request = project(tmp_path)
    (tmp_path / "rtl/top.sv").write_text(text + RTL)
    with pytest.raises(SequentialError, match=expected):
        run_request(request, root=tmp_path)


def test_ambiguous_header_lookup_rejected(tmp_path: Path) -> None:
    request = project(tmp_path)
    (tmp_path / "rtl/defs.svh").write_text(HEADER)
    request["preprocess"]["headers"].append("rtl/defs.svh")
    with pytest.raises(SequentialError, match="exactly one"):
        run_request(request, root=tmp_path)


def test_comments_utf8_crlf_function_macros_and_logical_lines(tmp_path: Path) -> None:
    request = project(tmp_path)
    header = '// café `include "bad"\n/* `pragma protect */\n`define VALUE(a) \\\n ((a) + 0)\n'
    (tmp_path / "inc/defs.svh").write_bytes(header.replace("\n", "\r\n").encode())
    assert run_request(request, root=tmp_path)["status"] == "proven"
    assert _logical_end(b"a\\\r\nb\r\nc", 0) == 6
    assert _logical_end(b"no newline", 0) == 10


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        1,
        {"other": []},
        {"headers": "x"},
        {"headers": ["../x"]},
        {"headers": ["X.svh", "x.svh"]},
        {"headers": ["rtl/top.sv"]},
        {"headers": ["CON"]},
        {"include_dirs": "inc"},
        {"include_dirs": ["inc", "inc"]},
        {"defines": []},
        {"defines": {"X": 4}},
        {"defines": {"X": "`include"}},
        {"defines": {"X": "0\n`include"}},
        {"defines": {"X": "a/*b"}},
        {"defines": {"include": "1"}},
        {"defines": {"__FILE__": "1"}},
        {"defines": {"X=1": "2"}},
    ],
)
def test_preprocess_request_shape(tmp_path: Path, value: Any) -> None:
    request = project(tmp_path)
    request["preprocess"] = value
    with pytest.raises(SequentialError):
        normalize(request)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "/root",
        "a//b",
        "a/./b",
        "a/../b",
        "c:\\x",
        "a ",
        "a.",
        "COM1.svh",
        "LPT9",
        "a\0b",
        "é.svh",
    ],
)
def test_portable_path_rejection(value: str) -> None:
    with pytest.raises(SequentialError):
        _path(value)


def test_normalization_is_idempotent_and_legacy_is_unchanged(tmp_path: Path) -> None:
    request = project(tmp_path)
    assert normalize(normalize(request)) == normalize(request)
    del request["preprocess"]
    assert "preprocess" not in normalize(request)
    with pytest.raises(SequentialError, match="preprocessor"):
        run_request(request, root=tmp_path)
    assert normalize_preprocess({}, ["a.sv"]) == {"headers": [], "include_dirs": [], "defines": {}}
    assert _path(".", directory=True) == "."
    assert _resolve("top.sv", "a.svh", {"a.svh"}, ["."]) == "a.svh"


@pytest.mark.parametrize("link", ["symlink-outside", "hardlink", "fifo"])
def test_unsafe_input_aliases(tmp_path: Path, link: str) -> None:
    request = project(tmp_path)
    header = tmp_path / "inc/defs.svh"
    header.unlink()
    if link == "symlink-outside":
        other = tmp_path.parent / (tmp_path.name + "-external.svh")
        other.write_text(HEADER)
        try:
            header.symlink_to(other)
        except OSError:
            pytest.skip("symlinks unavailable")
    elif link == "hardlink":
        os.link(tmp_path / "rtl/top.sv", header)
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("POSIX named pipes unavailable")
        os.mkfifo(header)
    with pytest.raises(SequentialError):
        run_request(request, root=tmp_path)


@pytest.mark.parametrize("command", ["check", "replay", "certify", "verify-certificate"])
def test_cli_never_overwrites_declared_headers(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    request = project(tmp_path)
    req = tmp_path / "request.json"
    req.write_text(json.dumps(request))
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            run_request(request, root=tmp_path)
            if command == "replay"
            else certify(request, root=tmp_path)
        )
    )
    args = ["sequential", command, str(req)]
    if command in {"replay", "verify-certificate"}:
        args.append(str(receipt))
    header = tmp_path / "inc/defs.svh"
    original = header.read_bytes()
    assert main([*args, "--output", str(header)]) == 2
    assert "alias" in capsys.readouterr().out
    assert header.read_bytes() == original


def test_parallel_frontends_are_isolated(tmp_path: Path) -> None:
    roots = [tmp_path / str(i) for i in range(6)]
    requests = [project(p) for p in roots]
    for i, r in enumerate(requests):
        r["preprocess"]["defines"]["WIDTH"] = str(i + 1)

    def run(i: int) -> Any:
        return run_request(requests[i], root=roots[i])

    expected = [run(i) for i in range(6)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(run, range(6))) == expected


def test_undeclared_disk_header_cannot_shadow_manifest(tmp_path: Path) -> None:
    request = project(tmp_path)
    (tmp_path / "rtl/defs.svh").write_text("`define VALUE(a) (~a)\n")
    assert run_request(request, root=tmp_path)["status"] == "proven"


def test_resource_limits_and_binary_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opencollate import sequential_sources as sources

    request = project(tmp_path)
    header = tmp_path / "inc/defs.svh"
    header.write_bytes(b"x" * (1048576 + 1))
    with pytest.raises(SequentialError, match="byte limit"):
        run_request(request, root=tmp_path)
    header.write_bytes(b"abc\0def")
    with pytest.raises(SequentialError, match="NUL"):
        run_request(request, root=tmp_path)
    header.write_text(HEADER)
    monkeypatch.setattr(sources, "_MAX_TOKENS", 2)
    with pytest.raises(SequentialError, match="token limit"):
        run_request(request, root=tmp_path)


def test_source_relocation_preserves_binding(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    request = project(a)
    shutil.copytree(a, b)
    receipt = run_request(request, root=a)
    assert run_request(request, root=b) == receipt
    certificate = certify(request, root=a)
    assert verify_certificate(request, certificate, root=b)["status"] == "certificate-verified"


def test_native_include_recursion_is_bounded(tmp_path: Path) -> None:
    request = project(tmp_path)
    (tmp_path / "inc/defs.svh").write_text('`define VALUE(a) a\n`include "defs.svh"\n')
    with pytest.raises(SequentialError, match="elaboration failed"):
        run_request(request, root=tmp_path)


def test_icarus_macro_configuration_oracle(tmp_path: Path) -> None:
    if not shutil.which("iverilog") or not shutil.which("vvp"):
        pytest.skip("independent Icarus simulator unavailable")
    request = project(tmp_path)
    tb = """module tb;
reg clk=0; reg [7:0] a; wire [7:0] q;
top dut(clk,a,q);
initial begin
for (integer i=0;i<256;i=i+1) begin
 a=i; #1; clk=1; #1; $display("FRAME %0d %0d", a,q); clk=0; #1;
end
$finish;
end
endmodule
"""
    (tmp_path / "tb.sv").write_text(tb)
    for invert in (False, True):
        args = [
            "iverilog",
            "-g2012",
            "-s",
            "tb",
            "-DWIDTH=8",
            "-I",
            str(tmp_path / "inc"),
            "-o",
            str(tmp_path / "sim"),
        ]
        if invert:
            args.append("-DINVERT=1")
            request["preprocess"]["defines"]["INVERT"] = "1"
        subprocess.run(
            [*args, str(tmp_path / "rtl/top.sv"), str(tmp_path / "tb.sv")],
            check=True,
            capture_output=True,
            timeout=20,
        )
        output = subprocess.run(
            ["vvp", str(tmp_path / "sim")], check=True, capture_output=True, text=True, timeout=20
        ).stdout
        frames = [
            list(map(int, row.split()[1:]))
            for row in output.splitlines()
            if row.startswith("FRAME ")
        ]
        assert frames == [[i, i ^ (255 if invert else 0)] for i in range(256)]
        receipt = run_request(request, root=tmp_path)
        assert receipt["status"] == ("counterexample" if invert else "proven")


def test_rehashed_binding_cannot_turn_old_proof_into_new_build(tmp_path: Path) -> None:
    from opencollate.sequential_certificate import _binding
    from opencollate.sequential_cnf import digest
    from opencollate.sequential_spec import validate_signals

    request = project(tmp_path)
    cert = certify(request, root=tmp_path)
    request["preprocess"]["defines"]["INVERT"] = "1"
    spec = normalize(request)
    circuit = load_circuit(
        spec["files"],
        root=tmp_path,
        top=spec["top"],
        clock=spec["clock"],
        preprocess=spec["preprocess"],
    )
    validate_signals(circuit, spec)
    cert["binding"] = _binding(circuit, spec)
    cert["certificate_sha256"] = digest(
        {k: v for k, v in cert.items() if k != "certificate_sha256"}
    )
    with pytest.raises((SequentialError, ProofError)):
        verify_certificate(request, cert, root=tmp_path)


def test_header_locations_and_native_line_macro(tmp_path: Path) -> None:
    request = project(tmp_path)
    header = "`define VALUE(a) (a)\nlogic [7:0] line_number;\nassign line_number = `__LINE__;\n"
    (tmp_path / "inc/defs.svh").write_text(header)
    (tmp_path / "rtl/top.sv").write_text(
        "module top(input logic clk, input logic [7:0] a, output logic [7:0] q);\n"
        '`include "defs.svh"\nalways_ff @(posedge clk) q <= a;\nendmodule\n'
    )
    request["properties"].append({"id": "line", "sink": "line_number", "equals": 3})
    receipt = run_request(request, root=tmp_path)
    assert receipt["status"] == "proven"
    assert receipt["model"]["locations"]["line_number"]["source"] == "inc/defs.svh"
    assert receipt["model"]["locations"]["line_number"]["line"] == 2


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_output_alias_cannot_overwrite_header(
    tmp_path: Path, alias_kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    request = project(tmp_path)
    req = tmp_path / "request.json"
    req.write_text(json.dumps(request))
    alias = tmp_path / "aliased-output.json"
    header = tmp_path / "inc/defs.svh"
    try:
        if alias_kind == "symlink":
            alias.symlink_to(header)
        else:
            os.link(header, alias)
    except OSError:
        pytest.skip("filesystem alias creation unavailable")
    before = header.read_bytes()
    assert main(["sequential", "certify", str(req), "-o", str(alias)]) == 2
    assert "alias" in capsys.readouterr().out
    assert header.read_bytes() == before


def test_empty_manifest_can_enable_directives_without_headers(tmp_path: Path) -> None:
    request = project(tmp_path)
    request["preprocess"] = {}
    (tmp_path / "rtl/top.sv").write_text(
        "`define W 8\nmodule top(input logic clk, input logic [`W-1:0] a, "
        "output logic [`W-1:0] q);\nalways_ff @(posedge clk) q <= a;\nendmodule\n"
    )
    assert run_request(request, root=tmp_path)["status"] == "proven"


def test_public_preprocessing_oracles() -> None:
    from benchmarks.preprocessing import run_suite

    result = run_suite()
    assert result["status"] == "pass"
    assert len(result["cases"]) == 12
    assert all(row["pass"] for row in result["cases"])
    assert sum(row["expected"] == "proven" for row in result["cases"]) == 4
    with pytest.raises(ValueError):
        run_suite(repeat=0)


def test_lexical_budget_is_shared_before_reading_each_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencollate import sequential_sources as sources

    request = project(tmp_path)
    # The first file fits, but roots and headers together exceed the cap.
    import pyslang

    first = (tmp_path / request["files"][0]).read_bytes()
    count = len(sources._tokens(pyslang, first, remaining=200000))
    monkeypatch.setattr(sources, "_MAX_TOKENS", count + 1)
    original = sources._tokens
    budgets: list[int] = []

    def tracked(s: Any, data: bytes, *, remaining: int) -> list[tuple[str, str, int, int]]:
        budgets.append(remaining)
        return original(s, data, remaining=remaining)

    monkeypatch.setattr(sources, "_tokens", tracked)
    with pytest.raises(SequentialError, match="token limit"):
        run_request(request, root=tmp_path)
    assert budgets == [count + 1, 1]


def test_many_literal_includes_keep_original_line_order(tmp_path: Path) -> None:
    request = project(tmp_path)
    source = tmp_path / request["files"][0]
    original = source.read_text()
    source.write_text('`include "defs.svh"\n' * 128 + original)
    assert run_request(request, root=tmp_path)["status"] == "proven"

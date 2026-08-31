from __future__ import annotations

from pathlib import Path

import pytest

from opencollate.config import ProjectConfig, SourceConfig
from opencollate.diagnostics import Severity
from opencollate.engine import ComparisonEngine
from opencollate.model import FactState, ViewId
from opencollate.parsers import systemrdl as systemrdl_parser
from opencollate.parsers.ipxact import parse_ipxact
from opencollate.parsers.systemrdl import SystemRdlParser, parse_systemrdl

FIXTURES = Path(__file__).parent / "fixtures" / "systemrdl"


def _codes(view: object) -> set[str]:
    return {item.code for item in view.diagnostics}  # type: ignore[attr-defined]


def test_registers_fields_hierarchy_arrays_and_provenance_are_extracted() -> None:
    view = parse_systemrdl(
        FIXTURES / "uart.rdl",
        view_id="systemrdl.golden",
        top="uart_regs",
        component_name="uart0",
    )

    assert view.view == ViewId("systemrdl", "golden")
    assert view.complete
    assert not view.diagnostics
    assert [register.native_name for register in view.registers] == [
        "CTRL",
        "DATA[0]",
        "DATA[1]",
    ]

    control, data0, data1 = view.registers
    assert (
        control.component,
        control.memory_map,
        control.address_block,
        control.address_offset,
        control.absolute_address,
        control.size_bits,
        control.access,
    ) == ("uart0", "uart_regs", "uart_regs", 0, 0, 32, None)
    assert control.attributes["local_address_offset"] == 0
    assert control.attributes["register_files"] == []
    assert [
        (
            item.native_name,
            item.bit_offset,
            item.bit_width,
            item.access,
            item.reset_value,
            item.status,
        )
        for item in control.fields
    ] == [
        ("ENABLE", 0, 1, "read-write", 0, FactState.KNOWN),
        ("READY", 1, 1, "read-only", 1, FactState.KNOWN),
    ]
    assert control.provenance is not None
    assert control.provenance.source.endswith("uart.rdl")
    assert (control.provenance.line, control.provenance.column) == (14, 7)

    assert (
        data0.address_block,
        data0.address_offset,
        data0.absolute_address,
        data0.size_bits,
        data0.access,
    ) == ("uart_regs", 0x110, 0x110, 16, "read-write")
    assert data0.attributes["local_address_offset"] == 0x10
    assert data0.attributes["register_files"] == ["channel"]
    assert data0.attributes["array_indices"] == [0]
    assert data1.address_offset == 0x114
    assert data1.absolute_address == 0x114
    assert data1.attributes["local_address_offset"] == 0x14
    assert data1.attributes["array_indices"] == [1]
    assert data0.fields[0].reset_value == 0x5A
    assert view.attributes["backend_version"].startswith("1.32.")
    assert view.attributes["register_definitions"] == 2


def test_nested_systemrdl_and_ipxact_register_hierarchies_reconcile(
    tmp_path: Path,
) -> None:
    rdl_path = tmp_path / "nested.rdl"
    rdl_path.write_text(
        """
addrmap csr {
    regfile {
        reg { regwidth = 32; field { sw = rw; hw = r; } VALUE[0:0]; } CTRL @ 0x0;
    } a @ 0x100;
    regfile {
        reg { regwidth = 32; field { sw = rw; hw = r; } VALUE[0:0]; } CTRL @ 0x0;
    } b @ 0x200;
    regfile {
        reg { regwidth = 16; field { sw = rw; hw = r; } VALUE[7:0]; }
            DATA[2] @ 0x10 += 0x4;
    } channel @ 0x300;
};
""".lstrip(),
        encoding="utf-8",
    )
    ipxact_path = tmp_path / "nested.xml"
    ipxact_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ipxact:component xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2022">
  <ipxact:vendor>example.org</ipxact:vendor>
  <ipxact:library>registers</ipxact:library>
  <ipxact:name>uart0</ipxact:name>
  <ipxact:version>1.0</ipxact:version>
  <ipxact:memoryMaps>
    <ipxact:memoryMap>
      <ipxact:name>map0</ipxact:name>
      <ipxact:addressBlock>
        <ipxact:name>csr</ipxact:name>
        <ipxact:baseAddress>0</ipxact:baseAddress>
        <ipxact:range>1024</ipxact:range>
        <ipxact:width>32</ipxact:width>
        <ipxact:registerFile>
          <ipxact:name>a</ipxact:name>
          <ipxact:addressOffset>256</ipxact:addressOffset>
          <ipxact:register>
            <ipxact:name>CTRL</ipxact:name>
            <ipxact:addressOffset>0</ipxact:addressOffset>
            <ipxact:size>32</ipxact:size>
            <ipxact:access>read-write</ipxact:access>
            <ipxact:field>
              <ipxact:name>VALUE</ipxact:name>
              <ipxact:bitOffset>0</ipxact:bitOffset>
              <ipxact:bitWidth>1</ipxact:bitWidth>
              <ipxact:access>read-write</ipxact:access>
            </ipxact:field>
          </ipxact:register>
        </ipxact:registerFile>
        <ipxact:registerFile>
          <ipxact:name>b</ipxact:name>
          <ipxact:addressOffset>512</ipxact:addressOffset>
          <ipxact:register>
            <ipxact:name>CTRL</ipxact:name>
            <ipxact:addressOffset>0</ipxact:addressOffset>
            <ipxact:size>32</ipxact:size>
            <ipxact:access>read-write</ipxact:access>
            <ipxact:field>
              <ipxact:name>VALUE</ipxact:name>
              <ipxact:bitOffset>0</ipxact:bitOffset>
              <ipxact:bitWidth>1</ipxact:bitWidth>
              <ipxact:access>read-write</ipxact:access>
            </ipxact:field>
          </ipxact:register>
        </ipxact:registerFile>
        <ipxact:registerFile>
          <ipxact:name>channel</ipxact:name>
          <ipxact:addressOffset>768</ipxact:addressOffset>
          <ipxact:register>
            <ipxact:name>DATA</ipxact:name>
            <ipxact:array>
              <ipxact:dim>2</ipxact:dim>
              <ipxact:stride>4</ipxact:stride>
            </ipxact:array>
            <ipxact:addressOffset>16</ipxact:addressOffset>
            <ipxact:size>16</ipxact:size>
            <ipxact:access>read-write</ipxact:access>
            <ipxact:field>
              <ipxact:name>VALUE</ipxact:name>
              <ipxact:bitOffset>0</ipxact:bitOffset>
              <ipxact:bitWidth>8</ipxact:bitWidth>
              <ipxact:access>read-write</ipxact:access>
            </ipxact:field>
          </ipxact:register>
        </ipxact:registerFile>
      </ipxact:addressBlock>
      <ipxact:addressUnitBits>8</ipxact:addressUnitBits>
    </ipxact:memoryMap>
  </ipxact:memoryMaps>
</ipxact:component>
""",
        encoding="utf-8",
    )
    rdl = parse_systemrdl(
        rdl_path,
        view_id="systemrdl.nested",
        top="csr",
        component_name="uart0",
    )
    ipxact = parse_ipxact(ipxact_path, view_id="ipxact.nested")
    project = ProjectConfig(
        path=tmp_path / "opencollate.toml",
        root=tmp_path,
        name="nested-registers",
        sources=(
            SourceConfig(rdl.view, (rdl_path,)),
            SourceConfig(ipxact.view, (ipxact_path,)),
        ),
    )

    result = ComparisonEngine(project).run((rdl, ipxact))

    assert rdl.complete and ipxact.complete
    assert not rdl.diagnostics and not ipxact.diagnostics
    rdl_registers = {(item.native_name, item.address_offset) for item in rdl.registers}
    ipxact_registers = {(item.native_name, item.address_offset) for item in ipxact.registers}
    assert (
        rdl_registers
        == ipxact_registers
        == {
            ("CTRL", 0x100),
            ("CTRL", 0x200),
            ("DATA[0]", 0x310),
            ("DATA[1]", 0x314),
        }
    )
    assert {
        (item.address_block, tuple(item.attributes["register_files"])) for item in rdl.registers
    } == {
        ("csr", ("a",)),
        ("csr", ("b",)),
        ("csr", ("channel",)),
    }
    assert not {"OC6301", "OC6302", "OC6307", "OC6310"}.intersection(
        item.code for item in result.diagnostics
    )
    assert len(result.generated_contract.registers) == 4
    assert sorted(item.address_block for item in result.generated_contract.registers) == [
        "csr/a",
        "csr/b",
        "csr/channel",
        "csr/channel",
    ]

    frozen_result = ComparisonEngine(project).run(
        (rdl, ipxact),
        contract=result.generated_contract,
    )
    assert not {"OC6301", "OC6302", "OC6307", "OC6310"}.intersection(
        item.code for item in frozen_result.diagnostics
    )


def test_multiple_compilation_units_are_explicit_and_ordered() -> None:
    paths = (FIXTURES / "types.rdl", FIXTURES / "multifile_top.rdl")
    view = parse_systemrdl(paths, top="multi_top", component_name="soc")

    assert view.complete
    assert len(view.registers) == 1
    register = view.registers[0]
    assert (register.native_name, register.component, register.absolute_address) == (
        "STATUS",
        "soc",
        0x20,
    )
    assert register.provenance is not None
    assert register.provenance.source.endswith("multifile_top.rdl")

    reversed_view = parse_systemrdl(tuple(reversed(paths)), top="multi_top")
    assert not reversed_view.complete
    assert "OC1101" in _codes(reversed_view)
    assert not reversed_view.registers


def test_selected_top_is_honored_and_default_is_last_defined(tmp_path: Path) -> None:
    source = tmp_path / "tops.rdl"
    source.write_text(
        """
addrmap first_map {
    reg { field { sw = rw; } A[0:0]; } FIRST @ 0x0;
};
addrmap second_map {
    reg { field { sw = rw; } B[0:0]; } SECOND @ 0x4;
};
""".lstrip(),
        encoding="utf-8",
    )

    selected = parse_systemrdl(source, top="first_map")
    defaulted = parse_systemrdl(source)

    assert [item.native_name for item in selected.registers] == ["FIRST"]
    assert selected.attributes["selected_top"] == "first_map"
    assert [item.native_name for item in defaulted.registers] == ["SECOND"]
    assert defaulted.attributes["selected_top"] == "second_map"


def test_malformed_source_and_absent_top_fail_closed() -> None:
    malformed = parse_systemrdl(FIXTURES / "malformed.rdl", top="broken")
    missing_top = parse_systemrdl(FIXTURES / "uart.rdl", top="not_a_top")

    for view in (malformed, missing_top):
        assert not view.complete
        assert view.tainted_scopes == frozenset({"*"})
        assert not view.registers
        assert any(
            item.code == "OC1101" and item.severity == Severity.FATAL for item in view.diagnostics
        )
    parse_error = next(item for item in malformed.diagnostics if item.code == "OC1101")
    assert parse_error.provenance is not None
    assert parse_error.provenance.source.endswith("malformed.rdl")
    assert (parse_error.provenance.line, parse_error.provenance.column) == (4, 5)


@pytest.mark.parametrize(
    ("source_text", "construct"),
    [
        (
            "<% system('this-must-not-run'); %>\naddrmap top {};\n",
            "perl",
        ),
        (
            '`include "secret.rdl"\naddrmap top {};\n',
            "include",
        ),
    ],
)
def test_executable_preprocessing_is_rejected_before_backend_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_text: str,
    construct: str,
) -> None:
    source = tmp_path / "unsafe.rdl"
    source.write_text(source_text, encoding="utf-8")

    def backend_must_not_load() -> None:
        raise AssertionError("backend was loaded before SystemRDL preflight completed")

    monkeypatch.setattr(systemrdl_parser, "_load_systemrdl", backend_must_not_load)
    view = parse_systemrdl(source, top="top")

    assert not view.complete
    assert view.attributes["preflight_rejected"] is True
    finding = next(item for item in view.diagnostics if item.code == "OC1101")
    assert finding.severity == Severity.FATAL
    assert finding.metadata["construct"] == construct
    assert "not run" in finding.message or "explicitly" in finding.message


def test_comment_text_does_not_trigger_preprocessor_rejection(tmp_path: Path) -> None:
    source = tmp_path / "comments.rdl"
    source.write_text(
        """
// <% ignored %>
/* `include "ignored.rdl" */
addrmap comments {
    reg { field { sw = rw; } VALUE[0:0]; } CTRL @ 0x0;
};
""".lstrip(),
        encoding="utf-8",
    )

    view = parse_systemrdl(source, top="comments")

    assert view.complete
    assert [item.native_name for item in view.registers] == ["CTRL"]


def test_backend_compiles_the_preflighted_snapshot_not_a_reopened_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mutable.rdl"
    payload = tmp_path / "payload.rdl"
    source.write_text(
        "addrmap safe_top { reg { field { sw = rw; } V[0:0]; } SAFE @ 0; };\n",
        encoding="utf-8",
    )
    payload.write_text(
        "addrmap injected_top { reg { field { sw = rw; } V[0:0]; } INJECTED @ 0; };\n",
        encoding="utf-8",
    )
    api = systemrdl_parser._load_systemrdl()
    assert api is not None
    original_compile = api.compiler.compile_file
    compiled_paths: list[Path] = []

    def swap_then_compile(compiler: object, path: str, **kwargs: object) -> object:
        source.write_text('`include "payload.rdl"\n', encoding="utf-8")
        compiled_paths.append(Path(path).resolve())
        return original_compile(compiler, path, **kwargs)

    monkeypatch.setattr(api.compiler, "compile_file", swap_then_compile)

    view = parse_systemrdl(source, top="safe_top")

    assert view.complete
    assert [item.native_name for item in view.registers] == ["SAFE"]
    assert all(path != source.resolve() for path in compiled_paths)
    assert compiled_paths and not any(path.exists() for path in compiled_paths)
    assert view.registers[0].provenance is not None
    assert view.registers[0].provenance.source == str(source)


def test_source_limit_uses_one_bounded_stream_read_despite_stale_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "growing.rdl"
    source.write_text(
        "addrmap bounded {}\n// " + "x" * 256,
        encoding="utf-8",
    )
    limit = 64
    read_sizes: list[int] = []
    original_open = Path.open
    original_read_bytes = Path.read_bytes
    original_stat = Path.stat

    class GuardedReader:
        def __init__(self, stream: object) -> None:
            self.stream = stream

        def __enter__(self) -> GuardedReader:
            self.stream.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *args: object) -> object:
            return self.stream.__exit__(*args)  # type: ignore[attr-defined]

        def read(self, size: int = -1) -> bytes:
            if size != limit + 1:
                raise RuntimeError(f"unbounded SystemRDL read requested with size {size}")
            read_sizes.append(size)
            return self.stream.read(size)  # type: ignore[attr-defined,no-any-return]

    class StaleStat:
        st_size = 1

    def guarded_open(path: Path, *args: object, **kwargs: object) -> object:
        stream = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
        mode = args[0] if args else kwargs.get("mode", "r")
        return GuardedReader(stream) if path == source and mode == "rb" else stream

    def stale_stat(path: Path, *args: object, **kwargs: object) -> object:
        if path == source:
            return StaleStat()
        return original_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    def forbid_unbounded_read(path: Path) -> bytes:
        if path == source:
            raise RuntimeError("Path.read_bytes must not be used for SystemRDL input")
        return original_read_bytes(path)

    monkeypatch.setattr(systemrdl_parser, "_MAX_SOURCE_BYTES", limit)
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "stat", stale_stat)
    monkeypatch.setattr(Path, "read_bytes", forbid_unbounded_read)
    monkeypatch.setattr(
        systemrdl_parser,
        "_load_systemrdl",
        lambda: (_ for _ in ()).throw(RuntimeError("backend must not load")),
    )

    view = parse_systemrdl(source, top="bounded")

    assert not view.complete
    assert read_sizes == [limit + 1]
    finding = next(item for item in view.diagnostics if item.code == "OC1101")
    assert finding.metadata == {
        "limit": limit,
        "actual": limit + 1,
        "truncated": True,
    }


def test_missing_backend_produces_an_unavailable_tainted_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(systemrdl_parser, "_load_systemrdl", lambda: None)

    view = parse_systemrdl(FIXTURES / "uart.rdl", top="uart_regs")

    assert not view.complete
    assert view.tainted_scopes == frozenset({"*"})
    assert not view.registers
    finding = next(item for item in view.diagnostics if item.code == "OC1102")
    assert finding.severity == Severity.FATAL
    assert "systemrdl-compiler" in finding.message


def test_behavior_and_legal_overlaps_are_explicit_and_never_false_fail_layout() -> None:
    view = parse_systemrdl(FIXTURES / "unsupported.rdl", top="unsupported_top")

    assert view.complete
    assert {item.code for item in view.diagnostics} == {"OC1102"}
    assert view.tainted_scopes == frozenset({"CTRL", "ALIASED_LAYOUT"})
    control, overlapping = view.registers
    assert control.fields[0].status == FactState.KNOWN
    assert control.fields[0].attributes["behavior"] == {
        "onwrite": "woclr",
        "woclr": True,
    }
    assert all(field.status == FactState.UNSUPPORTED for field in overlapping.fields)
    assert all(field.attributes["overlapping"] for field in overlapping.fields)


def test_source_array_register_and_field_caps_are_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        ("_MAX_SOURCE_BYTES", 1, "byte limit"),
        ("_MAX_ARRAY_ELEMENTS", 1, "array"),
        ("_MAX_REGISTERS", 2, "concrete-register"),
        ("_MAX_FIELDS_PER_REGISTER", 1, "field limit"),
    )
    for constant, value, expected in cases:
        with monkeypatch.context() as patch:
            patch.setattr(systemrdl_parser, constant, value)
            view = parse_systemrdl(FIXTURES / "uart.rdl", top="uart_regs")
        assert not view.complete
        assert not view.registers
        finding = next(item for item in view.diagnostics if item.code == "OC1101")
        assert finding.severity == Severity.FATAL
        assert expected in finding.message


def test_preflight_file_count_aggregate_size_missing_and_utf8_errors_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_utf8 = tmp_path / "invalid_utf8.rdl"
    invalid_utf8.write_bytes(b"addrmap top {};\n\xff")
    missing = tmp_path / "missing.rdl"

    cases = (
        ("_MAX_SOURCE_FILES", 0, (FIXTURES / "uart.rdl",), "source-file"),
        ("_MAX_TOTAL_SOURCE_BYTES", 1, (FIXTURES / "uart.rdl",), "aggregate"),
        (None, None, (missing,), "Cannot read"),
        (None, None, (invalid_utf8,), "not valid UTF-8"),
    )
    for constant, value, paths, expected in cases:
        with monkeypatch.context() as patch:
            if constant is not None:
                patch.setattr(systemrdl_parser, constant, value)
            view = parse_systemrdl(paths, top="uart_regs")
        assert not view.complete
        assert not view.registers
        finding = next(item for item in view.diagnostics if item.severity == Severity.FATAL)
        assert finding.code in {"OC1002", "OC1101"}
        assert expected in finding.message


@pytest.mark.parametrize(
    ("constant", "value", "message"),
    [
        ("_MAX_NODE_DEFINITIONS", 1, "node-definition"),
        ("_MAX_HIERARCHY_DEPTH", 1, "hierarchy-depth"),
        ("_MAX_REGISTER_DEFINITIONS", 1, "register-definition"),
        ("_MAX_FIELDS", 1, "concrete-field"),
    ],
)
def test_elaborated_model_caps_are_fatal(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    value: int,
    message: str,
) -> None:
    monkeypatch.setattr(systemrdl_parser, constant, value)

    view = parse_systemrdl(FIXTURES / "uart.rdl", top="uart_regs")

    assert not view.complete
    assert not view.registers
    finding = next(item for item in view.diagnostics if item.severity == Severity.FATAL)
    assert finding.code == "OC1101"
    assert message in finding.message


def test_signals_and_user_defined_properties_are_explicit_semantic_frontiers() -> None:
    view = parse_systemrdl(
        FIXTURES / "semantic_frontiers.rdl",
        top="semantic_frontiers",
    )

    assert view.complete
    assert [item.native_name for item in view.registers] == ["CTRL"]
    assert view.tainted_scopes == frozenset({"reset_n", "semantic_frontiers"})
    messages = "\n".join(item.message for item in view.diagnostics)
    assert "signal declaration" in messages
    assert "vendor_tag" in messages


def test_unsupported_diagnostic_volume_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(systemrdl_parser, "_MAX_SEMANTIC_DIAGNOSTICS", 1)

    view = parse_systemrdl(FIXTURES / "unsupported.rdl", top="unsupported_top")

    assert not view.complete
    assert "*" in view.tainted_scopes
    assert len([item for item in view.diagnostics if item.code == "OC1102"]) == 1
    summary = next(item for item in view.diagnostics if item.code == "OC1104")
    assert summary.metadata["suppressed"] == 2


def test_empty_top_is_observable_and_parser_adapter_matches_function(tmp_path: Path) -> None:
    source = tmp_path / "empty.rdl"
    source.write_text(
        "addrmap empty { external mem { mementries=1; memwidth=8; } m @ 0; };\n",
        encoding="utf-8",
    )

    function_view = parse_systemrdl(source, top="empty")
    adapter_view = SystemRdlParser().parse((source,), view_id="systemrdl.adapter", top="empty")

    assert function_view.complete
    assert not function_view.registers
    assert "OC1105" in _codes(function_view)
    assert adapter_view.view == ViewId("systemrdl", "adapter")
    assert adapter_view.registers == function_view.registers


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"top": ""}, "top must be"),
        ({"component_name": ""}, "component_name must be"),
        ({"include_dirs": [FIXTURES]}, "include_dirs are unsupported"),
        ({"defines": []}, "defines must be"),
        ({"defines": {"": 1}}, "define names must be"),
    ],
)
def test_public_option_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_systemrdl(FIXTURES / "uart.rdl", **kwargs)  # type: ignore[arg-type]

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from opencollate.config import (
    AliasRule,
    ConfigError,
    ParticipationRule,
    SourceConfig,
    Waiver,
    load_config,
    load_contract,
)
from opencollate.model import (
    BusShape,
    ComponentKind,
    ContractComponent,
    ContractPort,
    DesignContract,
    Direction,
    PortRole,
    ViewId,
)


def write_manifest(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "opencollate.toml"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def minimal_source(extra: str = "") -> str:
    return f"""
[sources.rtl.default]
files = ["rtl.sv"]
{extra}
"""


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: AliasRule("net", "irq", "rtl", "irq"), "component.*port"),
        (lambda: AliasRule("component", "", "rtl", "uart"), "must not be empty"),
        (lambda: AliasRule("port", "irq", "rtl", "IRQ"), "requires.*component"),
        (lambda: ParticipationRule("", ("rtl",)), "pattern must not be empty"),
        (lambda: ParticipationRule("uart", ()), "at least one view"),
        (lambda: Waiver("BAD", "reason"), "invalid waiver code"),
        (lambda: Waiver("OC4101", ""), "nonempty reason"),
        (lambda: Waiver("OC_DOES_NOT_EXIST", "reason"), "unknown waiver code"),
    ],
)
def test_direct_rule_dataclasses_reject_invalid_values(
    factory: Callable[[], object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        factory()


def test_optional_globs_and_missing_paths_are_explicit(tmp_path: Path) -> None:
    missing_glob = SourceConfig(ViewId("rtl"), (tmp_path / "rtl" / "*.sv",))
    assert missing_glob.expand_files(require_matches=False) == ()
    with pytest.raises(ConfigError) as glob_error:
        missing_glob.expand_files()
    assert glob_error.value.code == "OC1003"

    missing_file = SourceConfig(ViewId("lef"), (tmp_path / "macro.lef",))
    assert missing_file.expand_files(require_matches=False) == (tmp_path / "macro.lef",)
    with pytest.raises(ConfigError) as file_error:
        missing_file.expand_files()
    assert file_error.value.code == "OC1002"


def test_glob_expansion_is_sorted_and_deduplicated(tmp_path: Path) -> None:
    (tmp_path / "b.sv").write_text("module b; endmodule\n", encoding="utf-8")
    (tmp_path / "A.sv").write_text("module A; endmodule\n", encoding="utf-8")
    source = SourceConfig(
        ViewId("rtl"),
        (tmp_path / "*.sv", tmp_path / "b.sv"),
    )
    assert [path.name for path in source.expand_files()] == ["A.sv", "b.sv"]

    ordered = SourceConfig(
        ViewId("systemrdl"),
        (tmp_path / "b.sv", tmp_path / "A.sv", tmp_path / "b.sv"),
    )
    assert [path.name for path in ordered.expand_files()] == ["b.sv", "A.sv"]


def test_source_shorthand_mapping_defines_and_project_root(tmp_path: Path) -> None:
    source_root = tmp_path / "inputs"
    source_root.mkdir()
    (source_root / "top.sv").write_text("module top; endmodule\n", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        """
[project]
name = "short"
root = "inputs"

[sources.rtl]
path = "top.sv"
defines = { WIDTH = 8, FEATURE = true }
custom_option = "kept"
""",
    )
    project = load_config(manifest)
    source = project.source("rtl.default")
    assert project.root == source_root.resolve()
    assert source.expand_files() == ((source_root / "top.sv").resolve(),)
    assert source.defines == {"WIDTH": "8", "FEATURE": "True"}
    assert source.options == {"custom_option": "kept"}


def test_alias_shorthand_loads_and_sorts(tmp_path: Path) -> None:
    manifest = write_manifest(
        tmp_path,
        minimal_source(
            """
[[alias]]
kind = "port"
component = "uart"
canonical = "irq"
view = "liberty"
native = "IRQ"

[[alias]]
kind = "component"
canonical = "uart"
view = "liberty"
native = "UART"
"""
        ),
    )
    aliases = load_config(manifest).aliases
    assert [(rule.kind, rule.canonical) for rule in aliases] == [
        ("component", "uart"),
        ("port", "irq"),
    ]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("[aliases]\ncomponents = {}", "array of tables"),
        ("alias = {}", "array of tables"),
        (
            """
[[aliases.components]]
canonical = "uart"
view = "rtl"
native = "u0"
[[aliases.components]]
canonical = "other"
view = "rtl"
native = "u0"
""",
            "alias collision",
        ),
        (
            """
[[aliases.components]]
canonical = "uart"
view = "rtl"
native = "u0"
[[aliases.components]]
canonical = "uart"
view = "rtl"
native = "u1"
""",
            "alias collision",
        ),
        (
            """
[[aliases.components]]
canonical = "uart"
names = ["uart"]
""",
            "must be a TOML table",
        ),
    ],
)
def test_invalid_alias_forms_and_collisions(tmp_path: Path, extra: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_manifest(tmp_path, extra + "\n" + minimal_source()))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("participation = {}", "array of tables"),
        (
            """
[[participation]]
component = "uart"
views = 4
""",
            "string or an array of strings",
        ),
        (
            """
[[participation]]
component = "uart"
views = ["rtl"]
roles = ["not_a_role"]
""",
            "unknown participation role",
        ),
    ],
)
def test_invalid_participation_forms(tmp_path: Path, extra: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_manifest(tmp_path, extra + "\n" + minimal_source()))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("waivers = {}", "array of tables"),
        (
            """
[[waivers]]
code = "OC4101"
reason = "temporary"
views = 1
""",
            "string or an array of strings",
        ),
        (
            """
[[waivers]]
code = "OC4101"
reason = "temporary"
expires = "next week"
""",
            "ISO date",
        ),
        (
            """
[[waivers]]
code = "OC_NOT_REAL"
reason = "temporary"
""",
            "unknown waiver code",
        ),
    ],
)
def test_invalid_waiver_forms(tmp_path: Path, extra: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_manifest(tmp_path, extra + "\n" + minimal_source()))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("this is not = valid = toml", "invalid TOML"),
        ("schema_version = 'one'\n" + minimal_source(), "schema_version must be an integer"),
        ("project = []\n" + minimal_source(), "project must be a TOML table"),
        ("[project]\nname = ''\n" + minimal_source(), "project.name must not be empty"),
        ("[project]\nname = 'x'\n", "sources must contain at least one"),
        (
            minimal_source("[contract]\nbaseline = 'liberty.tt'"),
            "unknown source view",
        ),
        (
            minimal_source("[contract]\nbaseline = ''"),
            "contract.baseline",
        ),
        (
            minimal_source("[policy]\nrtl_power_pins = 'sometimes'"),
            "optional, required, or ignore",
        ),
        (
            minimal_source("[policy]\nmax_boolean_inputs = 'many'"),
            "must be an integer",
        ),
        (
            minimal_source("[policy]\ndeny_warnings = 'false'"),
            "must be a boolean",
        ),
        (
            minimal_source("[policy.severity_overrides]\nOC1102 = 'catastrophic'"),
            "invalid severity",
        ),
    ],
)
def test_invalid_manifest_boundaries_are_config_errors(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_manifest(tmp_path, text))


@pytest.mark.parametrize(
    ("source_text", "message"),
    [
        ("[sources]\nrtl = 'file.sv'", "must be a TOML table"),
        ("[sources.rtl.default]\nprofile = 'x'", "requires files or path"),
        ("[sources.rtl.default]\nfiles = []", "requires files or path"),
        ("[sources.rtl.default]\nfiles = ['x.sv']\ninclude_dirs = 3", "array of strings"),
        ("[sources.rtl.default]\nfiles = ['x.sv']\ndefines = ['']", "empty define"),
        (
            "[sources.systemrdl.default]\nfiles = ['x.rdl']\ndefines = { \"\" = 1 }",
            "empty define",
        ),
        (
            "[sources.rtl.default]\nfiles = ['x.sv']\ndefines = { BAD = [1] }",
            "must be a scalar",
        ),
    ],
)
def test_invalid_source_tables_are_actionable(
    tmp_path: Path, source_text: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_manifest(tmp_path, source_text))


def test_contract_loader_failures_are_normalized(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ConfigError) as missing_error:
        load_contract(missing)
    assert missing_error.value.code == "OC1002"

    cases: tuple[tuple[str, str], ...] = (
        ("{", "invalid contract JSON"),
        ("[]", "contract root must be a JSON object"),
        ('{"schema_version": 2, "components": []}', "schema_version"),
        ('{"schema_version": 1, "components": [42]}', r"components\[0\]"),
        (
            '{"schema_version": 1, "components": [{"canonical_name": "x", "ports": [4]}]}',
            r"ports\[0\]",
        ),
    )
    for index, (payload, message) in enumerate(cases):
        path = tmp_path / f"bad-{index}.json"
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(ConfigError, match=message):
            load_contract(path)


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ('{"schema_version": ' + "9" * 5_000 + "}", "invalid contract JSON"),
        (
            "[" * 2_000 + "]" * 2_000,
            "invalid contract JSON|contract root must be a JSON object",
        ),
    ),
)
def test_contract_loader_decoder_safety_failures_are_normalized(
    tmp_path: Path, payload: str, message: str
) -> None:
    path = tmp_path / "decoder-limit.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_contract(path)


def test_full_contract_round_trip_preserves_shape_and_roles(tmp_path: Path) -> None:
    contract = DesignContract(
        (
            ContractComponent(
                "uart",
                ComponentKind.MODULE,
                {"rtl.default": "uart", "liberty.tt": "UART"},
                ("rtl.default", "liberty.tt"),
                (
                    ContractPort(
                        "data",
                        {"rtl.default": "data_i", "liberty.tt": "DATA"},
                        Direction.INPUT,
                        PortRole.SIGNAL,
                        BusShape(width=8, left=7, right=0),
                    ),
                ),
            ),
        ),
        generated_by="test",
    )
    path = tmp_path / "contract.oc.json"
    path.write_text(
        json.dumps(contract.to_dict(), sort_keys=True),
        encoding="utf-8",
    )
    loaded = load_contract(path)
    assert loaded.to_dict() == contract.to_dict()
    assert loaded.components[0].ports[0].shape.ordered_indices == tuple(range(7, -1, -1))


def test_valid_waiver_date_is_kept_as_date(tmp_path: Path) -> None:
    manifest = write_manifest(
        tmp_path,
        minimal_source(
            """
[[waivers]]
code = "OC41*"
reason = "temporary bus migration"
expires = 2099-12-31
"""
        ),
    )
    waiver = load_config(manifest).waivers[0]
    assert waiver.expires == date(2099, 12, 31)

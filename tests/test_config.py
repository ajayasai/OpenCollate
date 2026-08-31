from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollate.config import ConfigError, load_config, load_contract
from opencollate.diagnostics import Severity
from opencollate.model import ViewId


def write_project(tmp_path: Path) -> Path:
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    (rtl / "include").mkdir()
    (rtl / "uart.sv").write_text("module uart(input clk); endmodule\n", encoding="utf-8")
    (tmp_path / "cells.lib").write_text("library(x) {}\n", encoding="utf-8")
    config = tmp_path / "opencollate.toml"
    config.write_text(
        """
schema_version = 1

[project]
name = "demo"

[sources.rtl.default]
files = ["rtl/*.sv"]
include_dirs = ["rtl/include"]
defines = ["WIDTH=8", "SYNTHESIS"]
top = "uart"

[sources.liberty.tt]
path = "cells.lib"

[contract]
baseline = "rtl.default"

[policy]
max_boolean_inputs = 10
rtl_power_pins = "optional"
deny_warnings = true

[policy.severity_overrides]
OC1102 = "error"

[[aliases.components]]
canonical = "uart"
names = { "rtl.default" = "uart", "liberty.tt" = "UART" }

[[aliases.ports]]
component = "uart"
canonical = "irq"
names = { "rtl.default" = "irq_o", "liberty.tt" = "IRQ" }

[[participation]]
component = "uart"
views = ["rtl.default", "liberty.tt"]

[[waivers]]
code = "OC4202"
object = "component:uart/port:VNW"
reason = "Implicit substrate connection"
expires = 2099-01-01
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config


def test_load_complete_config(tmp_path: Path) -> None:
    project = load_config(write_project(tmp_path))
    assert project.name == "demo"
    assert project.views == (ViewId("liberty", "tt"), ViewId("rtl"))
    assert project.contract.baseline == ViewId("rtl")
    assert project.policy.max_boolean_inputs == 10
    assert project.policy.deny_warnings
    assert project.policy.severity_overrides == {"OC1102": Severity.ERROR}
    rtl = project.source("rtl.default")
    assert [path.name for path in rtl.expand_files()] == ["uart.sv"]
    assert rtl.defines == {"WIDTH": "8", "SYNTHESIS": None}
    assert rtl.options == {"top": "uart"}
    assert len(project.aliases) == 4
    assert project.participation[0].component == "uart"
    assert project.waivers[0].reason == "Implicit substrate connection"


def test_missing_configuration_has_source_code(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as captured:
        load_config(tmp_path / "missing.toml")
    assert captured.value.code == "OC1002"


def test_empty_glob_is_distinct_from_missing_file(tmp_path: Path) -> None:
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text('[sources.rtl.default]\nfiles = ["rtl/*.sv"]\n', encoding="utf-8")
    source = load_config(manifest).sources[0]
    with pytest.raises(ConfigError) as captured:
        source.expand_files()
    assert captured.value.code == "OC1003"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("schema_version = 2\n[sources]\n", "schema_version"),
        (
            '[sources.rtl.default]\nfiles=["x.sv"]\n[policy]\nmax_boolean_inputs=0\n',
            "between 1 and 24",
        ),
        (
            '[sources.rtl.default]\nfiles=["x.sv"]\n[[alias]]\nkind="port"\ncanonical="irq"\nview="rtl"\nnative="IRQ"\n',
            "requires its canonical component",
        ),
        ("[sources.rtl.default]\nfiles = 42\n", "array of strings"),
    ],
)
def test_invalid_configuration_is_actionable(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_contract_loader_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "contract.oc.json"
    payload = {
        "schema_version": 1,
        "generated_by": "test",
        "components": [
            {
                "canonical_name": "uart",
                "kind": "module",
                "names": {"rtl.default": "uart"},
                "required_views": ["rtl.default"],
                "ports": [],
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_contract(path).to_dict() == payload

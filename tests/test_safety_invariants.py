from __future__ import annotations

from pathlib import Path

import pytest

from opencollate.config import (
    ConfigError,
    PolicySettings,
    ProjectConfig,
    SourceConfig,
    Waiver,
    load_config,
)
from opencollate.diagnostics import Diagnostic, Severity
from opencollate.engine import ComparisonEngine
from opencollate.model import ViewId, ViewObservation
from opencollate.parsers.verilog import parse_verilog

MALFORMED_RTL = Path(__file__).parent / "fixtures" / "rtl" / "malformed.sv"


def _project(*, policy: PolicySettings, waivers: tuple[Waiver, ...]) -> ProjectConfig:
    view = ViewId("rtl")
    return ProjectConfig(
        path=Path("opencollate.toml"),
        root=Path("."),
        name="fatal-invariant",
        sources=(SourceConfig(view, (Path("design.sv"),)),),
        policy=policy,
        waivers=waivers,
    )


def test_fatal_diagnostic_cannot_be_downgraded_or_wildcard_waived() -> None:
    fatal = Diagnostic.from_rule("OC1101", "RTL could not be parsed.")
    observation = ViewObservation(
        ViewId("rtl"),
        (),
        diagnostics=(fatal,),
        complete=False,
        tainted_scopes=frozenset({"*"}),
    )
    config = _project(
        policy=PolicySettings(severity_overrides={"OC1101": Severity.INFO}),
        waivers=(Waiver("OC11*", "Broad parser waiver must not hide fatal failures."),),
    )

    result = ComparisonEngine(config).run((observation,))
    finding = next(item for item in result.diagnostics if item.code == "OC1101")

    assert finding.severity == Severity.FATAL
    assert not finding.waived
    assert result.exit_code == 2
    assert result.to_dict()["status"] == "fail"


def test_exact_fatal_waiver_is_rejected() -> None:
    with pytest.raises(ConfigError, match="fatal rule OC1101 cannot be waived"):
        Waiver("OC1101", "Unsafe waiver")


def test_config_rejects_fatal_severity_downgrade(tmp_path: Path) -> None:
    manifest = tmp_path / "opencollate.toml"
    manifest.write_text(
        """
[sources.rtl.default]
files = ["design.sv"]

[policy.severity_overrides]
OC1101 = "info"
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="fatal rule OC1101 cannot be downgraded"):
        load_config(manifest)


def test_real_systemverilog_parse_failure_cannot_be_wildcard_waived() -> None:
    observation = parse_verilog(MALFORMED_RTL, view_id="rtl.default")
    config = ProjectConfig(
        path=Path("opencollate.toml"),
        root=Path("."),
        name="malformed-rtl",
        sources=(SourceConfig(ViewId("rtl"), (MALFORMED_RTL,)),),
        waivers=(Waiver("OC11*", "Must not hide parser integrity failures."),),
    )

    result = ComparisonEngine(config).run((observation,))
    failures = [item for item in result.diagnostics if item.code == "OC1101"]

    assert failures
    assert all(item.severity == Severity.FATAL for item in failures)
    assert not any(item.waived for item in failures)
    assert result.exit_code == 2

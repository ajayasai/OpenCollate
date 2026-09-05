from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from opencollate.cli import _schema_text, main
from opencollate.contract_review import diff_contracts
from opencollate.contracts import ContractViewSnapshot
from opencollate.engine import write_contract
from opencollate.model import DesignContract


def contract(**kwargs: object) -> DesignContract:
    return DesignContract((), views=(ContractViewSnapshot("rtl.default", **kwargs),))


def test_unchanged_and_schema() -> None:
    value = contract(clocks=({"name": "clk", "period": 10},))
    report = diff_contracts(value, value)
    assert report["exit_code"] == 0
    assert report["changes"] == []
    Draft202012Validator(json.loads(_schema_text("contract-diff"))).validate(report)


@pytest.mark.parametrize(
    ("family", "row"),
    [
        ("components", {"name": "core"}),
        ("pin_mappings", {"package_ball": "A1"}),
        ("objects", {"kind": "port", "qualified_name": "top/A", "relation": "definition"}),
        ("clocks", {"name": "clk", "period": 10}),
        ("interfaces", {"name": "bus", "component": "core"}),
        ("registers", {"name": "CTRL", "component": "core"}),
        ("connectivity_endpoints", {"key": "top/A"}),
        (
            "connectivity_edges",
            {"source": {"key": "top/A"}, "sink": {"key": "top/B"}, "kind": "assign"},
        ),
        ("connectivity_requirements", {"id": "route"}),
    ],
)
def test_all_snapshot_families_detect_drift(family: str, row: dict) -> None:
    first = contract(**{family: (row,)})
    second = contract(**{family: ({**row, "status": "tainted"},)})
    report = diff_contracts(first, second)
    assert report["summary"]["changed"] == 1
    assert report["changes"][0]["family"] == family
    assert report["changes"][0]["after"][0]["status"] == "tainted"
    Draft202012Validator(json.loads(_schema_text("contract-diff"))).validate(report)


def test_duplicates_are_not_collapsed_or_arbitrarily_paired() -> None:
    row = {"name": "clk", "period": 10}
    original = contract(clocks=(row, row, {**row, "period": 20}))
    current = contract(clocks=(row, {**row, "period": 30}))
    report = diff_contracts(original, current)
    assert len(report["changes"][0]["before"]) == 2
    assert len(report["changes"][0]["after"]) == 1
    reverse = contract(clocks=tuple(reversed(original.views[0].clocks)))
    assert diff_contracts(original, current) == diff_contracts(reverse, current)


def test_view_additions_removals_and_metadata() -> None:
    original = contract()
    current = replace(original, views=(*original.views, ContractViewSnapshot("sdc.other")))
    assert diff_contracts(original, current)["summary"]["added"] == 1
    assert diff_contracts(current, original)["summary"]["removed"] == 1
    changed = contract(attributes={"power_domain": "new"}, extensions={"company": {"v": 2}})
    assert diff_contracts(original, changed)["summary"]["changed"] == 2


@pytest.mark.parametrize(
    "value",
    [
        DesignContract((), schema_version=1),
        DesignContract(()),
        contract(complete=False),
        contract(tainted_scopes=("*",)),
    ],
)
def test_partial_snapshot_coverage_cannot_pass(value: DesignContract) -> None:
    report = diff_contracts(value, value)
    assert report["exit_code"] == 2
    assert report["snapshot_coverage"] == "incomplete"


def test_stale_in_memory_snapshot_digest_is_rejected() -> None:
    first = contract(clocks=({"name": "clk", "period": 10},))
    first.views[0].clocks[0]["period"] = 20
    with pytest.raises(ValueError, match="does not match"):
        diff_contracts(first, first)


def test_cli_full_contract_diff_and_alias(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = write_contract(contract(), tmp_path / "a.json")
    b = write_contract(contract(clocks=({"name": "clk"},)), tmp_path / "b.json")
    assert main(["contract", "diff", str(a), str(b)]) == 1
    assert json.loads(capsys.readouterr().out)["changes"][0]["family"] == "clocks"
    assert main(["contract", "diff", str(a), str(b), "-o", str(a)]) == 2


@pytest.mark.parametrize("digest", [None, "", "not-a-digest"])
def test_serialized_snapshots_must_not_omit_integrity_check(digest: str | None) -> None:
    payload = contract().views[0].to_dict()
    payload["content_sha256"] = digest
    with pytest.raises(ValueError, match="require.*content_sha256"):
        ContractViewSnapshot.from_dict(payload)
    del payload["content_sha256"]
    with pytest.raises(ValueError, match="require.*content_sha256"):
        ContractViewSnapshot.from_dict(payload)


def test_contract_read_limits_and_duplicate_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opencollate.config as config

    path = tmp_path / "contract.json"
    path.write_text('{"schema_version":1,"schema_version":2}')
    with pytest.raises(config.ConfigError, match="duplicate"):
        config.load_contract(path)
    path.write_text('{"nested":' + "[" * 129 + "0" + "]" * 129 + "}")
    with pytest.raises(config.ConfigError, match="nesting"):
        config.load_contract(path)
    path.write_text(" " * 20)
    monkeypatch.setattr(config, "MAX_CONTRACT_JSON_BYTES", 10)
    with pytest.raises(config.ConfigError, match="byte limit"):
        config.load_contract(path)


def test_contract_nesting_ignores_json_string_brackets(tmp_path: Path) -> None:
    from opencollate.config import load_contract

    value = contract(attributes={"text": '["\\]' * 200})
    path = write_contract(value, tmp_path / "contract.json")
    assert load_contract(path) == value

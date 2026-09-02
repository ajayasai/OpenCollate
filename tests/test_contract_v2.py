from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from opencollate.cli import main
from opencollate.config import ProjectConfig, load_contract
from opencollate.contracts import (
    CONTRACT_SCHEMA_VERSION,
    ContractViewSnapshot,
    upgrade_contract,
)
from opencollate.engine import ComparisonEngine
from opencollate.model import (
    BusShape,
    ClockObservation,
    ComponentKind,
    ComponentObservation,
    ConnectivityEdge,
    ConnectivityEndpoint,
    ConnectivityExpectation,
    ConnectivityRequirement,
    ConnectivityTransform,
    DesignContract,
    DesignObjectObservation,
    Direction,
    InterfaceObservation,
    PinMappingObservation,
    PortObservation,
    PortRole,
    RegisterFieldObservation,
    RegisterObservation,
    ViewId,
    ViewObservation,
)
from opencollate.parsers.sdc import TimingConstraintObservation


def _observation() -> ViewObservation:
    source = ConnectivityEndpoint("top/request", bit_index=3, ordinal=0, width=4)
    sink = ConnectivityEndpoint("top/grant", bit_index=0, ordinal=0, width=4)
    return ViewObservation(
        ViewId("sdc", "signoff"),
        components=(
            ComponentObservation(
                "top",
                kind=ComponentKind.MODULE,
                ports=(
                    PortObservation(
                        "request",
                        direction=Direction.INPUT,
                        role=PortRole.SIGNAL,
                        shape=BusShape(left=3, right=0),
                        attributes={"protocol": "ready-valid"},
                    ),
                    PortObservation(
                        "grant",
                        direction=Direction.OUTPUT,
                        role=PortRole.SIGNAL,
                        shape=BusShape(left=3, right=0),
                    ),
                ),
                functions={"grant": "request"},
                attributes={"language": "systemverilog"},
            ),
        ),
        pin_mappings=(
            PinMappingObservation(
                "PAD_A1",
                "A1",
                "request[3]",
                component="top",
                direction=Direction.INPUT,
                attributes={"io_standard": "LVCMOS18"},
            ),
        ),
        objects=(
            DesignObjectObservation(
                "instance",
                "u_uart",
                scope="top",
                attributes={"master": "uart"},
            ),
            DesignObjectObservation(
                "power_domain",
                "PD_UART",
                attributes={"supply": "VDD_UART"},
            ),
        ),
        clocks=(
            ClockObservation(
                "core_clk",
                targets=("top/clk",),
                period=2.5,
                waveform=(0.0, 1.25),
                attributes={"uncertainty": 0.08},
            ),
        ),
        interfaces=(
            InterfaceObservation(
                "control",
                component="top",
                bus_type="amba.com:AMBA4:APB4:r0p0_0",
                abstraction_type="amba.com:AMBA4:APB4_rtl:r0p0_0",
                mode="slave",
                port_maps={"PADDR": "request", "PRDATA": "grant"},
            ),
        ),
        registers=(
            RegisterObservation(
                "CTRL",
                component="top",
                memory_map="APB",
                address_block="UART",
                address_offset=0,
                absolute_address=0x4000_1000,
                size_bits=32,
                access="rw",
                fields=(
                    RegisterFieldObservation(
                        "ENABLE",
                        bit_offset=0,
                        bit_width=1,
                        access="rw",
                        reset_value=0,
                        attributes={"side_effect": "none"},
                    ),
                ),
            ),
        ),
        connectivity_endpoints=(source, sink),
        connectivity_edges=(ConnectivityEdge(source, sink, kind="assign", inverted=False),),
        connectivity_requirements=(
            ConnectivityRequirement(
                "REQ-1",
                source.key,
                sink.key,
                expectation=ConnectivityExpectation.REACHABLE,
                transform=ConnectivityTransform.IDENTITY,
                through=("top/u_uart",),
                description="Request reaches grant through the UART wrapper",
            ),
        ),
        complete=False,
        tainted_scopes=frozenset({"top/u_unknown"}),
        attributes={
            "timing_constraints": (
                TimingConstraintObservation(
                    "set_input_delay",
                    value=0.4,
                    objects=("top/request",),
                    clocks=("core_clk",),
                    attributes={"rise": True},
                ),
            ),
            "power_intent": {"domains": {"PD_UART"}},
        },
    )


def _project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        path=tmp_path / "opencollate.toml",
        root=tmp_path,
        name="contract-v2-test",
        sources=(),
    )


def _contract_schema() -> dict[str, object]:
    text = (
        resources.files("opencollate.schemas")
        .joinpath("contract.schema.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def test_view_snapshot_round_trip_preserves_every_observation_family() -> None:
    snapshot = ContractViewSnapshot.from_observation(_observation())
    payload = snapshot.to_dict()

    assert payload["snapshot_version"] == 1
    assert len(payload["content_sha256"]) == 64
    assert payload["complete"] is False
    assert payload["tainted_scopes"] == ["top/u_unknown"]
    assert payload["components"][0]["ports"][0]["shape"]["width"] == 4
    assert payload["objects"][1]["kind"] == "power_domain"
    assert payload["clocks"][0]["period"] == 2.5
    assert payload["interfaces"][0]["mode"] == "slave"
    assert payload["pin_mappings"][0]["package_ball"] == "A1"
    assert payload["registers"][0]["fields"][0]["reset_value"] == 0
    assert payload["connectivity_edges"][0]["source"]["key"] == "top/request[3]"
    assert payload["connectivity_requirements"][0]["id"] == "REQ-1"
    assert payload["attributes"]["timing_constraints"][0]["command"] == "set_input_delay"
    assert ContractViewSnapshot.from_dict(payload) == snapshot


def test_view_snapshot_digest_detects_semantic_tampering() -> None:
    payload = ContractViewSnapshot.from_observation(_observation()).to_dict()
    payload["clocks"][0]["period"] = 10.0

    with pytest.raises(ValueError, match="content_sha256"):
        ContractViewSnapshot.from_dict(payload)


def test_engine_generates_schema_v2_with_complete_view_snapshots(tmp_path: Path) -> None:
    observation = _observation()
    result = ComparisonEngine(_project(tmp_path)).run((observation,))
    contract = result.generated_contract

    assert contract.schema_version == CONTRACT_SCHEMA_VERSION
    assert [item.view for item in contract.views] == ["sdc.signoff"]
    assert contract.views[0].content_sha256
    payload = contract.to_dict()
    Draft202012Validator(_contract_schema()).validate(payload)
    assert DesignContract.from_dict(payload) == contract


def test_v1_contract_remains_readable_and_migrates_without_invented_facts() -> None:
    original = DesignContract.from_dict(
        {
            "schema_version": 1,
            "generated_by": "legacy",
            "components": [],
            "registers": [],
        }
    )
    assert original.schema_version == 1

    upgraded = upgrade_contract(original)
    assert upgraded.schema_version == CONTRACT_SCHEMA_VERSION
    assert upgraded.views == ()
    assert upgraded.extensions["opencollate.migration"]["view_snapshots_available"] is False
    Draft202012Validator(_contract_schema()).validate(original.to_dict())
    Draft202012Validator(_contract_schema()).validate(upgraded.to_dict())
    assert DesignContract.from_dict(upgraded.to_dict()) == upgraded


def test_cli_migrates_v1_contract_to_v2(tmp_path: Path) -> None:
    source = tmp_path / "legacy.oc.json"
    target = tmp_path / "contract.v2.oc.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_by": "legacy",
                "components": [],
                "registers": [],
            }
        ),
        encoding="utf-8",
    )

    assert main(["contract", "migrate", str(source), "-o", str(target)]) == 0
    migrated = load_contract(target)
    assert migrated.schema_version == CONTRACT_SCHEMA_VERSION
    assert migrated.views == ()
    assert migrated.extensions["opencollate.migration"]["source_schema_version"] == 1

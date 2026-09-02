from __future__ import annotations

from opencollate.contracts import ContractViewSnapshot
from opencollate.diagnostics import json_safe
from opencollate.model import (
    ComponentObservation,
    FactState,
    PortObservation,
    ViewId,
    ViewObservation,
)


def test_json_safe_converts_str_enum_before_primitive_shortcut() -> None:
    assert json_safe(FactState.KNOWN) == "known"
    assert type(json_safe(FactState.KNOWN)) is str


def test_contract_snapshot_normalizes_enum_valued_field_states() -> None:
    observation = ViewObservation(
        ViewId("rtl", "enum-regression"),
        components=(
            ComponentObservation(
                "top",
                ports=(
                    PortObservation(
                        "data",
                        field_states={"direction": FactState.KNOWN},
                    ),
                ),
            ),
        ),
    )

    snapshot = ContractViewSnapshot.from_observation(observation).to_dict()
    assert snapshot["components"][0]["ports"][0]["field_states"] == {"direction": "known"}

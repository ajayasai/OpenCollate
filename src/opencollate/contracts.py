"""Durable, integrity-checked contract view snapshots.

This module intentionally has no import-time dependency on :mod:`opencollate.model`.
The model imports :class:`ContractViewSnapshot`, while snapshot construction only
loads the JSON normalizer lazily after the model is fully initialized.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

CONTRACT_SCHEMA_VERSION = 2
CONTRACT_VIEW_SNAPSHOT_VERSION = 1
_MAX_JSON_NESTING = 128
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RECORD_FIELDS = (
    "components",
    "pin_mappings",
    "objects",
    "clocks",
    "interfaces",
    "registers",
    "connectivity_endpoints",
    "connectivity_edges",
    "connectivity_requirements",
)


def _normalize_json(value: Any, *, path: str = "$", depth: int = 0) -> Any:
    if depth > _MAX_JSON_NESTING:
        raise ValueError(f"{path} exceeds maximum contract JSON nesting")
    if value is None or type(value) in {str, bool, int}:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError(f"{path} object keys must be strings")
        return {
            key: _normalize_json(item, path=f"{path}.{key}", depth=depth + 1)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json(item, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains unsupported JSON value {type(value).__name__}")


def normalize_contract_extensions(
    value: Mapping[str, Any] | None,
    *,
    path: str = "extensions",
) -> dict[str, Any]:
    if value is None:
        return {}
    normalized = _normalize_json(value, path=path)
    if not isinstance(normalized, dict):
        raise TypeError(f"{path} must be an object")
    return normalized


def _normalize_records(value: Any, *, path: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{path} must be an array")
    records: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise TypeError(f"{path}[{index}] must be an object")
        normalized = _normalize_json(item, path=f"{path}[{index}]")
        if not isinstance(normalized, dict):  # defensive; Mapping normalized above
            raise TypeError(f"{path}[{index}] must be an object")
        records.append(normalized)
    return tuple(sorted(records, key=_canonical_json))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _content_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _status(value: Any) -> str:
    status = getattr(value, "value", value)
    return str(status)


def _safe_mapping(value: Any, *, path: str) -> dict[str, Any]:
    normalized = _normalize_json(value, path=path)
    if not isinstance(normalized, dict):
        raise TypeError(f"{path} must be an object")
    return normalized


@dataclass(frozen=True, slots=True)
class ContractViewSnapshot:
    """A deterministic snapshot of every parser-neutral fact from one view.

    The frozen canonical component/register sections remain the compact reviewed
    identity model. View snapshots retain the broader fact surface used by
    clocks, constraints, power intent, hierarchy, package, and connectivity
    checks so a contract is no longer an incomplete subset of the run.
    """

    view: str
    complete: bool = True
    tainted_scopes: tuple[str, ...] = ()
    components: tuple[Mapping[str, Any], ...] = ()
    pin_mappings: tuple[Mapping[str, Any], ...] = ()
    objects: tuple[Mapping[str, Any], ...] = ()
    clocks: tuple[Mapping[str, Any], ...] = ()
    interfaces: tuple[Mapping[str, Any], ...] = ()
    registers: tuple[Mapping[str, Any], ...] = ()
    connectivity_endpoints: tuple[Mapping[str, Any], ...] = ()
    connectivity_edges: tuple[Mapping[str, Any], ...] = ()
    connectivity_requirements: tuple[Mapping[str, Any], ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)
    extensions: Mapping[str, Any] = field(default_factory=dict)
    snapshot_version: int = CONTRACT_VIEW_SNAPSHOT_VERSION
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.view, str) or not self.view.strip():
            raise TypeError("contract view snapshot view must be a nonempty string")
        if type(self.complete) is not bool:
            raise TypeError("contract view snapshot complete must be a boolean")
        if type(self.snapshot_version) is not int:
            raise TypeError("contract view snapshot version must be an integer")
        if self.snapshot_version != CONTRACT_VIEW_SNAPSHOT_VERSION:
            raise ValueError(
                f"unsupported contract view snapshot version {self.snapshot_version}; "
                f"this release supports {CONTRACT_VIEW_SNAPSHOT_VERSION}"
            )
        if not isinstance(self.tainted_scopes, (list, tuple, set, frozenset)) or not all(
            isinstance(item, str) and item for item in self.tainted_scopes
        ):
            raise TypeError("contract view snapshot tainted_scopes must contain nonempty strings")
        object.__setattr__(self, "view", self.view.strip())
        object.__setattr__(self, "tainted_scopes", tuple(sorted(set(self.tainted_scopes))))
        for field_name in _RECORD_FIELDS:
            object.__setattr__(
                self,
                field_name,
                _normalize_records(getattr(self, field_name), path=f"views.{self.view}.{field_name}"),
            )
        object.__setattr__(
            self,
            "attributes",
            _safe_mapping(self.attributes, path=f"views.{self.view}.attributes"),
        )
        object.__setattr__(
            self,
            "extensions",
            normalize_contract_extensions(
                self.extensions,
                path=f"views.{self.view}.extensions",
            ),
        )
        digest = _content_digest(self._payload())
        supplied = self.content_sha256
        if supplied is not None:
            if not isinstance(supplied, str) or _SHA256.fullmatch(supplied) is None:
                raise TypeError("contract view snapshot content_sha256 must be lowercase SHA-256")
            if supplied != digest:
                raise ValueError(
                    f"contract view snapshot {self.view!r} content_sha256 does not match its facts"
                )
        object.__setattr__(self, "content_sha256", digest)

    def _payload(self) -> dict[str, Any]:
        return {
            "snapshot_version": self.snapshot_version,
            "view": self.view,
            "complete": self.complete,
            "tainted_scopes": list(self.tainted_scopes),
            **{
                field_name: [dict(item) for item in getattr(self, field_name)]
                for field_name in _RECORD_FIELDS
            },
            "attributes": dict(self.attributes),
            "extensions": dict(self.extensions),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "content_sha256": self.content_sha256}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ContractViewSnapshot:
        if not isinstance(data, Mapping):
            raise TypeError("contract view snapshot must be an object")
        allowed = {
            "snapshot_version",
            "view",
            "complete",
            "tainted_scopes",
            *_RECORD_FIELDS,
            "attributes",
            "extensions",
            "content_sha256",
        }
        unexpected = sorted(set(data) - allowed)
        if unexpected:
            raise ValueError(
                "contract view snapshot contains unsupported properties: "
                + ", ".join(unexpected)
            )
        view = data.get("view")
        if not isinstance(view, str) or not view:
            raise TypeError("contract view snapshot view must be a nonempty string")
        return cls(
            view=view,
            complete=data.get("complete", True),
            tainted_scopes=tuple(data.get("tainted_scopes", ())),
            **{field_name: tuple(data.get(field_name, ())) for field_name in _RECORD_FIELDS},
            attributes=data.get("attributes", {}),
            extensions=data.get("extensions", {}),
            snapshot_version=data.get("snapshot_version", CONTRACT_VIEW_SNAPSHOT_VERSION),
            content_sha256=data.get("content_sha256"),
        )

    @classmethod
    def from_observation(cls, observation: Any) -> ContractViewSnapshot:
        """Convert one ``ViewObservation`` without importing the model at module load."""

        from opencollate.diagnostics import json_safe

        def attributes(value: Any, path: str) -> dict[str, Any]:
            return _safe_mapping(json_safe(value), path=path)

        components: list[dict[str, Any]] = []
        for component in observation.components:
            ports = []
            for port in component.ports:
                ports.append(
                    {
                        "name": port.native_name,
                        "direction": _status(port.direction),
                        "role": _status(port.role),
                        "shape": port.shape.to_dict(),
                        "status": _status(port.status),
                        "field_states": json_safe(port.field_states),
                        "attributes": attributes(
                            port.attributes,
                            f"views.{observation.view}.components.{component.native_name}.ports",
                        ),
                    }
                )
            components.append(
                {
                    "name": component.native_name,
                    "kind": _status(component.kind),
                    "status": _status(component.status),
                    "ports": ports,
                    "functions": json_safe(component.functions),
                    "attributes": attributes(
                        component.attributes,
                        f"views.{observation.view}.components.{component.native_name}.attributes",
                    ),
                }
            )

        pin_mappings = [
            {
                "die_pad": item.die_pad,
                "package_ball": item.package_ball,
                "signal": item.signal,
                "component": item.component,
                "direction": _status(item.direction),
                "role": _status(item.role),
                "status": _status(item.status),
                "attributes": attributes(
                    item.attributes,
                    f"views.{observation.view}.pin_mappings.attributes",
                ),
            }
            for item in observation.pin_mappings
        ]
        objects = [
            {
                "kind": item.kind,
                "name": item.native_name,
                "qualified_name": item.qualified_name,
                "relation": item.relation,
                "scope": item.scope,
                "status": _status(item.status),
                "attributes": attributes(
                    item.attributes,
                    f"views.{observation.view}.objects.{item.native_name}.attributes",
                ),
            }
            for item in observation.objects
        ]
        clocks = [
            {
                "name": item.native_name,
                "targets": list(item.targets),
                "period": item.period,
                "waveform": None if item.waveform is None else list(item.waveform),
                "source": item.source,
                "generated": item.generated,
                "status": _status(item.status),
                "attributes": attributes(
                    item.attributes,
                    f"views.{observation.view}.clocks.{item.native_name}.attributes",
                ),
            }
            for item in observation.clocks
        ]
        interfaces = [
            {
                "name": item.native_name,
                "component": item.component,
                "bus_type": item.bus_type,
                "abstraction_type": item.abstraction_type,
                "mode": item.mode,
                "port_maps": dict(sorted(item.port_maps.items())),
                "status": _status(item.status),
                "attributes": attributes(
                    item.attributes,
                    f"views.{observation.view}.interfaces.{item.native_name}.attributes",
                ),
            }
            for item in observation.interfaces
        ]
        registers = []
        for register in observation.registers:
            fields = [
                {
                    "name": item.native_name,
                    "bit_offset": item.bit_offset,
                    "bit_width": item.bit_width,
                    "access": item.access,
                    "reset_value": item.reset_value,
                    "status": _status(item.status),
                    "attributes": attributes(
                        item.attributes,
                        f"views.{observation.view}.registers.{register.native_name}.fields",
                    ),
                }
                for item in register.fields
            ]
            registers.append(
                {
                    "name": register.native_name,
                    "component": register.component,
                    "memory_map": register.memory_map,
                    "address_block": register.address_block,
                    "address_offset": register.address_offset,
                    "absolute_address": register.absolute_address,
                    "size_bits": register.size_bits,
                    "access": register.access,
                    "status": _status(register.status),
                    "fields": fields,
                    "attributes": attributes(
                        register.attributes,
                        f"views.{observation.view}.registers.{register.native_name}.attributes",
                    ),
                }
            )
        connectivity_endpoints = [
            {
                **item.to_dict(),
                "attributes": attributes(
                    item.attributes,
                    f"views.{observation.view}.connectivity_endpoints.{item.key}.attributes",
                ),
            }
            for item in observation.connectivity_endpoints
        ]
        connectivity_edges = [
            {
                "source": item.source.to_dict(),
                "sink": item.sink.to_dict(),
                "kind": item.kind,
                "inverted": item.inverted,
                "status": _status(item.status),
                "attributes": attributes(
                    item.attributes,
                    f"views.{observation.view}.connectivity_edges.attributes",
                ),
            }
            for item in observation.connectivity_edges
        ]
        connectivity_requirements = [
            {
                **item.to_dict(),
                "attributes": attributes(
                    item.attributes,
                    f"views.{observation.view}.connectivity_requirements.{item.identifier}",
                ),
            }
            for item in observation.connectivity_requirements
        ]
        return cls(
            view=str(observation.view),
            complete=observation.complete,
            tainted_scopes=tuple(observation.tainted_scopes),
            components=tuple(components),
            pin_mappings=tuple(pin_mappings),
            objects=tuple(objects),
            clocks=tuple(clocks),
            interfaces=tuple(interfaces),
            registers=tuple(registers),
            connectivity_endpoints=tuple(connectivity_endpoints),
            connectivity_edges=tuple(connectivity_edges),
            connectivity_requirements=tuple(connectivity_requirements),
            attributes=attributes(
                observation.attributes,
                f"views.{observation.view}.attributes",
            ),
        )


def snapshots_from_observations(
    observations: Sequence[Any],
) -> tuple[ContractViewSnapshot, ...]:
    snapshots = (ContractViewSnapshot.from_observation(item) for item in observations)
    return tuple(sorted(snapshots, key=lambda item: item.view))


def upgrade_contract(contract: Any) -> Any:
    """Upgrade a parsed v1 contract to v2 without inventing unavailable view facts."""

    from opencollate.model import DesignContract

    if not isinstance(contract, DesignContract):
        raise TypeError("upgrade_contract requires a DesignContract")
    if contract.schema_version == CONTRACT_SCHEMA_VERSION:
        return contract
    if contract.schema_version != 1:
        raise ValueError(f"cannot upgrade unsupported contract schema {contract.schema_version}")
    extensions = normalize_contract_extensions(getattr(contract, "extensions", {}))
    extensions.setdefault(
        "opencollate.migration",
        {
            "source_schema_version": 1,
            "view_snapshots_available": False,
            "reason": "schema v1 did not persist run-local observation families",
        },
    )
    return DesignContract(
        contract.components,
        schema_version=CONTRACT_SCHEMA_VERSION,
        generated_by=contract.generated_by,
        registers=contract.registers,
        views=(),
        extensions=extensions,
    )


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "CONTRACT_VIEW_SNAPSHOT_VERSION",
    "ContractViewSnapshot",
    "normalize_contract_extensions",
    "snapshots_from_observations",
    "upgrade_contract",
]

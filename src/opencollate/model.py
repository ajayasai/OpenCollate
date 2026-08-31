"""Canonical, parser-neutral data model used throughout OpenCollate.

Parsers deliberately emit *observations*, not canonical truth.  Reconciliation
groups those observations while retaining their view and source provenance so
checks can explain every conflicting value.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import reduce
from operator import mul
from pathlib import Path
from typing import Any, Generic, TypeVar


class FactState(StrEnum):
    """How confidently an observed fact can be used by a checker."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"
    TAINTED = "tainted"
    NOT_APPLICABLE = "not_applicable"


class Direction(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    INOUT = "inout"
    INTERNAL = "internal"
    FEEDTHROUGH = "feedthrough"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str | Direction | None) -> Direction:
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.UNKNOWN
        normalized = str(value).strip().lower().replace("-", "").replace("_", "")
        aliases = {
            "input": cls.INPUT,
            "in": cls.INPUT,
            "output": cls.OUTPUT,
            "out": cls.OUTPUT,
            "inout": cls.INOUT,
            "bidir": cls.INOUT,
            "bidirectional": cls.INOUT,
            "internal": cls.INTERNAL,
            "feedthru": cls.FEEDTHROUGH,
            "feedthrough": cls.FEEDTHROUGH,
            "unknown": cls.UNKNOWN,
            "": cls.UNKNOWN,
        }
        return aliases.get(normalized, cls.UNKNOWN)


class PortRole(StrEnum):
    SIGNAL = "signal"
    CLOCK = "clock"
    RESET = "reset"
    POWER = "power"
    GROUND = "ground"
    ANALOG = "analog"
    TIE = "tie"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str | PortRole | None) -> PortRole:
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.UNKNOWN
        normalized = str(value).strip().lower().replace("-", "_")
        aliases = {
            "signal": cls.SIGNAL,
            "clock": cls.CLOCK,
            "clk": cls.CLOCK,
            "reset": cls.RESET,
            "power": cls.POWER,
            "primary_power": cls.POWER,
            "internal_power": cls.POWER,
            "backup_power": cls.POWER,
            "ground": cls.GROUND,
            "primary_ground": cls.GROUND,
            "internal_ground": cls.GROUND,
            "analog": cls.ANALOG,
            "tie": cls.TIE,
            "tie_high": cls.TIE,
            "tie_low": cls.TIE,
            "unknown": cls.UNKNOWN,
            "": cls.UNKNOWN,
        }
        return aliases.get(normalized, cls.UNKNOWN)


class ComponentKind(StrEnum):
    MODULE = "module"
    CELL = "cell"
    MACRO = "macro"
    PAD = "pad"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str | ComponentKind | None) -> ComponentKind:
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.UNKNOWN
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True, order=True, slots=True)
class ViewId:
    """A source-view identity, for example ``rtl.default`` or ``liberty.tt``."""

    kind: str
    name: str = "default"

    def __post_init__(self) -> None:
        kind = self.kind.strip().lower()
        name = self.name.strip()
        if not kind:
            raise ValueError("view kind must not be empty")
        if not name:
            raise ValueError("view name must not be empty")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "name", name)

    @property
    def key(self) -> str:
        return f"{self.kind}.{self.name}"

    def matches(self, selector: str) -> bool:
        selector = selector.strip().lower()
        return selector in {"*", self.kind, self.key.lower()}

    def __str__(self) -> str:
        return self.key

    @classmethod
    def parse(cls, value: str | ViewId) -> ViewId:
        if isinstance(value, cls):
            return value
        text = str(value).strip()
        if "." not in text:
            return cls(text)
        kind, name = text.split(".", 1)
        return cls(kind, name)


@dataclass(frozen=True, order=True, slots=True)
class SourceSpan:
    source: str
    line: int = 1
    column: int = 1
    end_line: int | None = None
    end_column: int | None = None

    def __post_init__(self) -> None:
        if self.line < 1 or self.column < 1:
            raise ValueError("source positions are one-based")
        if self.end_line is not None and self.end_line < self.line:
            raise ValueError("end_line cannot precede line")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": self.source,
            "line": self.line,
            "column": self.column,
        }
        if self.end_line is not None:
            result["end_line"] = self.end_line
        if self.end_column is not None:
            result["end_column"] = self.end_column
        return result


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where an observation came from.

    ``raw_name`` is retained separately because source spelling can matter even
    after an escaped identifier is decoded for reconciliation.
    """

    source: str
    line: int = 1
    column: int = 1
    view: ViewId = field(default_factory=lambda: ViewId("unknown"))
    raw_name: str | None = None
    end_line: int | None = None
    end_column: int | None = None

    @property
    def span(self) -> SourceSpan:
        return SourceSpan(
            self.source,
            self.line,
            self.column,
            self.end_line,
            self.end_column,
        )

    def to_dict(self) -> dict[str, Any]:
        result = self.span.to_dict()
        result["view"] = str(self.view)
        if self.raw_name is not None:
            result["raw_name"] = self.raw_name
        return result


@dataclass(frozen=True, order=True, slots=True)
class IndexRange:
    left: int
    right: int

    @property
    def ascending(self) -> bool:
        return self.right > self.left

    @property
    def step(self) -> int:
        if self.right == self.left:
            return 0
        return 1 if self.ascending else -1

    @property
    def width(self) -> int:
        return abs(self.left - self.right) + 1

    @property
    def ordered_indices(self) -> tuple[int, ...]:
        if self.left == self.right:
            return (self.left,)
        return tuple(range(self.left, self.right + self.step, self.step))

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "step": self.step,
            "width": self.width,
        }


@dataclass(frozen=True, slots=True)
class BusShape:
    """Logical port shape without prematurely flattening its declaration."""

    width: int | None = None
    left: int | None = None
    right: int | None = None
    ascending: bool | None = None
    packed: tuple[IndexRange, ...] = ()
    unpacked: tuple[IndexRange, ...] = ()
    bit_indices: tuple[int, ...] = ()
    explicit_scalar: bool | None = None

    def __post_init__(self) -> None:
        packed = tuple(
            item if isinstance(item, IndexRange) else IndexRange(*item) for item in self.packed
        )
        unpacked = tuple(
            item if isinstance(item, IndexRange) else IndexRange(*item) for item in self.unpacked
        )
        object.__setattr__(self, "packed", packed)
        object.__setattr__(self, "unpacked", unpacked)
        object.__setattr__(self, "bit_indices", tuple(self.bit_indices))

        if self.left is not None and self.right is not None and not packed:
            packed = (IndexRange(self.left, self.right),)
            object.__setattr__(self, "packed", packed)
        elif packed and self.left is None and self.right is None and len(packed) == 1:
            object.__setattr__(self, "left", packed[0].left)
            object.__setattr__(self, "right", packed[0].right)

        derived_width: int | None = None
        if packed:
            derived_width = reduce(mul, (item.width for item in packed), 1)
        elif self.bit_indices:
            derived_width = len(set(self.bit_indices))
        elif self.left is not None and self.right is not None:
            derived_width = abs(self.left - self.right) + 1

        if self.width is None and derived_width is not None:
            object.__setattr__(self, "width", derived_width)
        if self.width is not None and self.width < 1:
            raise ValueError("bus width must be positive")

        if self.ascending is None:
            if self.left is not None and self.right is not None:
                object.__setattr__(self, "ascending", self.right > self.left)
            elif len(self.bit_indices) > 1:
                object.__setattr__(self, "ascending", self.bit_indices[-1] > self.bit_indices[0])

        if self.explicit_scalar is None and self.width == 1:
            object.__setattr__(
                self,
                "explicit_scalar",
                not packed and not self.bit_indices and self.left is None,
            )

    @property
    def known(self) -> bool:
        return self.width is not None

    @property
    def dimensions(self) -> tuple[IndexRange, ...]:
        return self.packed

    @property
    def ordered_indices(self) -> tuple[int, ...] | None:
        if self.bit_indices:
            return self.bit_indices
        if len(self.packed) == 1:
            return self.packed[0].ordered_indices
        return None

    @property
    def has_duplicate_bits(self) -> bool:
        return bool(self.bit_indices) and len(self.bit_indices) != len(set(self.bit_indices))

    @property
    def has_bit_gap(self) -> bool:
        unique = sorted(set(self.bit_indices))
        if len(unique) < 2:
            return False
        return unique != list(range(unique[0], unique[-1] + 1))

    def signature(self) -> tuple[Any, ...]:
        return (
            self.width,
            tuple((item.left, item.right) for item in self.packed),
            tuple((item.left, item.right) for item in self.unpacked),
            self.bit_indices,
            self.explicit_scalar,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "width": self.width,
            "packed": [item.to_dict() for item in self.packed],
            "unpacked": [item.to_dict() for item in self.unpacked],
        }
        if self.left is not None:
            result["left"] = self.left
        if self.right is not None:
            result["right"] = self.right
        if self.ascending is not None:
            result["ascending"] = self.ascending
        if self.bit_indices:
            result["bit_indices"] = list(self.bit_indices)
        if self.explicit_scalar is not None:
            result["explicit_scalar"] = self.explicit_scalar
        return result

    @classmethod
    def scalar(cls) -> BusShape:
        return cls(width=1, explicit_scalar=True)

    @classmethod
    def unknown(cls) -> BusShape:
        return cls()


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Fact(Generic[T]):
    state: FactState
    value: T | None = None
    evidence: tuple[Provenance, ...] = ()
    detail: str | None = None

    @classmethod
    def known(cls, value: T, *evidence: Provenance) -> Fact[T]:
        return cls(FactState.KNOWN, value, tuple(evidence))

    @classmethod
    def unknown(cls, detail: str | None = None) -> Fact[T]:
        return cls(FactState.UNKNOWN, detail=detail)


@dataclass(frozen=True, slots=True)
class PortObservation:
    native_name: str
    direction: Direction = Direction.UNKNOWN
    role: PortRole = PortRole.UNKNOWN
    shape: BusShape = field(default_factory=BusShape.unknown)
    provenance: Provenance | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    status: FactState = FactState.KNOWN
    field_states: Mapping[str, FactState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.native_name:
            raise ValueError("port native_name must not be empty")
        object.__setattr__(self, "direction", Direction.parse(self.direction))
        object.__setattr__(self, "role", PortRole.parse(self.role))
        if not isinstance(self.shape, BusShape):
            raise TypeError("shape must be a BusShape")
        object.__setattr__(self, "attributes", dict(self.attributes))
        object.__setattr__(
            self,
            "field_states",
            {str(key): FactState(value) for key, value in self.field_states.items()},
        )

    @property
    def name(self) -> str:
        """Compatibility alias for callers that already use canonical spelling."""

        return self.native_name

    def state_for(self, field_name: str) -> FactState:
        if self.status != FactState.KNOWN:
            return self.status
        explicit = self.field_states.get(field_name)
        if explicit is not None:
            return explicit
        value = getattr(self, field_name)
        if field_name == "direction" and value == Direction.UNKNOWN:
            return FactState.UNKNOWN
        if field_name == "role" and value == PortRole.UNKNOWN:
            return FactState.UNKNOWN
        if field_name == "shape" and not value.known:
            return FactState.UNKNOWN
        return FactState.KNOWN


@dataclass(frozen=True, slots=True)
class ComponentObservation:
    native_name: str
    kind: ComponentKind = ComponentKind.UNKNOWN
    ports: tuple[PortObservation, ...] = ()
    functions: Mapping[str, Any] = field(default_factory=dict)
    provenance: Provenance | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    status: FactState = FactState.KNOWN

    def __post_init__(self) -> None:
        if not self.native_name:
            raise ValueError("component native_name must not be empty")
        object.__setattr__(self, "kind", ComponentKind.parse(self.kind))
        object.__setattr__(self, "ports", tuple(self.ports))
        object.__setattr__(self, "functions", dict(self.functions))
        object.__setattr__(self, "attributes", dict(self.attributes))

    @property
    def name(self) -> str:
        return self.native_name

    def interface_signature(self) -> tuple[Any, ...]:
        return tuple(
            sorted(
                (
                    port.native_name,
                    port.direction.value,
                    port.role.value,
                    port.shape.signature(),
                )
                for port in self.ports
            )
        )


@dataclass(frozen=True, slots=True)
class PinMappingObservation:
    die_pad: str | None
    package_ball: str | None
    signal: str | None
    component: str | None = None
    direction: Direction = Direction.UNKNOWN
    role: PortRole = PortRole.UNKNOWN
    provenance: Provenance | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    status: FactState = FactState.KNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", Direction.parse(self.direction))
        object.__setattr__(self, "role", PortRole.parse(self.role))
        object.__setattr__(self, "attributes", dict(self.attributes))


@dataclass(frozen=True, slots=True)
class DesignObjectObservation:
    """A named design object defined or referenced by collateral.

    This deliberately small record is shared by RTL hierarchy, SDC queries,
    and UPF scope references.  ``relation`` distinguishes authoritative
    definitions from references that must resolve in another view.  Parsers
    retain command-specific detail in ``attributes`` instead of flattening
    every language into an unreliable pseudo-netlist.
    """

    kind: str
    native_name: str
    relation: str = "definition"
    scope: str | None = None
    provenance: Provenance | None = None
    status: FactState = FactState.KNOWN
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = self.kind.strip().lower().replace("-", "_")
        relation = self.relation.strip().lower().replace("-", "_")
        if not kind:
            raise ValueError("design object kind must not be empty")
        if not self.native_name:
            raise ValueError("design object native_name must not be empty")
        if relation not in {"definition", "reference"}:
            raise ValueError("design object relation must be definition or reference")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "relation", relation)
        object.__setattr__(self, "status", FactState(self.status))
        object.__setattr__(self, "attributes", dict(self.attributes))

    @property
    def qualified_name(self) -> str:
        if not self.scope or self.native_name.startswith(f"{self.scope}/"):
            return self.native_name
        return f"{self.scope}/{self.native_name}"


@dataclass(frozen=True, slots=True)
class ClockObservation:
    """A primary or generated clock declaration with statically known facts."""

    native_name: str
    targets: tuple[str, ...] = ()
    period: float | None = None
    waveform: tuple[float, float] | None = None
    source: str | None = None
    generated: bool = False
    provenance: Provenance | None = None
    status: FactState = FactState.KNOWN
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.native_name:
            raise ValueError("clock native_name must not be empty")
        if self.period is not None and self.period <= 0:
            raise ValueError("clock period must be positive")
        waveform = None if self.waveform is None else tuple(self.waveform)
        if waveform is not None and len(waveform) != 2:
            raise ValueError("clock waveform must contain exactly two edges")
        object.__setattr__(self, "targets", tuple(self.targets))
        object.__setattr__(self, "waveform", waveform)
        object.__setattr__(self, "status", FactState(self.status))
        object.__setattr__(self, "attributes", dict(self.attributes))


@dataclass(frozen=True, slots=True)
class InterfaceObservation:
    """An IP-XACT-style bus interface and its logical-to-physical port map."""

    native_name: str
    component: str | None = None
    bus_type: str | None = None
    abstraction_type: str | None = None
    mode: str | None = None
    port_maps: Mapping[str, str] = field(default_factory=dict)
    provenance: Provenance | None = None
    status: FactState = FactState.KNOWN
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.native_name:
            raise ValueError("interface native_name must not be empty")
        object.__setattr__(self, "port_maps", dict(self.port_maps))
        object.__setattr__(self, "status", FactState(self.status))
        object.__setattr__(self, "attributes", dict(self.attributes))


@dataclass(frozen=True, slots=True)
class RegisterFieldObservation:
    """A field inside a hardware or software register declaration."""

    native_name: str
    bit_offset: int | None = None
    bit_width: int | None = None
    access: str | None = None
    reset_value: int | None = None
    provenance: Provenance | None = None
    status: FactState = FactState.KNOWN
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.native_name:
            raise ValueError("register field native_name must not be empty")
        if self.bit_offset is not None and self.bit_offset < 0:
            raise ValueError("register field bit_offset must not be negative")
        if self.bit_width is not None and self.bit_width < 1:
            raise ValueError("register field bit_width must be positive")
        object.__setattr__(self, "status", FactState(self.status))
        object.__setattr__(self, "attributes", dict(self.attributes))


@dataclass(frozen=True, slots=True)
class RegisterObservation:
    """A register address-map observation from hardware or software collateral."""

    native_name: str
    component: str | None = None
    memory_map: str | None = None
    address_block: str | None = None
    address_offset: int | None = None
    absolute_address: int | None = None
    size_bits: int | None = None
    access: str | None = None
    fields: tuple[RegisterFieldObservation, ...] = ()
    provenance: Provenance | None = None
    status: FactState = FactState.KNOWN
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.native_name:
            raise ValueError("register native_name must not be empty")
        if self.address_offset is not None and self.address_offset < 0:
            raise ValueError("register address_offset must not be negative")
        if self.absolute_address is not None and self.absolute_address < 0:
            raise ValueError("register absolute_address must not be negative")
        if self.size_bits is not None and self.size_bits < 1:
            raise ValueError("register size_bits must be positive")
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "status", FactState(self.status))
        object.__setattr__(self, "attributes", dict(self.attributes))


@dataclass(frozen=True, slots=True)
class ViewObservation:
    view: ViewId
    components: tuple[ComponentObservation, ...] = ()
    diagnostics: tuple[Any, ...] = ()
    complete: bool = True
    tainted_scopes: frozenset[str] = frozenset()
    pin_mappings: tuple[PinMappingObservation, ...] = ()
    objects: tuple[DesignObjectObservation, ...] = ()
    clocks: tuple[ClockObservation, ...] = ()
    interfaces: tuple[InterfaceObservation, ...] = ()
    registers: tuple[RegisterObservation, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "view", ViewId.parse(self.view))
        object.__setattr__(self, "components", tuple(self.components))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "tainted_scopes", frozenset(self.tainted_scopes))
        object.__setattr__(self, "pin_mappings", tuple(self.pin_mappings))
        object.__setattr__(self, "objects", tuple(self.objects))
        object.__setattr__(self, "clocks", tuple(self.clocks))
        object.__setattr__(self, "interfaces", tuple(self.interfaces))
        object.__setattr__(self, "registers", tuple(self.registers))
        object.__setattr__(self, "attributes", dict(self.attributes))

    def scope_is_tainted(self, native_name: str | None = None) -> bool:
        if not self.complete and not self.tainted_scopes:
            return True
        if native_name is None:
            return False
        return native_name in self.tainted_scopes or "*" in self.tainted_scopes


@dataclass(frozen=True, slots=True)
class ComponentMember:
    view: ViewId
    observation: ComponentObservation


@dataclass(frozen=True, slots=True)
class PortMember:
    view: ViewId
    component_native_name: str
    observation: PortObservation


@dataclass(frozen=True, slots=True)
class CanonicalPort:
    canonical_name: str
    members: tuple[PortMember, ...]

    def views(self) -> tuple[ViewId, ...]:
        return tuple(sorted({member.view for member in self.members}))


@dataclass(frozen=True, slots=True)
class CanonicalComponent:
    canonical_name: str
    members: tuple[ComponentMember, ...]
    ports: tuple[CanonicalPort, ...]
    required_views: tuple[ViewId, ...] = ()

    @property
    def id(self) -> str:
        return f"component:{self.canonical_name}"

    def port_id(self, port_name: str) -> str:
        return f"{self.id}/port:{port_name}"

    def port(self, canonical_name: str) -> CanonicalPort | None:
        return next((item for item in self.ports if item.canonical_name == canonical_name), None)

    def views(self) -> tuple[ViewId, ...]:
        return tuple(sorted({member.view for member in self.members}))


@dataclass(frozen=True, slots=True)
class CanonicalDesign:
    components: tuple[CanonicalComponent, ...]
    views: tuple[ViewId, ...]

    def component(self, canonical_name: str) -> CanonicalComponent | None:
        return next(
            (item for item in self.components if item.canonical_name == canonical_name),
            None,
        )


@dataclass(frozen=True, slots=True)
class ContractPort:
    canonical_name: str
    names: Mapping[str, str] = field(default_factory=dict)
    direction: Direction = Direction.UNKNOWN
    role: PortRole = PortRole.UNKNOWN
    shape: BusShape = field(default_factory=BusShape.unknown)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "names": dict(sorted(self.names.items())),
            "direction": self.direction.value,
            "role": self.role.value,
            "shape": self.shape.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ContractComponent:
    canonical_name: str
    kind: ComponentKind = ComponentKind.UNKNOWN
    names: Mapping[str, str] = field(default_factory=dict)
    required_views: tuple[str, ...] = ()
    ports: tuple[ContractPort, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "kind": self.kind.value,
            "names": dict(sorted(self.names.items())),
            "required_views": sorted(self.required_views),
            "ports": [
                item.to_dict() for item in sorted(self.ports, key=lambda item: item.canonical_name)
            ],
        }


@dataclass(frozen=True, slots=True)
class ContractRegisterField:
    canonical_name: str
    names: Mapping[str, str] = field(default_factory=dict)
    bit_offset: int | None = None
    bit_width: int | None = None
    access: str | None = None
    reset_value: int | None = None

    def __post_init__(self) -> None:
        if not self.canonical_name:
            raise ValueError("contract register-field name must not be empty")
        if self.bit_offset is not None and self.bit_offset < 0:
            raise ValueError("contract register-field offset must not be negative")
        if self.bit_width is not None and self.bit_width < 1:
            raise ValueError("contract register-field width must be positive")
        object.__setattr__(self, "names", dict(self.names))

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "names": dict(sorted(self.names.items())),
            "bit_offset": self.bit_offset,
            "bit_width": self.bit_width,
            "access": self.access,
            "reset_value": self.reset_value,
        }


@dataclass(frozen=True, slots=True)
class ContractRegister:
    canonical_name: str
    component: str
    names: Mapping[str, str] = field(default_factory=dict)
    memory_map: str | None = None
    address_block: str | None = None
    address_offset: int | None = None
    absolute_address: int | None = None
    size_bits: int | None = None
    access: str | None = None
    fields: tuple[ContractRegisterField, ...] = ()

    def __post_init__(self) -> None:
        if not self.canonical_name or not self.component:
            raise ValueError("contract register component and name must not be empty")
        if self.address_offset is not None and self.address_offset < 0:
            raise ValueError("contract register offset must not be negative")
        if self.absolute_address is not None and self.absolute_address < 0:
            raise ValueError("contract register address must not be negative")
        if self.size_bits is not None and self.size_bits < 1:
            raise ValueError("contract register width must be positive")
        for field_name in ("memory_map", "address_block"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"contract register {field_name} must be a nonempty string")
        object.__setattr__(self, "names", dict(self.names))
        object.__setattr__(self, "fields", tuple(self.fields))

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "component": self.component,
            "names": dict(sorted(self.names.items())),
            "memory_map": self.memory_map,
            "address_block": self.address_block,
            "address_offset": self.address_offset,
            "absolute_address": self.absolute_address,
            "size_bits": self.size_bits,
            "access": self.access,
            "fields": [
                item.to_dict() for item in sorted(self.fields, key=lambda item: item.canonical_name)
            ],
        }


@dataclass(frozen=True, slots=True)
class DesignContract:
    components: tuple[ContractComponent, ...]
    schema_version: int = 1
    generated_by: str = "OpenCollate"
    registers: tuple[ContractRegister, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", tuple(self.components))
        object.__setattr__(self, "registers", tuple(self.registers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_by": self.generated_by,
            "components": [
                item.to_dict()
                for item in sorted(self.components, key=lambda item: item.canonical_name)
            ],
            "registers": [
                item.to_dict()
                for item in sorted(
                    self.registers,
                    key=lambda item: (
                        item.component,
                        item.memory_map or "",
                        item.address_block or "",
                        item.canonical_name,
                    ),
                )
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DesignContract:
        schema_version = data.get("schema_version", 1)
        if type(schema_version) is not int:
            raise TypeError("schema_version must be an integer")
        if schema_version != 1:
            raise ValueError(
                f"unsupported schema_version {schema_version}; this release supports 1"
            )
        generated_by = data.get("generated_by", "OpenCollate")
        if not isinstance(generated_by, str):
            raise TypeError("generated_by must be a string")
        raw_components = data.get("components", ())
        if not isinstance(raw_components, (list, tuple)):
            raise TypeError("components must be an array")
        components: list[ContractComponent] = []
        for component_index, raw_component in enumerate(raw_components):
            component_path = f"components[{component_index}]"
            if not isinstance(raw_component, Mapping):
                raise TypeError(f"{component_path} must be an object")
            canonical_component = raw_component.get("canonical_name")
            if not isinstance(canonical_component, str) or not canonical_component:
                raise TypeError(f"{component_path}.canonical_name must be a nonempty string")
            raw_component_names = raw_component.get("names", {})
            if not isinstance(raw_component_names, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in raw_component_names.items()
            ):
                raise TypeError(f"{component_path}.names must map strings to strings")
            raw_required_views = raw_component.get("required_views", ())
            if not isinstance(raw_required_views, (list, tuple)) or not all(
                isinstance(item, str) for item in raw_required_views
            ):
                raise TypeError(f"{component_path}.required_views must be an array of strings")
            raw_kind = raw_component.get("kind")
            kind = ComponentKind.parse(raw_kind)
            if raw_kind is not None and kind == ComponentKind.UNKNOWN and raw_kind != "unknown":
                raise ValueError(f"{component_path}.kind is invalid: {raw_kind!r}")
            raw_ports = raw_component.get("ports", ())
            if not isinstance(raw_ports, (list, tuple)):
                raise TypeError(f"{component_path}.ports must be an array")
            ports: list[ContractPort] = []
            for port_index, raw_port in enumerate(raw_ports):
                port_path = f"{component_path}.ports[{port_index}]"
                if not isinstance(raw_port, Mapping):
                    raise TypeError(f"{port_path} must be an object")
                canonical_port = raw_port.get("canonical_name")
                if not isinstance(canonical_port, str) or not canonical_port:
                    raise TypeError(f"{port_path}.canonical_name must be a nonempty string")
                raw_port_names = raw_port.get("names", {})
                if not isinstance(raw_port_names, Mapping) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in raw_port_names.items()
                ):
                    raise TypeError(f"{port_path}.names must map strings to strings")
                raw_shape = raw_port.get("shape", {})
                if not isinstance(raw_shape, Mapping):
                    raise TypeError(f"{port_path}.shape must be an object")
                raw_packed = raw_shape.get("packed", ())
                raw_unpacked = raw_shape.get("unpacked", ())
                if not isinstance(raw_packed, (list, tuple)):
                    raise TypeError(f"{port_path}.shape.packed must be an array")
                if not isinstance(raw_unpacked, (list, tuple)):
                    raise TypeError(f"{port_path}.shape.unpacked must be an array")

                def parse_range(item: Any, dimension_path: str) -> IndexRange:
                    if not isinstance(item, Mapping):
                        raise TypeError(f"{dimension_path} must be an object")
                    left = item.get("left")
                    right = item.get("right")
                    if type(left) is not int or type(right) is not int:
                        raise TypeError(f"{dimension_path} bounds must be integers")
                    return IndexRange(left, right)

                packed = tuple(
                    parse_range(item, f"{port_path}.shape.packed[{index}]")
                    for index, item in enumerate(raw_packed)
                )
                unpacked = tuple(
                    parse_range(item, f"{port_path}.shape.unpacked[{index}]")
                    for index, item in enumerate(raw_unpacked)
                )
                raw_direction = raw_port.get("direction")
                direction = Direction.parse(raw_direction)
                if (
                    raw_direction is not None
                    and direction == Direction.UNKNOWN
                    and raw_direction != "unknown"
                ):
                    raise ValueError(f"{port_path}.direction is invalid: {raw_direction!r}")
                raw_role = raw_port.get("role")
                role = PortRole.parse(raw_role)
                if raw_role is not None and role == PortRole.UNKNOWN and raw_role != "unknown":
                    raise ValueError(f"{port_path}.role is invalid: {raw_role!r}")
                ports.append(
                    ContractPort(
                        canonical_name=canonical_port,
                        names=dict(raw_port_names),
                        direction=direction,
                        role=role,
                        shape=BusShape(
                            width=raw_shape.get("width"),
                            left=raw_shape.get("left"),
                            right=raw_shape.get("right"),
                            ascending=raw_shape.get("ascending"),
                            packed=packed,
                            unpacked=unpacked,
                            bit_indices=tuple(raw_shape.get("bit_indices", ())),
                            explicit_scalar=raw_shape.get("explicit_scalar"),
                        ),
                    )
                )
            components.append(
                ContractComponent(
                    canonical_name=canonical_component,
                    kind=kind,
                    names=dict(raw_component_names),
                    required_views=tuple(raw_required_views),
                    ports=tuple(ports),
                )
            )

        raw_registers = data.get("registers", ())
        if not isinstance(raw_registers, (list, tuple)):
            raise TypeError("registers must be an array")
        registers: list[ContractRegister] = []
        for register_index, raw_register in enumerate(raw_registers):
            register_path = f"registers[{register_index}]"
            if not isinstance(raw_register, Mapping):
                raise TypeError(f"{register_path} must be an object")
            canonical_register = raw_register.get("canonical_name")
            component = raw_register.get("component")
            if not isinstance(canonical_register, str) or not canonical_register:
                raise TypeError(f"{register_path}.canonical_name must be a nonempty string")
            if not isinstance(component, str) or not component:
                raise TypeError(f"{register_path}.component must be a nonempty string")
            raw_names = raw_register.get("names", {})
            if not isinstance(raw_names, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in raw_names.items()
            ):
                raise TypeError(f"{register_path}.names must map strings to strings")

            def optional_integer(source: Mapping[str, Any], key: str, where: str) -> int | None:
                value = source.get(key)
                if value is not None and type(value) is not int:
                    raise TypeError(f"{where}.{key} must be an integer or null")
                return value

            raw_fields = raw_register.get("fields", ())
            if not isinstance(raw_fields, (list, tuple)):
                raise TypeError(f"{register_path}.fields must be an array")
            register_fields: list[ContractRegisterField] = []
            for field_index, raw_field in enumerate(raw_fields):
                field_path = f"{register_path}.fields[{field_index}]"
                if not isinstance(raw_field, Mapping):
                    raise TypeError(f"{field_path} must be an object")
                canonical_field = raw_field.get("canonical_name")
                if not isinstance(canonical_field, str) or not canonical_field:
                    raise TypeError(f"{field_path}.canonical_name must be a nonempty string")
                raw_field_names = raw_field.get("names", {})
                if not isinstance(raw_field_names, Mapping) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in raw_field_names.items()
                ):
                    raise TypeError(f"{field_path}.names must map strings to strings")
                field_access = raw_field.get("access")
                if field_access is not None and not isinstance(field_access, str):
                    raise TypeError(f"{field_path}.access must be a string or null")
                register_fields.append(
                    ContractRegisterField(
                        canonical_name=canonical_field,
                        names=dict(raw_field_names),
                        bit_offset=optional_integer(raw_field, "bit_offset", field_path),
                        bit_width=optional_integer(raw_field, "bit_width", field_path),
                        access=field_access,
                        reset_value=optional_integer(raw_field, "reset_value", field_path),
                    )
                )
            register_access = raw_register.get("access")
            if register_access is not None and not isinstance(register_access, str):
                raise TypeError(f"{register_path}.access must be a string or null")
            memory_map = raw_register.get("memory_map")
            address_block = raw_register.get("address_block")
            if memory_map is not None and not isinstance(memory_map, str):
                raise TypeError(f"{register_path}.memory_map must be a string or null")
            if address_block is not None and not isinstance(address_block, str):
                raise TypeError(f"{register_path}.address_block must be a string or null")
            registers.append(
                ContractRegister(
                    canonical_name=canonical_register,
                    component=component,
                    names=dict(raw_names),
                    memory_map=memory_map,
                    address_block=address_block,
                    address_offset=optional_integer(raw_register, "address_offset", register_path),
                    absolute_address=optional_integer(
                        raw_register, "absolute_address", register_path
                    ),
                    size_bits=optional_integer(raw_register, "size_bits", register_path),
                    access=register_access,
                    fields=tuple(register_fields),
                )
            )
        return cls(
            tuple(components),
            schema_version=schema_version,
            generated_by=generated_by,
            registers=tuple(registers),
        )


def decoded_identifier(name: str) -> str:
    """Decode Verilog escaped-identifier delimiters without altering its content."""

    stripped = name.strip()
    if stripped.startswith("\\"):
        return stripped[1:].rstrip()
    return stripped


def choose_provenance(items: Iterable[Provenance | None]) -> Provenance | None:
    """Select a deterministic primary location from an evidence collection."""

    present = [item for item in items if item is not None]
    if not present:
        return None
    return min(
        present,
        key=lambda item: (
            str(Path(item.source)),
            item.line,
            item.column,
            str(item.view),
        ),
    )


__all__ = [
    "BusShape",
    "CanonicalComponent",
    "CanonicalDesign",
    "CanonicalPort",
    "ClockObservation",
    "ComponentKind",
    "ComponentMember",
    "ComponentObservation",
    "ContractComponent",
    "ContractPort",
    "ContractRegister",
    "ContractRegisterField",
    "DesignContract",
    "DesignObjectObservation",
    "Direction",
    "Fact",
    "FactState",
    "IndexRange",
    "InterfaceObservation",
    "PinMappingObservation",
    "PortMember",
    "PortObservation",
    "PortRole",
    "Provenance",
    "RegisterFieldObservation",
    "RegisterObservation",
    "SourceSpan",
    "ViewId",
    "ViewObservation",
    "choose_provenance",
    "decoded_identifier",
]

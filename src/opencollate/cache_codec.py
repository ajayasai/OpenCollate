"""Bounded, allowlisted JSON serialization of parser observations (never pickle).

This is an internal cache format, not a public interchange format. Unknown types,
fields, invalid annotations, duplicates and oversized trees are cache misses.
"""

from __future__ import annotations

import json
import math
import types
from collections.abc import Mapping
from dataclasses import fields
from enum import Enum
from functools import cache
from typing import Any, Union, get_args, get_origin, get_type_hints

from opencollate import boolean, diagnostics, model
from opencollate.parsers.sdc import TimingConstraintObservation

MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_CACHE_DEPTH = 80
MAX_CACHE_NODES = 1_000_000
_RECORDS = (
    TimingConstraintObservation,
    model.ViewId,
    model.SourceSpan,
    model.Provenance,
    model.IndexRange,
    model.BusShape,
    model.PortObservation,
    model.ComponentObservation,
    model.PinMappingObservation,
    model.DesignObjectObservation,
    model.ClockObservation,
    model.InterfaceObservation,
    model.RegisterFieldObservation,
    model.RegisterObservation,
    model.ConnectivityEndpoint,
    model.ConnectivityEdge,
    model.ConnectivityRequirement,
    model.ViewObservation,
    diagnostics.Diagnostic,
    diagnostics.DiagnosticObject,
    diagnostics.DiagnosticEvidence,
    boolean.BoolConst,
    boolean.BoolVar,
    boolean.BoolNot,
    boolean.BoolAnd,
    boolean.BoolOr,
    boolean.BoolXor,
)
_ENUMS = (
    model.FactState,
    model.Direction,
    model.PortRole,
    model.ComponentKind,
    model.ConnectivityExpectation,
    model.ConnectivityTransform,
    diagnostics.Severity,
)
_TYPES: dict[str, Any] = {cls.__name__: cls for cls in (*_RECORDS, *_ENUMS)}


class CacheCodecError(ValueError):
    """The cached observation cannot be safely reconstructed."""


class _Budget:
    def __init__(self) -> None:
        self.nodes = 0

    def visit(self, depth: int) -> None:
        self.nodes += 1
        if depth > MAX_CACHE_DEPTH or self.nodes > MAX_CACHE_NODES:
            raise CacheCodecError("cache object exceeds depth/node limits")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _encode(value: Any, budget: _Budget, depth: int) -> Any:
    budget.visit(depth)
    cls = type(value)
    if value is None or cls in (str, int, bool):
        return value
    if cls is float:
        if not math.isfinite(value):
            raise CacheCodecError("cache contains nonfinite number")
        return value
    if cls in _ENUMS:
        return {"t": cls.__name__, "v": value.value}
    if cls in _RECORDS:
        return {
            "t": cls.__name__,
            "v": {
                item.name: _encode(getattr(value, item.name), budget, depth + 1)
                for item in fields(value)
            },
        }
    if cls in (tuple, list, frozenset, set):
        items = [_encode(item, budget, depth + 1) for item in value]
        if cls in (frozenset, set):
            items.sort(key=canonical_bytes)
        return {"t": cls.__name__, "v": items}
    if isinstance(value, Mapping):
        if not all(type(key) is str for key in value):
            raise CacheCodecError("cache mapping keys must be strings")
        return {
            "t": "dict",
            "v": {key: _encode(item, budget, depth + 1) for key, item in value.items()},
        }
    raise CacheCodecError(f"cache cannot encode {cls.__name__}")


def _matches(value: Any, hint: Any) -> bool:
    if hint is Any:
        return True
    origin, args = get_origin(hint), get_args(hint)
    if origin in (Union, types.UnionType):
        return any(_matches(value, arg) for arg in args)
    if origin is tuple:
        if type(value) is not tuple:
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches(item, args[0]) for item in value)
        return len(value) == len(args) and all(
            _matches(item, arg) for item, arg in zip(value, args, strict=True)
        )
    if origin in (dict, Mapping):
        return type(value) is dict and all(
            _matches(key, args[0]) and _matches(item, args[1]) for key, item in value.items()
        )
    if origin in (set, frozenset, list):
        return type(value) is origin and all(_matches(item, args[0]) for item in value)
    if hint in (str, int, float, bool, type(None)):
        # Float annotations also permit integers, but never bool masquerading as int.
        return type(value) is hint or (hint is float and type(value) is int)
    return isinstance(hint, type) and isinstance(value, hint)


@cache
def _hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls)


def _decode(value: Any, budget: _Budget, depth: int) -> Any:
    budget.visit(depth)
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if not isinstance(value, dict) or set(value) != {"t", "v"}:
        raise CacheCodecError("invalid cache value envelope")
    tag, payload = value["t"], value["v"]
    if type(tag) is not str:
        raise CacheCodecError("cache type tag must be a string")
    if tag in {"tuple", "list", "set", "frozenset"}:
        if not isinstance(payload, list):
            raise CacheCodecError("invalid cache sequence")
        items = [_decode(item, budget, depth + 1) for item in payload]
        constructors = {"tuple": tuple, "list": list, "set": set, "frozenset": frozenset}
        result = constructors[tag](items)
        if tag in {"set", "frozenset"} and len(result) != len(items):
            raise CacheCodecError("duplicate cache set member")
        return result
    if tag == "dict":
        if not isinstance(payload, dict):
            raise CacheCodecError("invalid cache mapping")
        return {key: _decode(item, budget, depth + 1) for key, item in payload.items()}
    cls = _TYPES.get(tag)
    if cls is None:
        raise CacheCodecError(f"unrecognized cache type: {tag}")
    if issubclass(cls, Enum):
        if type(payload) is not str:
            raise CacheCodecError("invalid enum cache value")
        return cls(payload)
    if not isinstance(payload, dict) or set(payload) != {item.name for item in fields(cls)}:
        raise CacheCodecError(f"wrong fields for cache type {tag}")
    decoded = {key: _decode(item, budget, depth + 1) for key, item in payload.items()}
    for name, hint in _hints(cls).items():
        if name in decoded and not _matches(decoded[name], hint):
            raise CacheCodecError(f"invalid cached {tag}.{name}")
    record = cls(**decoded)
    if isinstance(record, model.ViewObservation) and not all(
        isinstance(item, diagnostics.Diagnostic) for item in record.diagnostics
    ):
        raise CacheCodecError("cache diagnostics must be Diagnostic records")
    return record


def encode_observation(value: model.ViewObservation) -> Any:
    if type(value) is not model.ViewObservation:
        raise CacheCodecError("expected ViewObservation")
    return _encode(value, _Budget(), 0)


def decode_observation(value: Any) -> model.ViewObservation:
    try:
        result = _decode(value, _Budget(), 0)
        if type(result) is not model.ViewObservation:
            raise CacheCodecError("cache root is not a ViewObservation")
        # Reject constructors silently normalizing malformed cached values.
        if encode_observation(result) != value:
            raise CacheCodecError("cache record is not a canonical round trip")
        return result
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
        raise CacheCodecError(str(error)) from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CacheCodecError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise CacheCodecError(f"nonfinite JSON number: {value}")


def read_cache_json(data: bytes) -> Any:
    if len(data) > MAX_CACHE_BYTES:
        raise CacheCodecError("cache entry exceeds byte limit")
    try:
        text = data.decode("utf-8")
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > 2 * MAX_CACHE_DEPTH + 8:
                    raise CacheCodecError("cache JSON exceeds nesting limit")
            elif char in "]}":
                depth -= 1
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise CacheCodecError(str(error)) from error

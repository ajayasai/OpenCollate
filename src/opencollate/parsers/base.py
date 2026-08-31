"""Shared parser contracts and source-handling helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from opencollate.diagnostics import Diagnostic, Severity
from opencollate.model import (
    FactState,
    PortRole,
    Provenance,
    ViewId,
    ViewObservation,
)

Pathish = str | Path


class UnsupportedFormatError(ValueError):
    """Raised when no parser is registered for a requested format."""


class ParserDependencyError(RuntimeError):
    """Raised internally when an optional parser backend is unavailable."""


@dataclass(frozen=True, slots=True)
class SourceText:
    path: Path
    text: str
    encoding: str
    diagnostics: tuple[Diagnostic, ...] = ()
    tainted: bool = False


@runtime_checkable
class ViewParser(Protocol):
    """Structural protocol implemented by parser adapter objects."""

    format_name: str

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation: ...


def coerce_paths(paths: Pathish | Sequence[Pathish]) -> tuple[Path, ...]:
    result: tuple[Path, ...]
    if isinstance(paths, (str, Path)):
        result = (Path(paths),)
    else:
        result = tuple(Path(path) for path in paths)
    if not result:
        raise ValueError("at least one input path is required")
    return result


def coerce_view(
    view_id: ViewId | str | None,
    *,
    kind: str,
    name: str = "default",
) -> ViewId:
    if view_id is None:
        return ViewId(kind, name)
    parsed = ViewId.parse(view_id)
    if parsed.kind == "unknown":
        return ViewId(kind, parsed.name)
    return parsed


def provenance(
    path: Pathish,
    view: ViewId,
    *,
    line: int = 1,
    column: int = 1,
    raw_name: str | None = None,
) -> Provenance:
    return Provenance(
        source=str(Path(path)),
        line=max(1, int(line)),
        column=max(1, int(column)),
        view=view,
        raw_name=raw_name,
    )


def parser_diagnostic(
    code: str,
    severity: Severity | str,
    message: str,
    *,
    location: Provenance | None = None,
    help: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Diagnostic:
    return Diagnostic.from_rule(
        code=code,
        severity=Severity.parse(severity),
        message=message,
        provenance=location,
        help=help,
        metadata={} if metadata is None else metadata,
    )


def infer_role_from_name(name: str) -> tuple[PortRole, FactState]:
    """Conservative role hinting for formats without explicit role metadata.

    A name-derived role is always tainted.  Checkers can display it as useful
    context but must not treat it like an explicit Liberty/LEF/CSV declaration.
    """

    normalized = name.strip("\\").lower().replace("-", "_")
    segments = tuple(part for part in normalized.replace("/", "_").split("_") if part)
    if normalized in {"vss", "gnd", "vssa", "vssd", "vssio", "vgnd"} or (
        segments and segments[0] in {"vss", "gnd"}
    ):
        return PortRole.GROUND, FactState.TAINTED
    if normalized in {"vdd", "vcc", "vdda", "vddd", "vddio", "vpwr"} or (
        segments and segments[0] in {"vdd", "vcc", "vpwr"}
    ):
        return PortRole.POWER, FactState.TAINTED
    if normalized in {"clk", "clock"} or "clk" in segments or "clock" in segments:
        return PortRole.CLOCK, FactState.TAINTED
    if normalized in {"rst", "reset", "rstn", "resetn"} or any(
        part in {"rst", "reset", "rstn", "resetn"} for part in segments
    ):
        return PortRole.RESET, FactState.TAINTED
    return PortRole.UNKNOWN, FactState.UNKNOWN


def read_source(
    path: Pathish,
    view: ViewId,
    *,
    max_bytes: int | None = None,
) -> SourceText:
    """Read one text source, optionally through a hard byte ceiling.

    The bounded form reads at most one byte beyond the ceiling, so a file that
    changes between metadata inspection and opening cannot bypass a parser's
    advertised resource limit.
    """

    source_path = Path(path)
    try:
        if max_bytes is None:
            data = source_path.read_bytes()
        else:
            if max_bytes < 1:
                raise ValueError("max_bytes must be positive")
            with source_path.open("rb") as handle:
                data = handle.read(max_bytes + 1)
    except OSError as error:
        diagnostic = parser_diagnostic(
            "OC1002",
            Severity.FATAL,
            f"Cannot read {source_path}: {error}",
            location=provenance(source_path, view),
        )
        return SourceText(source_path, "", "unreadable", (diagnostic,), True)

    if max_bytes is not None and len(data) > max_bytes:
        diagnostic = parser_diagnostic(
            "OC1101",
            Severity.FATAL,
            f"Cannot read {source_path}: source exceeds the {max_bytes:,}-byte limit",
            location=provenance(source_path, view),
        )
        return SourceText(source_path, "", "over-limit", (diagnostic,), True)

    try:
        return SourceText(source_path, data.decode("utf-8-sig"), "utf-8-sig")
    except UnicodeDecodeError as error:
        # Latin-1 is a lossless byte-to-codepoint fallback.  Marking the source
        # tainted prevents the fallback from silently becoming canonical truth.
        diagnostic = parser_diagnostic(
            "OC1104",
            Severity.WARNING,
            (
                f"{source_path} is not valid UTF-8 near byte {error.start}; "
                "decoded as Latin-1 and marked tainted"
            ),
            location=provenance(source_path, view),
            help="Convert collateral to UTF-8 to make identifier spelling portable.",
        )
        return SourceText(
            source_path,
            data.decode("latin-1"),
            "latin-1",
            (diagnostic,),
            True,
        )


def unavailable_view(
    *,
    view: ViewId,
    paths: Sequence[Path],
    code: str,
    message: str,
    help: str | None = None,
) -> ViewObservation:
    location = provenance(paths[0], view) if paths else None
    diagnostic = parser_diagnostic(
        code,
        Severity.FATAL,
        message,
        location=location,
        help=help,
    )
    return ViewObservation(
        view=view,
        diagnostics=(diagnostic,),
        complete=False,
        tainted_scopes=frozenset(("*",)),
        attributes={"parser_state": FactState.UNSUPPORTED.value},
    )


__all__ = [
    "ParserDependencyError",
    "Pathish",
    "SourceText",
    "UnsupportedFormatError",
    "ViewParser",
    "coerce_paths",
    "coerce_view",
    "infer_role_from_name",
    "parser_diagnostic",
    "provenance",
    "read_source",
    "unavailable_view",
]

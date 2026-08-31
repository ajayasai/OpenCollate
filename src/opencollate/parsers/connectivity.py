"""Bounded, non-executing CSV connectivity-intent importer."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from opencollate.diagnostics import Diagnostic, Severity
from opencollate.model import (
    ConnectivityExpectation,
    ConnectivityRequirement,
    ConnectivityTransform,
    FactState,
    Provenance,
    ViewId,
    ViewObservation,
)
from opencollate.parsers.base import (
    Pathish,
    coerce_paths,
    coerce_view,
    parser_diagnostic,
    read_source,
)

_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_ROWS = 250_000
_MAX_COLUMNS = 32
_MAX_FIELD_CHARACTERS = 65_536
_MAX_SELECTOR_CHARACTERS = 4_096
_MAX_LIST_SELECTORS = 64


def _header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")


_HEADER_ALIASES = {
    "id": "id",
    "identifier": "id",
    "name": "id",
    "source": "source",
    "from": "source",
    "driver": "source",
    "sink": "sink",
    "to": "sink",
    "receiver": "sink",
    "expect": "expect",
    "expectation": "expect",
    "mode": "expect",
    "transform": "transform",
    "through": "through",
    "waypoints": "through",
    "exclude": "exclude",
    "avoid": "exclude",
    "description": "description",
    "comment": "description",
}

_SELECTION = re.compile(r"(?:\[(?:\*|[+-]?\d+|[+-]?\d+\s*:\s*[+-]?\d+)\])?$")
_HIERARCHY_SEGMENT = re.compile(r"^[^\[\]]+(?:\[[+-]?\d+\])*$")


def _valid_selector(value: str) -> bool:
    if not value or len(value) > _MAX_SELECTOR_CHARACTERS:
        return False
    if any(ord(character) < 0x20 or character.isspace() for character in value):
        return False
    match = _SELECTION.search(value)
    if match is None:
        return False
    base = value[: match.start()] if match.group() else value
    if not base or base.startswith("/") or base.endswith("/") or "//" in base:
        return False
    if ";" in base:
        return False
    segments = base.split("/")
    return all(
        segment not in {"", ".", ".."} and _HIERARCHY_SEGMENT.fullmatch(segment) is not None
        for segment in segments
    )


def _selector_list(value: str) -> tuple[str, ...] | None:
    if not value.strip():
        return ()
    result = tuple(item.strip() for item in value.split(";"))
    if len(result) > _MAX_LIST_SELECTORS or any(not _valid_selector(item) for item in result):
        return None
    return result


def _resolve_headers(
    headers: Sequence[str],
    column_map: Mapping[str, str],
) -> tuple[list[str | None], tuple[str, ...], tuple[str, ...]]:
    custom = {_header(source): _header(target) for source, target in column_map.items()}
    resolved: list[str | None] = []
    seen: set[str] = set()
    duplicate: set[str] = set()
    unknown: set[str] = set()
    for raw in headers:
        normalized = _header(raw)
        mapped = custom.get(normalized, _HEADER_ALIASES.get(normalized))
        if mapped not in set(_HEADER_ALIASES.values()):
            mapped = None
            unknown.add(raw.strip() or "<empty>")
        if mapped is not None and mapped in seen:
            duplicate.add(mapped)
        if mapped is not None:
            seen.add(mapped)
        resolved.append(mapped)
    return resolved, tuple(sorted(duplicate)), tuple(sorted(unknown))


def _diagnostic(
    code: str,
    severity: Severity,
    message: str,
    path: Path,
    view: ViewId,
    line: int = 1,
) -> Diagnostic:
    return parser_diagnostic(
        code,
        severity,
        message,
        location=Provenance(str(path), max(1, line), 1, view),
    )


def parse_connectivity_csv(
    paths: Pathish | Sequence[Pathish],
    *,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    delimiter: str = ",",
    column_map: Mapping[str, str] | None = None,
) -> ViewObservation:
    """Parse declarative static connectivity requirements from strict CSV.

    Required columns are ``id``, ``source``, ``sink``, and ``expect``.  The
    parser only records intent; it never executes expressions or regular
    expressions from the source file.
    """

    if len(delimiter) != 1 or delimiter in {'"', "\r", "\n"}:
        raise ValueError("connectivity CSV delimiter must be one safe character")
    source_paths = coerce_paths(paths)
    view = coerce_view(view_id, kind="connectivity", name=view_name)
    diagnostics: list[Diagnostic] = []
    requirements: list[ConnectivityRequirement] = []
    complete = True
    tainted_scopes: set[str] = set()
    identifiers: dict[str, int] = {}
    normalized_requirements: dict[tuple[Any, ...], int] = {}
    opposing: dict[tuple[Any, ...], tuple[ConnectivityExpectation, int]] = {}
    custom_columns = column_map or {}

    previous_field_limit = csv.field_size_limit()
    csv.field_size_limit(_MAX_FIELD_CHARACTERS)
    try:
        for path in source_paths:
            source = read_source(path, view, max_bytes=_MAX_FILE_BYTES)
            diagnostics.extend(source.diagnostics)
            if not source.text:
                complete = False
                tainted_scopes.add("*")
                continue
            reader = csv.reader(
                io.StringIO(source.text, newline=""),
                delimiter=delimiter,
                strict=True,
            )
            try:
                headers = next(reader)
            except (StopIteration, csv.Error) as error:
                diagnostics.append(
                    _diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        f"Cannot read connectivity CSV header from {path}: {error}",
                        path,
                        view,
                    )
                )
                complete = False
                tainted_scopes.add("*")
                continue
            if len(headers) > _MAX_COLUMNS:
                diagnostics.append(
                    _diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        f"Connectivity CSV {path} has more than {_MAX_COLUMNS} columns",
                        path,
                        view,
                    )
                )
                complete = False
                tainted_scopes.add("*")
                continue
            resolved, duplicate_headers, unknown_headers = _resolve_headers(headers, custom_columns)
            if duplicate_headers or unknown_headers:
                details: list[str] = []
                if duplicate_headers:
                    details.append("duplicate columns " + ", ".join(duplicate_headers))
                if unknown_headers:
                    details.append("unsupported columns " + ", ".join(unknown_headers))
                diagnostics.append(
                    _diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        f"Connectivity CSV {path} has " + "; ".join(details),
                        path,
                        view,
                    )
                )
                complete = False
                tainted_scopes.add("*")
                continue
            present = {item for item in resolved if item is not None}
            missing = sorted({"id", "source", "sink", "expect"} - present)
            if missing:
                diagnostics.append(
                    _diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        f"Connectivity CSV {path} is missing required columns: "
                        + ", ".join(missing),
                        path,
                        view,
                    )
                )
                complete = False
                tainted_scopes.add("*")
                continue

            try:
                for row_number, raw_row in enumerate(reader, start=2):
                    if row_number > _MAX_ROWS + 1:
                        diagnostics.append(
                            _diagnostic(
                                "OC1101",
                                Severity.FATAL,
                                f"Connectivity CSV {path} exceeds the {_MAX_ROWS:,}-row limit",
                                path,
                                view,
                                row_number,
                            )
                        )
                        complete = False
                        tainted_scopes.add("*")
                        break
                    if not raw_row or not any(item.strip() for item in raw_row):
                        continue
                    if len(raw_row) != len(headers):
                        diagnostics.append(
                            _diagnostic(
                                "OC1101",
                                Severity.ERROR,
                                f"Connectivity row {row_number} has {len(raw_row)} fields; "
                                f"header has {len(headers)}",
                                path,
                                view,
                                row_number,
                            )
                        )
                        complete = False
                        tainted_scopes.add("*")
                        continue
                    values = {
                        canonical: value.strip()
                        for canonical, value in zip(resolved, raw_row, strict=True)
                        if canonical is not None
                    }
                    identifier = values.get("id", "")
                    source_selector = values.get("source", "")
                    sink_selector = values.get("sink", "")
                    expectation_text = values.get("expect", "").casefold()
                    transform_text = values.get("transform", "any").casefold() or "any"
                    location = Provenance(str(path), row_number, 1, view)
                    row_errors: list[str] = []
                    if (
                        not identifier
                        or len(identifier) > 256
                        or any(ord(character) < 0x20 for character in identifier)
                    ):
                        row_errors.append("id must contain 1 to 256 printable characters")
                    if not _valid_selector(source_selector):
                        row_errors.append("source is not a valid bounded endpoint selector")
                    if not _valid_selector(sink_selector):
                        row_errors.append("sink is not a valid bounded endpoint selector")
                    try:
                        expectation = ConnectivityExpectation(expectation_text)
                    except ValueError:
                        expectation = ConnectivityExpectation.REACHABLE
                        row_errors.append("expect must be reachable or unreachable")
                    try:
                        transform = ConnectivityTransform(transform_text)
                    except ValueError:
                        transform = ConnectivityTransform.ANY
                        row_errors.append("transform must be any, identity, reverse, or inverted")
                    through = _selector_list(values.get("through", ""))
                    exclude = _selector_list(values.get("exclude", ""))
                    if through is None:
                        row_errors.append("through contains an invalid or over-limit selector list")
                    if exclude is None:
                        row_errors.append("exclude contains an invalid or over-limit selector list")
                    description = values.get("description") or None
                    if description is not None and len(description) > _MAX_FIELD_CHARACTERS:
                        row_errors.append("description exceeds the field-size limit")
                    if expectation == ConnectivityExpectation.UNREACHABLE and (
                        transform != ConnectivityTransform.ANY
                    ):
                        row_errors.append("unreachable requirements must use transform=any")
                    if row_errors:
                        diagnostics.append(
                            parser_diagnostic(
                                "OC1101",
                                Severity.ERROR,
                                f"Invalid connectivity row {row_number}: " + "; ".join(row_errors),
                                location=location,
                            )
                        )
                        complete = False
                        tainted_scopes.add("*")
                        continue

                    requirement = ConnectivityRequirement(
                        identifier=identifier,
                        source=source_selector,
                        sink=sink_selector,
                        expectation=expectation,
                        transform=transform,
                        through=through or (),
                        exclude=exclude or (),
                        description=description,
                        provenance=location,
                        status=FactState.TAINTED if source.tainted else FactState.KNOWN,
                        attributes={"row": dict(values)},
                    )
                    requirement_key = (
                        requirement.source,
                        requirement.sink,
                        requirement.transform,
                        requirement.through,
                        requirement.exclude,
                    )
                    contradiction_key = (
                        requirement.source,
                        requirement.sink,
                        requirement.through,
                        requirement.exclude,
                    )
                    conflicting_indices: set[int] = set()
                    if identifier in identifiers:
                        conflicting_indices.add(identifiers[identifier])
                    if requirement_key in normalized_requirements:
                        conflicting_indices.add(normalized_requirements[requirement_key])
                    earlier = opposing.get(contradiction_key)
                    if earlier is not None and earlier[0] != requirement.expectation:
                        conflicting_indices.add(earlier[1])
                    if conflicting_indices:
                        for index in conflicting_indices:
                            requirements[index] = replace(
                                requirements[index], status=FactState.TAINTED
                            )
                        requirement = replace(requirement, status=FactState.TAINTED)
                        diagnostics.append(
                            parser_diagnostic(
                                "OC6509",
                                Severity.ERROR,
                                f"Connectivity requirement {identifier!r} duplicates or "
                                "contradicts an earlier row",
                                location=location,
                            )
                        )
                        complete = False
                    identifiers.setdefault(identifier, len(requirements))
                    normalized_requirements.setdefault(requirement_key, len(requirements))
                    opposing.setdefault(
                        contradiction_key,
                        (requirement.expectation, len(requirements)),
                    )
                    requirements.append(requirement)
            except csv.Error as error:
                diagnostics.append(
                    _diagnostic(
                        "OC1101",
                        Severity.FATAL,
                        f"Cannot parse connectivity CSV {path}: {error}",
                        path,
                        view,
                        max(1, reader.line_num),
                    )
                )
                complete = False
                tainted_scopes.add("*")
    finally:
        csv.field_size_limit(previous_field_limit)

    return ViewObservation(
        view=view,
        diagnostics=tuple(diagnostics),
        complete=complete,
        tainted_scopes=frozenset(tainted_scopes),
        connectivity_requirements=tuple(requirements),
        attributes={
            "parser": "stdlib-csv-connectivity",
            "source_files": [str(path) for path in source_paths],
            "limits": {
                "file_bytes": _MAX_FILE_BYTES,
                "rows": _MAX_ROWS,
                "field_characters": _MAX_FIELD_CHARACTERS,
            },
        },
    )


class ConnectivityCsvParser:
    format_name = "connectivity"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        return parse_connectivity_csv(paths, view_id=view_id, **options)


__all__ = ["ConnectivityCsvParser", "parse_connectivity_csv"]

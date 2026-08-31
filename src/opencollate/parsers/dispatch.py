"""Format inference and uniform parser dispatch."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from opencollate.model import ViewId, ViewObservation
from opencollate.parsers.base import (
    Pathish,
    UnsupportedFormatError,
    ViewParser,
    coerce_paths,
)
from opencollate.parsers.cdl import CdlParser
from opencollate.parsers.cheader import CHeaderParser
from opencollate.parsers.connectivity import ConnectivityCsvParser
from opencollate.parsers.csvpins import CsvPinMapParser
from opencollate.parsers.defparser import DefParser
from opencollate.parsers.gds import GdsParser
from opencollate.parsers.ipxact import IpxactParser
from opencollate.parsers.lef import LefParser
from opencollate.parsers.liberty import LibertyParser
from opencollate.parsers.sdc import SdcParser
from opencollate.parsers.systemrdl import SystemRdlParser
from opencollate.parsers.upf import UpfParser
from opencollate.parsers.verilog import VerilogParser

_PARSERS: dict[str, ViewParser] = {
    "verilog": VerilogParser(),
    "connectivity": ConnectivityCsvParser(),
    "liberty": LibertyParser(),
    "lef": LefParser(),
    "csv": CsvPinMapParser(),
    "ipxact": IpxactParser(),
    "sdc": SdcParser(),
    "upf": UpfParser(),
    "header": CHeaderParser(),
    "cdl": CdlParser(),
    "def": DefParser(),
    "gds": GdsParser(),
    "systemrdl": SystemRdlParser(),
}

_ALIASES = {
    "v": "verilog",
    "sv": "verilog",
    "systemverilog": "verilog",
    "rtl": "verilog",
    "lib": "liberty",
    "timing": "liberty",
    "pinmap": "csv",
    "pin-map": "csv",
    "package": "csv",
    "tsv": "csv",
    "ip-xact": "ipxact",
    "ip_xact": "ipxact",
    "spirit": "ipxact",
    "c-header": "header",
    "c_header": "header",
    "cheader": "header",
    "software-header": "header",
    "spice": "cdl",
    "sp": "cdl",
    "circuit": "cdl",
    "design-exchange-format": "def",
    "gdsii": "gds",
    "gds2": "gds",
    "stream": "gds",
    "rdl": "systemrdl",
    "system-rdl": "systemrdl",
    "system_rdl": "systemrdl",
    "conn": "connectivity",
    "connectivity-spec": "connectivity",
    "connectivity_spec": "connectivity",
}

_EXTENSIONS = {
    ".v": "verilog",
    ".vh": "verilog",
    ".sv": "verilog",
    ".svh": "verilog",
    ".lib": "liberty",
    ".lef": "lef",
    ".csv": "csv",
    ".tsv": "csv",
    ".xml": "ipxact",
    ".ipxact": "ipxact",
    ".sdc": "sdc",
    ".upf": "upf",
    ".h": "header",
    ".hh": "header",
    ".hpp": "header",
    ".cdl": "cdl",
    ".cir": "cdl",
    ".ckt": "cdl",
    ".sp": "cdl",
    ".spi": "cdl",
    ".spice": "cdl",
    ".def": "def",
    ".gds": "gds",
    ".gdsii": "gds",
    ".rdl": "systemrdl",
    ".occonn": "connectivity",
}


def normalize_format(format_name: str) -> str:
    normalized = format_name.strip().lower().lstrip(".")
    normalized = _ALIASES.get(normalized, normalized)
    if normalized not in _PARSERS:
        supported = ", ".join(sorted(_PARSERS))
        raise UnsupportedFormatError(
            f"unsupported collateral format {format_name!r}; supported formats: {supported}"
        )
    return normalized


def infer_format(paths: Sequence[Path]) -> str:
    formats: set[str] = set()
    unknown: list[str] = []
    for path in paths:
        detected = _EXTENSIONS.get(path.suffix.lower())
        if detected is None:
            unknown.append(str(path))
        else:
            formats.add(detected)
    if unknown:
        raise UnsupportedFormatError("cannot infer collateral format for " + ", ".join(unknown))
    if len(formats) != 1:
        raise UnsupportedFormatError(
            "a single parser dispatch cannot mix formats: " + ", ".join(sorted(formats))
        )
    return formats.pop()


def get_parser(format_name: str) -> ViewParser:
    return _PARSERS[normalize_format(format_name)]


def parse(
    format_or_paths: str | Path | Sequence[Pathish],
    paths: Pathish | Sequence[Pathish] | None = None,
    *,
    format: str | None = None,
    view_id: ViewId | str | None = None,
    view_name: str = "default",
    **options: Any,
) -> ViewObservation:
    """Parse collateral using an explicit or inferred format.

    Both public call styles are accepted::

        parse("verilog", [Path("top.sv")], view_id="rtl.synthesis")
        parse(Path("cells.lib"), view_name="tt")
    """

    if paths is None:
        source_paths = coerce_paths(format_or_paths)
        selected_format = normalize_format(format) if format else infer_format(source_paths)
    else:
        source_paths = coerce_paths(paths)
        selected_format = normalize_format(format or str(format_or_paths))
    if selected_format in {"csv", "connectivity"} and source_paths[0].suffix.lower() == ".tsv":
        options.setdefault("delimiter", "\t")
    parser = get_parser(selected_format)
    return parser.parse(
        source_paths,
        view_id=view_id,
        view_name=view_name,
        **options,
    )


def registered_formats() -> tuple[str, ...]:
    return tuple(sorted(_PARSERS))


__all__ = [
    "get_parser",
    "infer_format",
    "normalize_format",
    "parse",
    "registered_formats",
]

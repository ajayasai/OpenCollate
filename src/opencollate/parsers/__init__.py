"""Collateral parser public API."""

from opencollate.parsers.base import (
    ParserDependencyError,
    UnsupportedFormatError,
    ViewParser,
)
from opencollate.parsers.cdl import (
    CDLParser,
    CdlParser,
    SpiceParser,
    parse_cdl,
    parse_spice,
)
from opencollate.parsers.cheader import CHeaderParser, parse_c_header
from opencollate.parsers.csvpins import CsvPinMapParser, parse_pin_csv
from opencollate.parsers.defparser import DefLimits, DefParser, parse_def
from opencollate.parsers.dispatch import (
    get_parser,
    infer_format,
    normalize_format,
    parse,
    registered_formats,
)
from opencollate.parsers.gds import (
    GDSIIParser,
    GdsiiParser,
    GDSParser,
    GdsParser,
    parse_gds,
    parse_gdsii,
)
from opencollate.parsers.ipxact import (
    IPXACTParser,
    IpxactParser,
    parse_ip_xact,
    parse_ipxact,
)
from opencollate.parsers.lef import LefParser, parse_lef
from opencollate.parsers.liberty import LibertyParser, parse_liberty
from opencollate.parsers.sdc import SdcParser, TimingConstraintObservation, parse_sdc
from opencollate.parsers.upf import UpfParser, parse_upf
from opencollate.parsers.verilog import VerilogParser, parse_verilog

__all__ = [
    "CHeaderParser",
    "CDLParser",
    "CdlParser",
    "CsvPinMapParser",
    "DefLimits",
    "DefParser",
    "GDSIIParser",
    "GDSParser",
    "GdsParser",
    "GdsiiParser",
    "IPXACTParser",
    "IpxactParser",
    "LefParser",
    "LibertyParser",
    "ParserDependencyError",
    "SdcParser",
    "SpiceParser",
    "TimingConstraintObservation",
    "UnsupportedFormatError",
    "UpfParser",
    "VerilogParser",
    "ViewParser",
    "get_parser",
    "infer_format",
    "normalize_format",
    "parse",
    "parse_c_header",
    "parse_cdl",
    "parse_def",
    "parse_gds",
    "parse_gdsii",
    "parse_ip_xact",
    "parse_ipxact",
    "parse_lef",
    "parse_liberty",
    "parse_pin_csv",
    "parse_sdc",
    "parse_spice",
    "parse_upf",
    "parse_verilog",
    "registered_formats",
]

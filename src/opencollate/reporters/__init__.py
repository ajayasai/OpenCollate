"""Deterministic report renderers."""

from opencollate.reporters.diff import (
    render_diff_json,
    render_diff_markdown,
    render_diff_sarif,
    render_diff_text,
)
from opencollate.reporters.json_report import render_json
from opencollate.reporters.markdown import render_markdown
from opencollate.reporters.sarif import render_sarif, sarif_dict
from opencollate.reporters.text import render_text

__all__ = [
    "render_diff_json",
    "render_diff_markdown",
    "render_diff_sarif",
    "render_diff_text",
    "render_json",
    "render_markdown",
    "render_sarif",
    "render_text",
    "sarif_dict",
]

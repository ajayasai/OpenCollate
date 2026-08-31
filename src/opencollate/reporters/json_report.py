"""JSON report rendering."""

from __future__ import annotations

import json

from opencollate.reporters.common import report_dict


def render_json(result: object, *, pretty: bool = True) -> str:
    """Serialize a result deterministically."""

    report = report_dict(result)
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            sort_keys=True,
        )
        + "\n"
    )

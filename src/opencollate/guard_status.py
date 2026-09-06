"""Non-clobbering watchdog status destinations, checked before worker execution."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def prepare_status_output(path: Path, command: Sequence[str]) -> Path:
    """Require a fresh destination outside the child's declared outputs.

    Protect *every* existing file, not merely inputs discoverable by the parent.
    This includes plugin-private dependencies and native HDL include files without
    executing their parsers outside the watchdog. Publication must also use an
    atomic exclusive create, to preserve files that appear after this preflight.
    """
    from opencollate.cli import _paths_alias, build_parser
    from opencollate.guard import _ALLOWED_COMMANDS

    candidate = path.expanduser().absolute()
    if candidate.exists() or candidate.is_symlink():
        raise ValueError(
            "guard status output must be a new file; refusing to overwrite an existing "
            "input, artifact, or symlink (choose a fresh per-run status filename)"
        )
    candidate = candidate.resolve()
    if not command or command[0] not in _ALLOWED_COMMANDS:
        raise ValueError("guard status requires a supported nonrecursive command")
    try:
        child = build_parser().parse_args(list(command))
    except SystemExit as error:
        raise ValueError(
            "cannot validate guard status against incomplete child arguments"
        ) from error
    # Argument parsing only: no configuration, source parser or plugin is run here.
    outputs = [getattr(child, field, None) for field in ("output", "write_report")]
    if child.command == "contract" and child.contract_command == "migrate" and not child.output:
        source = Path(child.input).expanduser().resolve()
        outputs.append(source.with_name(f"{source.stem}.v2{source.suffix}"))
    stdout_output = child.command != "contract" or child.contract_command == "diff"
    for output in outputs:
        if (
            output is not None
            and not (str(output) == "-" and stdout_output)
            and _paths_alias(candidate, output)
        ):
            raise ValueError("guard status output must not alias a guarded command output")
    for field in ("output_dir", "cache_dir"):
        directory = getattr(child, field, None)
        if directory is not None:
            root = Path(directory).expanduser().resolve()
            if candidate == root or candidate.is_relative_to(root):
                raise ValueError(
                    "guard status output must be outside generated output/cache directories"
                )
    return candidate

"""Publish complete UTF-8 artifacts without truncating the previous version."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, overwrite: bool = True) -> None:
    """Replace only after a successful write and fsync, using a private temp file.

    Abrupt process death can leave the private temporary file, but never a
    partially written replacement at the final destination. With overwrite=False,
    publish by an exclusive hard link: existing paths (including symlinks) are
    never replaced, even if they appeared after an earlier existence check. A
    filesystem without hard-link support fails closed. This is not a transaction
    across several different output paths or a directory fsync.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.oc-",
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
            temporary = None
        else:
            os.link(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)

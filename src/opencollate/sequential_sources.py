"""Manifest-bound native preprocessing for the synchronous verification frontend.

All source bytes are snapshotted before lexing. Literal includes are resolved only
against the declared inventory and redirected to preloaded SourceManager buffers.
Macro expansion and conditional compilation remain the native frontend's job.
This restricted source loader is not an OS sandbox for the native frontend.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from opencollate.sequential_ir import Circuit, SequentialError

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z", re.ASCII)
_PART = re.compile(r"[A-Za-z0-9_$.-]+\Z", re.ASCII)
_ALLOWED = frozenset(
    {
        "define",
        "undef",
        "undefineall",
        "ifdef",
        "ifndef",
        "elsif",
        "else",
        "endif",
        "include",
        "timescale",
        "default_nettype",
        "resetall",
    }
)
_FORBIDDEN = frozenset(
    {
        "include",
        "line",
        "pragma",
        "begin_keywords",
        "end_keywords",
        "protect",
        "endprotect",
        "protected",
        "endprotected",
        "celldefine",
        "endcelldefine",
        "unconnected_drive",
        "nounconnected_drive",
        "uselib",
    }
)
_MAX_TOKENS = 200_000


def _path(value: Any, *, directory: bool = False) -> str:
    if directory and value == ".":
        return "."
    if not isinstance(value, str) or not value or len(value) > 512:
        raise SequentialError("preprocess paths must be nonempty relative POSIX paths")
    parts = value.split("/")
    if any(
        not _PART.fullmatch(p)
        or p in {".", ".."}
        or p.endswith(".")
        or p.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(10)),
            *(f"LPT{i}" for i in range(10)),
        }
        for p in parts
    ):
        raise SequentialError(
            "preprocess paths must use portable relative POSIX names without '..'"
        )
    return value


def normalize_preprocess(value: Any, files: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"headers", "include_dirs", "defines"}:
        raise SequentialError("preprocess must contain only headers, include_dirs, and defines")
    if not isinstance(files, list) or not 1 <= len(files) <= 32:
        raise SequentialError("preprocessing requires 1..32 root source files")
    headers = value.get("headers", [])
    dirs = value.get("include_dirs", [])
    defines = value.get("defines", {})
    if not isinstance(headers, list) or len(headers) > 128:
        raise SequentialError("preprocess headers must be an array of at most 128 paths")
    if not isinstance(dirs, list) or len(dirs) > 32:
        raise SequentialError("preprocess include_dirs must be an array of at most 32 paths")
    headers = [_path(p) for p in headers]
    dirs = [_path(p, directory=True) for p in dirs]
    inventory = [_path(p) for p in files] + headers
    if len({p.casefold() for p in inventory}) != len(inventory) or len(set(dirs)) != len(dirs):
        raise SequentialError("duplicate or case-ambiguous preprocessing path")
    if not isinstance(defines, dict) or len(defines) > 128:
        raise SequentialError("preprocess defines must map at most 128 names to literal strings")
    clean = {}
    for key, val in defines.items():
        if (
            not isinstance(key, str)
            or len(key) > 128
            or not _NAME.fullmatch(key)
            or key in _ALLOWED | _FORBIDDEN | {"__FILE__", "__LINE__"}
            or not isinstance(val, str)
            or len(val) > 1024
            or any(ord(ch) < 32 or ord(ch) > 126 for ch in val)
            or "`" in val
            or "\\" in val
            or "//" in val
            or "/*" in val
        ):
            raise SequentialError(
                "invalid predefined macro name or replacement; "
                "use a source header for complex macros"
            )
        clean[key] = val
    return {
        "headers": sorted(headers),
        "include_dirs": dirs,
        "defines": dict(sorted(clean.items())),
    }


def declared_files(spec: dict[str, Any]) -> list[str]:
    """Every input whose contents must be protected from output publication."""
    return [*spec["files"], *spec.get("preprocess", {}).get("headers", [])]


def _snapshot(files: list[str], root: Path) -> dict[str, bytes]:
    root = root.resolve()
    result: dict[str, bytes] = {}
    paths: set[Path] = set()
    identities: set[tuple[int, int]] = set()
    total = 0
    for filename in files:
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or path in paths:
            raise SequentialError(
                "preprocessing source paths must be distinct and inside the request directory"
            )
        # O_NONBLOCK avoids blocking on a named pipe before the regular-file check.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            if not stat.S_ISREG(info.st_mode) or identity in identities:
                raise SequentialError(
                    "preprocessing inputs must be distinct regular files, not aliases"
                )
            data = stream.read(1048576 + 1)
        total += len(data)
        if len(data) > 1048576 or total > 4194304:
            raise SequentialError(
                "preprocessing source byte limit exceeded (1 MiB/file, 4 MiB total)"
            )
        data.decode("utf-8")
        if b"\0" in data:
            raise SequentialError("NUL bytes are not allowed in preprocessing sources")
        result[filename] = data
        identities.add(identity)
        paths.add(path)
    return result


def _logical_end(data: bytes, offset: int) -> int:
    while True:
        end = data.find(b"\n", offset)
        if end < 0:
            return len(data)
        last = end - 1 if end and data[end - 1] == 13 else end
        if last and data[last - 1] == 92:
            offset = end + 1
        else:
            return end


def _tokens(s: Any, data: bytes, *, remaining: int) -> list[tuple[str, str, int, int]]:
    sm = s.SourceManager()
    allocator = s.BumpAllocator()
    diagnostics = s.Diagnostics()
    lexer = s.parsing.Lexer(sm.assignText(data.decode("utf-8")), allocator, diagnostics, sm)
    result: list[tuple[str, str, int, int]] = []
    while True:
        token = lexer.lex()
        kind = token.kind.name
        if kind == "EndOfFile":
            break
        if len(result) >= remaining:
            raise SequentialError("preprocessing lexical token limit exceeded")
        text = token.rawText
        start = int(token.location.offset)
        result.append((kind, text, start, start + len(text.encode("utf-8"))))
    if any(d.isError() for d in diagnostics):
        raise SequentialError("preprocessing source contains invalid lexical tokens")
    return result


def _resolve(filename: str, literal: str, headers: set[str], dirs: list[str]) -> str:
    literal = _path(literal)
    candidates = [str(PurePosixPath(filename).parent / literal)]
    candidates.extend(str(PurePosixPath(d) / literal) for d in dirs)
    matches = sorted(set(candidates) & headers)
    if len(matches) != 1:
        raise SequentialError(
            f"{filename}: include {literal!r} must resolve to exactly one declared header; "
            f"found {matches}"
        )
    return matches[0]


def _rewrite(
    data: bytes,
    tokens: list[tuple[str, str, int, int]],
    filename: str,
    options: dict[str, Any],
    names: set[str],
    virtual: dict[str, str],
) -> bytes:
    edits = []
    macro_end = -1
    for i, (kind, text, start, end) in enumerate(tokens):
        if kind in {"MacroPaste", "MacroQuote", "MacroEscapedQuote", "MacroTripleQuote"}:
            raise SequentialError(
                "token-pasting and macro stringification are unsupported in manifest preprocessing"
            )
        if kind != "Directive":
            continue
        directive = text[1:]
        if not _NAME.fullmatch(directive):
            raise SequentialError("escaped preprocessor names are unsupported")
        if directive == "define":
            if start < macro_end:
                raise SequentialError(
                    "compiler directives inside macro replacements are unsupported"
                )
            macro_end = _logical_end(data, end)
        elif directive == "include":
            if start < macro_end:
                raise SequentialError(
                    "include directives inside macro replacements are unsupported"
                )
            if i + 1 >= len(tokens):
                raise SequentialError("include requires a literal quoted header path")
            k, literal, a, b = tokens[i + 1]
            logical_end = _logical_end(data, end)
            if (
                k != "StringLiteral"
                or a >= logical_end
                or "\\" in literal
                or not literal.startswith('"')
            ):
                raise SequentialError(
                    "include requires a literal quoted header path; "
                    "computed includes are unsupported"
                )
            if i + 2 < len(tokens) and tokens[i + 2][2] < logical_end:
                raise SequentialError("unexpected tokens after literal include")
            target = _resolve(
                filename, literal[1:-1], set(options["headers"]), options["include_dirs"]
            )
            edits.append((a, b, ('"' + virtual[target] + '"').encode("ascii")))
        elif directive in _FORBIDDEN or directive not in _ALLOWED and directive not in names:
            raise SequentialError(f"unsupported directive or undeclared macro: {text}")
        elif directive in _ALLOWED and start < macro_end:
            raise SequentialError("compiler directives inside macro replacements are unsupported")
    chunks: list[bytes] = []
    cursor = 0
    for a, b, value in edits:
        chunks.extend((data[cursor:a], value))
        cursor = b
    chunks.append(data[cursor:])
    return b"".join(chunks)


def add_preprocessed_sources(
    s: Any,
    circuit: Circuit,
    manager: Any,
    compilation: Any,
    files: list[str],
    root: Path,
    options: dict[str, Any],
) -> dict[str, str]:
    """Preload exact bounded snapshots and parse a single ordered compilation unit."""
    options = normalize_preprocess(options, files)
    data = _snapshot([*files, *options["headers"]], root)
    tokens = {}
    remaining = _MAX_TOKENS
    for filename, content in data.items():
        rows = _tokens(s, content, remaining=remaining)
        tokens[filename] = rows
        remaining -= len(rows)
    names = set(options["defines"]) | {"__LINE__"}
    for rows in tokens.values():
        for i, (kind, text, _a, _b) in enumerate(rows):
            if kind == "Directive" and text == "`define":
                if i + 1 >= len(rows) or rows[i + 1][0] != "Identifier":
                    raise SequentialError("invalid macro definition name")
                macro = rows[i + 1][1]
                if macro in _ALLOWED | _FORBIDDEN | {"__LINE__", "__FILE__"} or not _NAME.fullmatch(
                    macro
                ):
                    raise SequentialError("reserved or escaped macro definition name")
                names.add(macro)
    # Absolute, non-proximate names ensure every rewritten include hits the
    # in-memory cache directly. No user include directories reach native I/O.
    prefix = Path(root.resolve().anchor) / "__opencollate_manifest_v1__"
    virtual = {f: (prefix / f).as_posix() for f in data}
    rewritten = {
        f: _rewrite(content, tokens[f], f, options, names, virtual) for f, content in data.items()
    }
    manager.setDisableProximatePaths(True)
    manager.setDisableLocalIncludes(True)
    buffers = {
        f: manager.assignText(virtual[f], text.decode("utf-8")) for f, text in rewritten.items()
    }
    for f, content in data.items():
        circuit.sources.append(
            {"path": f, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
        )
    pp = s.parsing.PreprocessorOptions()
    pp.maxIncludeDepth = 32
    preamble_path = (prefix / "__build_defines__").as_posix()
    if preamble_path in virtual.values():
        raise SequentialError("reserved build-defines source name")
    preamble = "".join(f"`define {k} {v}\n" for k, v in options["defines"].items())
    command_buffer = manager.assignText(preamble_path, preamble)
    tree = s.syntax.SyntaxTree.fromBuffers(
        [command_buffer, *[buffers[f] for f in files]], manager, s.Bag([pp])
    )
    # Defense in depth: only macro-expansion buffers may be native-generated.
    # Every actual file buffer must contain the exact preloaded, rewritten text.
    expected = {virtual[f]: text.decode("utf-8") + "\0" for f, text in rewritten.items()}
    expected[preamble_path] = preamble + "\0"
    for buf in manager.getAllBuffers():
        if manager.getBufferKind(buf).name in {"Macro", "MacroArg"}:
            continue
        path = str(manager.getFullPath(buf)).replace("\\", "/")
        if path not in expected or manager.getSourceText(buf) != expected[path]:
            raise SequentialError(
                "native preprocessing introduced undeclared or changed source bytes"
            )
    compilation.addSyntaxTree(tree)
    return {path: f for f, path in virtual.items()}

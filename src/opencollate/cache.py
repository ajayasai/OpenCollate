"""Opt-in local parser caching with conservative dependency eligibility.

Only observations are cached: reconciliation, rules, contracts and waivers always
run again. A cache is trusted local build state, not authenticated remote evidence.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from threading import RLock

from opencollate.cache_codec import (
    MAX_CACHE_BYTES,
    CacheCodecError,
    canonical_bytes,
    decode_observation,
    encode_observation,
    read_cache_json,
)
from opencollate.config import ConfigError, SourceConfig
from opencollate.model import ViewObservation
from opencollate.parsers import UnsupportedFormatError, get_registration

_ENTRY_NAME = re.compile(r"oc1-[0-9a-f]{64}\.json")
_MAX_SOURCE_BYTES = 256 * 1024 * 1024
_MAX_SOURCE_FILES = 4096
_STATIC_FORMATS = frozenset(
    {"liberty", "lef", "csv", "ipxact", "sdc", "upf", "header", "cdl", "def", "gds", "connectivity"}
)


def implementation_digest() -> str:
    """Invalidate editable checkouts as well as published version changes."""
    root = Path(__file__).resolve().parent
    files = sorted((*root.rglob("*.py"), *root.rglob("*.json")))
    digests = []
    for path in files:
        digests.append(
            (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        )
    dependencies: dict[str, str | None] = {}
    for name in ("pyslang", "systemrdl-compiler", "antlr4-python3-runtime"):
        try:
            dependencies[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name] = None
    return hashlib.sha256(
        canonical_bytes(
            {
                "codec": 1,
                "files": digests,
                "dependencies": dependencies,
                "python": sys.version,
                "platform": platform.platform(),
            }
        )
    ).hexdigest()


class ObservationCache:
    """Private, atomic, bounded-entry cache with best-effort disk eviction.

    No package entry point is cached. Native preprocessed inputs are bypassed
    until dependency-complete cache contracts exist. Plain directive-free RTL is
    eligible; the presence of *any* backtick conservatively bypasses it.
    """

    def __init__(self, directory: Path, *, max_bytes: int = 256 * 1024 * 1024) -> None:
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2 * 1024**3:
            raise ConfigError("cache max_bytes must be between 1 and 2147483648")
        candidate = directory.expanduser().absolute()
        if candidate.is_symlink():
            raise ConfigError("cache directory must not be a symbolic link")
        try:
            candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = candidate.stat()
        except OSError as error:
            raise ConfigError(f"cannot create/read cache directory: {error}") from error
        if not stat.S_ISDIR(info.st_mode):
            raise ConfigError("cache location is not a directory")
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ConfigError("cache directory must be owned by this user with permissions 0700")
        self.directory = candidate.resolve()
        self.max_bytes = max_bytes
        self.namespace = implementation_digest()
        self._counts: Counter[str] = Counter()
        self._lock = RLock()

    def _count(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                name: self._counts[name]
                for name in (
                    "hits",
                    "misses",
                    "stores",
                    "invalid_entries",
                    "write_errors",
                    "evictions",
                    "bypassed_plugins",
                    "bypassed_dependencies",
                    "bypassed_limits",
                    "bypassed_codec",
                )
            }

    def _fingerprint(
        self, source: SourceConfig, format_name: str
    ) -> tuple[str, tuple[Path, ...]] | None:
        paths = source.expand_files()
        if len(paths) > _MAX_SOURCE_FILES:
            return None
        file_digests = []
        total = 0
        for path in paths:
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    total += len(block)
                    if total > _MAX_SOURCE_BYTES:
                        return None
                    if format_name == "verilog" and b"`" in block:
                        return None
                    hasher.update(block)
            file_digests.append((str(path), hasher.hexdigest()))
        payload = {
            "namespace": self.namespace,
            "view": str(source.view),
            "format": format_name,
            "patterns": [str(path) for path in source.files],
            "files": file_digests,
            "include_dirs": [str(path) for path in source.include_dirs],
            "defines": dict(source.defines),
            "profile": source.profile,
            "columns": dict(source.columns),
            "options": dict(source.options),
        }
        return hashlib.sha256(canonical_bytes(payload)).hexdigest(), paths

    def _load(self, key: str) -> ViewObservation | None:
        path = self.directory / f"oc1-{key}.json"
        try:
            # lstat also protects platforms that lack O_NOFOLLOW.
            if not stat.S_ISREG(path.lstat().st_mode):
                self._count("invalid_entries")
                return None
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(path, flags), "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_BYTES:
                    raise CacheCodecError("cache entry is not a bounded regular file")
                entry = read_cache_json(stream.read(MAX_CACHE_BYTES + 1))
            if not isinstance(entry, dict) or set(entry) != {
                "schema_version",
                "key",
                "sha256",
                "observation",
            }:
                raise CacheCodecError("invalid cache entry header")
            if (
                type(entry["schema_version"]) is not int
                or entry["schema_version"] != 1
                or entry["key"] != key
            ):
                raise CacheCodecError("incompatible cache entry")
            encoded = entry["observation"]
            digest = hashlib.sha256(canonical_bytes(encoded)).hexdigest()
            if entry["sha256"] != digest:
                raise CacheCodecError("cache content digest mismatch")
            return decode_observation(encoded)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, RecursionError):
            self._count("invalid_entries")
            return None

    def _make_room(self, size: int, destination: Path) -> bool:
        """Only evict cache-owned regular files, never unrelated user data.

        This is serialized within one process. Concurrent processes may briefly
        exceed the target: an OS quota is needed for a hard cross-process limit.
        """
        if size > self.max_bytes:
            return False
        entries = []
        for index, path in enumerate(self.directory.iterdir()):
            if index > _MAX_SOURCE_FILES:
                return False
            if not _ENTRY_NAME.fullmatch(path.name) or path == destination:
                continue
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode):
                    entries.append((info.st_mtime_ns, path.name, info.st_size, path))
            except FileNotFoundError:
                continue
        total = sum(item[2] for item in entries)
        for _, _, length, path in sorted(entries):
            if total + size <= self.max_bytes:
                break
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            total -= length
            self._count("evictions")
        return True

    def _store(self, key: str, observation: ViewObservation) -> None:
        try:
            encoded = encode_observation(observation)
            # Check the exact same codec path before persisting a cache entry.
            if decode_observation(encoded) != observation:
                raise CacheCodecError("observation did not round trip")
            data = canonical_bytes(
                {
                    "schema_version": 1,
                    "key": key,
                    "sha256": hashlib.sha256(canonical_bytes(encoded)).hexdigest(),
                    "observation": encoded,
                }
            )
            if len(data) > MAX_CACHE_BYTES:
                self._count("bypassed_limits")
                return
        except (CacheCodecError, TypeError, ValueError, RecursionError):
            self._count("bypassed_codec")
            return
        temporary: str | None = None
        try:
            with self._lock:
                destination = self.directory / f"oc1-{key}.json"
                if not self._make_room(len(data), destination):
                    self._count("bypassed_limits")
                    return
                with tempfile.NamedTemporaryFile(
                    dir=self.directory, prefix=".oc-write-", delete=False
                ) as stream:
                    temporary = stream.name
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                temporary = None
                self._count("stores")
        except OSError:
            self._count("write_errors")
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    self._count("write_errors")

    def parse(
        self, source: SourceConfig, parser: Callable[[SourceConfig], ViewObservation]
    ) -> ViewObservation:
        try:
            registration = get_registration(source.view.kind)
        except UnsupportedFormatError:
            return parser(source)
        if not registration.builtin:
            self._count("bypassed_plugins")
            return parser(source)
        kind = registration.format_name
        if kind not in _STATIC_FORMATS and not (
            kind == "verilog" and not source.include_dirs and not source.defines
        ):
            self._count("bypassed_dependencies")
            return parser(source)
        try:
            fingerprint = self._fingerprint(source, kind)
        except (OSError, ValueError, TypeError, RecursionError):
            # Preserve the uncached parser/configuration error semantics.
            self._count("bypassed_limits")
            return parser(source)
        if fingerprint is None:
            self._count("bypassed_dependencies" if kind == "verilog" else "bypassed_limits")
            return parser(source)
        key, paths = fingerprint
        cached = self._load(key)
        if cached is not None and cached.view == source.view:
            observation = cached
        else:
            self._count("misses")
            observation = parser(replace(source, files=paths))
        try:
            after = self._fingerprint(source, kind)
        except (OSError, ValueError) as error:
            raise ConfigError(
                f"{source.view}: inputs changed during cached analysis; rerun"
            ) from error
        if after != fingerprint:
            raise ConfigError(f"{source.view}: inputs changed during cached analysis; rerun")
        if cached is not None and cached.view == source.view:
            self._count("hits")
        else:
            self._store(key, observation)
        return observation

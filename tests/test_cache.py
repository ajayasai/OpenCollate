from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from opencollate.cache import ObservationCache, implementation_digest
from opencollate.cache_codec import (
    CacheCodecError,
    canonical_bytes,
    decode_observation,
    encode_observation,
    read_cache_json,
)
from opencollate.cli import _load_observations, _parse_source, main
from opencollate.config import ConfigError, SourceConfig, Waiver, load_config
from opencollate.diagnostics import Diagnostic
from opencollate.engine import ComparisonEngine
from opencollate.model import ViewId, ViewObservation

ROOT = Path(__file__).resolve().parents[1]


def _project(tmp_path: Path) -> Path:
    (tmp_path / "top.sv").write_text(
        "module top(input a, output y); assign y=a; endmodule\n", encoding="utf-8"
    )
    (tmp_path / "top.lib").write_text(
        "library(L) { cell(top) { pin(a) {direction: input;} "
        'pin(y) {direction: output; function: "a";} } }\n',
        encoding="utf-8",
    )
    path = tmp_path / "opencollate.toml"
    path.write_text(
        """schema_version = 1
[project]
name = "cache-regression"
[sources.rtl.default]
files = ["top.sv"]
[sources.liberty.tt]
files = ["top.lib"]
""",
        encoding="utf-8",
    )
    return path


def _source(tmp_path: Path) -> SourceConfig:
    _project(tmp_path)
    return SourceConfig(ViewId("liberty", "tt"), (tmp_path / "top.lib",))


def test_all_uart_observations_round_trip_without_lost_evidence() -> None:
    for observation in _load_observations(load_config(ROOT / "examples/uart/opencollate.toml")):
        assert (
            decode_observation(read_cache_json(canonical_bytes(encode_observation(observation))))
            == observation
        )


def test_cli_cold_warm_and_uncached_results_are_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _project(tmp_path)
    assert main(["check", str(config), "--format", "json"]) == 0
    plain = capsys.readouterr().out
    args = [
        "check",
        str(config),
        "--format",
        "json",
        "--jobs",
        "2",
        "--cache-dir",
        str(tmp_path / "cache"),
        "--cache-stats",
    ]
    assert main(args) == 0
    cold = capsys.readouterr()
    assert cold.out == plain
    assert json.loads(cold.err)["cache"]["stores"] == 2
    assert main(args) == 0
    warm = capsys.readouterr()
    assert warm.out == plain
    assert json.loads(warm.err)["cache"]["hits"] == 2


def test_unchanged_size_and_timestamp_cannot_hide_changed_file(tmp_path: Path) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")
    original = cache.parse(source, _parse_source)
    path = source.files[0]
    stamp = path.stat()
    path.write_text(path.read_text().replace('function: "a"', 'function: "0"'), encoding="utf-8")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    changed = cache.parse(source, _parse_source)
    assert changed != original
    assert cache.stats()["misses"] == 2
    assert changed == _parse_source(source)


def test_glob_addition_removal_and_order_are_keyed(tmp_path: Path) -> None:
    _project(tmp_path)
    source = SourceConfig(ViewId("liberty"), (tmp_path / "*.lib",))
    cache = ObservationCache(tmp_path / "cache")
    first = cache.parse(source, _parse_source)
    extra = tmp_path / "second.lib"
    extra.write_text("library(L) {cell(extra) {pin(x) {direction: input;}}}", encoding="utf-8")
    second = cache.parse(source, _parse_source)
    assert len(second.components) == len(first.components) + 1
    extra.unlink()
    assert cache.parse(source, _parse_source) == first
    assert cache.stats()["hits"] == 1


def test_include_and_macro_sources_are_never_cached(tmp_path: Path) -> None:
    path = tmp_path / "top.sv"
    header = tmp_path / "width.vh"
    path.write_text(
        '`include "width.vh"\nmodule top(input [`W-1:0] a); endmodule', encoding="utf-8"
    )
    header.write_text("`define W 4\n", encoding="utf-8")
    source = SourceConfig(ViewId("rtl"), (path,), include_dirs=(tmp_path,))
    cache = ObservationCache(tmp_path / "cache")
    first = cache.parse(source, _parse_source)
    header.write_text("`define W 8\n", encoding="utf-8")
    second = cache.parse(source, _parse_source)
    assert first.components[0].ports[0].shape.width == 4
    assert second.components[0].ports[0].shape.width == 8
    assert cache.stats()["bypassed_dependencies"] == 2
    assert cache.stats()["hits"] == 0
    # Even without include_dirs, a literal directive requires an uncached parse.
    assert cache.parse(replace(source, include_dirs=()), _parse_source) == _parse_source(
        replace(source, include_dirs=())
    )
    assert cache.stats()["hits"] == 0


def test_source_options_and_view_identity_invalidate_cache(tmp_path: Path) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")
    calls = []

    def parse(item: SourceConfig) -> ViewObservation:
        calls.append(item)
        return ViewObservation(item.view, attributes=dict(item.options))

    for configured in (
        source,
        replace(source, view=ViewId("liberty", "ss")),
        replace(source, options={"meaning": 1}),
        replace(source, columns={"a": "b"}),
        replace(source, profile="new"),
        replace(source, defines={"X": "1"}),
    ):
        cache.parse(configured, parse)
    assert len(calls) == 6
    cache.parse(source, parse)
    assert len(calls) == 6


def test_namespace_changes_invalidate_cached_results(tmp_path: Path) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")
    cache.parse(source, _parse_source)
    cache.namespace = "changed-code-or-dependency"
    cache.parse(source, _parse_source)
    assert cache.stats()["misses"] == 2
    assert implementation_digest() == implementation_digest()


def test_file_changes_during_parse_cannot_publish_or_pass(tmp_path: Path) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")

    def unstable(item: SourceConfig) -> ViewObservation:
        result = _parse_source(item)
        item.files[0].write_text("changed during parse", encoding="utf-8")
        return result

    with pytest.raises(ConfigError, match="inputs changed"):
        cache.parse(source, unstable)
    assert not list(cache.directory.glob("oc1-*.json"))


def test_glob_changes_during_parse_are_rejected(tmp_path: Path) -> None:
    _project(tmp_path)
    source = SourceConfig(ViewId("liberty"), (tmp_path / "*.lib",))
    cache = ObservationCache(tmp_path / "cache")

    def unstable(item: SourceConfig) -> ViewObservation:
        result = _parse_source(item)
        (tmp_path / "new.lib").write_text("library(new) {}", encoding="utf-8")
        return result

    with pytest.raises(ConfigError, match="inputs changed"):
        cache.parse(source, unstable)


@pytest.mark.parametrize(
    "damage",
    [
        b"{",
        b'{"schema_version":1,"schema_version":1}',
        b'{"x":NaN}',
        b"\xff",
        b"[" * 500 + b"]" * 500,
    ],
)
def test_corrupt_entries_are_misses_not_success(tmp_path: Path, damage: bytes) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")
    first = cache.parse(source, _parse_source)
    entry = next(cache.directory.glob("oc1-*.json"))
    entry.write_bytes(damage)
    assert cache.parse(source, _parse_source) == first
    assert cache.stats()["invalid_entries"] == 1
    assert cache.stats()["hits"] == 0


@pytest.mark.parametrize(
    "field,value",
    [("key", "wrong"), ("sha256", "0" * 64), ("schema_version", True), ("schema_version", 2)],
)
def test_invalid_entry_headers_are_misses(tmp_path: Path, field: str, value: object) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")
    first = cache.parse(source, _parse_source)
    path = next(cache.directory.glob("oc1-*.json"))
    body = json.loads(path.read_text())
    body[field] = value
    path.write_text(json.dumps(body), encoding="utf-8")
    assert cache.parse(source, _parse_source) == first
    assert cache.stats()["invalid_entries"] == 1


def test_recomputed_hash_does_not_bypass_schema_validation(tmp_path: Path) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")
    first = cache.parse(source, _parse_source)
    path = next(cache.directory.glob("oc1-*.json"))
    body = json.loads(path.read_text())
    body["observation"]["v"]["complete"] = "true"
    body["sha256"] = hashlib.sha256(canonical_bytes(body["observation"])).hexdigest()
    path.write_text(json.dumps(body), encoding="utf-8")
    assert cache.parse(source, _parse_source) == first
    assert cache.stats()["invalid_entries"] == 1


def test_cache_preserves_fatal_diagnostics_and_taint(tmp_path: Path) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")
    failure = ViewObservation(
        source.view,
        complete=False,
        tainted_scopes=frozenset({"*"}),
        diagnostics=(Diagnostic.from_rule("OC1101", "bad input"),),
    )
    assert cache.parse(source, lambda _: failure) == failure
    assert cache.parse(source, lambda _: pytest.fail("should hit cache")) == failure
    assert cache.stats()["hits"] == 1


def test_waiver_expiry_and_policy_are_recomputed_on_hits(tmp_path: Path) -> None:
    config = load_config(_project(tmp_path))
    path = tmp_path / "top.lib"
    path.write_text(path.read_text().replace('function: "a"', 'function: "0"'), encoding="utf-8")
    cache = ObservationCache(tmp_path / "cache")
    waiver = Waiver("OC4301", "temporary exception", expires=date(2026, 9, 5))
    config = replace(config, waivers=(waiver,))
    before = ComparisonEngine(config).run(
        _load_observations(config, cache=cache), today=date(2026, 9, 5)
    )
    after = ComparisonEngine(config).run(
        _load_observations(config, cache=cache), today=date(2026, 9, 6)
    )
    assert before.exit_code == 0
    assert after.exit_code == 1
    assert cache.stats()["hits"] == 2


def test_parallel_atomic_writes_never_expose_partial_entries(tmp_path: Path) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(lambda _: cache.parse(source, _parse_source), range(12)))
    assert all(item == outputs[0] for item in outputs)
    assert cache.stats()["invalid_entries"] == 0
    assert not list(cache.directory.glob(".oc-write-*"))


def test_byte_budget_evicts_only_cache_files(tmp_path: Path) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache", max_bytes=6000)
    keep = cache.directory / "keep.txt"
    keep.write_text("unrelated", encoding="utf-8")
    for index in range(6):
        cache.parse(replace(source, view=ViewId("liberty", f"v{index}")), _parse_source)
    assert keep.read_text() == "unrelated"
    assert cache.stats()["evictions"] > 0
    assert sum(path.stat().st_size for path in cache.directory.glob("oc1-*.json")) <= 6000


@pytest.mark.skipif(os.name != "posix", reason="POSIX private-directory permissions")
def test_shared_cache_directory_is_rejected(tmp_path: Path) -> None:
    cache = tmp_path / "shared"
    cache.mkdir(mode=0o755)
    with pytest.raises(ConfigError, match="0700"):
        ObservationCache(cache)


@pytest.mark.skipif(os.name != "posix", reason="symlink privilege varies on Windows")
def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")
    original = cache.parse(source, _parse_source)
    path = next(cache.directory.glob("oc1-*.json"))
    target = tmp_path / "keep.txt"
    target.write_text("do not overwrite", encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    assert cache.parse(source, _parse_source) == original
    assert target.read_text() == "do not overwrite"
    assert cache.stats()["invalid_entries"] == 1
    linked = tmp_path / "linked"
    linked.symlink_to(cache.directory, target_is_directory=True)
    with pytest.raises(ConfigError, match="symbolic link"):
        ObservationCache(linked)


def test_cache_write_errors_leave_results_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    cache = ObservationCache(tmp_path / "cache")

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("simulated full disk")

    monkeypatch.setattr("opencollate.cache.os.replace", fail)
    assert cache.parse(source, _parse_source) == _parse_source(source)
    assert cache.stats()["write_errors"] == 1
    assert not list(cache.directory.glob(".oc-write-*"))


@pytest.mark.parametrize("value", [0, -1, True, 2**32])
def test_cache_size_validation(tmp_path: Path, value: int) -> None:
    with pytest.raises(ConfigError):
        ObservationCache(tmp_path / "cache", max_bytes=value)


def test_stats_require_cache_directory(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["check", str(_project(tmp_path)), "--cache-stats"]) == 2
    assert "requires --cache-dir" in capsys.readouterr().err


@pytest.mark.parametrize(
    "payload",
    [
        {"t": "os.system", "v": "echo unsafe"},
        {"t": ["ViewObservation"], "v": {}},
        {"t": "list", "v": {}},
        {"t": "dict", "v": []},
        {"t": "ViewObservation", "v": {}},
        {"t": "FactState", "v": False},
        {"t": "frozenset", "v": ["same", "same"]},
        [],
        float("inf"),
    ],
)
def test_codec_rejects_untrusted_types_and_shapes(payload: object) -> None:
    with pytest.raises(CacheCodecError):
        decode_observation(payload)


def test_codec_never_executes_objects_or_loses_unknown_fields() -> None:
    observation = ViewObservation(ViewId("test"), attributes={"bad": object()})
    with pytest.raises(CacheCodecError):
        encode_observation(observation)
    body = encode_observation(ViewObservation(ViewId("test")))
    body["v"]["unexpected"] = True
    with pytest.raises(CacheCodecError):
        decode_observation(body)


def test_codec_rejects_nondiagnostic_items_even_under_any_annotation() -> None:
    body = encode_observation(ViewObservation(ViewId("test"), diagnostics=(None,)))
    with pytest.raises(CacheCodecError, match="diagnostics"):
        decode_observation(body)

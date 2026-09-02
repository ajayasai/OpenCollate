from __future__ import annotations

from pathlib import Path
from threading import Barrier, Lock, get_ident

import pytest

from opencollate.cli import _load_observations, build_parser
from opencollate.config import ProjectConfig, SourceConfig
from opencollate.model import ViewId, ViewObservation
from opencollate.parsers import (
    ParserPluginSpec,
    register_parser_plugin,
    unregister_parser_plugin,
)


class _SerialPluginParser:
    format_name = "serial_plugin"

    def parse(self, paths, *, view_id=None, **options):  # type: ignore[no-untyped-def]
        del paths, options
        return ViewObservation(ViewId.parse(view_id) if isinstance(view_id, str) else view_id)


def _project(tmp_path: Path, sources: tuple[SourceConfig, ...]) -> ProjectConfig:
    return ProjectConfig(
        path=tmp_path / "opencollate.toml",
        root=tmp_path,
        name="parallel-parsing-test",
        sources=sources,
    )


def test_cli_accepts_bounded_explicit_jobs() -> None:
    parser = build_parser()
    assert parser.parse_args(["check", "--jobs", "4"]).jobs == 4
    assert parser.parse_args(["review", "--jobs", "2", "--baseline", "old.json"]).jobs == 2
    assert parser.parse_args(["demo", "--jobs", "3"]).jobs == 3
    assert parser.parse_args(["contract", "build", "--jobs", "5"]).jobs == 5

    with pytest.raises(SystemExit):
        parser.parse_args(["check", "--jobs", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["check", "--jobs", "65"])


def test_load_observations_parallelizes_builtins_and_preserves_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = (
        SourceConfig(ViewId("rtl", "first"), (Path("first.sv"),)),
        SourceConfig(ViewId("liberty", "second"), (Path("second.lib"),)),
    )
    barrier = Barrier(2, timeout=5)
    thread_ids: set[int] = set()
    lock = Lock()

    def fake_parse(source: SourceConfig) -> ViewObservation:
        with lock:
            thread_ids.add(get_ident())
        barrier.wait()
        return ViewObservation(source.view)

    monkeypatch.setattr("opencollate.cli._parse_source", fake_parse)
    observations = _load_observations(_project(tmp_path, sources), jobs=2)

    assert [item.view for item in observations] == [source.view for source in sources]
    assert len(thread_ids) == 2


def test_serial_plugin_is_a_hard_barrier_between_safe_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_parser_plugin(
        ParserPluginSpec(
            parser=_SerialPluginParser(),
            name="serial-plugin-test",
            extensions=(".serial",),
        )
    )
    try:
        sources = (
            SourceConfig(ViewId("rtl", "a"), (Path("a.sv"),)),
            SourceConfig(ViewId("liberty", "b"), (Path("b.lib"),)),
            SourceConfig(ViewId("serial_plugin", "barrier"), (Path("c.serial"),)),
            SourceConfig(ViewId("lef", "d"), (Path("d.lef"),)),
            SourceConfig(ViewId("sdc", "e"), (Path("e.sdc"),)),
        )
        active_safe = 0
        unsafe_active_counts: list[int] = []
        first_barrier = Barrier(2, timeout=5)
        second_barrier = Barrier(2, timeout=5)
        lock = Lock()

        def fake_parse(source: SourceConfig) -> ViewObservation:
            nonlocal active_safe
            if source.view.kind == "serial_plugin":
                with lock:
                    unsafe_active_counts.append(active_safe)
                return ViewObservation(source.view)
            with lock:
                active_safe += 1
            try:
                if source.view.name in {"a", "b"}:
                    first_barrier.wait()
                else:
                    second_barrier.wait()
                return ViewObservation(source.view)
            finally:
                with lock:
                    active_safe -= 1

        monkeypatch.setattr("opencollate.cli._parse_source", fake_parse)
        observations = _load_observations(_project(tmp_path, sources), jobs=4)

        assert [item.view for item in observations] == [source.view for source in sources]
        assert unsafe_active_counts == [0]
    finally:
        unregister_parser_plugin("serial-plugin-test")

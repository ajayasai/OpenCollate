from __future__ import annotations

from threading import Barrier, Lock, get_ident

import pytest

from opencollate.execution import MAX_PARSE_JOBS, ordered_parallel_map, parse_job_count


def test_parse_job_count_rejects_ambiguous_or_unbounded_values() -> None:
    assert parse_job_count("4") == 4
    assert parse_job_count(MAX_PARSE_JOBS) == MAX_PARSE_JOBS
    for value in (True, 0, -1, MAX_PARSE_JOBS + 1, "auto", "1.5"):
        with pytest.raises(ValueError, match="parser jobs"):
            parse_job_count(value)


def test_parallel_map_executes_safe_items_concurrently_and_preserves_order() -> None:
    barrier = Barrier(2, timeout=5)
    thread_ids: set[int] = set()
    lock = Lock()

    def work(value: int) -> int:
        with lock:
            thread_ids.add(get_ident())
        barrier.wait()
        return value * 10

    assert ordered_parallel_map((2, 1), work, jobs=2) == (20, 10)
    assert len(thread_ids) == 2


def test_parallel_map_treats_unsafe_items_as_nonoverlapping_barriers() -> None:
    active = 0
    unsafe_saw_active: list[int] = []
    lock = Lock()

    def work(value: int) -> int:
        nonlocal active
        if value == 0:
            with lock:
                unsafe_saw_active.append(active)
            return value
        with lock:
            active += 1
        try:
            return value
        finally:
            with lock:
                active -= 1

    result = ordered_parallel_map(
        (1, 2, 0, 3, 4),
        work,
        jobs=2,
        parallel_safe=lambda value: value != 0,
    )
    assert result == (1, 2, 0, 3, 4)
    assert unsafe_saw_active == [0]


def test_parallel_map_surfaces_the_earliest_configured_failure() -> None:
    barrier = Barrier(2, timeout=5)

    def fail(value: int) -> int:
        barrier.wait()
        raise RuntimeError(f"failure-{value}")

    with pytest.raises(RuntimeError, match="failure-1"):
        ordered_parallel_map((1, 2), fail, jobs=2)


def test_parallel_map_remains_serial_by_default() -> None:
    thread_ids: list[int] = []

    def work(value: int) -> int:
        thread_ids.append(get_ident())
        return value

    assert ordered_parallel_map((1, 2, 3), work) == (1, 2, 3)
    assert thread_ids == [get_ident(), get_ident(), get_ident()]

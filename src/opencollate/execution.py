"""Deterministic bounded execution primitives.

OpenCollate keeps serial execution as the compatibility default. Callers may
opt into threads for independent parser views; result order and first-failure
order still follow configuration order, and non-parallel-safe work is a hard
barrier that never overlaps a worker block.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")

MAX_PARSE_JOBS = 64


def parse_job_count(value: str | int) -> int:
    """Validate an explicit parser worker count for CLI and library callers."""

    try:
        jobs = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("parser jobs must be an integer") from error
    if isinstance(value, bool) or not 1 <= jobs <= MAX_PARSE_JOBS:
        raise ValueError(f"parser jobs must be between 1 and {MAX_PARSE_JOBS}")
    return jobs


def _run_parallel_block(
    items: Sequence[InputT],
    function: Callable[[InputT], OutputT],
    *,
    jobs: int,
) -> list[OutputT]:
    executor = ThreadPoolExecutor(
        max_workers=min(jobs, len(items)),
        thread_name_prefix="opencollate-parser",
    )
    futures: list[Future[OutputT]] = []
    try:
        futures = [executor.submit(function, item) for item in items]
        # Resolve in source order. If several workers fail, the earliest
        # configured source remains the deterministic surfaced exception.
        return [future.result() for future in futures]
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


def ordered_parallel_map(
    items: Sequence[InputT],
    function: Callable[[InputT], OutputT],
    *,
    jobs: int = 1,
    parallel_safe: Callable[[InputT], bool] | None = None,
) -> tuple[OutputT, ...]:
    """Map while preserving order and serializing unsafe items.

    Consecutive parallel-safe items form worker blocks. Every unsafe item is a
    barrier: preceding workers finish before it starts, and the next worker
    block starts only after it completes. This lets third-party parsers remain
    serial by default while built-ins and explicitly safe plugins scale.
    """

    worker_count = parse_job_count(jobs)
    selected = tuple(items)
    if worker_count == 1 or len(selected) < 2:
        return tuple(function(item) for item in selected)
    is_safe = parallel_safe or (lambda item: True)
    results: list[OutputT] = []
    block: list[InputT] = []

    def flush() -> None:
        if not block:
            return
        results.extend(_run_parallel_block(tuple(block), function, jobs=worker_count))
        block.clear()

    for item in selected:
        if is_safe(item):
            block.append(item)
            continue
        flush()
        results.append(function(item))
    flush()
    return tuple(results)


__all__ = ["MAX_PARSE_JOBS", "ordered_parallel_map", "parse_job_count"]

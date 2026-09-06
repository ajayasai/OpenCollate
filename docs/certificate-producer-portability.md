# Windows certificate producer isolation

The Windows producer runs each SAT query in a fresh, one-shot child using the installed
Python interpreter in isolated mode. This addresses a reproduced PySAT MSVC proof-stream
buffering/lifetime defect: nontrivial UNSAT queries returned an empty proof and repeated
instances could crash the interpreter during shutdown. Merely accepting the native UNSAT
status, or skipping Windows proof tests, would not be an acceptable workaround.

Only the child loads the Universal CRT flush adapter, restricted to the system DLL directory.
It flushes proof output while its descriptor is still open and exits immediately after closing
the JSON response, avoiding the upstream stale-stream shutdown path. The host application
never globally flushes C streams. Linux/macOS retain their existing in-process producer path.
There is extra process-startup cost on Windows; this change makes no speedup claim.

The parent enforces the original obligation deadline across startup and solving, kills/reaps
an over-time worker, rejects abnormal exit codes even with plausible output, and bounds decoded
transport messages to 64 MiB. Messages are data-only JSON with duplicate keys and nonfinite
values rejected. All temporary transport files are removed on success, timeout and error.
A clean worker exit is not evidence: the parent still checks witnesses and RUP against its
own reconstructed formulas. No certificate format or logical acceptance rule is weakened.

Process isolation is not a native-memory/disk security sandbox; use OS/container limits for
hostile workloads. The child is part of the untrusted producer, not an additional trusted
proof checker. The base-package solver-free verifier is unchanged.

See [independent Boolean certificates](independent-boolean-certificates.md) for usage,
proof semantics, trust boundaries, benchmark scope and the independent DRAT-trim workflow.
The portability tests include real worker crashes and stalls, malformed/rehashed evidence,
strict transport limits, guarded mismatches, vacuity, and native stream lifetime ordering.

## Relevant upstream interfaces

- PySAT proof-stream implementation: https://github.com/pysathq/pysat/blob/master/solvers/pysolvers.cc
- Python isolated mode: https://docs.python.org/3/using/cmdline.html#cmdoption-I
- Python ctypes DLL loading: https://docs.python.org/3/library/ctypes.html
- Microsoft fflush: https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/fflush

These references explain the native interfaces; the repository's actual CI logs establish
which platform and interpreter combinations have been tested for a particular commit.

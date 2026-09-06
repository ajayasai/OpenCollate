# Incremental checks and external process supervision

This is development code after v0.3.0. It adds safer repeated checking and failure
containment, not additional sequential verification, source-language completeness,
or signoff qualification. The optional cache and process guard work together.

## Reuse observations, never verdicts

```console
opencollate check opencollate.toml --cache-dir .opencollate-cache --cache-stats
opencollate check opencollate.toml --jobs 8 --cache-dir .opencollate-cache --format html --output review.html
```

`check`, `review`, `demo`, and `contract build` accept the cache options. Without
`--cache-dir`, the original uncached behavior is retained. Cache counters are
written to stderr, leaving JSON/SARIF/HTML stdout intact. `--cache-stats` requires
`--cache-dir`. Cache files are internal build state, not a public interchange format.

Keys cover file bytes and expanded file order, source options, profile, columns,
view identity, include/define settings, parser implementation and codec source,
Python/platform, and parser dependency versions. Unchanged size or mtime cannot
hide changed contents. Input expansion and content hashing are repeated after
parsing or a hit; observed changes abort with status 2. Inputs must be settled:
this is not a filesystem snapshot, transaction, or defense against adversarial
A-to-B-to-A changes during a single read.

Reconciliation, every enabled rule, the current frozen contract and waiver date
are evaluated again on each run. Fatal diagnostics, completeness, tainted scopes,
provenance and all supported observation families survive cache round trips.
Corrupt, incompatible, oversized or wrongly typed entries are misses and are
reparsed; failed writes do not alter the analysis result. A cache hit is never a
stored pass/fail verdict.

### Deliberately conservative dependency boundary

The cache supports the eleven built-in static formats: Liberty, LEF, CSV,
IP-XACT, SDC, UPF, C headers, CDL, DEF, GDSII and connectivity intent. RTL is
eligible only with no configured include directory, no defines, and no backtick
anywhere in any explicit source. This intentionally bypasses even harmless RTL
directives and backticks in comments. All SystemRDL and external parser plugins
are reparsed until they have dependency-complete cache contracts. Unsupported
source dependencies must never be inferred to be absent to improve hit rates.

### Storage and trust

The codec is bounded allowlisted JSON, not pickle. It constructs only explicit
OpenCollate records/enums, validates their field types, rejects duplicate keys,
nonfinite numbers and excessive trees, and verifies canonical round trips and
SHA-256 integrity. It never loads a class or executes code named by a cache file.
These hashes are **not authentication**. Anyone able to rewrite the private cache
can forge matching hashes. Use only a cache you own and trust; do not ingest an
untrusted CI artifact cache as authoritative build state.

On POSIX the selected cache directory must be owned by the current user with
0700 permissions; new entries are private. Symlink roots and nonregular entries
are not accepted. On platforms without O_NOFOLLOW, checks are not race-proof;
private, trusted storage is still required. Do not share cache state across
mutually untrusted processes running under the same account.

Entries are capped at 32 MiB; source hashing is capped at 256 MiB and 4096 files
per view (larger views reparse). A default 256 MiB disk target evicts oldest
cache-owned regular files and leaves unrelated files alone. Eviction is
serialized inside a process, but the target is best-effort across concurrent
processes; use a filesystem quota for a hard disk bound.

## External deadline and resource guard

```console
opencollate guard --wall-seconds 60 --status-output guard-status.json -- check opencollate.toml --cache-dir .opencollate-cache --format json --output report.json
```

On POSIX, an optional virtual-address-space cap can also be requested:

```console
opencollate guard --wall-seconds 60 --memory-mib 2048 -- check opencollate.toml
opencollate guard --wall-seconds 30 -- formal check examples/formal/obligations.json
opencollate schema guard-status
```

The parent runs a fixed local Python worker, not an arbitrary executable or shell
command. Only `check`, `review`, `contract`, `formal`, `report`, and `demo` commands
are accepted; guards cannot recurse. Invoke help/version commands outside the
guard. For CI use `check` rather than the intentionally non-gating default demo.

The parent accepts results only when both the process exit and its private
normal-completion record agree. `os._exit(0)` without a record is a failed
analysis, not success. Hangs are stopped externally even when a parser, plugin
iterator, or native solver cannot cooperate. POSIX cancellation kills the worker
process group and reaps descendants remaining after normal worker completion.
Windows cancellation guarantees the direct worker only, not arbitrary descendants.

Limits apply before importing parsers and plugins. POSIX workers set RLIMIT_AS
when requested, plus CPU, per-file output and core-dump limits. **RLIMIT_AS limits
virtual address space, not resident memory or aggregate process-tree usage.**
Availability/enforcement depends on the host kernel. Windows rejects requested
memory limits rather than pretending to enforce them. POSIX descendant processes
inherit limits but are not collectively accounted. These are not cgroup or
Windows Job Object limits. Wall-clock cancellation is supervisory, not a hard
real-time guarantee against uninterruptible operating-system I/O.

`--output-limit-bytes` bounds each output file/stream (32 MiB default, up to 64 MiB).
On POSIX it also limits saved artifacts and cache files created by the worker.
Elsewhere the parent polls stdout/stderr size, so there can be a brief overshoot.
Stderr is retained up to the configured bound. Partial stdout from killed or
abnormally terminated workers is not emitted as a valid machine report.

| Guard outcome | Exit status |
| --- | ---: |
| Normal complete analysis | Preserve child 0, 1, 2, or 130 |
| Deadline/output breach, crash, missing or inconsistent completion | 2 |
| Invalid guard configuration or failure to publish requested status | 2 |

The status JSON is schema-validated in tests and reports actual enforcement
modes. A completed status alone does not mean a clean design: check `exit_code`.

**This is reliability containment, not a security sandbox.** Installed Python
plugins remain trusted code with the user's filesystem and network access. A
malicious plugin can escape a process group, tamper with private worker state,
or spawn external work. Use an OS/container security boundary for untrusted code.
A guard does not authenticate imported design evidence or strengthen a proof.

## Plugin and artifact integrity fixes

Parser/checker execution and entry-point discovery now catch `SystemExit`, so
`sys.exit(0)` cannot masquerade as normal completion. Invalid diagnostic entries,
including `None`, become fatal plugin failures. A checker is capped at 10000
returned diagnostics; exceeding the bound is fatal and its partial findings are
not silently accepted. A blocked iterator still requires the external guard.
KeyboardInterrupt remains an interruption rather than an apparent clean run.

CLI text/JSON/HTML outputs and frozen contracts are published by writing a private
temporary file, flushing/fsyncing it, then atomically replacing the destination.
Ordinary write/replace failures leave the old artifact intact. Abrupt termination
may leave a private temporary file. This is not a multi-file transaction or a
fully crash-durable directory fsync. On a failed guard, a prior report or another
already-completed artifact may still exist: **never consume it as this run's
success without the matching successful guard status**.

## Evidence and limits

Run `python benchmarks/incremental.py --json-output incremental.json` for measured
uncached/cold-cache/warm-cache full-check paths with identical-result assertions
and a source mutation that must match fresh checking. These are in-process
synthetic Liberty/LEF workloads with deliberately skipped timing tables, not
native timing verification or benchmarks against commercial products. Cold
caching can be slower; parsing-bound unchanged views benefit most. Include-file
heavy RTL and plugin-heavy projects deliberately get fewer hits.

Run `python benchmarks/upstream_cells.py --json-output upstream-cells.json` for
unmodified pinned SkyWater inverter/NAND interface controls and six mutations of
temporary source files. Explicit signal-pin/direction/rail oracles are independent
of parser output. Every fixture's SHA-256 and original Git blob identity are
verified offline. This small cell-level corpus is not production-SoC validation,
functional proof, foundry endorsement, or evidence of universal superiority.

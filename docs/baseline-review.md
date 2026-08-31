# Baseline review and regression ratchets

OpenCollate can distinguish an existing reviewed finding from a newly introduced or semantically
changed finding. A baseline never changes rule severity and never acts as a waiver.

## Create a baseline

Write the ordinary JSON report that reviewers have accepted:

```console
opencollate check opencollate.toml --format json --output .opencollate/baseline.json
```

The command still returns status 1 when the accepted report contains active errors. Inspect and
commit the report intentionally; do not refresh it automatically after a failed review.

## Review a live configuration

```console
opencollate review opencollate.toml \
  --baseline .opencollate/baseline.json \
  --write-report build/opencollate-current.json \
  --format markdown \
  --output build/opencollate-review.md
```

The default `--fail-on changed` gate rejects both new and changed active errors while allowing
unchanged baseline errors and resolved findings. Available gates are:

| Gate | Status 1 condition |
| --- | --- |
| `none` | No finding-based gate; fatal current analysis still returns 2 |
| `new` | A new active error is present |
| `changed` | A new or semantically changed active error is present |
| `all` | Any active current error is present, including unchanged findings |

Add `--deny-warnings` to apply the selected gate to active warnings. A finding explicitly waived
by project policy remains visible in the diff but does not fail a finding gate. A current fatal
finding always returns status 2, even if an external report marks it suppressed or the selected
gate is `none`.

## Compare two saved reports

```console
opencollate report diff baseline.json current.json --format text
opencollate report diff baseline.json current.json --format json --output diff.json
opencollate report diff baseline.json current.json --format sarif --output diff.sarif
```

Saved-report comparison is non-gating by default. It accepts the same `--fail-on`,
`--deny-warnings`, and `--include-unchanged` options as live review.

JSON diffs validate against the bundled versioned schema:

```console
opencollate schema diff --output opencollate-diff.schema.json
```

SARIF results use the standard `baselineState` values `new`, `updated`, `unchanged`, and `absent`,
which lets code-scanning consumers present the same classification.

## Identity versus content

A diagnostic fingerprint identifies the issue family: rule code, canonical object, property, and
participating views. It deliberately excludes source line numbers. OpenCollate separately computes
a full SHA-256 content digest from severity, message, evidence values, metadata, and effective
suppression state. Source movement and evidence ordering do not change that content digest.

For duplicate fingerprints, reports are compared as multisets. Exact content matches are consumed
first; remaining one-to-one occurrences are `changed`, and count imbalances become `new` or
`resolved`. Output ordering is deterministic.

Baseline and current artifacts must be OpenCollate report schema version 1 and contain structurally
valid diagnostics. Malformed or future-schema reports fail with status 2 rather than being guessed
or silently accepted.

Saved reports are treated as untrusted input. Each baseline or current JSON file is limited to
256 MiB and 128 nested JSON objects/arrays. Invalid UTF-8, decoder safety-limit failures, and these
resource-limit breaches return status 2 before a live review or saved-report comparison proceeds.

## CI pattern

A minimal pull-request job can install the checked-out source and preserve both the full current
report and the smaller review artifact:

```yaml
- name: Install OpenCollate
  run: python -m pip install .
- name: Reject collateral regressions
  run: >-
    opencollate review opencollate.toml
    --baseline .opencollate/baseline.json
    --write-report build/opencollate-current.json
    --format markdown
    --output build/opencollate-review.md
    --fail-on changed
```

Upload both files as ordinary protected CI artifacts. They can contain design names, hierarchy,
expressions, and source paths and should be handled like the original collateral.

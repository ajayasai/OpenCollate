# Full frozen-contract review and offline reports

## Snapshot-aware drift review

Build a frozen baseline and a current contract from the corresponding source revisions, then
compare their recorded facts:

```console
opencollate contract build --output baseline.oc.json
# After changing collateral, build a separate current snapshot.
opencollate contract build --output current.oc.json
opencollate contract diff baseline.oc.json current.oc.json --output drift.json
opencollate schema contract-diff
```

The diff covers canonical components/registers and every schema-v2 observation family: components,
package mappings, hierarchical objects, clocks, interfaces, registers, connectivity endpoints,
edges, and requirements. It also checks view inventory, attributes, extensions, completeness, and
tainted scopes. Both inputs are revalidated, including snapshot checksums.

Repeated identities use multiset subtraction. Missing duplicates are detected, and unmatched
ambiguous groups are retained rather than arbitrarily matched one-to-one. Each change contains its
scope, family, identity, state, and before/after evidence. This is structural drift of recorded
facts, not equivalence of arbitrary metadata or a decision that every recorded change is a bug.
For example, changed provenance may be a legitimate file movement requiring review.

| Exit status | Meaning |
| ---: | --- |
| 0 | Both contracts have nonempty, complete, untainted snapshots and no recorded changes |
| 1 | Complete snapshots differ; review the reported changes |
| 2 | Invalid input, a resource bound, or incomplete snapshot coverage prevents a complete result |

Schema-v1 contracts, empty snapshot sets, incomplete views, and tainted views cannot produce a
complete-comparison pass. An incomplete comparison may still emit useful change evidence, with
status 2. More than 10000 change groups is an explicit failure, not silent truncation. Contract
files are bounded to 32 MiB and 128 JSON nesting levels, require UTF-8, and reject duplicate keys.
Every serialized v2 snapshot must supply a valid SHA-256 digest. Digests are integrity checks,
not digital signatures or authentication of a design supplier.

This command is an additional CI review gate. It does not automatically add new semantic rules to
the existing `check` command, and snapshot completeness reflects recorded parser metadata, not
complete coverage of every construct in the source language.

## Self-contained interactive HTML

```console
opencollate check --format html --output review.html
opencollate review --baseline previous.json --format html --output changes.html
opencollate report diff previous.json current.json --format html --output changes.html
opencollate demo --format html > demo.html
```

Open the HTML in a browser. Search includes evidence and design names. Filters select severity,
rule, and baseline state; pagination shows 50 findings per page. Expanding a finding reveals
provenance, metadata, waiver reasons, and before/after evidence where available. Full machine data
is retained in an expandable section. With JavaScript disabled, all finding cards remain available
and native details panels still open. `--include-unchanged` controls unchanged baseline rows.

The file needs no server, account, external library, network request, font download, or telemetry.
All collateral text is escaped. Fixed scripts and styles are allowed by exact content-security-policy
hashes; arbitrary inline scripts, remote resources, frames, forms, and network connections are not
allowed. Source paths are displayed, not automatically opened or fetched. Reports still contain
potentially confidential design information and must be protected like the source collateral.

Tests exercise HTML injection attempts, CSP hashes, filtering, pagination, mobile overflow,
no-network rendering, and JavaScript-disabled evidence in a real Chromium browser. Local browser
policy may restrict file access; the report requires no special permission or policy changes.

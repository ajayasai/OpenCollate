# Exit codes

OpenCollate uses three normal process exit statuses:

| Code | Meaning | CI interpretation |
| ---: | --- | --- |
| 0 | Command completed; no unwaived error-level violations | Pass |
| 1 | Check completed; one or more unwaived violations | Design-collateral failure |
| 2 | Configuration, input, parsing, output, or internal failure prevented a trustworthy result | Tool or infrastructure failure |

Warnings alone do not produce status 1 unless policy promotes their rule. A waived error remains
in complete reports but does not produce status 1. Fatal findings cannot be downgraded or waived.

`review` and `report diff` apply their selected `--fail-on` ratchet after validating both reports.
`review` defaults to new-or-changed active errors; saved `report diff` is non-gating by default.
Regardless of the ratchet, a current fatal finding or current status 2 returns 2. See
[baseline review](baseline-review.md).

## Recommended CI pattern

```sh
opencollate check opencollate.toml --format sarif --output opencollate.sarif
status=$?

case "$status" in
  0) echo "OpenCollate clean" ;;
  1) echo "Collateral inconsistencies found" >&2 ;;
  2) echo "OpenCollate could not complete reliably" >&2 ;;
  *) echo "Unexpected OpenCollate exit status: $status" >&2 ;;
esac

exit "$status"
```

Do not convert status 2 to success. Doing so can turn unreadable input or unsupported critical
syntax into an apparent clean check.

Commands such as `--help`, `--version`, `capabilities`, `explain`, and successful schema
generation return 0. `demo` also returns 0 by default because its errors are deliberate; pass
`--strict-exit` to propagate the demonstration check status. Invalid arguments return 2. An
interactive interruption returns the conventional status 130.

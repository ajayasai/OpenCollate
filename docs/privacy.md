# Privacy and design-data handling

OpenCollate is local-first:

- No account or license server is required.
- No telemetry is collected.
- No design file, contract, diagnostic, path, or usage event is uploaded.
- Checking inputs does not require network access after installation.
- The tool does not execute hooks from design files.

Dependency installation and external CI services are separate network activities controlled by
the user. A GitHub Actions workflow that uploads SARIF or test artifacts sends those outputs to
GitHub under that repository's settings; OpenCollate does not do so automatically.

## Reports can still be sensitive

Reports and contracts may contain:

- Module, cell, pin, macro, pad, and ball names.
- Source paths and source spans.
- Boolean expressions.
- Package connectivity.
- Configuration aliases plus waiver selectors, expiration dates, and reasons.

Treat generated artifacts at least as confidentially as source collateral. Prefer
repository-relative paths in shared reports. Inspect minimized reproductions before attaching
them to a public issue.

## Synthetic public examples

Every example shipped in the OpenCollate repository must be created for the project or have a
clearly documented redistribution license. Do not derive a "synthetic" fixture by merely renaming
proprietary objects while retaining structure or values.

## Reproducible output

OpenCollate deterministically orders contract objects, evidence, and diagnostics. This supports
reviewable diffs but does not anonymize the content. Fingerprints are identifiers, not
cryptographic redaction.

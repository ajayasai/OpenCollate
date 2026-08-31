# Privacy and design-data handling

OpenCollate is local-first:

- No account or license server is required.
- No telemetry is collected.
- No design file, contract, diagnostic, path, or usage event is uploaded.
- Checking inputs does not require network access after installation.
- The tool does not execute hooks from design files.

SDC and UPF are parsed as static Tcl-shaped text without starting Tcl. IP-XACT parsing does not
fetch schemas or expand external definitions, C headers do not invoke a preprocessor, and
CDL/SPICE is not simulated. See the [security model](security-model.md) for the exact trust and
execution boundary.

Dependency installation and external CI services are separate network activities controlled by
the user. A GitHub Actions workflow that uploads SARIF or test artifacts sends those outputs to
GitHub under that repository's settings; OpenCollate does not do so automatically.

## Reports can still be sensitive

Reports and contracts may contain:

- Module, cell, pin, macro, pad, ball, GDSII structure, and text-label names.
- Source paths and source spans.
- Boolean expressions.
- Package connectivity.
- Hierarchical object references, constraint targets, power-intent names, and register addresses.
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

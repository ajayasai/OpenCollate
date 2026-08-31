# Diagnostics

OpenCollate diagnostics are designed to be read by an engineer and consumed by automation without
separate interpretations.

## Structure

Every finding has:

- `code`: stable rule identifier within a documented compatibility window.
- `severity`: fatal, error, warning, or info.
- `message`: concise description using design object names.
- `object`: kind, canonical identifier, and display name.
- `property`: the property being checked.
- `evidence`: source-view values and locations.
- `help`: a concrete next step when one is known.
- `fingerprint`: deterministic semantic identity for review and waiver matching.
- Waiver state and reason when applicable.

Generate the exact report schema for the installed release:

```console
opencollate schema report --output report.schema.json
```

## Codes

Code families are intentionally separated:

| Range | Family |
| --- | --- |
| OC1001–OC1105 | Input, configuration, and parser completeness |
| OC2001–OC2005 | Resolution and contract reconciliation |
| OC3001–OC3104 | Component and port inventory |
| OC4001–OC4303 | Direction, shape, role, and Boolean semantics |
| OC5001–OC5006 | Package and cross-domain mappings |
| OC6001–OC6003 | Static SDC object and clock consistency |
| OC6101–OC6104 | Static UPF references and object integrity |
| OC6201–OC6202 | IP-XACT interface port maps |
| OC6301–OC6309 | Hardware/software register maps and fields |
| OC6401 | DEF endpoint resolution against elaborated RTL |
| OC9001 and above | Internal integrity failures |

Use `capabilities` to inspect the installed format/output surface and `explain` for a rule's
installed summary and remediation:

```console
opencollate capabilities
opencollate explain OC4001
```

Do not build automation by matching English message text. Match `code`, canonical object ID, and
structured properties.

## Severity and exit status

Severity describes the finding. Policy may promote or demote a rule, but it cannot convert
unknown evidence into known evidence. Any unwaived error-level finding produces exit status 1.
A fatal condition that prevents a trustworthy check produces status 2.
Fatal findings cannot be downgraded or waived.

A status-0 run is not a signoff result. It means only that enabled rules found no unwaived
error-level contradiction in known facts. Review parser diagnostics, unknown/unsupported facts,
tainted scopes, configured participation, and the [supported syntax boundary](supported-syntax.md).

## Evidence

Conflicts show actual values and views, not only an expected/actual pair. When a configured
baseline supplies intent, it is labeled as baseline evidence. Source spans are one-based and may
be absent for formats or generated observations that cannot provide one.

## Waivers

A waiver is an auditable policy decision, not deletion. It must target a diagnostic code and
stable object or fingerprint, include a reason, and can include an expiration date. Waived
findings remain in complete machine reports and summary counts.

Avoid broad code-only waivers. If a source edit changes the semantic fingerprint, review the
waiver again.

## SARIF

SARIF maps diagnostic code to `ruleId`, message to the result message, evidence spans to
locations, and the OpenCollate fingerprint to a partial fingerprint. Downstream annotation is a
presentation; the OpenCollate JSON report remains the complete audit record.

## Completeness before mismatch

`OC1102`–`OC1105` are not cosmetic warnings. They explain where a rule could not safely compare a
fact or where analysis coverage is partial. Automation that suppresses all warnings can erase the
distinction between “equal” and “not comparable.” In security- or tapeout-sensitive workflows,
review these findings explicitly and use process-level limits for untrusted inputs.

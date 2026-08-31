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

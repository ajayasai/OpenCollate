# Security policy

## Supported versions

Until OpenCollate 1.0, security fixes are made for the latest published 0.x release only.

| Version | Supported |
| --- | --- |
| Latest 0.x | Yes |
| Earlier releases | No |

## Report a vulnerability privately

Do not open a public issue for a suspected vulnerability.

Use GitHub's **Security → Report a vulnerability** form:

https://github.com/ajayasai/OpenCollate/security/advisories/new

Include the affected version, operating system, impact, reproduction steps, and a minimal
synthetic input where possible. Do not attach confidential design collateral. If confidential
evidence is essential, first describe the evidence and wait for a maintainer to agree on a safe
transfer method.

The project aims to acknowledge reports within three business days and provide an initial triage
within seven. Fix and disclosure timing depends on impact and release complexity. Please allow a
reasonable remediation window before public disclosure.

## Security scope

Examples include:

- Code execution or command injection while parsing input or configuration.
- Path traversal or writing outside an explicitly requested output location.
- Malicious input causing unreasonable CPU, memory, or disk consumption.
- Report injection that can execute in a downstream viewer.
- Dependency or release-pipeline compromise.
- Leaking source collateral through an unintended network operation.

A false positive, false negative, unsupported syntax construct, or incorrect diagnostic is
normally a correctness bug, not a security vulnerability, unless it creates a concrete security
impact.

## Safe-use expectations

OpenCollate parses complex, attacker-controlled text. Run it with the least filesystem privilege
needed, keep dependencies current, and do not process untrusted files in a privileged CI runner.
The project has no telemetry or built-in upload path, but reports may contain names and source
locations from the input design. Treat reports according to the design's confidentiality level.

OpenCollate is an analysis aid, not a signoff or security-certification tool.

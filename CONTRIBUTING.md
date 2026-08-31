# Contributing to OpenCollate

Thank you for helping make hardware collateral easier to trust. Contributions of code,
documentation, synthetic fixtures, specification expertise, and carefully minimized bug reports
are welcome.

## Before sharing data

Do not upload proprietary RTL, foundry libraries, package data, customer names, restricted
standards text, or confidential tool output. Reduce failures to a synthetic example whose license
permits public redistribution. If the issue is a security vulnerability, follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Development setup

```console
git clone https://github.com/ajayasai/OpenCollate.git
cd OpenCollate
python -m venv .venv
```

Activate the environment, then install the project and development tools:

```console
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pre-commit install
pre-commit install --hook-type pre-push
```

Run the same core checks as CI:

```console
ruff format --check .
ruff check .
mypy src/opencollate
pytest
coverage run -m pytest
coverage report
python -m build
twine check dist/*
```

Before opening a pull request, install the built wheel into a clean environment and run
`opencollate demo`.

## Contribution rules

- Keep pull requests focused and explain the user-visible outcome.
- Add tests for behavior changes and update documentation for public interfaces.
- Add a changelog entry for notable user-visible changes.
- Preserve deterministic ordering in contracts, diagnostics, and reports.
- Do not change a released diagnostic meaning without documenting the compatibility impact.
- Use only fixtures that are synthetic or demonstrably redistributable.
- Do not silently coerce unknown parser state into a known design fact.

### Adding or changing a parser

A parser contribution must include:

1. A documented syntax boundary in `docs/supported-syntax.md`.
2. A successful fixture for every newly supported construct.
3. Malformed, unknown, and unsupported cases.
4. Source-location assertions where the source format permits them.
5. Evidence that skipped sections cannot corrupt later recognized data.

Parser adapters emit facts and provenance; they do not own cross-view policy.

### Adding or changing a rule

A rule contribution must include:

1. A unique diagnostic code and entry in `docs/rule-catalog.md`.
2. A clean case, mismatch case, unknown case, and waiver case.
3. A stable, actionable human message.
4. JSON-schema-valid evidence and a deterministic fingerprint.
5. A clear statement of what the rule does not prove.

## Developer Certificate of Origin

OpenCollate uses the [Developer Certificate of Origin](DCO), not a contributor license
agreement. Sign every commit:

```console
git commit -s
```

The sign-off certifies that you have the right to submit the contribution under this project's
license. The sign-off name should be a name by which you can be identified, not an anonymous
alias.

## Review and decisions

Maintainers may ask for smaller changes, additional evidence, licensing clarification, or a
design discussion before merge. Substantial changes should begin as a GitHub issue with a
concrete proposal. The decision process is described in [GOVERNANCE.md](GOVERNANCE.md).

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

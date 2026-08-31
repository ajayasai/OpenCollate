## Outcome

<!-- What changes for an OpenCollate user? -->

## Approach

<!-- Summarize the implementation and important tradeoffs. -->

## Verification

<!-- List exact commands and relevant fixtures. -->

- [ ] Tests cover the clean, mismatch, and unknown/unsupported paths.
- [ ] `ruff format --check .`, `ruff check .`, `mypy src/opencollate`, and `pytest` pass.
- [ ] User-visible behavior and `CHANGELOG.md` are updated where needed.
- [ ] New diagnostics have a documented code, actionable message, evidence, and fingerprint.
- [ ] New syntax support is reflected in `docs/supported-syntax.md`.
- [ ] Fixtures are synthetic or have documented redistribution permission.
- [ ] No proprietary collateral, secrets, personal data, or restricted standards text is included.
- [ ] All commits include a DCO sign-off (`git commit -s`).

## Compatibility and risk

<!-- Contract/report schema, code meanings, CLI, performance, parser recovery, security. -->

# Release process

This checklist is for maintainers.

1. Confirm CI, security, conformance, and nightly jobs are green.
2. Audit runtime dependency and fixture licenses.
3. Update `CHANGELOG.md`, `ROADMAP.md`, version metadata, and compatibility tables.
4. Build wheel and sdist from a clean checkout.
5. Run `twine check` and inspect distribution contents.
6. Install the wheel in clean Linux and Windows environments.
7. Run `opencollate demo` and the committed UART example from the installed wheel.
8. Verify the release tag exactly matches the package version.
9. Attach distributions, SHA-256 checksums, SBOM, and build provenance to the GitHub release.
10. Verify installation from the GitHub release or repository tag.
11. Announce supported behavior and known limitations; do not advertise roadmap items as shipped.

OpenCollate is not published on PyPI at this stage. Treat released artifacts as immutable: if a
release is defective, publish a new version; never replace files under an existing version.

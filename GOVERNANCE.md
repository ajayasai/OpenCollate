# Governance

OpenCollate is currently a maintainer-led project.

## Roles

- **Contributors** improve code, tests, documentation, examples, or issue analysis.
- **Reviewers** are trusted contributors who regularly provide technically sound review.
- **Maintainers** merge changes, manage releases and security reports, and protect the project's
  long-term compatibility and licensing.

The initial maintainer is [@ajayasai](https://github.com/ajayasai).

## Decisions

Routine changes are decided through pull-request review. Changes to the canonical contract,
diagnostic schema, parser architecture, public CLI, license, governance, or compatibility policy
should begin with a public design issue. A proposal should state the problem, alternatives,
compatibility impact, test strategy, and migration plan.

The maintainer seeks rough consensus and records the decision and rationale. When consensus is
not possible, the maintainer decides in the project's stated interests and explains the decision
publicly. Security embargoes and private conduct matters are the exceptions to public discussion.

## Becoming a reviewer or maintainer

Sustained, constructive participation is the basis for additional responsibility. Maintainers
consider technical judgment, review quality, respectful collaboration, reliability, licensing
awareness, and care with confidential EDA data. A maintainer nominates and publicly records new
reviewers or maintainers.

## Releases

A maintainer approves a release after required CI, package, provenance, license, and clean-install
checks pass. Releases follow Semantic Versioning. Before 1.0, breaking changes may occur in a
minor release but must be prominent in the changelog and release notes.

## Project assets and succession

Repository, package-index, documentation, and release credentials should move to a shared project
organization when multiple maintainers are active. If the sole maintainer becomes unavailable,
an established reviewer may request stewardship through a public issue with evidence of
community support.

This document can be changed through the same public proposal process.

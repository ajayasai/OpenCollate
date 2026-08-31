# Competitive evidence and product target

OpenCollate's goal is to be the strongest **open, local-first, CI-native collateral consistency
checker**. That is narrower than claiming to replace every commercial SoC assembly, formal
verification, register-generation, or signoff product.

This page makes the comparison falsifiable. It records public vendor claims, the evidence
OpenCollate publishes, and the boundaries that must remain visible. It was last reviewed on
2026-08-31.

## What "better" means here

OpenCollate should lead in the workflows an open checker can objectively own:

- Every diagnostic, parser decision, and resource limit is inspectable in source.
- A clean-room installation can run locally and in ordinary CI without a license server or
  hosted design upload.
- Findings have stable identities, complete evidence, machine-readable schemas, and deterministic
  baseline states for pull-request review.
- Unsupported syntax and incomplete analysis are explicit; an unknown path cannot silently pass.
- Public fixtures, adversarial tests, performance artifacts, release provenance, dependency
  inventory, and exact limitations ship with the product.
- One configuration can reconcile interface, physical, package, power, timing, register,
  netlist, and static connectivity evidence without private format converters.

Those criteria are release gates, not marketing adjectives. The test suite and nightly benchmark
artifacts are the evidence.

## Public capability comparison

The commercial entries below summarize only capabilities stated on official public pages. They
are not results from licensed head-to-head testing. "Not stated" means the cited public material
does not establish the capability; it does not mean the product lacks it.

| Capability | OpenCollate target | Public commercial evidence |
| --- | --- | --- |
| Multi-view RTL/collateral consistency | Parser-neutral observations with provenance and explicit fact state across all supported inputs | [Defacto SoC Compiler](https://defactotech.com/products-solutions/soc-signoff-structural-verification) advertises one unified database for RTL/gate-level, IP-XACT, UPF, Liberty, SDC, and LEF/DEF coherency checks. |
| Declarative connectivity versus RTL | Bounded static paths with witness evidence; an unrepresented or tainted frontier is inconclusive, never a proof | [Cadence Jasper Connectivity](https://www.cadence.com/en_US/home/tools/system-design-and-verification/formal-and-static-verification/jasper-verification-platform/connectivity-verification-app.html) advertises exhaustive static, structural, temporal, and conditional checks from CSV or IP-XACT. [Siemens Questa Check Connect](https://eda.sw.siemens.com/en-US/ic/questa/formal-verification/) advertises exhaustive static and dynamic connectivity. [Synopsys VC Formal](https://www.synopsys.com/content/dam/synopsys/verification/datasheets/vc-formal-ds.pdf) advertises formal connectivity and register-verification applications. |
| SoC assembly and collateral generation | Deliberately not supported; OpenCollate reports contradictions and does not rewrite source collateral | [Defacto SoC Compiler](https://about.defactotech.com/products-solutions/soc-integration-at-rtl) generates and updates RTL, IP-XACT, SDC, and UPF. [Arteris Magillem](https://www.arteris.com/products/) advertises IP packaging, connectivity, register generation, and diff/merge. [Agnisys](https://www.agnisys.com/wp-content/uploads/2024/08/Agnisys-HSI-Brochure-Single-Pages-1.pdf) advertises register RTL, software, verification, and documentation generation. |
| Standard register descriptions | SystemRDL 2.0 is imported as another address-map authority and compared with IP-XACT and C-header observations | [Accellera](https://www.accellera.org/downloads/standards/systemrdl) defines SystemRDL as a single register source from which multiple views can be generated. Arteris and Agnisys advertise SystemRDL-based generation workflows. |
| Auditability and reproducibility | Apache-2.0 source, public schemas and fixtures, deterministic reports, public CI, SBOM, checksums, and build provenance | The cited products are proprietary offerings whose implementation and licensed regressions are not publicly inspectable. This row compares public evidence, not internal vendor quality. |
| Functional-safety certification and vendor support | No certification and no implied signoff | Defacto states that SoC Compiler is ISO 26262 certified. Commercial support quality is not evaluated by OpenCollate's public corpus. |

Siemens [announced an agreement to acquire Defacto Technologies on July 21,
2026](https://news.siemens.com/en-us/siemens-to-acquire-defacto-technologies/). That validates the
commercial importance of the workflow; it does not constitute a benchmark result.

## Claims OpenCollate does not make

OpenCollate does not currently claim formal exhaustiveness, temporal or conditional connectivity,
full SystemVerilog/UPF/SDC interpretation, design generation, million-register scale, functional
safety certification, or tapeout signoff. Cadence, Siemens, Synopsys, Defacto, Arteris, and
Agnisys publicly offer capabilities outside this project's scope.

A result is stronger than a marketing comparison only when both products are run on the same
permissibly publishable corpus, under recorded versions and hardware, with equivalent expected
outcomes. Until such a licensed evaluation is contributed, OpenCollate's competitive claims are
limited to properties visible in this repository and its published release artifacts.

## Reproducible acceptance gates

A release may describe a capability as shipped only when all applicable gates pass:

1. Positive, negative, malformed, adversarial, and optimized-Python tests are public.
2. The result is deterministic across repeated runs and supported operating systems.
3. Unknown, unsupported, and tainted evidence cannot create a false pass.
4. Machine output validates against the bundled JSON Schema.
5. Performance and resource behavior are captured by the public nightly benchmark.
6. A clean wheel install passes the demonstration and full-stack example.
7. The release includes checksums, dependency-complete SPDX SBOM, and build provenance.

Open issues and contributed minimized fixtures are welcome. A concrete missed contradiction or
false positive is more useful than an unmeasurable request to "beat" another tool.

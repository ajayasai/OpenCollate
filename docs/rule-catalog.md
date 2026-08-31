# Rule catalog

The runtime registry is authoritative. OpenCollate 0.2.0 registers 65 rules. Run
`opencollate explain CODE` for the installed summary and remediation, and
`opencollate capabilities --json` for the installed rule count.

## Input and completeness — OC1xxx

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC1001 | invalid-configuration | fatal | The project configuration is invalid |
| OC1002 | source-not-found | fatal | A configured source does not exist |
| OC1003 | source-glob-empty | fatal | A configured source pattern matched no files |
| OC1004 | waiver-expired | warning | A waiver has expired |
| OC1005 | waiver-unmatched | info | A waiver matched no diagnostic |
| OC1101 | parse-error | fatal | A source could not be parsed completely |
| OC1102 | unsupported-construct | warning | A source construct is not supported |
| OC1103 | unresolved-expression | warning | A value such as a bus width could not be resolved |
| OC1104 | analysis-scope-tainted | warning | A scope is incomplete after a parse error |
| OC1105 | check-not-applicable | info | A check cannot be applied to this object |

These rules prevent incomplete or unsupported input from masquerading as a clean comparison.
Fatal findings make the run untrustworthy and cannot be waived or downgraded.

## Reconciliation — OC2xxx

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC2001 | component-unassociated | warning | A component could not be associated across views |
| OC2002 | alias-collision | fatal | Aliases map more than one object to the same identity |
| OC2003 | duplicate-definition | error | A view defines the same component more than once |
| OC2004 | duplicate-definition-conflict | error | Duplicate definitions have different interfaces |
| OC2005 | ambiguous-name-normalization | error | Name normalization produced an ambiguous identity |

Aliases change grouping; they do not delete evidence. Resolve this family before treating
downstream value comparisons as authoritative.

## Inventory — OC3xxx

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC3001 | component-missing-from-view | error | A required view is missing a component |
| OC3002 | component-not-in-contract | error | A component is not present in the frozen contract |
| OC3101 | pin-missing-from-view | error | A participating view is missing a pin |
| OC3102 | pin-not-in-contract | error | A pin is not present in the frozen contract |
| OC3103 | duplicate-pin | error | A component view defines a pin more than once |
| OC3104 | likely-pin-name-mismatch | warning | A missing pin resembles a differently named pin |

Inventory is governed by component/view participation. Reference-only SDC, UPF, and C-header
views and partial package-map CSV views are not silently treated as complete interfaces. A GDSII
view contributes cell inventory; it contributes ports only when text-label selectors are explicit.

## Interface and function semantics — OC4xxx

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC4001 | direction-mismatch | error | Pin direction differs across views |
| OC4002 | unsupported-direction | warning | A direction cannot be compared directly |
| OC4101 | width-mismatch | error | Pin width differs across views |
| OC4102 | range-order-mismatch | error | Bus indices are declared in opposite orders |
| OC4103 | dimension-shape-mismatch | error | Bus dimensions or index sets differ across views |
| OC4104 | bus-bit-gap | error | An exploded bus has missing intermediate bits |
| OC4105 | duplicate-bus-bit | error | An exploded bus repeats a bit index |
| OC4106 | scalar-vector-mismatch | warning | A scalar is represented as a one-bit vector in another view |
| OC4201 | role-mismatch | error | Pin roles differ across views |
| OC4202 | power-ground-pin-missing | error | A required power or ground pin is missing |
| OC4203 | power-ground-polarity-mismatch | error | A rail is power in one view and ground in another |
| OC4204 | clock-reset-role-mismatch | error | Clock/reset classification differs across views |
| OC4301 | boolean-function-mismatch | error | RTL and Liberty Boolean functions are not equivalent |
| OC4302 | boolean-function-uncheckable | warning | Boolean equivalence cannot be established |
| OC4303 | liberty-function-references-unknown-pin | error | A Liberty function references an unknown input pin |

Boolean equivalence is exact only inside the supported expression grammar and configured input
bound. Unsupported, sequential, or over-limit functions are inconclusive rather than unequal.

## Package mappings — OC5xxx

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC5001 | unknown-die-pad | error | A package row references an unknown die pad |
| OC5002 | unknown-package-signal | error | A package row references an unknown logical signal |
| OC5003 | duplicate-package-ball | error | A package ball is assigned more than once |
| OC5004 | die-pad-mapped-multiple-times | error | A die pad is mapped to multiple balls |
| OC5005 | conflicting-package-signal | error | Package mappings disagree about the signal on an endpoint |
| OC5006 | invalid-pin-map-row | error | A package/pin-map row is incomplete or invalid |

These checks consume explicit mapping observations, normally CSV `package_map` rows. DEF net/pin
connectivity and GDSII text labels are not promoted to package mapping without an explicit
die-pad/ball relation.

## Constraints and power intent — OC60xx–OC61xx

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC6001 | sdc-object-not-found | error | An SDC query references no object in the elaborated RTL design |
| OC6002 | clock-definition-mismatch | error | Clock definitions disagree across constraint views |
| OC6003 | clock-target-role-mismatch | error | A clock is attached to a pin explicitly classified as a non-clock |
| OC6101 | upf-instance-not-found | error | UPF references an instance absent from elaborated RTL |
| OC6102 | upf-port-not-found | error | UPF references a port or pin absent from elaborated RTL |
| OC6103 | upf-object-not-found | error | UPF references a power-intent object that is not defined |
| OC6104 | duplicate-upf-object | error | A UPF view defines the same power-intent object more than once |

Reference absence is reported only when the relevant source fact is known and a suitable RTL or
UPF definition inventory exists. Dynamic or unsafe-to-match queries are not claimed absent.

## IP-XACT interfaces — OC62xx

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC6201 | ipxact-interface-port-not-found | error | An IP-XACT interface maps to a physical port absent from its component |
| OC6202 | ipxact-interface-port-map-conflict | error | An IP-XACT interface contains an ambiguous or conflicting port map |

This family validates the imported component/port-map relation; it does not validate external bus
definitions, protocol behavior, or an IP-XACT schema.

## Hardware/software registers — OC63xx

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC6301 | register-missing-from-view | error | A hardware or software address-map view is missing a register |
| OC6302 | register-address-mismatch | error | A register has different addresses or offsets across views |
| OC6303 | register-width-mismatch | error | A register has different widths across views |
| OC6304 | register-field-missing-from-view | error | A register field is absent from one participating address-map view |
| OC6305 | register-field-layout-mismatch | error | A register field has different bit positions or widths across views |
| OC6306 | register-access-mismatch | error | Register or field access permissions disagree across views |
| OC6307 | duplicate-register-definition | error | A view defines the same register or field more than once |
| OC6308 | register-field-reset-mismatch | error | A register field has different reset values across views |
| OC6309 | invalid-register-field-layout | error | Register fields overlap or extend beyond the declared register width |
| OC6310 | ambiguous-register-identity | error | An unscoped register name matches multiple address-map scopes |

Known IP-XACT and C-header facts participate. A C header that does not encode access or reset data
leaves those properties unknown; it does not inherit them from hardware collateral.

## DEF references — OC64xx

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC6401 | def-object-not-found | error | A DEF connection references no object in the elaborated RTL design |

Only structural DEF endpoints are checked. Skipped route coordinates and geometry never become
candidate RTL object names.

## Internal integrity — OC9xxx

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC9001 | parser-plugin-failure | fatal | A parser plugin failed unexpectedly |
| OC9002 | checker-plugin-failure | fatal | A checker plugin failed unexpectedly |
| OC9999 | internal-error | fatal | OpenCollate encountered an internal error |

Treat internal-integrity findings as tool defects and include a minimized synthetic reproduction
in a bug report. A run with one of these findings cannot be considered clean.

## Stability

During 0.x, rules can be added. Reusing a code for materially different meaning, removing a rule,
or changing a structured field incompatibly requires a documented breaking change. Do not match
automation on message prose; match the code, canonical object ID, property, and structured
evidence. See [CHANGELOG.md](../CHANGELOG.md).

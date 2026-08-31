# Rule catalog

The runtime registry is authoritative. Run `opencollate explain CODE` for the installed summary
and remediation. OpenCollate 0.1.0 ships these rules:

| Code | Name | Default | Meaning |
| --- | --- | --- | --- |
| OC1001 | invalid-configuration | fatal | Project configuration is invalid |
| OC1002 | source-not-found | fatal | A configured source does not exist |
| OC1003 | source-glob-empty | fatal | A configured source pattern matched no files |
| OC1004 | waiver-expired | warning | A waiver has expired |
| OC1005 | waiver-unmatched | info | A waiver matched no diagnostic |
| OC1101 | parse-error | fatal | A source could not be parsed completely |
| OC1102 | unsupported-construct | warning | A source construct is recognized but unsupported |
| OC1103 | unresolved-expression | warning | A bus width or similar value could not be resolved |
| OC1104 | analysis-scope-tainted | warning | A scope is incomplete after parser recovery |
| OC1105 | check-not-applicable | info | A check cannot be applied to an object |
| OC2001 | component-unassociated | warning | A component could not be associated across views |
| OC2002 | alias-collision | fatal | Aliases collapse incompatible object identities |
| OC2003 | duplicate-definition | error | A view defines a component more than once |
| OC2004 | duplicate-definition-conflict | error | Duplicate definitions have different interfaces |
| OC2005 | ambiguous-name-normalization | error | Normalization produced an ambiguous identity |
| OC3001 | component-missing-from-view | error | A participating view is missing a component |
| OC3002 | component-not-in-contract | error | A component is absent from the frozen contract |
| OC3101 | pin-missing-from-view | error | A participating view is missing a pin |
| OC3102 | pin-not-in-contract | error | A pin is absent from the frozen contract |
| OC3103 | duplicate-pin | error | A component view defines a pin more than once |
| OC3104 | likely-pin-name-mismatch | warning | A missing pin resembles a differently named pin |
| OC4001 | direction-mismatch | error | Pin direction differs across views |
| OC4002 | unsupported-direction | warning | A direction cannot be compared directly |
| OC4101 | width-mismatch | error | Pin width differs across views |
| OC4102 | range-order-mismatch | error | Bus indices are declared in opposite orders |
| OC4103 | dimension-shape-mismatch | error | Bus dimensions or index sets differ |
| OC4104 | bus-bit-gap | error | An exploded bus has missing intermediate bits |
| OC4105 | duplicate-bus-bit | error | An exploded bus repeats a bit index |
| OC4106 | scalar-vector-mismatch | warning | Scalar and one-bit-vector representations differ |
| OC4201 | role-mismatch | error | Pin roles differ across views |
| OC4202 | power-ground-pin-missing | error | A required power or ground pin is missing |
| OC4203 | power-ground-polarity-mismatch | error | A rail is power in one view and ground in another |
| OC4204 | clock-reset-role-mismatch | error | Clock/reset classification differs across views |
| OC4301 | boolean-function-mismatch | error | RTL and Liberty functions are not equivalent |
| OC4302 | boolean-function-uncheckable | warning | Boolean equivalence cannot be established |
| OC4303 | liberty-function-references-unknown-pin | error | A Liberty function references an unknown input |
| OC5001 | unknown-die-pad | error | A package row references an unknown die pad |
| OC5002 | unknown-package-signal | error | A package row references an unknown logical signal |
| OC5003 | duplicate-package-ball | error | A package ball is assigned more than once |
| OC5004 | die-pad-mapped-multiple-times | error | A die pad maps to multiple balls |
| OC5005 | conflicting-package-signal | error | Package mappings disagree about an endpoint signal |
| OC5006 | invalid-pin-map-row | error | A package/pin-map row is incomplete or invalid |
| OC9001 | parser-plugin-failure | fatal | A parser failed unexpectedly |
| OC9002 | checker-plugin-failure | fatal | A checker failed unexpectedly |
| OC9999 | internal-error | fatal | An OpenCollate invariant or operation failed |

```console
opencollate explain OC4001
```

## Input and completeness — OC1xxx

Detect malformed configuration, unreadable inputs, parser recovery, unsupported constructs,
unknown shapes, ambiguous CSV columns, and missing baseline views. If a parser could not establish
a fact, semantic rules must not report equality.

## Reconciliation — OC2xxx

Detect alias collisions, multiple canonical identities, contradictory baseline selection, and
participation-policy ambiguity. Resolution findings generally require fixing configuration or
source naming before semantic mismatches are meaningful.

## Inventory — OC3xxx

Compare expected and participating components and ports across views. A source object is not
"missing" from a view unless participation policy says that view should contain it.

## Semantics — OC4xxx

Compare direction, width, dimensions, bounds, order, role, and small combinational Boolean
functions. Boolean equivalence is exact only within the configured variable limit and supported
grammar. Unknown dependencies, sequential constructs, or expressions over the limit are
inconclusive rather than unequal.

## Mapping — OC5xxx

Validate CSV die-pad, signal, package-ball, and component relationships, including duplicate
assignments, missing endpoints, width expansion, and known role/direction contradictions.

## Internal integrity — OC9xxx

Indicate a violated OpenCollate invariant or unexpected failure. Treat these as tool defects and
include a synthetic reproduction in a bug report. A run with an internal-integrity diagnostic
cannot be considered clean.

## Stability

During 0.x, codes can be added freely. Reusing a code for a materially different meaning requires
a documented breaking change. Removals and incompatible field changes appear in
[CHANGELOG.md](../CHANGELOG.md).

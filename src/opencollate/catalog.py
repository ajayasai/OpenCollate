"""Stable OpenCollate rule catalog.

Rule identifiers are public API.  Messages can improve, but a code's semantic
meaning must not change incompatibly within a major release.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from opencollate.diagnostics import Severity


@dataclass(frozen=True, slots=True)
class RuleSpec:
    code: str
    name: str
    default_severity: Severity
    summary: str
    help: str


def _rule(
    code: str,
    name: str,
    severity: Severity,
    summary: str,
    help: str,
) -> RuleSpec:
    return RuleSpec(code, name, severity, summary, help)


_RULE_LIST = (
    _rule(
        "OC1001",
        "invalid-configuration",
        Severity.FATAL,
        "The project configuration is invalid.",
        "Correct the named key in opencollate.toml and run the check again.",
    ),
    _rule(
        "OC1002",
        "source-not-found",
        Severity.FATAL,
        "A configured source does not exist.",
        "Correct the source path or generate the missing collateral.",
    ),
    _rule(
        "OC1003",
        "source-glob-empty",
        Severity.FATAL,
        "A configured source pattern matched no files.",
        "Correct the pattern or add the expected source file.",
    ),
    _rule(
        "OC1004",
        "waiver-expired",
        Severity.WARNING,
        "A waiver has expired.",
        "Remove the waiver, extend it with justification, or fix the underlying issue.",
    ),
    _rule(
        "OC1005",
        "waiver-unmatched",
        Severity.INFO,
        "A waiver matched no diagnostic.",
        "Remove stale waivers or correct their code, object, view, or fingerprint selectors.",
    ),
    _rule(
        "OC1101",
        "parse-error",
        Severity.FATAL,
        "A source could not be parsed completely.",
        "Fix the syntax error; downstream checks suppress facts from the tainted scope.",
    ),
    _rule(
        "OC1102",
        "unsupported-construct",
        Severity.WARNING,
        "A source construct is not supported.",
        "Simplify the construct or track parser capability support; OpenCollate "
        "will not treat it as clean.",
    ),
    _rule(
        "OC1103",
        "unresolved-expression",
        Severity.WARNING,
        "A value such as a bus width could not be resolved.",
        "Provide parameter defaults, includes, or defines needed to resolve the expression.",
    ),
    _rule(
        "OC1104",
        "analysis-scope-tainted",
        Severity.WARNING,
        "A scope is incomplete after a parse error.",
        "Fix the preceding parse issue before trusting absence checks in this scope.",
    ),
    _rule(
        "OC1105",
        "check-not-applicable",
        Severity.INFO,
        "A check cannot be applied to this object.",
        "Review the evidence and supported-construct documentation for the reason.",
    ),
    _rule(
        "OC2001",
        "component-unassociated",
        Severity.WARNING,
        "A component could not be associated across views.",
        "Add an explicit component alias if the views intentionally use different names.",
    ),
    _rule(
        "OC2002",
        "alias-collision",
        Severity.FATAL,
        "Aliases map more than one object to the same identity.",
        "Make aliases one-to-one within a component and view.",
    ),
    _rule(
        "OC2003",
        "duplicate-definition",
        Severity.ERROR,
        "A view defines the same component more than once.",
        "Remove the duplicate or place intentional variants in distinct named views.",
    ),
    _rule(
        "OC2004",
        "duplicate-definition-conflict",
        Severity.ERROR,
        "Duplicate definitions have different interfaces.",
        "Select the intended definition or separate build variants into distinct views.",
    ),
    _rule(
        "OC2005",
        "ambiguous-name-normalization",
        Severity.ERROR,
        "Name normalization produced an ambiguous identity.",
        "Use exact aliases instead of relying on an ambiguous normalization rule.",
    ),
    _rule(
        "OC3001",
        "component-missing-from-view",
        Severity.ERROR,
        "A required view is missing a component.",
        "Add the component, correct its name, or adjust the participation policy.",
    ),
    _rule(
        "OC3002",
        "component-not-in-contract",
        Severity.ERROR,
        "A component is not present in the frozen contract.",
        "Update the frozen contract deliberately or remove the unexpected component.",
    ),
    _rule(
        "OC3101",
        "pin-missing-from-view",
        Severity.ERROR,
        "A participating view is missing a pin.",
        "Add the pin, correct its spelling, or declare an explicit alias/presence policy.",
    ),
    _rule(
        "OC3102",
        "pin-not-in-contract",
        Severity.ERROR,
        "A pin is not present in the frozen contract.",
        "Update the contract deliberately or remove the unexpected pin.",
    ),
    _rule(
        "OC3103",
        "duplicate-pin",
        Severity.ERROR,
        "A component view defines a pin more than once.",
        "Remove the duplicate declaration or correct bus-bit aggregation.",
    ),
    _rule(
        "OC3104",
        "likely-pin-name-mismatch",
        Severity.WARNING,
        "A missing pin resembles a differently named pin.",
        "Review the suggested spelling and add an explicit alias when intentional.",
    ),
    _rule(
        "OC4001",
        "direction-mismatch",
        Severity.ERROR,
        "Pin direction differs across views.",
        "Correct the direction in the outlying collateral or the canonical contract.",
    ),
    _rule(
        "OC4002",
        "unsupported-direction",
        Severity.WARNING,
        "A direction cannot be compared directly.",
        "Represent the direction explicitly or configure the view as not applicable.",
    ),
    _rule(
        "OC4101",
        "width-mismatch",
        Severity.ERROR,
        "Pin width differs across views.",
        "Correct the declared range, bus type, or CSV width.",
    ),
    _rule(
        "OC4102",
        "range-order-mismatch",
        Severity.ERROR,
        "Bus indices are declared in opposite orders.",
        "Correct the left/right bounds or provide an explicit bit mapping.",
    ),
    _rule(
        "OC4103",
        "dimension-shape-mismatch",
        Severity.ERROR,
        "Bus dimensions or index sets differ across views.",
        "Align packed dimensions and index bounds; equal width alone is insufficient.",
    ),
    _rule(
        "OC4104",
        "bus-bit-gap",
        Severity.ERROR,
        "An exploded bus has missing intermediate bits.",
        "Add the missing bit or correct the bit index.",
    ),
    _rule(
        "OC4105",
        "duplicate-bus-bit",
        Severity.ERROR,
        "An exploded bus repeats a bit index.",
        "Remove the duplicate bit entry.",
    ),
    _rule(
        "OC4106",
        "scalar-vector-mismatch",
        Severity.WARNING,
        "A scalar is represented as a one-bit vector in another view.",
        "Align the declarations or explicitly allow scalar/vector equivalence.",
    ),
    _rule(
        "OC4201",
        "role-mismatch",
        Severity.ERROR,
        "Pin roles differ across views.",
        "Correct the SIGNAL, CLOCK, RESET, POWER, GROUND, or ANALOG classification.",
    ),
    _rule(
        "OC4202",
        "power-ground-pin-missing",
        Severity.ERROR,
        "A required power or ground pin is missing.",
        "Add the rail pin or mark implicit RTL supplies optional in policy.",
    ),
    _rule(
        "OC4203",
        "power-ground-polarity-mismatch",
        Severity.ERROR,
        "A rail is power in one view and ground in another.",
        "Correct the PG type or LEF USE classification immediately.",
    ),
    _rule(
        "OC4204",
        "clock-reset-role-mismatch",
        Severity.ERROR,
        "Clock/reset classification differs across views.",
        "Correct the explicit role; name-based guesses are not used as truth.",
    ),
    _rule(
        "OC4301",
        "boolean-function-mismatch",
        Severity.ERROR,
        "RTL and Liberty Boolean functions are not equivalent.",
        "Correct the Liberty function or the RTL combinational implementation.",
    ),
    _rule(
        "OC4302",
        "boolean-function-uncheckable",
        Severity.WARNING,
        "Boolean equivalence cannot be established.",
        "Review unsupported sequential, four-state, tri-state, or oversized logic.",
    ),
    _rule(
        "OC4303",
        "liberty-function-references-unknown-pin",
        Severity.ERROR,
        "A Liberty function references an unknown input pin.",
        "Correct the function expression or add the missing pin.",
    ),
    _rule(
        "OC5001",
        "unknown-die-pad",
        Severity.ERROR,
        "A package row references an unknown die pad.",
        "Correct the die-pad name or add it to the canonical top-level pin inventory.",
    ),
    _rule(
        "OC5002",
        "unknown-package-signal",
        Severity.ERROR,
        "A package row references an unknown logical signal.",
        "Correct the signal/component name or add an explicit alias.",
    ),
    _rule(
        "OC5003",
        "duplicate-package-ball",
        Severity.ERROR,
        "A package ball is assigned more than once.",
        "Keep one assignment or mark an intentional multi-bond mapping explicitly.",
    ),
    _rule(
        "OC5004",
        "die-pad-mapped-multiple-times",
        Severity.ERROR,
        "A die pad is mapped to multiple balls.",
        "Correct the map or explicitly allow the intended multi-bond connection.",
    ),
    _rule(
        "OC5005",
        "conflicting-package-signal",
        Severity.ERROR,
        "Package mappings disagree about the signal on an endpoint.",
        "Correct the conflicting mapping rows.",
    ),
    _rule(
        "OC5006",
        "invalid-pin-map-row",
        Severity.ERROR,
        "A package/pin-map row is incomplete or invalid.",
        "Populate the required profile columns with string-preserving values.",
    ),
    _rule(
        "OC9001",
        "parser-plugin-failure",
        Severity.FATAL,
        "A parser plugin failed unexpectedly.",
        "Report the traceback and plugin version; source data was not treated as clean.",
    ),
    _rule(
        "OC9002",
        "checker-plugin-failure",
        Severity.FATAL,
        "A checker plugin failed unexpectedly.",
        "Report the traceback and plugin version; the affected check did not complete.",
    ),
    _rule(
        "OC9999",
        "internal-error",
        Severity.FATAL,
        "OpenCollate encountered an internal error.",
        "Report a minimal reproducer and the OpenCollate version.",
    ),
)


RULES: dict[str, RuleSpec] = {item.code: item for item in _RULE_LIST}


def get_rule(code: str) -> RuleSpec:
    normalized = code.strip().upper()
    try:
        return RULES[normalized]
    except KeyError as exc:
        raise KeyError(f"unknown OpenCollate rule code: {code!r}") from exc


def iter_rules() -> Iterable[RuleSpec]:
    return iter(sorted(RULES.values(), key=lambda item: item.code))


__all__ = ["RULES", "RuleSpec", "get_rule", "iter_rules"]

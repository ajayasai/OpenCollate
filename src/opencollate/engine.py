"""Reconciliation and semantic comparison engine.

The engine is intentionally parser-neutral: callers provide ``ViewObservation``
objects, and every diagnostic retains the observations that support it.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from opencollate import __version__
from opencollate.boolean import (
    BoolAnd,
    BoolConst,
    BooleanSyntaxError,
    BoolExpr,
    BoolNot,
    BoolOr,
    BoolVar,
    BoolXor,
    check_equivalence,
    parse_boolean,
)
from opencollate.catalog import RULES
from opencollate.config import AliasRule, ProjectConfig, Waiver
from opencollate.contracts import CONTRACT_SCHEMA_VERSION, snapshots_from_observations
from opencollate.diagnostics import (
    Diagnostic,
    DiagnosticEvidence,
    DiagnosticObject,
    Severity,
    json_safe,
    sort_diagnostics,
)
from opencollate.model import (
    BusShape,
    CanonicalComponent,
    CanonicalDesign,
    CanonicalPort,
    ClockObservation,
    ComponentMember,
    ComponentObservation,
    ConnectivityEdge,
    ConnectivityEndpoint,
    ConnectivityExpectation,
    ConnectivityRequirement,
    ConnectivityTransform,
    ContractComponent,
    ContractPort,
    ContractRegister,
    ContractRegisterField,
    DesignContract,
    DesignObjectObservation,
    Direction,
    FactState,
    PinMappingObservation,
    PortMember,
    PortObservation,
    PortRole,
    Provenance,
    RegisterFieldObservation,
    RegisterObservation,
    ViewId,
    ViewObservation,
    decoded_identifier,
)
from opencollate.plugins import CheckerContext, run_checker_plugins

_VIEW_KIND_ALIASES = {
    "sv": "rtl",
    "systemverilog": "rtl",
    "verilog": "rtl",
    "lib": "liberty",
    "pinmap": "csv",
    "pin_map": "csv",
    "pin-map": "csv",
    "ip_xact": "ipxact",
    "ip-xact": "ipxact",
    "spirit": "ipxact",
    "c_header": "header",
    "c-header": "header",
    "cheader": "header",
    "software": "header",
    "spice": "cdl",
    "sp": "cdl",
    "circuit": "cdl",
    "gdsii": "gds",
    "gds2": "gds",
    "stream": "gds",
    "rdl": "systemrdl",
    "system_rdl": "systemrdl",
    "system-rdl": "systemrdl",
    "conn": "connectivity",
    "connectivity_spec": "connectivity",
    "connectivity-spec": "connectivity",
}

_CONNECTIVITY_SELECTOR = re.compile(
    r"^(?P<base>.+?)(?:\[(?P<select>\*|[+-]?\d+|[+-]?\d+\s*:\s*[+-]?\d+)\])?$"
)
_MAX_CONNECTIVITY_PAIR_SEARCHES = 65_536
_MAX_CONNECTIVITY_REQUIREMENT_BITS = 1_024
_MAX_CONNECTIVITY_SEARCH_STATES = 500_000
_MAX_CONNECTIVITY_INDEX_DIGITS = 4_096
_CONNECTIVITY_SEARCH_LIMIT_SENTINEL = "@opencollate/search-limit"


def _semantic_view_kind(view: ViewId | str) -> str:
    kind = view.kind if isinstance(view, ViewId) else str(view)
    normalized = kind.strip().casefold()
    return _VIEW_KIND_ALIASES.get(normalized, normalized)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    design: CanonicalDesign
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class EngineResult:
    project: str
    design: CanonicalDesign
    diagnostics: tuple[Diagnostic, ...]
    generated_contract: DesignContract
    tool_version: str = __version__
    deny_warnings: bool = False

    @property
    def active_diagnostics(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if not item.waived)

    @property
    def exit_code(self) -> int:
        active = self.active_diagnostics
        if any(item.severity == Severity.FATAL for item in active):
            return 2
        if any(item.severity == Severity.ERROR for item in active):
            return 1
        if self.deny_warnings and any(item.severity == Severity.WARNING for item in active):
            return 1
        return 0

    @property
    def clean(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        active = self.active_diagnostics
        errors = sum(item.severity in {Severity.FATAL, Severity.ERROR} for item in active)
        warnings = sum(item.severity == Severity.WARNING for item in active)
        notes = sum(item.severity == Severity.INFO for item in active)
        suppressed = sum(item.waived for item in self.diagnostics)
        ports = sum(len(component.ports) for component in self.design.components)
        return {
            "schema_version": 1,
            "tool": {"name": "OpenCollate", "version": self.tool_version},
            "project": self.project,
            "status": "pass" if self.clean else "fail",
            "exit_code": self.exit_code,
            "summary": {
                "errors": errors,
                "warnings": warnings,
                "notes": notes,
                "suppressed": suppressed,
                "views": len(self.design.views),
                "components": len(self.design.components),
                "ports": ports,
                "registers": len(self.generated_contract.registers),
            },
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class _ValueEvidence:
    view: ViewId
    value: Any
    provenance: Provenance | None
    native_name: str | None = None
    order_known: bool = True

    def diagnostic_evidence(self) -> DiagnosticEvidence:
        return DiagnosticEvidence(
            self.view,
            self.value,
            self.provenance,
            native_name=self.native_name,
        )


class _AliasCollision(ValueError):
    def __init__(self, candidates: Sequence[str]) -> None:
        self.candidates = tuple(sorted(set(candidates)))
        super().__init__(", ".join(self.candidates))


class _AliasResolver:
    def __init__(
        self,
        config_rules: Iterable[AliasRule],
        contract: DesignContract | None,
    ) -> None:
        self.rules = list(config_rules)
        if contract is not None:
            for component in contract.components:
                for selector, native in component.names.items():
                    self.rules.append(
                        AliasRule(
                            "component",
                            component.canonical_name,
                            selector,
                            native,
                        )
                    )
                for port in component.ports:
                    for selector, native in port.names.items():
                        self.rules.append(
                            AliasRule(
                                "port",
                                port.canonical_name,
                                selector,
                                native,
                                component.canonical_name,
                            )
                        )

    @staticmethod
    def _selector_rank(view: ViewId, selector: str) -> int | None:
        normalized = selector.strip().lower()
        if normalized == view.key.lower():
            return 0
        if normalized == view.kind or _semantic_view_kind(normalized) == _semantic_view_kind(view):
            return 1
        if normalized == "*":
            return 3
        if fnmatchcase(view.key.lower(), normalized):
            return 2
        return None

    def _resolve(
        self,
        *,
        kind: str,
        view: ViewId,
        native: str,
        component: str | None,
    ) -> str:
        matches: list[tuple[int, str]] = []
        decoded = decoded_identifier(native)
        for rule in self.rules:
            if rule.kind != kind or rule.component != component:
                continue
            if decoded_identifier(rule.native) != decoded:
                continue
            rank = self._selector_rank(view, rule.view)
            if rank is not None:
                matches.append((rank, rule.canonical))
        if not matches:
            return decoded
        best_rank = min(rank for rank, _ in matches)
        candidates = {canonical for rank, canonical in matches if rank == best_rank}
        if len(candidates) != 1:
            raise _AliasCollision(tuple(candidates))
        return next(iter(candidates))

    def component(self, view: ViewId, native: str) -> str:
        return self._resolve(kind="component", view=view, native=native, component=None)

    def port(self, component: str, view: ViewId, native: str) -> str:
        return self._resolve(kind="port", view=view, native=native, component=component)


def _view_label(view: ViewId) -> str:
    kind = _semantic_view_kind(view)
    label = {
        "rtl": "RTL",
        "liberty": "Liberty",
        "lef": "LEF",
        "csv": "CSV",
        "ipxact": "IP-XACT",
        "header": "C header",
        "cdl": "CDL/SPICE",
        "gds": "GDSII",
        "systemrdl": "SystemRDL",
        "connectivity": "connectivity intent",
        "contract": "the contract",
    }.get(kind, kind.upper())
    if view.name in {"default", "frozen"}:
        return label
    return f"{label} ({view.name})"


def _object(kind: str, entity_id: str, display: str) -> DiagnosticObject:
    return DiagnosticObject(kind, entity_id, display)


def _stable_value_key(value: Any) -> str:
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"))


def _canonicalize_boolean_expression(expression: BoolExpr, aliases: Mapping[str, str]) -> BoolExpr:
    """Rename variables with aliases from the expression's originating view."""

    if isinstance(expression, BoolVar):
        canonical = aliases.get(
            expression.name,
            aliases.get(decoded_identifier(expression.name), expression.name),
        )
        return BoolVar(canonical)
    if isinstance(expression, BoolConst):
        return expression
    if isinstance(expression, BoolNot):
        return BoolNot(_canonicalize_boolean_expression(expression.operand, aliases))
    if isinstance(expression, (BoolAnd, BoolOr, BoolXor)):
        return type(expression)(
            tuple(
                _canonicalize_boolean_expression(operand, aliases)
                for operand in expression.operands
            )
        )
    raise TypeError(f"unsupported Boolean IR node: {type(expression).__name__}")


def _shape_structure(shape: BusShape) -> tuple[Any, ...] | None:
    """Return comparable explicit shape, leaving width-only views unspecified."""

    ordered = shape.ordered_indices
    if ordered is not None:
        packed: tuple[Any, ...] = ("ordered", ordered)
    elif shape.packed:
        packed = (
            "dimensions",
            tuple((dimension.left, dimension.right) for dimension in shape.packed),
        )
    elif shape.unpacked:
        packed = ("scalar" if shape.explicit_scalar else "unspecified",)
    else:
        return None
    unpacked = tuple((dimension.left, dimension.right) for dimension in shape.unpacked)
    return packed, unpacked


def _value_text(value: Any, property_name: str) -> str:
    if isinstance(value, Direction):
        return value.value
    if isinstance(value, PortRole):
        return value.value
    if property_name == "shape.width" and isinstance(value, int):
        return f"{value} bit" if value == 1 else f"{value} bits"
    if isinstance(value, BusShape):
        return json.dumps(value.to_dict(), sort_keys=True)
    if isinstance(value, Mapping):
        return json.dumps(json_safe(value), sort_keys=True)
    return str(value)


def _conflict_message(
    display: str,
    noun: str,
    property_name: str,
    values: Sequence[_ValueEvidence],
) -> str:
    grouped: dict[str, list[_ValueEvidence]] = defaultdict(list)
    for item in values:
        grouped[_stable_value_key(item.value)].append(item)
    ordered_groups = list(grouped.values())
    if len(ordered_groups) == 2 and len(values) == 2:
        left, right = ordered_groups
        return (
            f"{display} is {_value_text(left[0].value, property_name)} in "
            f"{_view_label(left[0].view)} but "
            f"{_value_text(right[0].value, property_name)} in "
            f"{_view_label(right[0].view)}."
        )
    parts = []
    for group in ordered_groups:
        labels = ", ".join(_view_label(item.view) for item in group)
        parts.append(f"{labels} = {_value_text(group[0].value, property_name)}")
    return f"{display} has conflicting {noun}: " + "; ".join(parts) + "."


class ComparisonEngine:
    """Reconcile source observations and run all built-in MVP checks."""

    def __init__(self, config: ProjectConfig) -> None:
        self.config = config

    def run(
        self,
        observations: Iterable[ViewObservation],
        *,
        contract: DesignContract | None = None,
        today: date | None = None,
    ) -> EngineResult:
        observed = tuple(sorted(observations, key=lambda item: item.view))
        if contract is None:
            contract = self.config.load_contract()
        resolver = _AliasResolver(self.config.aliases, contract)
        reconciliation = self._reconcile(observed, resolver, contract)
        diagnostics: list[Diagnostic] = list(reconciliation.diagnostics)

        for view in observed:
            for item in view.diagnostics:
                diagnostics.append(self._coerce_parser_diagnostic(item, view.view))
            if not view.complete and not view.diagnostics:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC1104",
                        f"{_view_label(view.view)} is incomplete; absence checks are suppressed.",
                        metadata={"view": str(view.view)},
                    )
                )

        observation_map = {item.view: item for item in observed}
        diagnostics.extend(
            self._check_design(
                reconciliation.design,
                observation_map,
                resolver,
                contract,
            )
        )
        diagnostics.extend(
            self._check_pin_mappings(
                reconciliation.design,
                observed,
                resolver,
            )
        )
        diagnostics.extend(self._check_object_references(reconciliation.design, observed))
        diagnostics.extend(self._check_clocks(reconciliation.design, observed))
        diagnostics.extend(self._check_interfaces(reconciliation.design, observed, resolver))
        diagnostics.extend(self._check_registers(observed, contract))
        diagnostics.extend(self._check_connectivity(observed))
        analysis_date = today or date.today()
        generated = self.build_contract(reconciliation.design, observed)
        diagnostics.extend(
            run_checker_plugins(
                CheckerContext(
                    config=self.config,
                    observations=observed,
                    design=reconciliation.design,
                    contract=contract,
                    generated_contract=generated,
                    today=analysis_date,
                )
            )
        )
        diagnostics = self._apply_severity_overrides(diagnostics)
        diagnostics = self._apply_waivers(diagnostics, today=analysis_date)
        ordered = sort_diagnostics(diagnostics)
        return EngineResult(
            project=self.config.name,
            design=reconciliation.design,
            diagnostics=ordered,
            generated_contract=generated,
            deny_warnings=self.config.policy.deny_warnings,
        )

    def reconcile(
        self,
        observations: Iterable[ViewObservation],
        *,
        contract: DesignContract | None = None,
    ) -> ReconciliationResult:
        observed = tuple(sorted(observations, key=lambda item: item.view))
        if contract is None:
            contract = self.config.load_contract()
        return self._reconcile(
            observed,
            _AliasResolver(self.config.aliases, contract),
            contract,
        )

    def _reconcile(
        self,
        observations: Sequence[ViewObservation],
        resolver: _AliasResolver,
        contract: DesignContract | None,
    ) -> ReconciliationResult:
        diagnostics: list[Diagnostic] = []
        collision_keys: set[tuple[str, str, str]] = set()
        groups: dict[str, list[ComponentMember]] = defaultdict(list)

        for view in observations:
            for component in sorted(
                view.components,
                key=lambda item: (
                    decoded_identifier(item.native_name),
                    item.provenance.source if item.provenance else "",
                    item.provenance.line if item.provenance else 0,
                ),
            ):
                try:
                    canonical = resolver.component(view.view, component.native_name)
                except _AliasCollision as error:
                    canonical = decoded_identifier(component.native_name)
                    collision_key = ("component", str(view.view), component.native_name)
                    if collision_key not in collision_keys:
                        diagnostics.append(
                            Diagnostic.from_rule(
                                "OC2002",
                                f"{_view_label(view.view)} component {component.native_name!r} "
                                f"matches conflicting aliases: {', '.join(error.candidates)}.",
                                provenance=component.provenance,
                                object=_object(
                                    "component",
                                    f"component:{canonical}",
                                    canonical,
                                ),
                            )
                        )
                        collision_keys.add(collision_key)
                groups[canonical].append(ComponentMember(view.view, component))

        active_views = set(self.config.views)
        active_views.update(item.view for item in observations)
        canonical_components: list[CanonicalComponent] = []
        for canonical, members_list in sorted(groups.items()):
            members = tuple(
                sorted(
                    members_list,
                    key=lambda item: (
                        item.view,
                        item.observation.native_name,
                        item.observation.provenance.source if item.observation.provenance else "",
                    ),
                )
            )
            diagnostics.extend(self._duplicate_component_diagnostics(canonical, members))
            port_groups: dict[str, list[PortMember]] = defaultdict(list)
            for member in members:
                if self._is_package_map_view(member.view):
                    # Package rows reference signals but are not a complete pin
                    # inventory. Dedicated mapping checks consume them later.
                    continue
                seen: dict[str, PortObservation] = {}
                for port in member.observation.ports:
                    try:
                        port_name = resolver.port(canonical, member.view, port.native_name)
                    except _AliasCollision as error:
                        port_name = decoded_identifier(port.native_name)
                        collision_key = (
                            f"port:{canonical}",
                            str(member.view),
                            port.native_name,
                        )
                        if collision_key not in collision_keys:
                            diagnostics.append(
                                Diagnostic.from_rule(
                                    "OC2002",
                                    f"{canonical}/{port.native_name} matches conflicting "
                                    f"aliases: {', '.join(error.candidates)}.",
                                    provenance=port.provenance,
                                    object=_object(
                                        "port",
                                        f"component:{canonical}/port:{port_name}",
                                        f"{canonical}/{port_name}",
                                    ),
                                )
                            )
                            collision_keys.add(collision_key)
                    if port_name in seen:
                        first = seen[port_name]
                        diagnostics.append(
                            Diagnostic.from_rule(
                                "OC3103",
                                f"{_view_label(member.view)} defines "
                                f"{canonical}/{port_name} more than once.",
                                provenance=port.provenance,
                                object=_object(
                                    "port",
                                    f"component:{canonical}/port:{port_name}",
                                    f"{canonical}/{port_name}",
                                ),
                                evidence=(
                                    DiagnosticEvidence(
                                        member.view,
                                        first.native_name,
                                        first.provenance,
                                    ),
                                    DiagnosticEvidence(
                                        member.view,
                                        port.native_name,
                                        port.provenance,
                                    ),
                                ),
                            )
                        )
                    else:
                        seen[port_name] = port
                    port_groups[port_name].append(
                        PortMember(member.view, member.observation.native_name, port)
                    )
            ports = tuple(
                CanonicalPort(
                    name,
                    tuple(
                        sorted(
                            port_members,
                            key=lambda item: (
                                item.view,
                                item.component_native_name,
                                item.observation.native_name,
                            ),
                        )
                    ),
                )
                for name, port_members in sorted(port_groups.items())
            )
            required_views = self._required_component_views(canonical, active_views, contract)
            canonical_components.append(
                CanonicalComponent(canonical, members, ports, required_views)
            )

        design = CanonicalDesign(tuple(canonical_components), tuple(sorted(active_views)))
        diagnostics.extend(self._unassociated_component_diagnostics(design))
        return ReconciliationResult(design, sort_diagnostics(diagnostics))

    def _duplicate_component_diagnostics(
        self,
        canonical: str,
        members: Sequence[ComponentMember],
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        by_view: dict[ViewId, list[ComponentObservation]] = defaultdict(list)
        for member in members:
            by_view[member.view].append(member.observation)
        for view, definitions in sorted(by_view.items()):
            if len(definitions) < 2:
                continue
            signatures = {definition.interface_signature() for definition in definitions}
            code = "OC2003" if len(signatures) == 1 else "OC2004"
            qualifier = "identical" if code == "OC2003" else "conflicting"
            diagnostics.append(
                Diagnostic.from_rule(
                    code,
                    f"{_view_label(view)} contains {len(definitions)} {qualifier} "
                    f"definitions of {canonical}.",
                    object=_object("component", f"component:{canonical}", canonical),
                    evidence=tuple(
                        DiagnosticEvidence(
                            view,
                            definition.native_name,
                            definition.provenance,
                        )
                        for definition in definitions
                    ),
                )
            )
        return diagnostics

    def _unassociated_component_diagnostics(self, design: CanonicalDesign) -> list[Diagnostic]:
        """Suggest only high-confidence matches; never reconcile fuzzily."""

        singletons = [item for item in design.components if len(item.views()) == 1]
        diagnostics: list[Diagnostic] = []
        used: set[tuple[str, str]] = set()
        for left_index, left in enumerate(singletons):
            best: tuple[float, CanonicalComponent] | None = None
            for right in singletons[left_index + 1 :]:
                if left.views()[0].kind == right.views()[0].kind:
                    continue
                score = SequenceMatcher(
                    None,
                    left.canonical_name.casefold(),
                    right.canonical_name.casefold(),
                ).ratio()
                if score < 0.92:
                    continue
                if best is None or score > best[0]:
                    best = (score, right)
            if best is None:
                continue
            right = best[1]
            key = (min(left.id, right.id), max(left.id, right.id))
            if key in used:
                continue
            used.add(key)
            diagnostics.append(
                Diagnostic.from_rule(
                    "OC2001",
                    f"{left.canonical_name!r} in {_view_label(left.views()[0])} "
                    f"resembles {right.canonical_name!r} in "
                    f"{_view_label(right.views()[0])}, but they are not associated.",
                    object=_object("component", left.id, left.canonical_name),
                    evidence=tuple(
                        DiagnosticEvidence(
                            member.view,
                            member.observation.native_name,
                            member.observation.provenance,
                        )
                        for component in (left, right)
                        for member in component.members
                    ),
                )
            )
        return diagnostics

    def _required_component_views(
        self,
        canonical: str,
        active_views: Iterable[ViewId],
        contract: DesignContract | None,
    ) -> tuple[ViewId, ...]:
        active = set(active_views)
        active = {view for view in active if self._is_component_contract_view(view)}
        required: set[ViewId] = set(active if self.config.policy.strict_inventory else ())
        optional: set[ViewId] = set()
        for rule in self.config.participation:
            if fnmatchcase(canonical, rule.component):
                required.update(self._expand_view_selectors(rule.views, active))
                optional.update(self._expand_view_selectors(rule.optional_views, active))
        contract_component = self._contract_component(contract, canonical)
        if contract_component is not None:
            required.update(self._expand_view_selectors(contract_component.required_views, active))
            required.update(self._expand_view_selectors(contract_component.names.keys(), active))
        return tuple(sorted(required - optional))

    @staticmethod
    def _expand_view_selectors(
        selectors: Iterable[str], active_views: Iterable[ViewId]
    ) -> set[ViewId]:
        active = set(active_views)
        expanded: set[ViewId] = set()
        for raw_selector in selectors:
            selector = str(raw_selector).strip().lower()
            matches = {
                view
                for view in active
                if selector in {"*", view.kind, view.key.lower()}
                or _semantic_view_kind(selector) == _semantic_view_kind(view)
                or fnmatchcase(view.key.lower(), selector)
            }
            if matches:
                expanded.update(matches)
            elif "*" not in selector and "?" not in selector and "[" not in selector:
                expanded.add(ViewId.parse(selector))
        return expanded

    def _check_design(
        self,
        design: CanonicalDesign,
        observations: Mapping[ViewId, ViewObservation],
        resolver: _AliasResolver,
        contract: DesignContract | None,
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        diagnostics.extend(self._check_component_inventory(design, observations, contract))
        for component in design.components:
            contract_component = self._contract_component(contract, component.canonical_name)
            diagnostics.extend(
                self._check_component_ports(
                    component,
                    observations,
                    resolver,
                    contract_component,
                )
            )
            diagnostics.extend(
                self._check_functions(component, resolver)
                if self.config.policy.compare_functions
                else ()
            )
        return diagnostics

    def _check_component_inventory(
        self,
        design: CanonicalDesign,
        observations: Mapping[ViewId, ViewObservation],
        contract: DesignContract | None,
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        design_by_name = {item.canonical_name: item for item in design.components}
        for component in design.components:
            present = set(component.views())
            missing = [
                view
                for view in component.required_views
                if view not in present and not self._whole_view_tainted(observations.get(view))
            ]
            if missing:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC3001",
                        f"{component.canonical_name} is missing from required "
                        f"{'view' if len(missing) == 1 else 'views'} "
                        + ", ".join(_view_label(view) for view in missing)
                        + ".",
                        object=_object("component", component.id, component.canonical_name),
                        evidence=tuple(
                            DiagnosticEvidence(
                                member.view,
                                member.observation.native_name,
                                member.observation.provenance,
                            )
                            for member in component.members
                        ),
                        metadata={"missing_views": [str(view) for view in missing]},
                    )
                )
        if contract is None:
            return diagnostics
        contract_by_name = {item.canonical_name: item for item in contract.components}
        for component in design.components:
            if component.canonical_name not in contract_by_name:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC3002",
                        f"{component.canonical_name} is present in source collateral "
                        "but not in the frozen contract.",
                        object=_object("component", component.id, component.canonical_name),
                        evidence=tuple(
                            DiagnosticEvidence(
                                member.view,
                                member.observation.native_name,
                                member.observation.provenance,
                            )
                            for member in component.members
                        ),
                    )
                )
        active = set(design.views)
        for expected in contract.components:
            if expected.canonical_name in design_by_name:
                continue
            expected_views = self._expand_view_selectors(
                expected.required_views or tuple(expected.names), active
            )
            diagnostics.append(
                Diagnostic.from_rule(
                    "OC3001",
                    f"{expected.canonical_name} is required by the frozen contract "
                    + (
                        "in " + ", ".join(_view_label(view) for view in sorted(expected_views))
                        if expected_views
                        else "but is absent from all source views"
                    )
                    + ".",
                    object=_object(
                        "component",
                        f"component:{expected.canonical_name}",
                        expected.canonical_name,
                    ),
                    metadata={"missing_views": [str(view) for view in sorted(expected_views)]},
                )
            )
        return diagnostics

    @staticmethod
    def _whole_view_tainted(observation: ViewObservation | None) -> bool:
        return observation is not None and (
            (not observation.complete and not observation.tainted_scopes)
            or "*" in observation.tainted_scopes
        )

    def _scope_tainted(
        self,
        observation: ViewObservation | None,
        native_component_names: Iterable[str],
    ) -> bool:
        if observation is None:
            return False
        if self._whole_view_tainted(observation):
            return True
        names = set(native_component_names)
        return bool(names & set(observation.tainted_scopes))

    def _check_component_ports(
        self,
        component: CanonicalComponent,
        observations: Mapping[ViewId, ViewObservation],
        resolver: _AliasResolver,
        contract: ContractComponent | None,
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        contract_ports = {item.canonical_name: item for item in contract.ports} if contract else {}
        design_ports = {item.canonical_name: item for item in component.ports}
        component_views = {
            view for view in component.views() if not self._is_package_map_view(view)
        }
        default_inventory_views = {
            view for view in component_views if self._is_component_contract_view(view)
        }
        native_components_by_view: dict[ViewId, set[str]] = defaultdict(set)
        for member in component.members:
            native_components_by_view[member.view].add(member.observation.native_name)

        if contract is not None:
            for port in component.ports:
                if port.canonical_name not in contract_ports:
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC3102",
                            f"{component.canonical_name}/{port.canonical_name} is present "
                            "in source collateral but not in the frozen contract.",
                            object=_object(
                                "port",
                                component.port_id(port.canonical_name),
                                f"{component.canonical_name}/{port.canonical_name}",
                            ),
                            evidence=self._port_evidence(port, "present"),
                        )
                    )
            for expected_port in contract.ports:
                if expected_port.canonical_name in design_ports:
                    continue
                expected_views = component_views | self._expand_view_selectors(
                    expected_port.names.keys(), component_views
                )
                expected_views = self._filter_optional_pg_views(expected_views, expected_port.role)
                code = (
                    "OC4202"
                    if expected_port.role in {PortRole.POWER, PortRole.GROUND}
                    else "OC3101"
                )
                display = f"{component.canonical_name}/{expected_port.canonical_name}"
                diagnostics.append(
                    Diagnostic.from_rule(
                        code,
                        f"{display} is required by the frozen contract but is missing "
                        "from "
                        + ", ".join(_view_label(view) for view in sorted(expected_views))
                        + ".",
                        object=_object(
                            "port",
                            component.port_id(expected_port.canonical_name),
                            display,
                        ),
                        property_name="presence",
                        metadata={"missing_views": [str(view) for view in sorted(expected_views)]},
                    )
                )

        for port in component.ports:
            expected = contract_ports.get(port.canonical_name)
            role = self._dominant_role(port, expected)
            required_views = set(default_inventory_views)
            for rule in self.config.participation:
                if not fnmatchcase(component.canonical_name, rule.component):
                    continue
                if rule.roles and role not in rule.roles:
                    continue
                required_views.update(self._expand_view_selectors(rule.views, component_views))
                required_views.difference_update(
                    self._expand_view_selectors(rule.optional_views, component_views)
                )
            if expected is not None:
                required_views.update(
                    self._expand_view_selectors(expected.names.keys(), component_views)
                )
            # A missing component already has OC3001; do not emit every pin too.
            required_views.intersection_update(component_views)
            required_views = self._filter_optional_pg_views(required_views, role)
            present_views = set(port.views())
            missing = [
                view
                for view in sorted(required_views - present_views)
                if not self._scope_tainted(observations.get(view), native_components_by_view[view])
            ]
            if missing:
                code = "OC4202" if role in {PortRole.POWER, PortRole.GROUND} else "OC3101"
                display = f"{component.canonical_name}/{port.canonical_name}"
                diagnostics.append(
                    Diagnostic.from_rule(
                        code,
                        f"{display} is present in "
                        + ", ".join(_view_label(view) for view in sorted(present_views))
                        + " but missing from "
                        + ", ".join(_view_label(view) for view in missing)
                        + ".",
                        object=_object("port", component.port_id(port.canonical_name), display),
                        property_name="presence",
                        evidence=self._port_evidence(port, "present"),
                        metadata={"missing_views": [str(view) for view in missing]},
                    )
                )
            diagnostics.extend(self._check_port_values(component, port, expected))
            diagnostics.extend(self._check_bus_integrity(component, port))
        diagnostics.extend(self._likely_port_name_diagnostics(component))
        return diagnostics

    def _filter_optional_pg_views(self, views: Iterable[ViewId], role: PortRole) -> set[ViewId]:
        result = set(views)
        if role not in {PortRole.POWER, PortRole.GROUND}:
            return result
        if self.config.policy.rtl_power_pins in {"optional", "ignore"}:
            result = {view for view in result if _semantic_view_kind(view) != "rtl"}
        return result

    def _is_package_map_view(self, view: ViewId) -> bool:
        if _semantic_view_kind(view) != "csv":
            return False
        try:
            source = self.config.source(view)
        except KeyError:
            return False
        return (source.profile or "").strip().lower().replace("-", "_") == "package_map"

    def _is_component_contract_view(self, view: ViewId) -> bool:
        if self._is_package_map_view(view):
            return False
        return _semantic_view_kind(view) not in {
            "sdc",
            "upf",
            "header",
        }

    @staticmethod
    def _port_evidence(port: CanonicalPort, value: Any) -> tuple[DiagnosticEvidence, ...]:
        return tuple(
            DiagnosticEvidence(
                member.view,
                value,
                member.observation.provenance,
                native_name=member.observation.native_name,
            )
            for member in port.members
        )

    @staticmethod
    def _dominant_role(port: CanonicalPort, expected: ContractPort | None) -> PortRole:
        if expected is not None and expected.role != PortRole.UNKNOWN:
            return expected.role
        known = [
            member.observation.role
            for member in port.members
            if member.observation.state_for("role") == FactState.KNOWN
            and member.observation.role != PortRole.UNKNOWN
        ]
        if not known:
            return PortRole.UNKNOWN
        if PortRole.POWER in known:
            return PortRole.POWER
        if PortRole.GROUND in known:
            return PortRole.GROUND
        return known[0]

    def _check_port_values(
        self,
        component: CanonicalComponent,
        port: CanonicalPort,
        expected: ContractPort | None,
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        display = f"{component.canonical_name}/{port.canonical_name}"
        entity = _object("port", component.port_id(port.canonical_name), display)
        directions = self._field_values(port, "direction", expected)
        if self._has_conflict(directions):
            diagnostics.append(
                self._conflict_diagnostic(
                    "OC4001", display, "directions", "direction", entity, directions
                )
            )

        roles = self._field_values(port, "role", expected)
        if self._has_conflict(roles):
            role_set = {item.value for item in roles}
            if PortRole.POWER in role_set and PortRole.GROUND in role_set:
                code = "OC4203"
            elif role_set & {PortRole.CLOCK, PortRole.RESET}:
                code = "OC4204"
            else:
                code = "OC4201"
            diagnostics.append(
                self._conflict_diagnostic(code, display, "roles", "role", entity, roles)
            )

        shapes = self._shape_values(port, expected)
        widths = [
            _ValueEvidence(item.view, item.value.width, item.provenance, item.native_name)
            for item in shapes
            if item.value.width is not None
        ]
        if self._has_conflict(widths):
            diagnostics.append(
                self._conflict_diagnostic(
                    "OC4101",
                    display,
                    "widths",
                    "shape.width",
                    entity,
                    widths,
                )
            )
            return diagnostics

        if len(shapes) < 2:
            return diagnostics
        scalar_flags = [
            _ValueEvidence(
                item.view,
                item.value.explicit_scalar,
                item.provenance,
                item.native_name,
            )
            for item in shapes
            if item.value.width == 1 and item.value.explicit_scalar is not None
        ]
        if not self.config.policy.scalar_vector_equivalent and self._has_conflict(scalar_flags):
            diagnostics.append(
                Diagnostic.from_rule(
                    "OC4106",
                    f"{display} is a scalar in some views but a one-bit vector in others.",
                    object=entity,
                    property_name="shape.scalar",
                    evidence=tuple(item.diagnostic_evidence() for item in scalar_flags),
                )
            )

        structured = [
            (item, structure)
            for item in shapes
            if (structure := _shape_structure(item.value)) is not None
        ]
        if (
            len(structured) >= 2
            and len({_stable_value_key(structure) for _, structure in structured}) > 1
        ):
            ordered_indices = [item.value.ordered_indices for item, _ in structured]
            unpacked_shapes = {
                tuple((dimension.left, dimension.right) for dimension in item.value.unpacked)
                for item, _ in structured
            }
            range_order_only = (
                all(indices is not None for indices in ordered_indices)
                and len({tuple(indices or ()) for indices in ordered_indices}) > 1
                and len({frozenset(indices or ()) for indices in ordered_indices}) == 1
                and len(unpacked_shapes) == 1
            )
            # Individually listed physical pins establish membership and width,
            # but their statement order does not declare logical bus ordering.
            if range_order_only and not all(item.order_known for item, _ in structured):
                return diagnostics
            code = "OC4102" if range_order_only else "OC4103"
            message = (
                f"{display} has the same bit indices but reversed or inconsistent ordering."
                if range_order_only
                else f"{display} has equal width but different declared index sets or dimensions."
            )
            diagnostics.append(
                Diagnostic.from_rule(
                    code,
                    message,
                    object=entity,
                    property_name=("shape.index_order" if range_order_only else "shape.dimensions"),
                    evidence=tuple(
                        DiagnosticEvidence(
                            item.view,
                            item.value.to_dict(),
                            item.provenance,
                            native_name=item.native_name,
                        )
                        for item, _ in structured
                    ),
                )
            )
        return diagnostics

    @staticmethod
    def _field_values(
        port: CanonicalPort,
        field_name: str,
        expected: ContractPort | None,
    ) -> list[_ValueEvidence]:
        values: list[_ValueEvidence] = []
        for member in port.members:
            observation = member.observation
            if observation.state_for(field_name) != FactState.KNOWN:
                continue
            value = getattr(observation, field_name)
            if value in {Direction.UNKNOWN, PortRole.UNKNOWN}:
                continue
            values.append(
                _ValueEvidence(
                    member.view,
                    value,
                    observation.provenance,
                    observation.native_name,
                )
            )
        if expected is not None:
            value = getattr(expected, field_name)
            if value not in {Direction.UNKNOWN, PortRole.UNKNOWN}:
                values.append(_ValueEvidence(ViewId("contract", "frozen"), value, None))
        return values

    @staticmethod
    def _shape_values(port: CanonicalPort, expected: ContractPort | None) -> list[_ValueEvidence]:
        values = [
            _ValueEvidence(
                member.view,
                member.observation.shape,
                member.observation.provenance,
                member.observation.native_name,
                member.observation.attributes.get("bit_order_known") is not False,
            )
            for member in port.members
            if member.observation.state_for("shape") == FactState.KNOWN
            and member.observation.shape.known
        ]
        if expected is not None and expected.shape.known:
            values.append(_ValueEvidence(ViewId("contract", "frozen"), expected.shape, None))
        return values

    def _ordered_values(self, values: Sequence[_ValueEvidence]) -> list[_ValueEvidence]:
        baseline = self.config.contract.baseline
        return sorted(
            values,
            key=lambda item: (
                0 if baseline is not None and item.view == baseline else 1,
                item.view,
                item.native_name or "",
            ),
        )

    @staticmethod
    def _has_conflict(values: Sequence[_ValueEvidence]) -> bool:
        return len({_stable_value_key(item.value) for item in values}) > 1

    def _conflict_diagnostic(
        self,
        code: str,
        display: str,
        noun: str,
        property_name: str,
        entity: DiagnosticObject,
        values: Sequence[_ValueEvidence],
    ) -> Diagnostic:
        ordered = self._ordered_values(values)
        return Diagnostic.from_rule(
            code,
            _conflict_message(display, noun, property_name, ordered),
            object=entity,
            property_name=property_name,
            evidence=tuple(item.diagnostic_evidence() for item in ordered),
        )

    def _check_bus_integrity(
        self, component: CanonicalComponent, port: CanonicalPort
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        display = f"{component.canonical_name}/{port.canonical_name}"
        entity = _object("port", component.port_id(port.canonical_name), display)
        for member in port.members:
            shape = member.observation.shape
            if member.observation.state_for("shape") != FactState.KNOWN:
                continue
            if shape.has_duplicate_bits:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC4105",
                        f"{display} repeats a bus bit in {_view_label(member.view)}: "
                        f"{list(shape.bit_indices)}.",
                        object=entity,
                        property_name="shape.bit_indices",
                        evidence=(
                            DiagnosticEvidence(
                                member.view,
                                list(shape.bit_indices),
                                member.observation.provenance,
                                native_name=member.observation.native_name,
                            ),
                        ),
                    )
                )
            if shape.has_bit_gap:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC4104",
                        f"{display} has noncontiguous bus bits in {_view_label(member.view)}: "
                        f"{list(shape.bit_indices)}.",
                        object=entity,
                        property_name="shape.bit_indices",
                        evidence=(
                            DiagnosticEvidence(
                                member.view,
                                list(shape.bit_indices),
                                member.observation.provenance,
                                native_name=member.observation.native_name,
                            ),
                        ),
                    )
                )
        return diagnostics

    def _likely_port_name_diagnostics(self, component: CanonicalComponent) -> list[Diagnostic]:
        single_view_ports = [port for port in component.ports if len(port.views()) == 1]
        diagnostics: list[Diagnostic] = []
        used: set[tuple[str, str]] = set()
        for index, left in enumerate(single_view_ports):
            for right in single_view_ports[index + 1 :]:
                if left.views()[0].kind == right.views()[0].kind:
                    continue
                score = SequenceMatcher(
                    None,
                    left.canonical_name.casefold(),
                    right.canonical_name.casefold(),
                ).ratio()
                if score < 0.9:
                    continue
                key = (
                    min(left.canonical_name, right.canonical_name),
                    max(left.canonical_name, right.canonical_name),
                )
                if key in used:
                    continue
                used.add(key)
                display = f"{component.canonical_name}/{left.canonical_name}"
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC3104",
                        f"{component.canonical_name}/{left.canonical_name} in "
                        f"{_view_label(left.views()[0])} resembles "
                        f"{component.canonical_name}/{right.canonical_name} in "
                        f"{_view_label(right.views()[0])}; add an alias if they are the same pin.",
                        object=_object("port", component.port_id(left.canonical_name), display),
                        property_name="name",
                        evidence=self._port_evidence(left, left.canonical_name)
                        + self._port_evidence(right, right.canonical_name),
                    )
                )
        return diagnostics

    def _check_functions(
        self, component: CanonicalComponent, resolver: _AliasResolver
    ) -> list[Diagnostic]:
        functions: dict[str, list[_ValueEvidence]] = defaultdict(list)
        aliases_by_view: dict[ViewId, dict[str, str]] = defaultdict(dict)
        native_ports_by_view: dict[ViewId, set[str]] = defaultdict(set)
        for member in component.members:
            for port in member.observation.ports:
                canonical_port = resolver.port(
                    component.canonical_name, member.view, port.native_name
                )
                aliases_by_view[member.view][port.native_name] = canonical_port
                aliases_by_view[member.view][decoded_identifier(port.native_name)] = canonical_port
                native_ports_by_view[member.view].add(port.native_name)
            for native_output, expression in member.observation.functions.items():
                try:
                    canonical_output = resolver.port(
                        component.canonical_name, member.view, native_output
                    )
                except _AliasCollision:
                    canonical_output = decoded_identifier(native_output)
                functions[canonical_output].append(
                    _ValueEvidence(
                        member.view,
                        expression,
                        member.observation.provenance,
                        native_output,
                    )
                )

        diagnostics: list[Diagnostic] = []
        for output, values in sorted(functions.items()):
            if len({_semantic_view_kind(item.view) for item in values}) < 2:
                continue
            ordered = self._ordered_values(values)
            parsed: list[tuple[_ValueEvidence, BoolExpr]] = []
            display = f"{component.canonical_name}/{output}"
            entity = _object("port", component.port_id(output), display)
            parse_failed = False
            for item in ordered:
                expression = self._function_expression(item.value)
                try:
                    parsed_expression = (
                        expression
                        if isinstance(expression, BoolExpr)
                        else parse_boolean(str(expression))
                    )
                except (BooleanSyntaxError, TypeError, ValueError) as error:
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC4302",
                            f"{display} Boolean function in {_view_label(item.view)} "
                            f"cannot be checked: {error}.",
                            object=entity,
                            property_name="boolean_function",
                            evidence=(item.diagnostic_evidence(),),
                        )
                    )
                    parse_failed = True
                    continue
                native_names = native_ports_by_view.get(item.view, set())
                unknown = {
                    name
                    for name in parsed_expression.variables()
                    if name not in native_names
                    and decoded_identifier(name)
                    not in {decoded_identifier(value) for value in native_names}
                }
                if unknown and _semantic_view_kind(item.view) == "liberty":
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC4303",
                            f"{display} Liberty function references unknown "
                            f"{'pin' if len(unknown) == 1 else 'pins'} "
                            + ", ".join(sorted(unknown))
                            + ".",
                            object=entity,
                            property_name="boolean_function.variables",
                            evidence=(item.diagnostic_evidence(),),
                        )
                    )
                    parse_failed = True
                parsed.append(
                    (
                        item,
                        _canonicalize_boolean_expression(
                            parsed_expression, aliases_by_view[item.view]
                        ),
                    )
                )
            if parse_failed or len(parsed) < 2:
                continue
            _reference_item, reference_expression = parsed[0]
            mismatch = False
            indeterminate_reason: str | None = None
            counterexample: Mapping[str, bool] | None = None
            for _item, expression in parsed[1:]:
                if self.config.policy.boolean_backend == "z3":
                    from opencollate.symbolic import SymbolicLimits, check_symbolic_equivalence

                    symbolic = check_symbolic_equivalence(
                        reference_expression,
                        expression,
                        limits=SymbolicLimits(
                            max_variables=self.config.policy.max_symbolic_inputs,
                            timeout_ms=self.config.policy.symbolic_timeout_ms,
                            resource_limit=self.config.policy.symbolic_resource_limit,
                            max_queries=self.config.policy.max_symbolic_inputs + 2,
                        ),
                    )
                    from opencollate.boolean import EquivalenceResult

                    result = EquivalenceResult(
                        equivalent=symbolic.equivalent,
                        variables=symbolic.variables,
                        checked_assignments=1 if symbolic.counterexample is not None else 0,
                        counterexample=symbolic.counterexample,
                        reason=symbolic.reason,
                    )
                else:
                    result = check_equivalence(
                        reference_expression,
                        expression,
                        max_variables=self.config.policy.max_boolean_inputs,
                    )
                if result.equivalent is None:
                    indeterminate_reason = result.reason
                    break
                if result.equivalent is False:
                    mismatch = True
                    counterexample = result.counterexample
                    break
            if indeterminate_reason is not None:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC4302",
                        f"{display} Boolean functions cannot be checked exactly: "
                        f"{indeterminate_reason}.",
                        severity=Severity.FATAL
                        if self.config.policy.boolean_backend == "z3"
                        else None,
                        object=entity,
                        property_name="boolean_function",
                        evidence=tuple(item.diagnostic_evidence() for item, _ in parsed),
                    )
                )
            elif mismatch:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC4301",
                        f"{display} implements different Boolean functions in "
                        + " and ".join(_view_label(item.view) for item, _ in parsed)
                        + ".",
                        object=entity,
                        property_name="boolean_function",
                        evidence=tuple(item.diagnostic_evidence() for item, _ in parsed),
                        metadata={"counterexample": counterexample or {}},
                    )
                )
        return diagnostics

    @staticmethod
    def _function_expression(value: Any) -> Any:
        return getattr(value, "expression", value)

    def _check_pin_mappings(
        self,
        design: CanonicalDesign,
        observations: Sequence[ViewObservation],
        resolver: _AliasResolver,
    ) -> list[Diagnostic]:
        rows: list[tuple[ViewId, PinMappingObservation]] = []
        physical_rows: list[tuple[ViewId, PinMappingObservation]] = []
        for observation in observations:
            if self._is_package_map_view(observation.view):
                rows.extend((observation.view, item) for item in observation.pin_mappings)
            else:
                physical_rows.extend(
                    (observation.view, item)
                    for item in observation.pin_mappings
                    if self._is_physical_pad_mapping(item)
                )
        diagnostics: list[Diagnostic] = []
        physical_by_pad: dict[str, list[tuple[ViewId, PinMappingObservation]]] = defaultdict(list)
        for item in physical_rows:
            _, row = item
            if row.status == FactState.KNOWN and row.die_pad:
                physical_by_pad[row.die_pad].append(item)
        valid_rows: list[tuple[ViewId, PinMappingObservation]] = []
        for view, row in rows:
            if row.status != FactState.KNOWN:
                continue
            if not row.package_ball or (not row.die_pad and not row.signal):
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC5006",
                        f"{_view_label(view)} pin-map row requires a package ball and "
                        "at least one die-pad or signal name.",
                        provenance=row.provenance,
                    )
                )
                continue
            valid_rows.append((view, row))
            if row.die_pad and physical_by_pad and row.die_pad not in physical_by_pad:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC5001",
                        f"Package ball {row.package_ball} references die pad "
                        f"{row.die_pad!r}, but that pad is absent from the physical design.",
                        provenance=row.provenance,
                        object=_object("die_pad", f"die-pad:{row.die_pad}", row.die_pad),
                        property_name="presence",
                        evidence=(DiagnosticEvidence(view, row.die_pad, row.provenance),),
                        metadata={
                            "physical_views": [
                                str(item.view)
                                for item in observations
                                if any(
                                    self._is_physical_pad_mapping(mapping)
                                    for mapping in item.pin_mappings
                                )
                            ]
                        },
                    )
                )
            physical_assignments = physical_by_pad.get(row.die_pad or "", ())
            physical_signals = {
                physical.signal
                for _, physical in physical_assignments
                if physical.signal and physical.signal.upper() not in {"NC", "DNP", "N/C"}
            }
            if (
                row.signal
                and row.signal.upper() not in {"NC", "DNP", "N/C"}
                and physical_signals
                and row.signal not in physical_signals
            ):
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC5005",
                        f"Package ball {row.package_ball} assigns signal {row.signal!r} to die "
                        f"pad {row.die_pad!r}, but the physical design assigns "
                        + ", ".join(repr(item) for item in sorted(physical_signals))
                        + ".",
                        provenance=row.provenance,
                        object=_object(
                            "die_pad",
                            f"die-pad:{row.die_pad}",
                            row.die_pad or "<unspecified>",
                        ),
                        property_name="signal",
                        evidence=(DiagnosticEvidence(view, row.signal, row.provenance),)
                        + tuple(
                            DiagnosticEvidence(
                                physical_view,
                                physical.signal,
                                physical.provenance,
                            )
                            for physical_view, physical in physical_assignments
                        ),
                    )
                )

        by_ball: dict[str, list[tuple[ViewId, PinMappingObservation]]] = defaultdict(list)
        by_pad: dict[str, list[tuple[ViewId, PinMappingObservation]]] = defaultdict(list)
        for item in valid_rows:
            view, row = item
            if row.package_ball is None:
                continue
            by_ball[row.package_ball].append(item)
            if row.die_pad:
                by_pad[row.die_pad].append(item)
            component = None
            if row.component:
                try:
                    canonical_component = resolver.component(view, row.component)
                except _AliasCollision:
                    canonical_component = decoded_identifier(row.component)
                component = design.component(canonical_component)
            if component is not None:
                # A die-pad label is a physical endpoint, not necessarily a
                # logical RTL/Liberty port.  Without an explicit physical-pad
                # authority, validate only its mapping uniqueness/consistency.
                if row.signal and row.signal.upper() not in {"NC", "DNP", "N/C"}:
                    if not self._component_has_port(component, view, row.signal, resolver):
                        diagnostics.append(
                            Diagnostic.from_rule(
                                "OC5002",
                                f"Package ball {row.package_ball} references unknown signal "
                                f"{row.component}/{row.signal}.",
                                provenance=row.provenance,
                                object=_object(
                                    "package_ball",
                                    f"package-ball:{row.package_ball}",
                                    row.package_ball,
                                ),
                                property_name="signal",
                                evidence=(DiagnosticEvidence(view, row.signal, row.provenance),),
                            )
                        )

        if not self.config.policy.allow_multi_bond:
            for ball, assignments in sorted(by_ball.items()):
                if len(assignments) > 1:
                    diagnostics.append(
                        self._mapping_duplicate_diagnostic(
                            "OC5003", "package ball", ball, assignments
                        )
                    )
            for pad, assignments in sorted(by_pad.items()):
                balls = {row.package_ball for _, row in assignments}
                if len(balls) > 1:
                    diagnostics.append(
                        self._mapping_duplicate_diagnostic("OC5004", "die pad", pad, assignments)
                    )
        for ball, assignments in sorted(by_ball.items()):
            signals = {
                row.signal
                for _, row in assignments
                if row.signal and row.signal.upper() not in {"NC", "DNP", "N/C"}
            }
            # When multi-bonding is disallowed, OC5003 already reports this
            # endpoint once with every conflicting row as evidence.
            if len(signals) > 1 and self.config.policy.allow_multi_bond:
                diagnostics.append(
                    self._mapping_duplicate_diagnostic("OC5005", "package ball", ball, assignments)
                )
        return diagnostics

    @staticmethod
    def _is_physical_pad_mapping(mapping: PinMappingObservation) -> bool:
        source = str(mapping.attributes.get("source") or "").casefold()
        return bool(mapping.die_pad) and (
            mapping.attributes.get("physical") is True or source in {"gds_pin_text", "physical_pad"}
        )

    @staticmethod
    def _component_has_port(
        component: CanonicalComponent,
        view: ViewId,
        native_name: str,
        resolver: _AliasResolver,
    ) -> bool:
        try:
            canonical = resolver.port(component.canonical_name, view, native_name)
        except _AliasCollision:
            canonical = decoded_identifier(native_name)
        return component.port(canonical) is not None

    @staticmethod
    def _mapping_duplicate_diagnostic(
        code: str,
        endpoint_kind: str,
        endpoint: str,
        assignments: Sequence[tuple[ViewId, PinMappingObservation]],
    ) -> Diagnostic:
        values = [
            {
                "die_pad": row.die_pad,
                "package_ball": row.package_ball,
                "signal": row.signal,
            }
            for _, row in assignments
        ]
        return Diagnostic.from_rule(
            code,
            f"{endpoint_kind.capitalize()} {endpoint} has conflicting or duplicate "
            f"mappings: " + "; ".join(json.dumps(value, sort_keys=True) for value in values) + ".",
            object=_object(
                endpoint_kind.replace(" ", "_"),
                f"{endpoint_kind.replace(' ', '-')}:{endpoint}",
                endpoint,
            ),
            property_name="mapping",
            evidence=tuple(
                DiagnosticEvidence(view, value, row.provenance)
                for (view, row), value in zip(assignments, values, strict=True)
            ),
        )

    @staticmethod
    def _name_variants(name: str, scope: str | None = None) -> set[str]:
        raw = decoded_identifier(str(name)).strip().strip("/")
        if not raw:
            return set()
        variants = {raw, raw.replace(".", "/")}
        if scope:
            normalized_scope = decoded_identifier(scope).strip().strip("/")
            scoped = {f"{normalized_scope}/{item}" for item in tuple(variants) if normalized_scope}
            variants.update(scoped)
            for item in tuple(variants):
                prefix = f"{normalized_scope}/"
                if normalized_scope and item.startswith(prefix):
                    variants.add(item[len(prefix) :])
        variants.update(item.replace("/", ".") for item in tuple(variants))
        return {item for item in variants if item}

    @classmethod
    def _reference_matches(
        cls,
        reference: DesignObjectObservation,
        candidates: set[str],
        *,
        source_kind: str,
        source_divider: str = "/",
    ) -> bool | None:
        reference_name = reference.native_name
        reference_scope = reference.scope
        if source_kind == "def":
            # DEF uses a backslash to quote its divider character.  Decode it
            # only for matching; diagnostics retain the exact source spelling.
            reference_name = reference_name.replace(f"\\{source_divider}", source_divider)
            if reference_scope is not None:
                reference_scope = reference_scope.replace(f"\\{source_divider}", source_divider)
        match_mode = str(reference.attributes.get("match_mode") or "").casefold()
        options = reference.attributes.get("options")
        nocase = isinstance(options, Mapping) and "-nocase" in options
        if match_mode == "regexp":
            regexp_candidates = candidates
            if reference_scope:
                scope_variants = cls._name_variants(reference_scope)
                regexp_candidates = set()
                for candidate in candidates:
                    for scope in scope_variants:
                        for separator in ("/", "."):
                            prefix = f"{scope}{separator}"
                            if candidate.startswith(prefix):
                                regexp_candidates.add(candidate)
                                regexp_candidates.add(candidate[len(prefix) :])
            outcomes = {
                cls._safe_regexp_search(
                    reference_name,
                    candidate,
                    nocase=nocase,
                )
                for candidate in regexp_candidates
            }
            if True in outcomes:
                return True
            if None in outcomes or not regexp_candidates:
                return (
                    None
                    if cls._safe_regexp_search(
                        reference_name,
                        "",
                        nocase=nocase,
                    )
                    is None
                    else False
                )
            return False

        patterns = cls._name_variants(reference_name)
        if reference_scope:
            scopes = cls._name_variants(reference_scope)
            qualified_patterns: set[str] = set()
            for pattern in patterns:
                if any(
                    pattern == scope
                    or pattern.startswith(f"{scope}/")
                    or pattern.startswith(f"{scope}.")
                    for scope in scopes
                ):
                    qualified_patterns.add(pattern)
                    continue
                for scope in scopes:
                    qualified_patterns.add(f"{scope}/{pattern}")
                    qualified_patterns.add(f"{scope}.{pattern}")
            patterns = qualified_patterns
        is_pattern = (
            match_mode == "glob"
            or bool(reference.attributes.get("pattern"))
            or any(marker in reference_name for marker in ("*", "?"))
        )
        if nocase:
            patterns = {item.casefold() for item in patterns}
            candidates = {item.casefold() for item in candidates}
        if not is_pattern:
            return bool(patterns & candidates)
        return any(
            fnmatchcase(candidate, pattern) for pattern in patterns for candidate in candidates
        )

    @staticmethod
    def _safe_regexp_search(
        pattern: str,
        candidate: str,
        *,
        nocase: bool,
    ) -> bool | None:
        """Evaluate a deliberately small, non-catastrophic Tcl-regexp subset.

        Grouping, alternation, counted repetition, and more than two ``*``
        quantifiers are left inconclusive.  This covers ordinary anchored SDC
        selectors without exposing checks to adversarial backtracking.
        """

        if len(pattern) > 512 or len(candidate) > 65_536:
            return None
        in_class = False
        escaped = False
        previous_atom = False
        previous_quantifier = False
        star_count = 0
        question_count = 0
        for index, character in enumerate(pattern):
            if escaped:
                if character.isdigit():
                    return None
                escaped = False
                previous_atom = True
                previous_quantifier = False
                continue
            if character == "\\":
                escaped = True
                continue
            if in_class:
                if character == "[":
                    # Tcl supports POSIX bracket classes such as [:digit:],
                    # which Python's re engine does not interpret equivalently.
                    return None
                if character == "]":
                    in_class = False
                    previous_atom = True
                    previous_quantifier = False
                continue
            if character == "[":
                in_class = True
                previous_atom = False
                previous_quantifier = False
                continue
            if character in "(){}|+":
                return None
            if character in "*?":
                if not previous_atom or previous_quantifier:
                    return None
                if character == "*":
                    star_count += 1
                    if star_count > 2:
                        return None
                else:
                    question_count += 1
                    if question_count > 8:
                        return None
                previous_quantifier = True
                continue
            if character == "^" and index != 0:
                return None
            if character == "$" and index != len(pattern) - 1:
                return None
            previous_atom = character not in "^$"
            previous_quantifier = False
        if escaped or in_class:
            return None
        try:
            compiled = re.compile(pattern, re.IGNORECASE if nocase else 0)
        except re.error:
            return None
        # Tcl's regexp command searches unless the caller supplied anchors.
        return compiled.search(candidate) is not None

    def _check_object_references(
        self,
        design: CanonicalDesign,
        observations: Sequence[ViewObservation],
    ) -> list[Diagnostic]:
        rtl_views = [item for item in observations if _semantic_view_kind(item.view) == "rtl"]
        has_rtl = bool(rtl_views)

        rtl_index: dict[str, set[str]] = defaultdict(set)
        for observation in rtl_views:
            for observed_component in observation.components:
                component_names = self._name_variants(observed_component.native_name)
                rtl_index["instance"].update(component_names)
                rtl_index["cell"].update(component_names)
                for observed_port in observed_component.ports:
                    names = self._name_variants(
                        observed_port.native_name, observed_component.native_name
                    )
                    for bit in observed_port.shape.bit_indices:
                        names.update(
                            self._name_variants(
                                f"{observed_port.native_name}[{bit}]",
                                observed_component.native_name,
                            )
                        )
                    rtl_index["port"].update(names)
            for item in observation.objects:
                if item.relation != "definition" or item.status != FactState.KNOWN:
                    continue
                rtl_index[item.kind].update(self._name_variants(item.native_name, item.scope))
        for canonical_component in design.components:
            rtl_members = [
                member
                for member in canonical_component.members
                if _semantic_view_kind(member.view) == "rtl"
            ]
            if not rtl_members:
                continue
            rtl_index["instance"].update(self._name_variants(canonical_component.canonical_name))
            rtl_index["cell"].update(self._name_variants(canonical_component.canonical_name))
            for canonical_port in canonical_component.ports:
                if not any(
                    _semantic_view_kind(member.view) == "rtl" for member in canonical_port.members
                ):
                    continue
                rtl_index["port"].update(
                    self._name_variants(
                        canonical_port.canonical_name, canonical_component.canonical_name
                    )
                )

        generic_index_by_view: dict[ViewId, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        for observation in observations:
            for clock in observation.clocks:
                if clock.status == FactState.KNOWN:
                    names = self._name_variants(clock.native_name)
                    generic_index_by_view[observation.view]["clock"].update(names)
            for item in observation.objects:
                if item.relation == "definition" and item.status == FactState.KNOWN:
                    generic_index_by_view[observation.view][item.kind].update(
                        self._name_variants(item.native_name, item.scope)
                    )

        aliases = {
            "cells": "instance",
            "cell": "instance",
            "instances": "instance",
            "ports": "port",
            "pins": "pin",
            "nets": "net",
            "clocks": "clock",
        }
        diagnostics: list[Diagnostic] = []
        for observation in observations:
            if _semantic_view_kind(observation.view) != "upf":
                continue
            definitions_by_key: dict[tuple[str, str], list[DesignObjectObservation]] = defaultdict(
                list
            )
            for item in observation.objects:
                if item.relation == "definition" and item.status == FactState.KNOWN:
                    definitions_by_key[(item.kind, item.qualified_name)].append(item)
            for (kind, name), definitions in sorted(definitions_by_key.items()):
                updates = [
                    index
                    for index, item in enumerate(definitions)
                    if item.attributes.get("update") is True
                ]
                initial = [
                    index
                    for index, item in enumerate(definitions)
                    if item.attributes.get("update") is not True
                ]
                legal_update_sequence = (
                    bool(updates) and len(initial) == 1 and initial[0] < min(updates)
                )
                if legal_update_sequence or (not updates and len(definitions) < 2):
                    continue
                if updates and not initial:
                    detail = "is updated before any initial definition"
                elif updates and len(initial) == 1:
                    detail = "is updated before its initial definition"
                else:
                    detail = f"has {len(initial)} initial definitions"
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC6104",
                        f"{_view_label(observation.view)} UPF {kind} {name!r} {detail}.",
                        object=_object(
                            kind,
                            f"upf:{observation.view.key}:{kind}:{name}",
                            name,
                        ),
                        property_name="definition",
                        evidence=tuple(
                            DiagnosticEvidence(
                                observation.view,
                                {
                                    "name": item.native_name,
                                    "update": item.attributes.get("update") is True,
                                },
                                item.provenance,
                            )
                            for item in definitions
                        ),
                    )
                )
        for observation in observations:
            observation_kind = _semantic_view_kind(observation.view)
            if observation_kind not in {"sdc", "upf", "def"}:
                continue
            for reference in observation.objects:
                if reference.relation != "reference" or reference.status != FactState.KNOWN:
                    continue
                kind = aliases.get(reference.kind, reference.kind)
                if (
                    observation_kind == "def"
                    and kind == "pin"
                    and reference.attributes.get("endpoint_type") == "top_pin"
                ):
                    kind = "port"
                rtl_object = kind in {"port", "pin", "net", "instance"}
                # An SDC/UPF-only project has no authoritative elaborated design
                # against which an RTL reference can be judged.  Internal UPF
                # references are still checked against definitions below.
                if rtl_object and not has_rtl:
                    continue
                candidates = (
                    rtl_index[kind] if rtl_object else generic_index_by_view[observation.view][kind]
                )
                match = self._reference_matches(
                    reference,
                    candidates,
                    source_kind=observation_kind,
                    source_divider=(
                        str(observation.attributes.get("dividerchar"))
                        if observation_kind == "def"
                        and isinstance(observation.attributes.get("dividerchar"), str)
                        and len(str(observation.attributes.get("dividerchar"))) == 1
                        else "/"
                    ),
                )
                if match is None:
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC1105",
                            f"{_view_label(observation.view)} regular expression "
                            f"{reference.native_name!r} is outside the bounded static subset; "
                            "object existence was not judged.",
                            provenance=reference.provenance,
                            object=_object(
                                kind,
                                f"reference:{observation.view.key}:{kind}:{reference.native_name}",
                                reference.native_name,
                            ),
                            property_name="reference",
                            evidence=(
                                DiagnosticEvidence(
                                    observation.view,
                                    reference.native_name,
                                    reference.provenance,
                                ),
                            ),
                            metadata={"match_mode": "regexp"},
                        )
                    )
                    continue
                if match:
                    continue
                tainted_reference_scopes = {
                    item
                    for item in (reference.scope, reference.native_name, reference.qualified_name)
                    if item
                }
                if self._whole_view_tainted(observation) or bool(
                    tainted_reference_scopes & set(observation.tainted_scopes)
                ):
                    # A static fact can remain useful in an incomplete UPF/SDC/DEF
                    # view, but the omitted dynamic or included content may define
                    # the object.  Do not turn that uncertainty into an absence.
                    continue
                if observation_kind == "sdc":
                    code = "OC6001"
                    subject = "SDC"
                elif observation_kind == "upf":
                    if kind == "instance":
                        code = "OC6101"
                    elif kind in {"port", "pin"}:
                        code = "OC6102"
                    else:
                        code = "OC6103"
                    subject = "UPF"
                else:
                    code = "OC6401"
                    subject = "DEF"
                command = str(reference.attributes.get("command") or "reference")
                display = f"{kind} {reference.native_name}"
                diagnostics.append(
                    Diagnostic.from_rule(
                        code,
                        f"{subject} command {command} references {kind} "
                        f"{reference.native_name!r}, but it "
                        "matches no statically elaborated design object.",
                        provenance=reference.provenance,
                        object=_object(
                            kind,
                            f"reference:{observation.view.key}:{kind}:{reference.native_name}",
                            display,
                        ),
                        property_name="reference",
                        evidence=(
                            DiagnosticEvidence(
                                observation.view,
                                reference.native_name,
                                reference.provenance,
                                native_name=reference.native_name,
                                label=command,
                            ),
                        ),
                        metadata={"command": command, "reference_kind": kind},
                    )
                )
        return diagnostics

    @classmethod
    def _resolved_clock_targets(
        cls,
        design: CanonicalDesign,
        targets: Sequence[str],
    ) -> tuple[str, ...]:
        resolved: set[str] = set()
        for target in targets:
            target_variants = cls._name_variants(target)
            target_matches: set[str] = set()
            is_pattern = any(marker in target for marker in ("*", "?", "["))
            for component in design.components:
                for port in component.ports:
                    port_variants = cls._name_variants(
                        port.canonical_name, component.canonical_name
                    )
                    port_variants.update(
                        variant
                        for member in port.members
                        for variant in cls._name_variants(
                            member.observation.native_name,
                            member.component_native_name,
                        )
                    )
                    matched = bool(target_variants & port_variants)
                    if is_pattern:
                        matched = any(
                            fnmatchcase(candidate, pattern)
                            for pattern in target_variants
                            for candidate in port_variants
                        )
                    if matched:
                        target_matches.add(f"{component.canonical_name}/{port.canonical_name}")
            if target_matches:
                resolved.update(target_matches)
            else:
                resolved.add(decoded_identifier(target).strip().replace(".", "/"))
        return tuple(sorted(resolved))

    def _check_clocks(
        self,
        design: CanonicalDesign,
        observations: Sequence[ViewObservation],
    ) -> list[Diagnostic]:
        grouped: dict[str, list[tuple[ViewId, ClockObservation]]] = defaultdict(list)
        for observation in observations:
            for clock in observation.clocks:
                if clock.status == FactState.KNOWN:
                    grouped[clock.native_name].append((observation.view, clock))

        diagnostics: list[Diagnostic] = []
        for name, declarations in sorted(grouped.items()):
            conflicts: dict[str, list[Any]] = {}
            for property_name in (
                "period",
                "waveform",
                "source",
                "generated",
                "targets",
            ):
                values = [
                    self._resolved_clock_targets(design, clock.targets)
                    if property_name == "targets"
                    else getattr(clock, property_name)
                    for _, clock in declarations
                ]
                present = [value for value in values if value is not None]
                stable = {_stable_value_key(value) for value in present}
                if len(stable) > 1:
                    conflicts[property_name] = present
            if conflicts:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC6002",
                        f"Clock {name} has conflicting definitions across "
                        + ", ".join(_view_label(view) for view, _ in declarations)
                        + ".",
                        object=_object("clock", f"clock:{name}", name),
                        property_name="clock_definition",
                        evidence=tuple(
                            DiagnosticEvidence(
                                view,
                                {
                                    "period": clock.period,
                                    "waveform": clock.waveform,
                                    "source": clock.source,
                                    "generated": clock.generated,
                                    "targets": list(clock.targets),
                                },
                                clock.provenance,
                                native_name=clock.native_name,
                            )
                            for view, clock in declarations
                        ),
                        metadata={"conflicts": conflicts},
                    )
                )

            for view, clock in declarations:
                for target in clock.targets:
                    target_variants = self._name_variants(target)
                    for component in design.components:
                        for port in component.ports:
                            port_variants = self._name_variants(
                                port.canonical_name, component.canonical_name
                            )
                            port_variants.update(
                                variant
                                for member in port.members
                                for variant in self._name_variants(
                                    member.observation.native_name,
                                    member.component_native_name,
                                )
                            )
                            if not target_variants & port_variants:
                                continue
                            known_roles = {
                                member.observation.role
                                for member in port.members
                                if member.observation.state_for("role") == FactState.KNOWN
                                and member.observation.role != PortRole.UNKNOWN
                            }
                            if not known_roles or PortRole.CLOCK in known_roles:
                                continue
                            display = f"{component.canonical_name}/{port.canonical_name}"
                            diagnostics.append(
                                Diagnostic.from_rule(
                                    "OC6003",
                                    f"Clock {name} targets {display}, but authoritative "
                                    "collateral classifies that pin as "
                                    + "/".join(sorted(role.value for role in known_roles))
                                    + ".",
                                    object=_object(
                                        "port",
                                        component.port_id(port.canonical_name),
                                        display,
                                    ),
                                    property_name="role",
                                    evidence=(
                                        DiagnosticEvidence(
                                            view,
                                            target,
                                            clock.provenance,
                                            label="clock target",
                                        ),
                                        *tuple(
                                            DiagnosticEvidence(
                                                member.view,
                                                member.observation.role,
                                                member.observation.provenance,
                                                native_name=member.observation.native_name,
                                            )
                                            for member in port.members
                                            if member.observation.state_for("role")
                                            == FactState.KNOWN
                                        ),
                                    ),
                                )
                            )
        return diagnostics

    def _check_interfaces(
        self,
        design: CanonicalDesign,
        observations: Sequence[ViewObservation],
        resolver: _AliasResolver,
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for observation in observations:
            for interface in observation.interfaces:
                if interface.status != FactState.KNOWN:
                    continue
                native_component = interface.component
                if native_component is None and len(observation.components) == 1:
                    native_component = observation.components[0].native_name
                component = None
                if native_component:
                    try:
                        canonical = resolver.component(observation.view, native_component)
                    except _AliasCollision:
                        canonical = decoded_identifier(native_component)
                    component = design.component(canonical)
                    if component is None:
                        component = next(
                            (
                                item
                                for item in design.components
                                if item.canonical_name.casefold() == canonical.casefold()
                            ),
                            None,
                        )
                if component is None:
                    continue
                valid_ports = {port.canonical_name for port in component.ports}
                valid_ports.update(
                    member.observation.native_name
                    for port in component.ports
                    for member in port.members
                )
                physical_to_logical: dict[str, list[str]] = defaultdict(list)
                for logical, physical_expression in sorted(interface.port_maps.items()):
                    physical = re.sub(r"(?:\[[^\]]+\])+$", "", physical_expression)
                    physical_to_logical[physical].append(logical)
                    if physical in valid_ports:
                        continue
                    display = f"{component.canonical_name}/{physical}"
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC6201",
                            f"IP-XACT interface {interface.native_name} maps logical port "
                            f"{logical} to {display}, which is absent from the component.",
                            provenance=interface.provenance,
                            object=_object("port", component.port_id(physical), display),
                            property_name="interface.port_map",
                            evidence=(
                                DiagnosticEvidence(
                                    observation.view,
                                    {"logical": logical, "physical": physical_expression},
                                    interface.provenance,
                                    native_name=interface.native_name,
                                ),
                            ),
                        )
                    )
                if interface.attributes.get("allow_many_to_one"):
                    continue
                for physical, logical_names in sorted(physical_to_logical.items()):
                    if len(logical_names) < 2:
                        continue
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC6202",
                            f"IP-XACT interface {interface.native_name} maps logical ports "
                            f"{', '.join(logical_names)} to the same physical port {physical}.",
                            provenance=interface.provenance,
                            object=_object(
                                "interface",
                                f"interface:{component.canonical_name}/{interface.native_name}",
                                interface.native_name,
                            ),
                            property_name="interface.port_map",
                            evidence=(
                                DiagnosticEvidence(
                                    observation.view,
                                    {"physical": physical, "logical": logical_names},
                                    interface.provenance,
                                ),
                            ),
                        )
                    )
        return diagnostics

    @staticmethod
    def _resolve_connectivity_selector(
        selector: str,
        endpoints_by_name: Mapping[str, tuple[ConnectivityEndpoint, ...]],
    ) -> tuple[tuple[ConnectivityEndpoint, ...], bool, bool]:
        """Resolve one bounded exact/glob endpoint selector.

        The second result reports ambiguity across multiple signal bases.  The
        third reports a selector that cannot be materialized within the public
        connectivity bit limit.  Bus bits from one matched signal are not
        ambiguous and retain their declaration order.
        """

        match = _CONNECTIVITY_SELECTOR.fullmatch(selector)
        if match is None:
            return (), False, False
        pattern = match.group("base")
        selection = match.group("select")
        is_glob = any(marker in pattern for marker in ("*", "?"))
        matching_names = tuple(
            sorted(
                name
                for name in endpoints_by_name
                if (fnmatchcase(name, pattern) if is_glob else name == pattern)
            )
        )
        if len(matching_names) != 1:
            return (), len(matching_names) > 1, False
        endpoints = endpoints_by_name[matching_names[0]]
        if selection is None or selection == "*":
            return endpoints, False, False
        if ":" not in selection:
            digits = selection.lstrip("+-")
            if len(digits) > _MAX_CONNECTIVITY_INDEX_DIGITS:
                return (), False, True
            try:
                bit = int(selection)
            except ValueError:
                return (), False, True
            selected = tuple(item for item in endpoints if item.bit_index == bit)
            return selected, False, False
        left_text, right_text = selection.split(":", 1)
        index_texts = (left_text.strip(), right_text.strip())
        if any(
            len(index_text.lstrip("+-")) > _MAX_CONNECTIVITY_INDEX_DIGITS
            for index_text in index_texts
        ):
            return (), False, True
        try:
            left, right = (int(index_text) for index_text in index_texts)
        except ValueError:
            return (), False, True
        if abs(right - left) + 1 > _MAX_CONNECTIVITY_REQUIREMENT_BITS:
            return (), False, True
        step = 1 if right > left else -1
        wanted = tuple(range(left, right + step, step))
        by_bit = {item.bit_index: item for item in endpoints}
        if any(bit not in by_bit for bit in wanted):
            return (), False, False
        return tuple(by_bit[bit] for bit in wanted), False, False

    @staticmethod
    def _find_connectivity_path(
        source: str,
        sink: str,
        adjacency: Mapping[str, tuple[ConnectivityEdge, ...]],
        *,
        through: tuple[frozenset[str], ...] = (),
        excluded: frozenset[str] = frozenset(),
        required_parity: bool | None = None,
        require_tainted: bool = False,
    ) -> tuple[tuple[ConnectivityEdge, ...] | None, frozenset[str]]:
        if source in excluded or sink in excluded:
            return None, frozenset((source,))

        def advance(key: str, index: int) -> int:
            while index < len(through) and key in through[index]:
                index += 1
            return index

        initial = (source, advance(source, 0), False, False)
        queue: deque[tuple[str, int, bool | None, bool]] = deque((initial,))
        previous: dict[
            tuple[str, int, bool | None, bool],
            tuple[tuple[str, int, bool | None, bool], ConnectivityEdge] | None,
        ] = {initial: None}
        visited_nodes: set[str] = {source}
        final_state: tuple[str, int, bool | None, bool] | None = None
        while queue:
            state = queue.popleft()
            node, waypoint_index, parity, crossed_tainted = state
            if (
                node == sink
                and waypoint_index == len(through)
                and (required_parity is None or parity == required_parity)
                and (not require_tainted or crossed_tainted)
            ):
                final_state = state
                break
            for edge in adjacency.get(node, ()):
                next_node = edge.sink.key
                if next_node in excluded:
                    continue
                next_parity = (
                    None if parity is None or edge.inverted is None else parity ^ edge.inverted
                )
                next_state = (
                    next_node,
                    advance(next_node, waypoint_index),
                    next_parity,
                    (
                        crossed_tainted or edge.status != FactState.KNOWN
                        if require_tainted
                        else False
                    ),
                )
                if next_state in previous:
                    continue
                if len(previous) >= _MAX_CONNECTIVITY_SEARCH_STATES:
                    visited_nodes.add(_CONNECTIVITY_SEARCH_LIMIT_SENTINEL)
                    return None, frozenset(visited_nodes)
                previous[next_state] = (state, edge)
                visited_nodes.add(next_node)
                queue.append(next_state)
        if final_state is None:
            return None, frozenset(visited_nodes)
        path: list[ConnectivityEdge] = []
        cursor = final_state
        while True:
            link = previous[cursor]
            if link is None:
                break
            prior, edge = link
            path.append(edge)
            cursor = prior
        path.reverse()
        return tuple(path), frozenset(visited_nodes)

    @staticmethod
    def _connectivity_evidence(
        intent_view: ViewId,
        requirement: ConnectivityRequirement,
        source: ConnectivityEndpoint | None = None,
        sink: ConnectivityEndpoint | None = None,
        path: Sequence[ConnectivityEdge] = (),
    ) -> tuple[DiagnosticEvidence, ...]:
        evidence: list[DiagnosticEvidence] = [
            DiagnosticEvidence(
                intent_view,
                requirement.to_dict(),
                requirement.provenance,
                native_name=requirement.identifier,
                label="requirement",
            )
        ]
        for label, endpoint in (("source", source), ("sink", sink)):
            if endpoint is not None and endpoint.provenance is not None:
                evidence.append(
                    DiagnosticEvidence(
                        endpoint.provenance.view,
                        endpoint.key,
                        endpoint.provenance,
                        native_name=endpoint.key,
                        label=label,
                    )
                )
        for index, edge in enumerate(path):
            edge_view = edge.provenance.view if edge.provenance is not None else ViewId("rtl")
            evidence.append(
                DiagnosticEvidence(
                    edge_view,
                    edge.to_dict(),
                    edge.provenance,
                    label=f"path edge {index + 1}",
                )
            )
        return tuple(evidence)

    @staticmethod
    def _connectivity_adjacency(
        edges: Sequence[ConnectivityEdge],
        *,
        include_tainted: bool,
    ) -> dict[str, tuple[ConnectivityEdge, ...]]:
        grouped: dict[str, list[ConnectivityEdge]] = defaultdict(list)
        for edge in edges:
            if edge.status == FactState.KNOWN and edge.inverted is not None:
                grouped[edge.source.key].append(edge)
            elif include_tainted and edge.status in {FactState.TAINTED, FactState.UNSUPPORTED}:
                grouped[edge.source.key].append(edge)
        return {
            source: tuple(
                sorted(
                    values,
                    key=lambda item: (
                        item.sink.key,
                        item.status.value,
                        item.kind,
                        -1 if item.inverted is None else int(item.inverted),
                    ),
                )
            )
            for source, values in grouped.items()
        }

    def _check_connectivity(
        self,
        observations: Sequence[ViewObservation],
    ) -> list[Diagnostic]:
        rtl_views = [item for item in observations if _semantic_view_kind(item.view) == "rtl"]
        intent_views = [item for item in observations if item.connectivity_requirements]
        diagnostics: list[Diagnostic] = []
        if not rtl_views:
            return diagnostics

        for intent in intent_views:
            for requirement in intent.connectivity_requirements:
                if requirement.status != FactState.KNOWN:
                    continue
                for rtl in rtl_views:
                    endpoints_by_name_lists: dict[str, list[ConnectivityEndpoint]] = defaultdict(
                        list
                    )
                    for endpoint in rtl.connectivity_endpoints:
                        endpoints_by_name_lists[endpoint.native_name].append(endpoint)
                    endpoints_by_name = {
                        name: tuple(sorted(values, key=lambda item: item.ordinal))
                        for name, values in endpoints_by_name_lists.items()
                    }
                    entity = _object(
                        "connectivity_requirement",
                        f"connectivity:{intent.view.key}:{requirement.identifier}:{rtl.view.key}",
                        requirement.identifier,
                    )

                    def resolve(
                        selector: str,
                        label: str,
                        endpoints_index: Mapping[
                            str, tuple[ConnectivityEndpoint, ...]
                        ] = endpoints_by_name,
                        selected_requirement: ConnectivityRequirement = requirement,
                        rtl_observation: ViewObservation = rtl,
                        selected_entity: DiagnosticObject = entity,
                        intent_view: ViewId = intent.view,
                    ) -> tuple[ConnectivityEndpoint, ...] | None:
                        resolved, ambiguous, selector_limited = self._resolve_connectivity_selector(
                            selector, endpoints_index
                        )
                        if resolved:
                            return resolved
                        if selector_limited:
                            code = "OC6505"
                        elif ambiguous:
                            code = "OC6502"
                        else:
                            code = "OC6501"
                        detail = (
                            f"expands beyond the bounded "
                            f"{_MAX_CONNECTIVITY_REQUIREMENT_BITS:,}-bit selector limit"
                            if selector_limited
                            else "matches multiple RTL signals"
                            if ambiguous
                            else "matches no RTL signal"
                        )
                        diagnostics.append(
                            Diagnostic.from_rule(
                                code,
                                f"Connectivity requirement "
                                f"{selected_requirement.identifier!r} {label} "
                                f"{selector!r} {detail} in "
                                f"{_view_label(rtl_observation.view)}.",
                                provenance=selected_requirement.provenance,
                                object=selected_entity,
                                property_name=f"connectivity.{label}",
                                evidence=self._connectivity_evidence(
                                    intent_view, selected_requirement
                                ),
                                metadata={
                                    "selector": selector,
                                    "rtl_view": str(rtl_observation.view),
                                    **(
                                        {"limit": _MAX_CONNECTIVITY_REQUIREMENT_BITS}
                                        if selector_limited
                                        else {}
                                    ),
                                },
                            )
                        )
                        return None

                    sources = resolve(requirement.source, "source")
                    sinks = resolve(requirement.sink, "sink")
                    if sources is None or sinks is None:
                        continue
                    through_groups: list[frozenset[str]] = []
                    selector_failed = False
                    for selector in requirement.through:
                        resolved = resolve(selector, "through")
                        if resolved is None:
                            selector_failed = True
                            break
                        through_groups.append(frozenset(item.key for item in resolved))
                    excluded_keys: set[str] = set()
                    if not selector_failed:
                        for selector in requirement.exclude:
                            resolved = resolve(selector, "exclude")
                            if resolved is None:
                                selector_failed = True
                                break
                            excluded_keys.update(item.key for item in resolved)
                    if selector_failed:
                        continue
                    if max(len(sources), len(sinks)) > _MAX_CONNECTIVITY_REQUIREMENT_BITS:
                        diagnostics.append(
                            Diagnostic.from_rule(
                                "OC6505",
                                f"Connectivity requirement {requirement.identifier!r} selects "
                                f"more than {_MAX_CONNECTIVITY_REQUIREMENT_BITS:,} bits and "
                                "is outside the bounded path-checking limit.",
                                provenance=requirement.provenance,
                                object=entity,
                                property_name="connectivity.width",
                                evidence=self._connectivity_evidence(
                                    intent.view, requirement, sources[0], sinks[0]
                                ),
                                metadata={
                                    "source_width": len(sources),
                                    "sink_width": len(sinks),
                                    "limit": _MAX_CONNECTIVITY_REQUIREMENT_BITS,
                                },
                            )
                        )
                        continue
                    if len(sources) != len(sinks):
                        diagnostics.append(
                            Diagnostic.from_rule(
                                "OC6506",
                                f"Connectivity requirement {requirement.identifier!r} selects "
                                f"{len(sources)} source bits but {len(sinks)} sink bits.",
                                provenance=requirement.provenance,
                                object=entity,
                                property_name="connectivity.width",
                                evidence=self._connectivity_evidence(
                                    intent.view,
                                    requirement,
                                    sources[0] if sources else None,
                                    sinks[0] if sinks else None,
                                ),
                                metadata={
                                    "source_width": len(sources),
                                    "sink_width": len(sinks),
                                    "rtl_view": str(rtl.view),
                                },
                            )
                        )
                        continue
                    known_adjacency = self._connectivity_adjacency(
                        rtl.connectivity_edges, include_tainted=False
                    )
                    possible_adjacency = self._connectivity_adjacency(
                        rtl.connectivity_edges, include_tainted=True
                    )
                    has_tainted_edges = any(
                        edge.status in {FactState.TAINTED, FactState.UNSUPPORTED}
                        for edge in rtl.connectivity_edges
                    )
                    through = tuple(through_groups)
                    excluded = frozenset(excluded_keys)
                    graph_complete = rtl.attributes.get("connectivity_complete") is not False

                    if requirement.expectation == ConnectivityExpectation.UNREACHABLE:
                        pair_count = len(sources) * len(sinks)
                        if pair_count > _MAX_CONNECTIVITY_PAIR_SEARCHES:
                            diagnostics.append(
                                Diagnostic.from_rule(
                                    "OC6505",
                                    f"Connectivity requirement {requirement.identifier!r} expands "
                                    f"to {pair_count:,} endpoint pairs, above the bounded search "
                                    f"limit of {_MAX_CONNECTIVITY_PAIR_SEARCHES:,}.",
                                    provenance=requirement.provenance,
                                    object=entity,
                                    property_name="connectivity.path",
                                    evidence=self._connectivity_evidence(intent.view, requirement),
                                )
                            )
                            continue
                        found = False
                        search_limited = False
                        possible: (
                            tuple[
                                ConnectivityEndpoint,
                                ConnectivityEndpoint,
                                tuple[ConnectivityEdge, ...],
                            ]
                            | None
                        ) = None
                        for source in sources:
                            for sink in sinks:
                                path, visited = self._find_connectivity_path(
                                    source.key,
                                    sink.key,
                                    known_adjacency,
                                    through=through,
                                    excluded=excluded,
                                )
                                search_limited = search_limited or (
                                    _CONNECTIVITY_SEARCH_LIMIT_SENTINEL in visited
                                )
                                if path is not None:
                                    diagnostics.append(
                                        Diagnostic.from_rule(
                                            "OC6504",
                                            f"Connectivity requirement {requirement.identifier!r} "
                                            f"forbids {source.key} reaching {sink.key}, but a "
                                            f"{len(path)}-edge static path exists.",
                                            provenance=requirement.provenance,
                                            object=entity,
                                            property_name="connectivity.path",
                                            evidence=self._connectivity_evidence(
                                                intent.view, requirement, source, sink, path
                                            ),
                                            metadata={
                                                "witness_path": [edge.to_dict() for edge in path],
                                                "rtl_view": str(rtl.view),
                                            },
                                        )
                                    )
                                    found = True
                                    break
                                possible_path, possible_visited = self._find_connectivity_path(
                                    source.key,
                                    sink.key,
                                    possible_adjacency,
                                    through=through,
                                    excluded=excluded,
                                )
                                search_limited = search_limited or (
                                    _CONNECTIVITY_SEARCH_LIMIT_SENTINEL in possible_visited
                                )
                                if possible is None and possible_path is not None:
                                    possible = (source, sink, possible_path)
                            if found:
                                break
                        if found:
                            continue
                        if possible is not None or not graph_complete or search_limited:
                            source, sink, path = possible or (sources[0], sinks[0], ())
                            frontier = next(
                                (edge for edge in path if edge.status != FactState.KNOWN),
                                None,
                            )
                            reason = (
                                str(frontier.attributes.get("reason") or frontier.kind)
                                if frontier is not None
                                else "the bounded path search reached its state limit"
                                if search_limited
                                else "the RTL connectivity graph has an unrepresented frontier"
                            )
                            diagnostics.append(
                                Diagnostic.from_rule(
                                    "OC6505",
                                    f"Connectivity requirement {requirement.identifier!r} "
                                    f"cannot prove isolation between {source.key} and {sink.key}: "
                                    f"{reason}.",
                                    provenance=requirement.provenance,
                                    object=entity,
                                    property_name="connectivity.path",
                                    evidence=self._connectivity_evidence(
                                        intent.view, requirement, source, sink, path
                                    ),
                                    metadata={
                                        "frontier": frontier.to_dict()
                                        if frontier is not None
                                        else None,
                                        "rtl_view": str(rtl.view),
                                    },
                                )
                            )
                        continue

                    ordered_sinks = (
                        tuple(reversed(sinks))
                        if requirement.transform == ConnectivityTransform.REVERSE
                        else sinks
                    )
                    required_parity = (
                        True
                        if requirement.transform == ConnectivityTransform.INVERTED
                        else False
                        if requirement.transform
                        in {ConnectivityTransform.IDENTITY, ConnectivityTransform.REVERSE}
                        else None
                    )
                    exact_mapping = requirement.transform in {
                        ConnectivityTransform.IDENTITY,
                        ConnectivityTransform.REVERSE,
                    }
                    mapping_pair_count = len(sources) * max(0, len(ordered_sinks) - 1)
                    if exact_mapping and mapping_pair_count > _MAX_CONNECTIVITY_PAIR_SEARCHES:
                        diagnostics.append(
                            Diagnostic.from_rule(
                                "OC6505",
                                f"Connectivity requirement {requirement.identifier!r} needs "
                                f"{mapping_pair_count:,} alternate selected-sink searches, "
                                f"above the bounded limit of "
                                f"{_MAX_CONNECTIVITY_PAIR_SEARCHES:,}.",
                                provenance=requirement.provenance,
                                object=entity,
                                property_name="connectivity.bit_mapping",
                                evidence=self._connectivity_evidence(intent.view, requirement),
                                metadata={
                                    "pairs": mapping_pair_count,
                                    "limit": _MAX_CONNECTIVITY_PAIR_SEARCHES,
                                },
                            )
                        )
                        continue
                    failure_emitted = False
                    for bit_ordinal, (source, sink) in enumerate(
                        zip(sources, ordered_sinks, strict=True)
                    ):
                        path, visited = self._find_connectivity_path(
                            source.key,
                            sink.key,
                            known_adjacency,
                            through=through,
                            excluded=excluded,
                            required_parity=required_parity,
                        )
                        if _CONNECTIVITY_SEARCH_LIMIT_SENTINEL in visited:
                            diagnostics.append(
                                Diagnostic.from_rule(
                                    "OC6505",
                                    f"Connectivity requirement {requirement.identifier!r} "
                                    f"exceeded the bounded path-search state limit from "
                                    f"{source.key} toward {sink.key}.",
                                    provenance=requirement.provenance,
                                    object=entity,
                                    property_name="connectivity.path",
                                    evidence=self._connectivity_evidence(
                                        intent.view, requirement, source, sink
                                    ),
                                    metadata={"limit": _MAX_CONNECTIVITY_SEARCH_STATES},
                                )
                            )
                            failure_emitted = True
                            break
                        if path is not None:
                            if required_parity is not None:
                                alternate, alternate_visited = self._find_connectivity_path(
                                    source.key,
                                    sink.key,
                                    known_adjacency,
                                    through=through,
                                    excluded=excluded,
                                    required_parity=not required_parity,
                                )
                                if (
                                    alternate is not None
                                    or _CONNECTIVITY_SEARCH_LIMIT_SENTINEL in alternate_visited
                                ):
                                    diagnostics.append(
                                        Diagnostic.from_rule(
                                            "OC6505",
                                            f"Connectivity requirement {requirement.identifier!r} "
                                            + (
                                                "has both inverted and non-inverted static paths "
                                                if alternate is not None
                                                else "cannot exclude an alternate-polarity path "
                                            )
                                            + f"between {source.key} and {sink.key}.",
                                            provenance=requirement.provenance,
                                            object=entity,
                                            property_name="connectivity.transform",
                                            evidence=self._connectivity_evidence(
                                                intent.view, requirement, source, sink, path
                                            ),
                                            metadata={
                                                "witness_path": [edge.to_dict() for edge in path],
                                                "alternate_path": [
                                                    edge.to_dict() for edge in alternate or ()
                                                ],
                                            },
                                        )
                                    )
                                    failure_emitted = True
                                    break
                                if exact_mapping:
                                    for candidate in ordered_sinks:
                                        if candidate.key == sink.key:
                                            continue
                                        cross_path, cross_visited = self._find_connectivity_path(
                                            source.key,
                                            candidate.key,
                                            known_adjacency,
                                            through=through,
                                            excluded=excluded,
                                        )
                                        cross_limited = (
                                            _CONNECTIVITY_SEARCH_LIMIT_SENTINEL in cross_visited
                                        )
                                        if cross_path is None and not cross_limited:
                                            continue
                                        if cross_limited:
                                            diagnostics.append(
                                                Diagnostic.from_rule(
                                                    "OC6505",
                                                    f"Connectivity requirement "
                                                    f"{requirement.identifier!r} cannot exclude "
                                                    f"an alternate selected-sink path from "
                                                    f"{source.key} to {candidate.key} within the "
                                                    "bounded search limit.",
                                                    provenance=requirement.provenance,
                                                    object=entity,
                                                    property_name=("connectivity.bit_mapping"),
                                                    evidence=self._connectivity_evidence(
                                                        intent.view,
                                                        requirement,
                                                        source,
                                                        candidate,
                                                        path,
                                                    ),
                                                    metadata={
                                                        "bit_ordinal": bit_ordinal,
                                                        "expected_sink": sink.key,
                                                        "candidate_sink": candidate.key,
                                                        "limit": (_MAX_CONNECTIVITY_SEARCH_STATES),
                                                        "witness_path": [
                                                            edge.to_dict() for edge in path
                                                        ],
                                                    },
                                                )
                                            )
                                        else:
                                            diagnostics.append(
                                                Diagnostic.from_rule(
                                                    "OC6507",
                                                    f"Connectivity requirement "
                                                    f"{requirement.identifier!r} maps source "
                                                    f"bit {source.key} to expected sink bit "
                                                    f"{sink.key}, but it also reaches selected "
                                                    f"sink bit {candidate.key}.",
                                                    provenance=requirement.provenance,
                                                    object=entity,
                                                    property_name=("connectivity.bit_mapping"),
                                                    evidence=self._connectivity_evidence(
                                                        intent.view,
                                                        requirement,
                                                        source,
                                                        candidate,
                                                        cross_path or (),
                                                    ),
                                                    metadata={
                                                        "bit_ordinal": bit_ordinal,
                                                        "expected_sink": sink.key,
                                                        "actual_sink": candidate.key,
                                                        "expected_path": [
                                                            edge.to_dict() for edge in path
                                                        ],
                                                        "witness_path": [
                                                            edge.to_dict()
                                                            for edge in cross_path or ()
                                                        ],
                                                    },
                                                )
                                            )
                                        failure_emitted = True
                                        break
                                    if failure_emitted:
                                        break
                                tainted_path: tuple[ConnectivityEdge, ...] | None = None
                                tainted_visited: frozenset[str] = frozenset()
                                tainted_sink = sink
                                if has_tainted_edges:
                                    tainted_path, tainted_visited = self._find_connectivity_path(
                                        source.key,
                                        sink.key,
                                        possible_adjacency,
                                        through=through,
                                        excluded=excluded,
                                        require_tainted=True,
                                    )
                                    if (
                                        exact_mapping
                                        and tainted_path is None
                                        and _CONNECTIVITY_SEARCH_LIMIT_SENTINEL
                                        not in tainted_visited
                                    ):
                                        for candidate in ordered_sinks:
                                            if candidate.key == sink.key:
                                                continue
                                            candidate_path, candidate_visited = (
                                                self._find_connectivity_path(
                                                    source.key,
                                                    candidate.key,
                                                    possible_adjacency,
                                                    through=through,
                                                    excluded=excluded,
                                                    require_tainted=True,
                                                )
                                            )
                                            tainted_visited = tainted_visited | candidate_visited
                                            if candidate_path is not None:
                                                tainted_path = candidate_path
                                                tainted_sink = candidate
                                                break
                                            if (
                                                _CONNECTIVITY_SEARCH_LIMIT_SENTINEL
                                                in candidate_visited
                                            ):
                                                tainted_sink = candidate
                                                break
                                tainted_limited = (
                                    _CONNECTIVITY_SEARCH_LIMIT_SENTINEL in tainted_visited
                                )
                                if (
                                    tainted_path is not None
                                    or not graph_complete
                                    or tainted_limited
                                ):
                                    frontier = next(
                                        (
                                            edge
                                            for edge in tainted_path or ()
                                            if edge.status != FactState.KNOWN
                                        ),
                                        None,
                                    )
                                    reason = (
                                        str(frontier.attributes.get("reason") or frontier.kind)
                                        if frontier is not None
                                        else (
                                            "the bounded alternate-path search reached "
                                            "its state limit"
                                        )
                                        if tainted_limited
                                        else (
                                            "the RTL connectivity graph has an "
                                            "unrepresented frontier"
                                        )
                                    )
                                    diagnostics.append(
                                        Diagnostic.from_rule(
                                            "OC6505",
                                            f"Connectivity requirement {requirement.identifier!r} "
                                            f"has a supported path from {source.key} to "
                                            f"{sink.key}, but its exact transform is "
                                            f"inconclusive: {reason}.",
                                            provenance=requirement.provenance,
                                            object=entity,
                                            property_name="connectivity.transform",
                                            evidence=self._connectivity_evidence(
                                                intent.view,
                                                requirement,
                                                source,
                                                tainted_sink,
                                                tainted_path or path,
                                            ),
                                            metadata={
                                                "witness_path": [edge.to_dict() for edge in path],
                                                "alternate_path": [
                                                    edge.to_dict() for edge in tainted_path or ()
                                                ],
                                                "frontier": frontier.to_dict()
                                                if frontier is not None
                                                else None,
                                                "expected_sink": sink.key,
                                                "possible_sink": tainted_sink.key,
                                                "rtl_view": str(rtl.view),
                                            },
                                        )
                                    )
                                    failure_emitted = True
                                    break
                            continue

                        any_polarity, any_visited = self._find_connectivity_path(
                            source.key,
                            sink.key,
                            known_adjacency,
                            through=through,
                            excluded=excluded,
                        )
                        if _CONNECTIVITY_SEARCH_LIMIT_SENTINEL in any_visited:
                            diagnostics.append(
                                Diagnostic.from_rule(
                                    "OC6505",
                                    f"Connectivity requirement {requirement.identifier!r} "
                                    "exceeded the bounded polarity search limit between "
                                    f"{source.key} and {sink.key}.",
                                    provenance=requirement.provenance,
                                    object=entity,
                                    property_name="connectivity.transform",
                                    evidence=self._connectivity_evidence(
                                        intent.view, requirement, source, sink
                                    ),
                                    metadata={"limit": _MAX_CONNECTIVITY_SEARCH_STATES},
                                )
                            )
                            failure_emitted = True
                            break
                        if any_polarity is not None and required_parity is not None:
                            diagnostics.append(
                                Diagnostic.from_rule(
                                    "OC6508",
                                    f"Connectivity requirement {requirement.identifier!r} "
                                    f"has the wrong inversion polarity between {source.key} "
                                    f"and {sink.key}.",
                                    provenance=requirement.provenance,
                                    object=entity,
                                    property_name="connectivity.transform",
                                    evidence=self._connectivity_evidence(
                                        intent.view, requirement, source, sink, any_polarity
                                    ),
                                    metadata={
                                        "expected_inverted": required_parity,
                                        "witness_path": [edge.to_dict() for edge in any_polarity],
                                    },
                                )
                            )
                            failure_emitted = True
                            break

                        wrong_sink: ConnectivityEndpoint | None = None
                        wrong_path: tuple[ConnectivityEdge, ...] | None = None
                        for candidate in ordered_sinks:
                            if candidate.key == sink.key or candidate.key not in visited:
                                continue
                            candidate_path, _ = self._find_connectivity_path(
                                source.key,
                                candidate.key,
                                known_adjacency,
                                through=through,
                                excluded=excluded,
                                required_parity=required_parity,
                            )
                            if candidate_path is not None:
                                wrong_sink, wrong_path = candidate, candidate_path
                                break
                        if wrong_sink is not None and wrong_path is not None:
                            diagnostics.append(
                                Diagnostic.from_rule(
                                    "OC6507",
                                    f"Connectivity requirement {requirement.identifier!r} maps "
                                    f"source bit {source.key} to {wrong_sink.key}, not expected "
                                    f"sink bit {sink.key}.",
                                    provenance=requirement.provenance,
                                    object=entity,
                                    property_name="connectivity.bit_mapping",
                                    evidence=self._connectivity_evidence(
                                        intent.view, requirement, source, wrong_sink, wrong_path
                                    ),
                                    metadata={
                                        "bit_ordinal": bit_ordinal,
                                        "expected_sink": sink.key,
                                        "actual_sink": wrong_sink.key,
                                        "witness_path": [edge.to_dict() for edge in wrong_path],
                                    },
                                )
                            )
                            failure_emitted = True
                            break

                        possible_path, possible_visited = self._find_connectivity_path(
                            source.key,
                            sink.key,
                            possible_adjacency,
                            through=through,
                            excluded=excluded,
                        )
                        possible_limited = _CONNECTIVITY_SEARCH_LIMIT_SENTINEL in possible_visited
                        if possible_path is not None or not graph_complete or possible_limited:
                            frontier = next(
                                (
                                    edge
                                    for edge in possible_path or ()
                                    if edge.status != FactState.KNOWN
                                ),
                                None,
                            )
                            reason = (
                                str(frontier.attributes.get("reason") or frontier.kind)
                                if frontier is not None
                                else "the bounded path search reached its state limit"
                                if possible_limited
                                else "the RTL connectivity graph has an unrepresented frontier"
                            )
                            diagnostics.append(
                                Diagnostic.from_rule(
                                    "OC6505",
                                    f"Connectivity requirement {requirement.identifier!r} "
                                    f"cannot prove a static path from {source.key} to "
                                    f"{sink.key}: {reason}.",
                                    provenance=requirement.provenance,
                                    object=entity,
                                    property_name="connectivity.path",
                                    evidence=self._connectivity_evidence(
                                        intent.view,
                                        requirement,
                                        source,
                                        sink,
                                        possible_path or (),
                                    ),
                                    metadata={
                                        "bit_ordinal": bit_ordinal,
                                        "frontier": frontier.to_dict()
                                        if frontier is not None
                                        else None,
                                    },
                                )
                            )
                        else:
                            cut = tuple(
                                sorted(node for node in visited if not known_adjacency.get(node))[
                                    :32
                                ]
                            )
                            diagnostics.append(
                                Diagnostic.from_rule(
                                    "OC6503",
                                    f"Connectivity requirement {requirement.identifier!r} "
                                    f"expects {source.key} to reach {sink.key}, but no static "
                                    "path exists in the supported graph.",
                                    provenance=requirement.provenance,
                                    object=entity,
                                    property_name="connectivity.path",
                                    evidence=self._connectivity_evidence(
                                        intent.view, requirement, source, sink
                                    ),
                                    metadata={
                                        "bit_ordinal": bit_ordinal,
                                        "reachable_cut": list(cut),
                                        "rtl_view": str(rtl.view),
                                    },
                                )
                            )
                        failure_emitted = True
                        break
                    if failure_emitted:
                        continue
        return diagnostics

    @staticmethod
    def _register_component(register: RegisterObservation) -> str:
        return decoded_identifier(
            register.component or register.memory_map or "<unspecified>"
        ).casefold()

    @staticmethod
    def _register_name(name: str) -> str:
        normalized = decoded_identifier(name).strip().casefold()
        return normalized[:-4] if normalized.endswith("_reg") else normalized

    @staticmethod
    def _register_field_name(name: str) -> str:
        # A field may legitimately be named MODE_REG alongside MODE.  The
        # register-level macro convenience suffix is not a field alias rule.
        return decoded_identifier(name).strip().casefold()

    @staticmethod
    def _register_scope(
        view: ViewId,
        register: RegisterObservation,
    ) -> tuple[str, ...]:
        # Software headers generally repeat the component/macro prefix in
        # memory_map; that is not structural address-map scope.  Keeping them
        # unscoped lets duplicate hardware registers produce OC6310 instead of
        # being paired arbitrarily.
        if _semantic_view_kind(view) == "header":
            return ()

        raw_register_files = register.attributes.get("register_files", ())
        register_files = (
            tuple(raw_register_files)
            if isinstance(raw_register_files, (list, tuple))
            and all(isinstance(item, str) for item in raw_register_files)
            else ()
        )
        anchor = register.address_block or register.memory_map
        segments: list[str] = []
        for item in (anchor, *register_files):
            if not item:
                continue
            segments.extend(
                normalized
                for segment in item.split("/")
                if (normalized := decoded_identifier(segment).strip().casefold())
            )
        return tuple(segments)

    @classmethod
    def _group_register_entries(
        cls,
        entries: Sequence[tuple[ViewId, RegisterObservation]],
    ) -> tuple[
        dict[tuple[str, str], list[tuple[ViewId, RegisterObservation]]],
        dict[tuple[str, str], set[ViewId]],
        list[
            tuple[
                str,
                str,
                tuple[tuple[str, ...], ...],
                tuple[tuple[ViewId, RegisterObservation], ...],
            ]
        ],
    ]:
        """Resolve register identity without flattening repeated address-block names.

        A register name is sufficient while every view has at most one scoped
        occurrence.  If any view contains that name in multiple maps/blocks,
        scoped occurrences remain distinct and unscoped software declarations
        are reported as ambiguous instead of being paired arbitrarily.
        """

        by_base: dict[tuple[str, str], list[tuple[ViewId, RegisterObservation]]] = defaultdict(list)
        for view, register in entries:
            by_base[
                (
                    cls._register_component(register),
                    cls._register_name(register.native_name),
                )
            ].append((view, register))

        groups: dict[tuple[str, str], list[tuple[ViewId, RegisterObservation]]] = defaultdict(list)
        suppressed_missing: dict[tuple[str, str], set[ViewId]] = defaultdict(set)
        ambiguities: list[
            tuple[
                str,
                str,
                tuple[tuple[str, ...], ...],
                tuple[tuple[ViewId, RegisterObservation], ...],
            ]
        ] = []
        for (component, name), members in sorted(by_base.items()):
            scopes_by_view: dict[ViewId, set[tuple[str, ...]]] = defaultdict(set)
            for view, register in members:
                scope = cls._register_scope(view, register)
                if scope:
                    scopes_by_view[view].add(scope)
            requires_scope = any(len(scopes) > 1 for scopes in scopes_by_view.values())
            if not requires_scope:
                groups[(component, name)].extend(members)
                continue

            scoped_views = {
                view for view, register in members if cls._register_scope(view, register)
            }
            unscoped_members = tuple(
                (view, register)
                for view, register in members
                if not cls._register_scope(view, register)
            )
            unscoped_views = {view for view, _ in unscoped_members}
            distinct_scopes = tuple(
                sorted(
                    {
                        cls._register_scope(view, register)
                        for view, register in members
                        if cls._register_scope(view, register)
                    }
                )
            )
            for view, register in members:
                scope = cls._register_scope(view, register)
                identity = "/".join((*scope, name)) if scope else name
                groups[(component, identity)].append((view, register))
                if scope:
                    suppressed_missing[(component, identity)].update(unscoped_views)
                else:
                    suppressed_missing[(component, identity)].update(scoped_views)
            if unscoped_members:
                ambiguities.append((component, name, distinct_scopes, tuple(members)))
        return groups, suppressed_missing, ambiguities

    @staticmethod
    def _normalized_access(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold().replace("-", "").replace("_", "")
        aliases = {
            "ro": "readonly",
            "read": "readonly",
            "wo": "writeonly",
            "write": "writeonly",
            "rw": "readwrite",
        }
        return aliases.get(normalized, normalized)

    def _check_registers(
        self,
        observations: Sequence[ViewObservation],
        contract: DesignContract | None,
    ) -> list[Diagnostic]:
        entries: list[tuple[ViewId, RegisterObservation]] = [
            (observation.view, register)
            for observation in observations
            for register in observation.registers
        ]
        contract_view = ViewId("contract", "frozen")
        if contract is not None:
            for expected in contract.registers:
                entries.append(
                    (
                        contract_view,
                        RegisterObservation(
                            native_name=expected.canonical_name,
                            component=expected.component,
                            memory_map=expected.memory_map,
                            address_block=expected.address_block,
                            address_offset=expected.address_offset,
                            absolute_address=expected.absolute_address,
                            size_bits=expected.size_bits,
                            access=expected.access,
                            fields=tuple(
                                RegisterFieldObservation(
                                    native_name=field.canonical_name,
                                    bit_offset=field.bit_offset,
                                    bit_width=field.bit_width,
                                    access=field.access,
                                    reset_value=field.reset_value,
                                )
                                for field in expected.fields
                            ),
                            attributes={"contract": True},
                        ),
                    )
                )

        groups, suppressed_missing, ambiguities = self._group_register_entries(entries)
        by_component: dict[str, dict[ViewId, dict[str, list[RegisterObservation]]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(list))
        )
        observation_map = {item.view: item for item in observations}
        for (component, identity), members in groups.items():
            for view, register in members:
                by_component[component][view][identity].append(register)

        register_view_kinds = {
            "ipxact",
            "ip_xact",
            "header",
            "c_header",
            "cheader",
            "software",
            "systemrdl",
        }
        declared_components: dict[ViewId, set[str]] = defaultdict(set)
        for observation in observations:
            if _semantic_view_kind(observation.view) not in register_view_kinds:
                continue
            declared_components[observation.view].update(
                decoded_identifier(item.native_name).casefold() for item in observation.components
            )
            declared_components[observation.view].update(
                self._register_component(register) for register in observation.registers
            )
            configured_component = observation.attributes.get("component_name")
            if isinstance(configured_component, str) and configured_component.strip():
                declared_components[observation.view].add(
                    decoded_identifier(configured_component).casefold()
                )
        for component, by_view in by_component.items():
            for view, components in declared_components.items():
                if component in components:
                    by_view.setdefault(view, {})

        diagnostics: list[Diagnostic] = [
            Diagnostic.from_rule(
                "OC6310",
                f"Register {component}/{name} appears in multiple address-map scopes "
                + ", ".join("/".join(scope) for scope in scopes)
                + "; the unscoped declaration cannot be associated unambiguously.",
                object=_object(
                    "register",
                    f"register:{component}/{name}",
                    f"{component}/{name}",
                ),
                property_name="identity",
                evidence=tuple(
                    DiagnosticEvidence(view, register.native_name, register.provenance)
                    for view, register in evidence_members
                ),
                metadata={"scopes": [list(scope) for scope in scopes]},
            )
            for component, name, scopes, evidence_members in ambiguities
        ]
        for component, by_view in sorted(by_component.items()):
            views = set(by_view)
            register_names = {
                name for view_registers in by_view.values() for name in view_registers
            }
            for view, view_registers in sorted(by_view.items()):
                for name, duplicate_definitions in sorted(view_registers.items()):
                    if len(duplicate_definitions) < 2:
                        continue
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC6307",
                            f"{_view_label(view)} defines register {component}/{name} "
                            f"{len(duplicate_definitions)} times.",
                            object=_object(
                                "register",
                                f"register:{component}/{name}",
                                f"{component}/{name}",
                            ),
                            property_name="definition",
                            evidence=tuple(
                                DiagnosticEvidence(
                                    view,
                                    definition.native_name,
                                    definition.provenance,
                                )
                                for definition in duplicate_definitions
                            ),
                        )
                    )
            for name in sorted(register_names):
                register_members: list[tuple[ViewId, RegisterObservation]] = [
                    (view, items[0])
                    for view, view_registers in sorted(by_view.items())
                    if (items := view_registers.get(name))
                ]
                present_views = {view for view, _ in register_members}
                missing_views = {
                    view
                    for view in views - present_views
                    if not self._whole_view_tainted(observation_map.get(view))
                    and view not in suppressed_missing.get((component, name), set())
                }
                display = f"{component}/{name}"
                entity = _object("register", f"register:{component}/{name}", display)
                if missing_views:
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC6301",
                            f"Register {display} is present in "
                            + ", ".join(_view_label(view) for view in sorted(present_views))
                            + " but missing from "
                            + ", ".join(_view_label(view) for view in sorted(missing_views))
                            + ".",
                            object=entity,
                            property_name="presence",
                            evidence=tuple(
                                DiagnosticEvidence(
                                    view,
                                    register.native_name,
                                    register.provenance,
                                )
                                for view, register in register_members
                            ),
                            metadata={
                                "missing_views": [str(view) for view in sorted(missing_views)]
                            },
                        )
                    )
                known = [
                    (view, register)
                    for view, register in register_members
                    if register.status == FactState.KNOWN
                ]
                diagnostics.extend(self._check_register_integrity(component, name, known, entity))
                offset_members = [
                    (view, register)
                    for view, register in known
                    if register.address_offset is not None
                ]
                offsets = {register.address_offset for _, register in offset_members}
                addresses = {
                    register.absolute_address
                    for _, register in known
                    if register.absolute_address is not None
                }
                offset_conflict = len(offsets) > 1
                address_conflict = len(addresses) > 1
                if offset_conflict or address_conflict:
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC6302",
                            f"Register {display} has conflicting addresses: "
                            + "; ".join(
                                f"{_view_label(view)} offset="
                                f"{register.address_offset!r}, address="
                                f"{register.absolute_address!r}"
                                for view, register in known
                            )
                            + ".",
                            object=entity,
                            property_name="address",
                            evidence=tuple(
                                DiagnosticEvidence(
                                    view,
                                    {
                                        "offset": register.address_offset,
                                        "absolute": register.absolute_address,
                                    },
                                    register.provenance,
                                )
                                for view, register in known
                            ),
                        )
                    )
                widths = {
                    register.size_bits for _, register in known if register.size_bits is not None
                }
                if len(widths) > 1:
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC6303",
                            f"Register {display} has conflicting widths: "
                            + "; ".join(
                                f"{_view_label(view)}={register.size_bits} bits"
                                for view, register in known
                                if register.size_bits is not None
                            )
                            + ".",
                            object=entity,
                            property_name="size_bits",
                            evidence=tuple(
                                DiagnosticEvidence(
                                    view,
                                    register.size_bits,
                                    register.provenance,
                                )
                                for view, register in known
                                if register.size_bits is not None
                            ),
                        )
                    )
                accesses = {
                    self._normalized_access(register.access)
                    for _, register in known
                    if register.access is not None
                }
                if len(accesses) > 1:
                    diagnostics.append(
                        Diagnostic.from_rule(
                            "OC6306",
                            f"Register {display} has conflicting access permissions.",
                            object=entity,
                            property_name="access",
                            evidence=tuple(
                                DiagnosticEvidence(view, register.access, register.provenance)
                                for view, register in known
                                if register.access is not None
                            ),
                        )
                    )
                diagnostics.extend(
                    self._check_register_fields(
                        component,
                        name,
                        register_members,
                        observation_map,
                    )
                )
        return diagnostics

    def _check_register_integrity(
        self,
        component: str,
        register_name: str,
        definitions: Sequence[tuple[ViewId, RegisterObservation]],
        entity: DiagnosticObject,
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for view, register in definitions:
            problems: list[str] = []
            evidence_fields: list[RegisterFieldObservation] = []
            intervals: list[tuple[int, int, RegisterFieldObservation]] = []
            for register_field in register.fields:
                if (
                    register_field.status != FactState.KNOWN
                    or register_field.bit_offset is None
                    or register_field.bit_width is None
                ):
                    continue
                first = register_field.bit_offset
                last = first + register_field.bit_width - 1
                if register.size_bits is not None and last >= register.size_bits:
                    problems.append(
                        f"{register_field.native_name}[{last}:{first}] exceeds "
                        f"{register.size_bits} bits"
                    )
                    evidence_fields.append(register_field)
                intervals.append((first, last, register_field))
            active: tuple[int, int, RegisterFieldObservation] | None = None
            for interval in sorted(
                intervals, key=lambda item: (item[0], item[1], item[2].native_name)
            ):
                if active is not None and interval[0] <= active[1]:
                    problems.append(f"{interval[2].native_name} overlaps {active[2].native_name}")
                    evidence_fields.extend((active[2], interval[2]))
                if active is None or interval[1] > active[1]:
                    active = interval
            if not problems:
                continue
            display = f"{component}/{register_name}"
            diagnostics.append(
                Diagnostic.from_rule(
                    "OC6309",
                    f"Register {display} has an invalid field layout in "
                    f"{_view_label(view)}: " + "; ".join(problems) + ".",
                    object=entity,
                    property_name="fields.layout",
                    evidence=tuple(
                        DiagnosticEvidence(
                            view,
                            {
                                "field": field.native_name,
                                "bit_offset": field.bit_offset,
                                "bit_width": field.bit_width,
                            },
                            field.provenance,
                        )
                        for field in evidence_fields
                    ),
                )
            )
        return diagnostics

    def _check_register_fields(
        self,
        component: str,
        register_name: str,
        definitions: Sequence[tuple[ViewId, RegisterObservation]],
        observations: Mapping[ViewId, ViewObservation],
    ) -> list[Diagnostic]:
        by_view: dict[ViewId, dict[str, list[RegisterFieldObservation]]] = defaultdict(
            lambda: defaultdict(list)
        )
        diagnostics: list[Diagnostic] = []
        for view, register in definitions:
            for field in register.fields:
                by_view[view][self._register_field_name(field.native_name)].append(field)
        field_names = {name for fields in by_view.values() for name in fields}
        if not field_names:
            return diagnostics
        views = {view for view, _ in definitions}
        for view, fields in sorted(by_view.items()):
            for name, items in sorted(fields.items()):
                if len(items) < 2:
                    continue
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC6307",
                        f"{_view_label(view)} defines field "
                        f"{component}/{register_name}/{name} {len(items)} times.",
                        object=_object(
                            "register_field",
                            f"register:{component}/{register_name}/field:{name}",
                            f"{component}/{register_name}/{name}",
                        ),
                        property_name="definition",
                        evidence=tuple(
                            DiagnosticEvidence(view, item.native_name, item.provenance)
                            for item in items
                        ),
                    )
                )
        for name in sorted(field_names):
            field_members: list[tuple[ViewId, RegisterFieldObservation]] = []
            for view, view_fields in sorted(by_view.items()):
                matching_fields = view_fields.get(name)
                if matching_fields:
                    field_members.append((view, matching_fields[0]))
            present = {view for view, _ in field_members}
            missing = {
                view
                for view in views - present
                if not self._whole_view_tainted(observations.get(view))
            }
            display = f"{component}/{register_name}/{name}"
            entity = _object(
                "register_field",
                f"register:{component}/{register_name}/field:{name}",
                display,
            )
            if missing:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC6304",
                        f"Register field {display} is missing from "
                        + ", ".join(_view_label(view) for view in sorted(missing))
                        + ".",
                        object=entity,
                        property_name="presence",
                        evidence=tuple(
                            DiagnosticEvidence(view, field.native_name, field.provenance)
                            for view, field in field_members
                        ),
                        metadata={"missing_views": [str(view) for view in sorted(missing)]},
                    )
                )
            known = [
                (view, field) for view, field in field_members if field.status == FactState.KNOWN
            ]
            offsets = {field.bit_offset for _, field in known if field.bit_offset is not None}
            widths = {field.bit_width for _, field in known if field.bit_width is not None}
            if len(offsets) > 1 or len(widths) > 1:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC6305",
                        f"Register field {display} has conflicting layouts.",
                        object=entity,
                        property_name="layout",
                        evidence=tuple(
                            DiagnosticEvidence(
                                view,
                                {"bit_offset": field.bit_offset, "bit_width": field.bit_width},
                                field.provenance,
                            )
                            for view, field in known
                        ),
                    )
                )
            accesses = {
                self._normalized_access(field.access)
                for _, field in known
                if field.access is not None
            }
            if len(accesses) > 1:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC6306",
                        f"Register field {display} has conflicting access permissions.",
                        object=entity,
                        property_name="access",
                        evidence=tuple(
                            DiagnosticEvidence(view, field.access, field.provenance)
                            for view, field in known
                            if field.access is not None
                        ),
                    )
                )
            reset_values = {
                field.reset_value for _, field in known if field.reset_value is not None
            }
            if len(reset_values) > 1:
                diagnostics.append(
                    Diagnostic.from_rule(
                        "OC6308",
                        f"Register field {display} has conflicting reset values.",
                        object=entity,
                        property_name="reset_value",
                        evidence=tuple(
                            DiagnosticEvidence(view, field.reset_value, field.provenance)
                            for view, field in known
                            if field.reset_value is not None
                        ),
                    )
                )
        return diagnostics

    @staticmethod
    def _contract_component(
        contract: DesignContract | None, canonical: str
    ) -> ContractComponent | None:
        if contract is None:
            return None
        return next(
            (
                component
                for component in contract.components
                if component.canonical_name == canonical
            ),
            None,
        )

    def _apply_severity_overrides(self, diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
        result = []
        for diagnostic in diagnostics:
            rule = RULES.get(diagnostic.code)
            if diagnostic.severity == Severity.FATAL or (
                rule is not None and rule.default_severity == Severity.FATAL
            ):
                result.append(diagnostic.with_severity(Severity.FATAL))
                continue
            override = self.config.policy.severity_overrides.get(diagnostic.code)
            result.append(
                diagnostic.with_severity(override) if override is not None else diagnostic
            )
        return result

    def _apply_waivers(self, diagnostics: Iterable[Diagnostic], *, today: date) -> list[Diagnostic]:
        result = list(diagnostics)
        match_counts = [0] * len(self.config.waivers)
        expired = [False] * len(self.config.waivers)
        for index, waiver in enumerate(self.config.waivers):
            if waiver.expires is not None and waiver.expires < today:
                expired[index] = True
                result.append(
                    Diagnostic.from_rule(
                        "OC1004",
                        f"Waiver {index + 1} for {waiver.code} expired on "
                        f"{waiver.expires.isoformat()}.",
                        object=_object("waiver", f"waiver:{index + 1}", f"waiver {index + 1}"),
                        metadata={"waiver_index": index + 1},
                    )
                )
        for diagnostic_index, diagnostic in enumerate(result[: len(result) - sum(expired)]):
            rule = RULES.get(diagnostic.code)
            if diagnostic.severity == Severity.FATAL or (
                rule is not None and rule.default_severity == Severity.FATAL
            ):
                continue
            for waiver_index, waiver in enumerate(self.config.waivers):
                if expired[waiver_index] or not self._waiver_matches(waiver, diagnostic):
                    continue
                match_counts[waiver_index] += 1
                result[diagnostic_index] = diagnostic.with_waiver(waiver.reason)
                break
        if self.config.policy.report_unmatched_waivers:
            for index, waiver in enumerate(self.config.waivers):
                if expired[index] or match_counts[index]:
                    continue
                result.append(
                    Diagnostic.from_rule(
                        "OC1005",
                        f"Waiver {index + 1} for {waiver.code} matched no diagnostic.",
                        object=_object("waiver", f"waiver:{index + 1}", f"waiver {index + 1}"),
                        metadata={"waiver_index": index + 1},
                    )
                )
        return result

    @staticmethod
    def _waiver_matches(waiver: Waiver, diagnostic: Diagnostic) -> bool:
        if not fnmatchcase(diagnostic.code, waiver.code):
            return False
        entity_id = diagnostic.object.id if diagnostic.object is not None else ""
        if not fnmatchcase(entity_id, waiver.object_pattern):
            return False
        if waiver.property_pattern is not None and not fnmatchcase(
            diagnostic.property_name or "", waiver.property_pattern
        ):
            return False
        if waiver.fingerprint is not None and waiver.fingerprint != diagnostic.fingerprint:
            return False
        diagnostic_views = {item.view for item in diagnostic.evidence}
        for selector in waiver.views:
            if not any(
                selector.lower() in {view.kind, view.key.lower()}
                or _semantic_view_kind(selector) == _semantic_view_kind(view)
                or fnmatchcase(view.key.lower(), selector.lower())
                for view in diagnostic_views
            ):
                return False
        return True

    @staticmethod
    def _coerce_parser_diagnostic(item: Any, view: ViewId) -> Diagnostic:
        if isinstance(item, Diagnostic):
            return item
        if isinstance(item, Mapping):
            try:
                return Diagnostic.from_rule(
                    str(item.get("code", "OC1101")),
                    str(item.get("message", "Parser reported an unspecified failure.")),
                    severity=str(item.get("severity", "error")),
                    metadata={"view": str(view)},
                )
            except (KeyError, ValueError):
                pass
        return Diagnostic.from_rule(
            "OC1101",
            f"{_view_label(view)} parser reported: {item}",
            metadata={"view": str(view)},
        )

    def build_contract(
        self,
        design: CanonicalDesign,
        observations: Sequence[ViewObservation] = (),
    ) -> DesignContract:
        components: list[ContractComponent] = []
        for component in design.components:
            component_member = self._preferred_component_member(component.members)
            names: dict[str, str] = {}
            for member in component.members:
                names.setdefault(str(member.view), member.observation.native_name)
            ports: list[ContractPort] = []
            for port in component.ports:
                port_member = self._preferred_port_member(port.members)
                port_names: dict[str, str] = {}
                for port_member_item in port.members:
                    port_names.setdefault(
                        str(port_member_item.view),
                        port_member_item.observation.native_name,
                    )
                ports.append(
                    ContractPort(
                        canonical_name=port.canonical_name,
                        names=port_names,
                        direction=port_member.observation.direction,
                        role=port_member.observation.role,
                        shape=port_member.observation.shape,
                    )
                )
            components.append(
                ContractComponent(
                    canonical_name=component.canonical_name,
                    kind=component_member.observation.kind,
                    names=names,
                    required_views=tuple(str(view) for view in component.required_views),
                    ports=tuple(ports),
                )
            )
        return DesignContract(
            tuple(components),
            schema_version=CONTRACT_SCHEMA_VERSION,
            registers=self._build_contract_registers(observations),
            views=snapshots_from_observations(observations),
        )

    def _build_contract_registers(
        self,
        observations: Sequence[ViewObservation],
    ) -> tuple[ContractRegister, ...]:
        groups, _, _ = self._group_register_entries(
            tuple(
                (observation.view, register)
                for observation in observations
                for register in observation.registers
            )
        )

        result: list[ContractRegister] = []
        baseline = self.config.contract.baseline
        register_authority = self.config.contract.authority.get("registers")

        def member_rank(view: ViewId, native_name: str) -> tuple[Any, ...]:
            authority_rank = (
                0 if register_authority is not None and view.matches(register_authority) else 1
            )
            baseline_rank = 0 if baseline is not None and view == baseline else 1
            format_rank = {
                "ipxact": 0,
                "systemrdl": 1,
                "header": 10,
                "c_header": 10,
                "software": 10,
            }.get(_semantic_view_kind(view), 5)
            return authority_rank, baseline_rank, format_rank, view, native_name

        for _, members in sorted(groups.items()):
            usable = [item for item in members if item[1].status == FactState.KNOWN] or members
            preferred_view, preferred = min(
                usable,
                key=lambda item: member_rank(item[0], item[1].native_name),
            )
            preferred_scope = self._register_scope(preferred_view, preferred)
            contract_address_block = (
                "/".join(preferred_scope) if preferred_scope else preferred.address_block
            )
            names: dict[str, str] = {}
            field_groups: dict[str, list[tuple[ViewId, RegisterFieldObservation]]] = defaultdict(
                list
            )
            for view, register in sorted(members, key=lambda item: (item[0], item[1].native_name)):
                names.setdefault(str(view), register.native_name)
                for register_field in register.fields:
                    field_groups[self._register_field_name(register_field.native_name)].append(
                        (view, register_field)
                    )
            contract_fields: list[ContractRegisterField] = []
            for field_members in field_groups.values():
                known_fields = [
                    item for item in field_members if item[1].status == FactState.KNOWN
                ] or field_members
                _, preferred_field = min(
                    known_fields,
                    key=lambda item: member_rank(item[0], item[1].native_name),
                )
                field_names: dict[str, str] = {}
                for view, register_field in sorted(
                    field_members, key=lambda item: (item[0], item[1].native_name)
                ):
                    field_names.setdefault(str(view), register_field.native_name)
                contract_fields.append(
                    ContractRegisterField(
                        canonical_name=decoded_identifier(preferred_field.native_name),
                        names=field_names,
                        bit_offset=preferred_field.bit_offset,
                        bit_width=preferred_field.bit_width,
                        access=preferred_field.access,
                        reset_value=preferred_field.reset_value,
                    )
                )
            component = decoded_identifier(
                preferred.component or preferred.memory_map or "<unspecified>"
            )
            result.append(
                ContractRegister(
                    canonical_name=decoded_identifier(preferred.native_name),
                    component=component,
                    names=names,
                    memory_map=preferred.memory_map,
                    address_block=contract_address_block,
                    address_offset=preferred.address_offset,
                    absolute_address=preferred.absolute_address,
                    size_bits=preferred.size_bits,
                    access=preferred.access,
                    fields=tuple(contract_fields),
                )
            )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.component,
                    item.memory_map or "",
                    item.address_block or "",
                    item.canonical_name,
                ),
            )
        )

    def _preferred_component_member(self, members: Sequence[ComponentMember]) -> ComponentMember:
        baseline = self.config.contract.baseline
        authority = self.config.contract.authority.get("components")
        return min(
            members,
            key=lambda item: (
                0 if authority is not None and item.view.matches(authority) else 1,
                0 if baseline is not None and item.view == baseline else 1,
                item.view,
                item.observation.native_name,
            ),
        )

    def _preferred_port_member(self, members: Sequence[PortMember]) -> PortMember:
        baseline = self.config.contract.baseline
        authority = self.config.contract.authority.get("ports")
        known = [item for item in members if item.observation.status == FactState.KNOWN] or list(
            members
        )
        return min(
            known,
            key=lambda item: (
                0 if authority is not None and item.view.matches(authority) else 1,
                0 if baseline is not None and item.view == baseline else 1,
                item.view,
                item.observation.native_name,
            ),
        )

    def export_contract(
        self,
        design: CanonicalDesign,
        path: str | Path,
        observations: Sequence[ViewObservation] = (),
    ) -> Path:
        contract = self.build_contract(design, observations)
        return write_contract(contract, path)


def write_contract(contract: DesignContract, path: str | Path) -> Path:
    """Write deterministic contract JSON and return its resolved path."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            contract.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return destination


__all__ = [
    "ComparisonEngine",
    "EngineResult",
    "ReconciliationResult",
    "write_contract",
]

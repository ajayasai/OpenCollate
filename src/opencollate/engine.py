"""Reconciliation and semantic comparison engine.

The engine is intentionally parser-neutral: callers provide ``ViewObservation``
objects, and every diagnostic retains the observations that support it.
"""

from __future__ import annotations

import json
from collections import defaultdict
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
    ComponentMember,
    ComponentObservation,
    ContractComponent,
    ContractPort,
    DesignContract,
    Direction,
    FactState,
    PinMappingObservation,
    PortMember,
    PortObservation,
    PortRole,
    Provenance,
    ViewId,
    ViewObservation,
    decoded_identifier,
)


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
            },
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class _ValueEvidence:
    view: ViewId
    value: Any
    provenance: Provenance | None
    native_name: str | None = None

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
        if normalized == view.kind:
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
    label = {
        "rtl": "RTL",
        "systemverilog": "RTL",
        "verilog": "RTL",
        "liberty": "Liberty",
        "lef": "LEF",
        "csv": "CSV",
        "contract": "the contract",
    }.get(view.kind, view.kind.upper())
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
        diagnostics = self._apply_severity_overrides(diagnostics)
        diagnostics = self._apply_waivers(diagnostics, today=today or date.today())
        ordered = sort_diagnostics(diagnostics)
        generated = self.build_contract(reconciliation.design)
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
        active = {view for view in active if not self._is_package_map_view(view)}
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
            required_views = set(component_views)
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
            result = {
                view for view in result if view.kind not in {"rtl", "verilog", "systemverilog"}
            }
        return result

    def _is_package_map_view(self, view: ViewId) -> bool:
        if view.kind not in {"csv", "pinmap", "pin_map"}:
            return False
        try:
            source = self.config.source(view)
        except KeyError:
            return False
        return (source.profile or "").strip().lower().replace("-", "_") == "package_map"

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
            if len({item.view.kind for item in values}) < 2:
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
                if unknown and item.view.kind == "liberty":
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
        for observation in observations:
            if not self._is_package_map_view(observation.view):
                continue
            rows.extend((observation.view, item) for item in observation.pin_mappings)
        diagnostics: list[Diagnostic] = []
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

    def build_contract(self, design: CanonicalDesign) -> DesignContract:
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
        return DesignContract(tuple(components))

    def _preferred_component_member(self, members: Sequence[ComponentMember]) -> ComponentMember:
        baseline = self.config.contract.baseline
        return min(
            members,
            key=lambda item: (
                0 if baseline is not None and item.view == baseline else 1,
                item.view,
                item.observation.native_name,
            ),
        )

    def _preferred_port_member(self, members: Sequence[PortMember]) -> PortMember:
        baseline = self.config.contract.baseline
        known = [item for item in members if item.observation.status == FactState.KNOWN] or list(
            members
        )
        return min(
            known,
            key=lambda item: (
                0 if baseline is not None and item.view == baseline else 1,
                item.view,
                item.observation.native_name,
            ),
        )

    def export_contract(
        self,
        design: CanonicalDesign,
        path: str | Path,
    ) -> Path:
        contract = self.build_contract(design)
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

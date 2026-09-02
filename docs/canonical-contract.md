# Canonical design contract

The design contract is OpenCollate’s reviewed, reconciliation-ready representation of design
identity plus durable snapshots of the parser-neutral facts used during analysis. It is an audit
artifact, not a replacement for source collateral and not a signoff database.

Build the current contract schema with:

```console
opencollate contract build [CONFIG] --output contract.oc.json
```

Generate the exact JSON Schema for the installed build with:

```console
opencollate schema contract --output contract.schema.json
```

## Schema version 2

Newly generated contracts use schema version 2. The compact canonical sections persist:

- Components: canonical name, kind, per-view native names, required views, and ports.
- Ports: canonical name, per-view native names, direction, role, and full bus shape.
- Registers: component, canonical and per-view names, memory map/address block, offset, absolute
  address, width, access, and fields.
- Register fields: canonical and per-view names, bit offset, bit width, access, and reset value.

Version 2 also persists one deterministic `views` snapshot for every parsed view. A snapshot
contains every parser-neutral observation family currently carried by `ViewObservation`:

- components, ports, functions, shapes, fact states, and parser attributes;
- die-pad, package-ball, and signal mappings;
- hierarchical definitions and references, including power-intent and constraint objects;
- primary and generated clocks;
- logical interfaces and logical-to-physical port maps;
- registers and register fields as observed in that individual view;
- static connectivity endpoints, edges, and declarative requirements;
- view-level parser attributes, including supported SDC/UPF metadata;
- view completeness and tainted scopes.

This closes the schema-version-1 gap where those facts participated in a live run but could not be
retained in a frozen contract. A semantic-checker plugin can inspect them through
`CheckerContext.contract.views` without reparsing the original collateral.

## Integrity and determinism

Each view snapshot carries `content_sha256`, calculated over canonical JSON containing all snapshot
facts except the digest itself. Map keys, record arrays, and tainted scopes are normalized into a
deterministic order before hashing and serialization.

OpenCollate recomputes the digest when loading a contract. A stale or modified snapshot is rejected
instead of being accepted as reviewed state. The digest detects accidental or unacknowledged
content changes; it is not a digital signature and does not prove authorship, approval, or trusted
provenance. Sign or attest the contract artifact separately when that assurance is required.

The contract also has a top-level `extensions` object. Extension values must be finite,
JSON-compatible data with string object keys and bounded nesting. Unknown extension namespaces are
preserved through loading and writing so organizations and plugins can attach durable metadata
without modifying core fields.

## Version 1 compatibility and migration

Schema-version-1 contracts remain readable. They retain their original components, ports, and
registers and continue to work as identity/baseline input. They cannot contain `views` or
`extensions`, because those fields did not exist in version 1.

Upgrade a legacy contract explicitly with:

```console
opencollate contract migrate legacy.oc.json --output contract.v2.oc.json
```

Migration never invents facts that the legacy file did not store. The resulting version-2
contract therefore has an empty `views` array and an `opencollate.migration` extension explaining
that view snapshots are unavailable. Rebuild from the original configured collateral to obtain a
complete version-2 contract.

## Identity and evidence

A canonical component can group an RTL module, Liberty cell, LEF macro, IP-XACT component,
CDL/SPICE subcircuit, DEF design interface, and GDSII cell structure. A canonical port groups the
corresponding native names. GDSII contributes ports only from explicitly filtered text labels,
with unknown direction, role, and logical shape. A canonical register groups hardware and
software register definitions by normalized component/register identity.

Original names remain in per-view `names`. Aliases change grouping; they do not rewrite source
evidence. Frozen view snapshots preserve facts and fact state, while diagnostics retain the rule
comparison evidence and source locations selected for each finding.

An abbreviated version-2 document has this shape:

```json
{
  "schema_version": 2,
  "generated_by": "OpenCollate",
  "components": [
    {
      "canonical_name": "uart",
      "kind": "module",
      "names": {
        "rtl.default": "uart",
        "ipxact.component": "uart"
      },
      "required_views": ["rtl.default", "ipxact.component"],
      "ports": [
        {
          "canonical_name": "data_i",
          "names": {"rtl.default": "data_i"},
          "direction": "input",
          "role": "signal",
          "shape": {
            "width": 8,
            "left": 7,
            "right": 0,
            "ascending": false,
            "packed": [{"left": 7, "right": 0, "step": -1, "width": 8}],
            "unpacked": []
          }
        }
      ]
    }
  ],
  "registers": [],
  "views": [
    {
      "snapshot_version": 1,
      "view": "sdc.signoff",
      "complete": true,
      "tainted_scopes": [],
      "components": [],
      "pin_mappings": [],
      "objects": [],
      "clocks": [{"name": "core_clk", "period": 2.5, "targets": ["clk"]}],
      "interfaces": [],
      "registers": [],
      "connectivity_endpoints": [],
      "connectivity_edges": [],
      "connectivity_requirements": [],
      "attributes": {},
      "extensions": {},
      "content_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ],
  "extensions": {}
}
```

This example is intentionally abbreviated: real snapshot records include the complete normalized
field set used to calculate their digest. Validate consumers against the generated schema rather
than copying the illustration.

## Shapes are structural

Bus shape retains more than width:

- Packed and unpacked dimensions.
- Declared left/right bounds and ordering.
- Explicit per-bit indices supplied by exploded CSV or DEF rows.
- Whether a one-bit value was an explicit scalar or vector where known.

This distinguishes `[7:0]` from `[0:7]`, a scalar from an unknown shape, a contiguous bus from a
gapped list, and packed from unpacked dimensions. A CDL/SPICE terminal normally contributes an
unknown logical shape; it does not force other views to scalar.

## Authority and baseline

`contract.baseline` selects a preferred view where intent is required. Optional
`contract.authority` selectors can prefer sources for contract categories such as components,
ports, or registers. Preference does not erase disagreement: checks still retain and report
conflicting observations.

A committed frozen contract can act as a reviewed baseline:

```toml
[contract]
file = "contract.oc.json"
```

Treat it like design source. Review changes, keep it under version control, and rebuild it only
after deciding whether source drift is intentional. Snapshot digests make an unreviewed content
change explicit, but repository review and artifact attestation remain the approval mechanisms.

## Unknown and tainted facts

Contract selection does not manufacture missing data. When no known candidate establishes a
property, it stays unknown in the selected canonical model. Unsupported or tainted observations
remain represented in their view snapshots, and the snapshot records whether the view was
complete. A generated contract must not be used as proof that every input was fully understood;
review parser diagnostics and tainted scopes alongside it.

## Compatibility

Contract documents carry `schema_version`, and each view snapshot carries `snapshot_version`.
This release reads contract schemas 1 and 2 and writes schema 2. Incompatible versions are rejected
rather than guessed. The version-2 extension object is the forward-compatible location for
namespaced metadata; core semantic changes still require an explicit schema version.

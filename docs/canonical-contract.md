# Canonical design contract

The design contract is OpenCollate’s reviewed, reconciliation-ready view of design identity and
selected interface/register facts. It is an audit artifact, not a new source of design intent and
not a signoff database.

Build one with:

```console
opencollate contract build [CONFIG] --output contract.oc.json
```

Generate the exact schema for the installed build with:

```console
opencollate schema contract --output contract.schema.json
```

## Schema version 1 contents

The 0.3.0 contract persists:

- Components: canonical name, kind, per-view native names, required views, and ports.
- Ports: canonical name, per-view native names, direction, role, and full bus shape.
- Registers: component, canonical and per-view names, memory map/address block, offset, absolute
  address, width, access, and fields.
- Register fields: canonical and per-view names, bit offset, bit width, access, and reset value.

The live observation model is broader. It also carries hierarchical design objects, SDC queries
and clocks, UPF objects, IP-XACT interfaces, DEF nets/endpoints, constraints, and package mappings.
Those facts participate in 0.3.0 rules but are not all serialized in frozen-contract schema
version 1. Do not assume a contract JSON file is a complete dump of every parsed fact.

Register offsets are normalized relative to their containing address block; nested register-file
segments remain part of register identity. Importers may retain an immediate parent-local offset
as observation metadata, but the contract field uses the normalized address-block-relative value.

## Identity and evidence

A canonical component can group an RTL module, Liberty cell, LEF macro, IP-XACT component,
CDL/SPICE subcircuit, DEF design interface, and GDSII cell structure. A canonical port groups the
corresponding native names. GDSII contributes ports only from explicitly filtered text labels,
with unknown direction, role, and logical shape. A canonical register groups hardware and
software register definitions by normalized component/register identity.

Original names remain in per-view `names`. Aliases change grouping; they do not rewrite source
evidence. Diagnostics—not the compact frozen contract—carry the complete rule evidence with view,
value, fact state, and source location.

Conceptually:

```json
{
  "schema_version": 1,
  "generated_by": "OpenCollate",
  "components": [
    {
      "canonical_name": "uart",
      "names": {
        "rtl.default": "uart",
        "ipxact.component": "uart"
      },
      "ports": [
        {
          "canonical_name": "data_i",
          "direction": "input",
          "role": "signal",
          "shape": {
            "width": 8,
            "left": 7,
            "right": 0,
            "packed": [{"left": 7, "right": 0, "step": -1, "width": 8}],
            "unpacked": []
          }
        }
      ]
    }
  ],
  "registers": [
    {
      "component": "uart",
      "canonical_name": "CTRL",
      "address_offset": 0,
      "absolute_address": 1073745920,
      "size_bits": 32,
      "fields": [{"canonical_name": "ENABLE", "bit_offset": 0, "bit_width": 1}]
    }
  ]
}
```

This example is abbreviated. Validate consumers against the generated schema rather than copying
the illustration.

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
after deciding whether source drift is intentional.

## Unknown and tainted facts

Contract selection does not manufacture missing data. When no known candidate establishes a
property, it stays unknown in the selected model. Unsupported or tainted observations remain in
the run and can make a comparison inconclusive. A generated contract should not be used as proof
that every input was fully understood; review parser diagnostics alongside it.

## Compatibility

Contract documents carry `schema_version`. OpenCollate 0.x can make breaking schema changes in a
minor release, documented in the changelog. A reader rejects unsupported schema versions. Use the
schema generated by the same installed build and do not infer compatibility from the package
version alone.

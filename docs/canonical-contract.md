# Canonical design contract

The design contract is OpenCollate's reconciled, provenance-preserving view of the design. It is
an audit artifact, not a new source of design intent.

Build one with:

```console
opencollate contract build [CONFIG] --output contract.oc.json
```

## Identity

A canonical object has a kind and stable identifier. Components can represent RTL modules,
Liberty cells, LEF macros, or package-facing entities. Ports belong to a component. Package
mappings form edges between die pads, signals, and package balls.

Original names remain in each observation. Aliases change grouping, not source evidence.

## Evidence

A canonical property contains observations from one or more views. Each observation records:

- Source view, such as `rtl.default` or `liberty.tt`.
- Value in normalized machine-readable form.
- Fact state.
- Path and one-based source span where available.
- Original spelling where normalization changed it.
- Parser notes relevant to confidence.

Conceptually:

```json
{
  "schema_version": 1,
  "components": [
    {
      "id": "uart",
      "ports": [
        {
          "id": "uart/irq_o",
          "direction": {
            "observations": [
              {
                "view": "rtl.default",
                "state": "known",
                "value": "output",
                "location": {"path": "rtl/uart.sv", "line": 8, "column": 5}
              },
              {
                "view": "liberty.tt",
                "state": "known",
                "value": "input",
                "location": {"path": "lib/uart.lib", "line": 33, "column": 5}
              }
            ]
          }
        }
      ]
    }
  ]
}
```

This is illustrative; use `opencollate schema contract` for the exact schema shipped with the
installed version.

## Shapes

Bus shape is not just width. The contract retains packed and unpacked dimensions, declared
left/right bounds, ordering, and explicit per-bit indices where supplied. This distinguishes:

- `[7:0]` from `[0:7]`.
- An explicit scalar from an unknown or failed vector parse.
- A contiguous bus from a CSV list with gaps or duplicate bits.
- Packed from unpacked dimensions.

## Baseline

`[contract].baseline` identifies the source of intended inventory or values where policy requires
an authority. A baseline does not erase conflicting evidence; diagnostics still show every view.

A frozen contract can be used as a reviewed baseline when the configuration points to it. Treat
that file like any other design source: review changes and version it with the design.

## Compatibility

Contract documents carry `schema_version`. OpenCollate 0.x may make breaking schema changes in a
minor release, documented in the changelog. Consumers should validate against the schema returned
by the same installed version:

```console
opencollate schema contract --output contract.schema.json
```

Do not infer compatibility from the OpenCollate package version alone.

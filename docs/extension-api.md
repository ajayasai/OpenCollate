# Extension API

OpenCollate can load independently distributed parsers and semantic checkers without patching the
core package. The extension surface is deliberately small, versioned, deterministic, and
fail-closed.

## Trust boundary

Python entry points execute code from installed packages in the OpenCollate process. Install only
plugins you trust. Set `OPENCOLLATE_DISABLE_PLUGINS=1` to disable package entry-point discovery.
Programmatically registered plugins remain available to the embedding process that registered them.

A parser-plugin exception becomes a fatal `OC9001` observation and taints the entire affected view.
A checker-plugin discovery or execution failure becomes a fatal `OC9002` diagnostic. A plugin
therefore cannot crash silently and turn incomplete analysis into a pass.

Run the following command to audit the exact extension providers and versions used by a build:

```console
opencollate capabilities --json
```

## Parser plugins

A parser implements the public `ViewParser` protocol and returns a `ViewObservation`.

```python
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from opencollate.model import ViewId, ViewObservation
from opencollate.parsers import ParserPluginSpec
from opencollate.parsers.base import Pathish, coerce_view


class OasisParser:
    format_name = "oasis"

    def parse(
        self,
        paths: Sequence[Pathish],
        *,
        view_id: ViewId | str | None = None,
        **options: Any,
    ) -> ViewObservation:
        view = coerce_view(view_id, kind=self.format_name)
        # Parse conservatively. Unsupported facts must remain unknown,
        # unsupported, or tainted rather than being omitted as clean.
        return ViewObservation(
            view=view,
            attributes={"source_files": [str(Path(path)) for path in paths]},
        )


plugin = ParserPluginSpec(
    parser=OasisParser(),
    name="oasis-structural",
    aliases=("oas",),
    extensions=(".oas", ".oasis"),
    parallel_safe=True,
)
```

Publish the object through a package entry point:

```toml
[project.entry-points."opencollate.parsers"]
oasis = "my_opencollate_plugin:plugin"
```

The entry point may expose a `ParserPluginSpec`, a parser instance, a parser class, or a zero-argument
factory. A spec is preferred because it declares aliases, filename extensions, and whether distinct
parser calls can safely overlap. Plugins default to `parallel_safe=False`; set it to true only when
the parser has no shared mutable state and its dependencies explicitly support concurrent calls.
Built-in parsers declare themselves safe. `--jobs N` never overlaps an unsafe plugin with any other
parser work.

The core rejects the complete parser registration when its canonical format, any alias, or any
extension conflicts with an existing owner. In particular, an external package cannot shadow a
built-in parser. Registration order cannot change the winner.

A configured plugin source uses the plugin format as the source table name:

```toml
[sources.oasis.final]
files = ["layout/chip.oas"]
strict_names = true
```

Unknown source-table keys are passed as parser keyword options. The generic `include_dirs`,
`defines`, `profile`, and `columns` fields are also forwarded when present.

For embedding or tests, registration can be performed without packaging:

```python
from opencollate.parsers import register_parser_plugin, unregister_parser_plugin

register_parser_plugin(plugin)
try:
    ...
finally:
    unregister_parser_plugin("oasis-structural")
```

## Semantic checker plugins

A checker receives immutable canonical and source context and returns `Diagnostic` objects.

```python
from opencollate.diagnostics import Diagnostic
from opencollate.plugins import CheckerContext, CheckerPluginSpec


def require_owned_interface(context: CheckerContext) -> tuple[Diagnostic, ...]:
    if any(
        interface.native_name == "debug"
        for view in context.observations
        for interface in view.interfaces
    ):
        return ()
    return (
        Diagnostic.from_rule(
            "OC1105",
            "The organization-specific debug interface policy was not applicable.",
            metadata={"checker": "company-interface-policy"},
        ),
    )


checker_plugin = CheckerPluginSpec(
    checker=require_owned_interface,
    name="company-interface-policy",
)
```

Publish it through:

```toml
[project.entry-points."opencollate.checkers"]
company_interface_policy = "my_opencollate_plugin:checker_plugin"
```

`CheckerContext` contains:

- the validated `ProjectConfig`;
- the deterministically ordered `ViewObservation` tuple;
- the reconciled `CanonicalDesign`;
- the optional frozen contract loaded for this run;
- the generated canonical contract;
- the effective analysis date used for waiver expiry.

Checker diagnostics pass through the same severity-override, waiver, deterministic sorting, JSON,
Markdown, text, SARIF, baseline-review, and exit-code machinery as built-in findings.

## Compatibility policy

The current extension API is `1`. Plugins must declare the exact supported version through
`ParserPluginSpec.api_version` or `CheckerPluginSpec.api_version`. OpenCollate rejects incompatible
versions rather than guessing.

Within a major OpenCollate release:

- the version-1 context fields and parser protocol will not be removed;
- built-in format ownership remains protected;
- plugin order and parallel result order are deterministic;
- plugins remain serial unless they explicitly declare `parallel_safe=True`;
- plugin exceptions remain fatal and retain provider/version metadata;
- capability JSON remains the authoritative runtime inventory.

A future incompatible API will use a new version and can coexist through an explicit adapter rather
than silently changing version 1.

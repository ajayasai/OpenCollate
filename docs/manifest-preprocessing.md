# Manifest-bound native RTL preprocessing

The development checkout supports an explicit preprocessing mode for sequential
checks, receipt replay, certificate creation and solver-free certificate checking.
It removes the previous blanket rejection of compiler directives without using
an ambient filesystem search or losing header/build-configuration binding.
This does not expand the supported clock, behavioral, or four-state semantics.

## Run the multi-file example

```console
python -m pip install -e ".[formal,certificates]"
opencollate sequential check examples/preprocessed/request.json --output receipt.json
opencollate sequential certify examples/preprocessed/request.json --output certificate.json
opencollate sequential verify-certificate examples/preprocessed/request.json certificate.json
```

The example contains a parameterized register module, a top-level instance and
an include-guarded function-like macro. The request declares every header and
predefined macro explicitly:

```json
"files": ["rtl/register.sv", "rtl/top.sv"],
"preprocess": {
  "headers": ["include/ops.svh"],
  "include_dirs": ["include"],
  "defines": {"DATA_WIDTH": "8"}
}
```

Paths are relative to the request directory. Defines are literal replacement
strings, not shell arguments or environment substitutions. An empty replacement
is allowed; use `"1"` for a Boolean-style feature flag. Add `"INVERT_DATA": "1"`
to the example's defines to expose an actual counterexample and reject certificate
creation. A proof is for the specified build configuration, not every possible
macro configuration. There is no claim that the example is a production design.

## Compilation and dependency semantics

Opt-in preprocessing uses **one ordered compilation unit**: build definitions,
then the root files in request order. Native slang preprocessing expands ordinary
object/function macros, continuations, include guards and conditional compilation.
Root-file ordering therefore matters. No multiple-compilation-unit mode is
implied. Without a `preprocess` object, the previous directive-rejecting behavior
remains unchanged; existing projects do not silently acquire new build semantics.

Literal quoted includes are resolved against declared `headers`, relative to the
including file and to each declared include directory. Exactly one distinct
candidate must match. Ambiguous candidates are rejected instead of choosing a
search-order winner. Unlisted files on disk are **not candidates**, even if they
would shadow a declared header in a conventional compiler invocation.

All declared files are snapshotted and checked before native parsing. This includes
unused headers and literal includes in inactive branches: their dependencies must
still be declared and their lexical form supported. This conservative policy
may reject a build a general-purpose compiler would accept. Duplicate paths,
case-ambiguous names, duplicate inode aliases, nonregular files, parent traversal,
absolute names, outside-root symlinks and nonportable path spellings are rejected.
The bounded source reads are not an atomic filesystem transaction across files.

The loader uses slang's **lexer**, not text regexes, to distinguish directives
from comments and strings. It redirects literal include names to exact preloaded
in-memory buffers and disables native local-include searching. Macro-generated
includes, compiler directives in macro replacements, token pasting/stringification,
angle-bracket includes, escaped include names, user `line`/`pragma` directives,
encrypted source and keyword-mode changes are rejected. This is deliberately not
all SystemVerilog preprocessing. `timescale`, `default_nettype`, `resetall`,
`define`/`undef`/`undefineall`, and ordinary conditionals are supported. `__LINE__`
is supported; `__FILE__` is rejected to avoid platform-dependent path semantics.

Each native file buffer is audited against the preloaded rewritten bytes. Accepted
includes never need a second disk read. Header changes after snapshotting cannot
change the bytes compiled in that run. Source locations retain logical paths,
line numbers and expansion-site columns, without temporary directory names.
Slang's native macro expansion and AST interpretation remain trusted components.

## Proof binding and publication safety

Original root-file and header bytes, including inactive and unused declarations,
are SHA-256 bound into the source inventory. Normalized defines, ordered root
files, headers and include directories are part of request binding. Changing a
header or build define invalidates old evidence even when the selected behavior
happens to remain the same. Moving the same source tree to another directory does
not alter its logical binding. Digests are not signatures or supplier authentication.

Receipt replay reconstructs and rechecks the build. Certificate receivers rebuild
the full model and CNF and check the RUP evidence; they need the base package/native
frontend but no SAT/SMT solver. Updating a saved checksum/binding does not convert
an old proof into a valid proof for changed RTL. No unsupported inactive build
configuration is claimed to have been formally checked.

Every declared header is protected from report/certificate output aliasing,
including hard links and symlinks. Existing watchdog fresh-status-file protections
continue to apply. Check exit status; an old output file is not evidence of a new
successful run. Receipt implementation bindings change when the implementation
changes; regenerate receipts rather than relabeling them.

## Limits, tests and trust boundary

Preprocessing accepts at most 32 root files and 128 headers, 32 include directories,
and 128 predefined macros. Individual files are limited to 1 MiB, all original
source bytes to 4 MiB, and lexical work to 200,000 tokens. Native include depth is
limited to 32. Existing IR, model, solver, and proof-checker bounds still apply.
Macro expansion/native allocation is not OS-isolated or bounded by the lexical
count. Use `opencollate guard` for an external process deadline. This loader is
not a security sandbox against a compromised frontend, plugin, or concurrent
adversarial filesystem changes.

```console
pytest -q tests/test_sequential_sources.py
python -m benchmarks.preprocessing --repeat 3 --json-output preprocessing.json
```

Tests cover actual source proofs and certificates, header/build mutations, stale
and rehashed evidence, missing/ambiguous/includes, guarded cycles, UTF-8/CRLF,
concurrent compilations, exact byte snapshots, relocation and input/output aliases.
An independent Icarus oracle checks all 256 byte values in both copy/invert builds
(512 simulator samples). The simulator test explicitly skips when Icarus is absent.
The dedicated workflow installs it and checks a separate solver-free wheel receiver.
These finite regressions are not industrial qualification or evidence of superiority
over licensed commercial tools.

Primary implementation references: slang v11.0 SourceManager.cpp, especially
assignBuffer/openCached/readHeader, and the native Lexer/SyntaxTree Python APIs.
See https://sv-lang.com/sourcemanagement.html and https://sv-lang.com/user-manual.html.

# Explicit multi-file build configuration

Run `opencollate sequential check examples/preprocessed/request.json` from the
repository root. The declared copy build proves a one-cycle byte transfer.
Generate/check its source-bound certificate using `sequential certify` and
`sequential verify-certificate` with the same request.

Adding `"INVERT_DATA": "1"` to `preprocess.defines` selects the inverted build.
The copy property then has a counterexample and cannot be certified. Header
contents and all build definitions are bound into the verification evidence.
See `docs/manifest-preprocessing.md` for exact dependency and compilation semantics.

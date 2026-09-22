# Functional-verification run-identity v1 contract fixtures (issue #2097)

Fixture bytes for the ratified Candidate A v1 contract pilot. Nothing in
this tree is ever handed to a simulator: the contract tests hash these
files, mutate them, relocate them, and execute exactly one tiny synthetic
Python check (`design/tb/tb_counter.py`, which imports no cocotb) in
disposable copies of the tree.

## Layout

| Path | Role in the manifest |
| --- | --- |
| `design/rtl/counter.sv` | automatic `rtl_source` (resolved RTL build order) |
| `design/rtl/include/counter_defs.svh` | caller-declared `rtl_include` — include *bytes* come only from the explicit inventory; include directories are never recursively hashed |
| `design/tb/tb_counter.py` | automatic `testbench_entry` (the resolved entry testbench); doubles as the executable synthetic check |
| `design/tb/expected_counts.py` | caller-declared `testbench_helper` (mandatory-helper control: omitting it fails the command) |
| `design/tb/fixtures/vectors.json` | caller-declared `fixture` |
| `design/sdf/tb_counter_typ.sdf` | automatic `sdf_original` (supplied SDF) |
| `generated/sdf_wrapper.sv` | automatic `generated_source` — root `generated` is reserved for KLT-created inputs; this stand-in annotates via a *relative* path so the golden digest stays machine-independent |
| `manifests/manifest.v1.json` | hand-authorable conforming `input_manifest`, stored in exact canonical bytes |
| `manifests/manifest.v1.reordered.json` | the same object, reversed key order / pretty-printed / `\u`-escaped — must hash identically |
| `manifests/golden_digests.json` | the literal published golden byte/digest vectors |

## Golden vectors (literal)

    canonical_manifest_sha256 = c8adf49a845034dda1f9982a33a844d630e5c67ea4083f8cb5a41395aae985cb
    input_sha256              = a408dd6ff75a61add23e40c1f793cc6c9c6eb277a38cf1e7ef00e82c7c60299a

Both vectors hash the same manifest object (see the reordered vector);
`input_sha256` = sha256(`klt-functional-verification-input-v1` + `0x00` +
canonical manifest bytes). Regenerating them by hand is a contract change
and needs a `schema_version` bump; see
[`docs/design/functional-verification-run-identity-v1.md`](../../design/functional-verification-run-identity-v1.md).

## One command

    uv run --frozen --extra dev python tests/helpers/identity_contract_v1.py
    uv run --frozen --extra dev pytest tests/test_functional_verification_identity_contract.py -q

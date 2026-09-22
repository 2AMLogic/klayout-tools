# Functional-verification run identity v1: the declared-input manifest contract

**Status: contract ratified for the v1 pilot (Candidate A, operator
ratification 2026-09-22 in
[#2097](https://github.com/2AMLogic/klayout-tools/issues/2097)). The
*contract, reference fixtures and tests* specified here are implemented
this increment. The *producer* (the `klt functional-verification` runner
emitting `run_identity`) and the *consumer* (signoff/citation integration)
are separately scoped follow-ups — see
[Follow-up drafts](#follow-up-drafts-producer-and-consumer). Nothing in
`klt`'s production output changes until those land; every example below is
**proposed / not yet emitted**.**

- Issue: [#2097](https://github.com/2AMLogic/klayout-tools/issues/2097)
  (first-increment proposal, candidate table, acceptance checklist, and the
  2026-09-22 operator ratification comment selecting Candidate A).
- Reference implementation (contract only, stdlib-only, outside the
  production runner): `tests/helpers/identity_contract_v1.py`.
- Fixtures + literal golden vectors:
  `tests/fixtures/functional_verification_identity_v1/`.
- Contract tests: `tests/test_functional_verification_identity_contract.py`.

One command to re-verify the contract vectors:

```bash
uv run --frozen --extra dev python tests/helpers/identity_contract_v1.py
uv run --frozen --extra dev pytest tests/test_functional_verification_identity_contract.py -q
```

## Problem and boundary

Functional-verification reports are admissible digital evidence
(#1959, merged via #1967), but they bind no identity of their inputs: an
RTL edit, a testbench-helper change, or a swapped fixture can accompany an
unchanged-looking PASS. The TorchSynth review (gf180-torchsynth#98) showed
a required fixture changing while evidence stayed green. Hashing the entry
testbench alone cannot establish closure: the runner resolves RTL sources
in build order, generates SDF wrapper/shim/split files, exposes Python
import paths, and inherits an ambient environment.

The ratified boundary for v1 is deliberately narrow. This contract:

- inventories **declared** inputs plus the resolution outputs the runner
  already computes (ordered RTL sources, the resolved entry testbench,
  supplied SDF, generated build inputs);
- hashes a versioned resolved manifest with an explicit canonical profile;
- labels closure **`partial`, structurally, always** in v1.

It does **not**:

- collect Python/native import closures or trace filesystem access
  (no production dependency collector);
- change signoff qualification (`signoff.py` is untouched this
  increment; #2094/#2095/#2096's fixes remain independent);
- authenticate the producer, prove the recorded command executed, or
  qualify hardware behavior. Hashes bind recorded bytes and
  configuration — no more.

## Candidate designs and selection

| Candidate | Mechanism | Benefit | Limit/cost |
| --- | --- | --- | --- |
| **A. Declared-input manifest (selected)** | Automatically inventory ordered RTL sources, the resolved entry testbench, supplied SDF and generated build inputs; callers explicitly declare additional helper/header/fixture files and selected environment variables. Hash a versioned resolved manifest. Always label v1 closure `partial`. | Bounded, deterministic, testable without a simulator; supports the existing runner; clearly separates known identity from unknown closure. | A missing Python import, external data read, environment value or tool component can affect a run without changing the declared digest. Visible forever; never complete-source coverage. |
| B. Restricted staged-run profile | Copy declared files to a disposable tree, explicit Python/tool environment, fixed cwd, reject unsupported external inputs, one supported simulator profile. | Tests an enforceable, narrower execution boundary; can catch omitted mandatory fixtures. | A temp dir is not a sandbox: absolute reads, network, native extensions, subprocesses need an actual enforcement design before the word "hermetic" is earned. Larger, separately scoped. |
| C. Observed dependency inventory | Capture supported simulator dependency output and Python import/read observations for one execution; hash the observed set. | Identifies some omissions in A; helps declaration maintenance. | One execution misses unexecuted branches, native/subprocess reads, dynamic paths, external services. Portable tracing is substantial work. Still partial, never proof. |

The operator ratified **A** for the v1 pilot (2026-09-22). B and C become
separate scoped issues only if the pilot demonstrates the `partial` label
is insufficient for consumers.

## Request surface: `options.evidence` (proposed, opt-in)

A future opt-in `options.evidence` object on the functional-verification
request:

```jsonc
{
  "engine": "icarus",
  "sources": ["rtl/counter.sv"],
  "hdl_toplevel": "counter",
  "testbench": {"module": "tb_counter"},
  "options": {
    "evidence": {
      "roots": {"design": ".", "tb_lib": "../tb-lib"},
      "files": [
        {"root": "design", "path": "rtl/include/counter_defs.svh", "role": "rtl_include"},
        {"root": "design", "path": "tb/expected_counts.py", "role": "testbench_helper"},
        {"root": "design", "path": "tb/fixtures/vectors.json", "role": "fixture"},
        {"root": "tb_lib", "path": "bench_config.json", "role": "configuration"}
      ],
      "environment": ["KLT_SEED", "PDK_ROOT"]
    }
  }
}
```

- `roots` — named root locators; default `{"design": "."}` relative to the
  request directory. Additional roots support an external
  `testbench.search_path` or library tree. Nested/overlapping roots are
  rejected (a file under two roots would have two competing logical IDs).
- `files` — explicit inventory entries; roles include `rtl_include`,
  `testbench_helper`, `fixture`, `configuration`. One file may carry
  several roles (sorted, deduplicated). Include/header **bytes enter the
  manifest only through this inventory** — include directories are never
  recursively hashed, but the include search-directory *order* is recorded
  in the recipe.
- `environment` — explicit variable names whose set/unset state and UTF-8
  value digest are recorded. The whole environment is never silently
  copied. The declared set lives in the manifest's `recipe` section
  (see [Ambiguities resolved](#ambiguities-resolved)).

Automatically added (from the runner's existing resolution points
`_resolve_sources`/`_resolve_testbench`/SDF wrapper and shim generation):
resolved RTL source files in original build order, the actual entry
testbench file, original SDF when used, and generated
wrapper/shim/split-SDF files actually consumed by the build. Python
imports are never guessed. Root name `generated` is reserved for
KLT-created inputs; caller declarations may not use it.

**Logical paths** are `<root-name>:<relative POSIX path>`. v1 opt-in
collection **rejects**: ambiguous overlapping roots, traversal (`..`),
absolute paths, unresolved files, directories masquerading as files,
duplicate logical IDs with conflicting contents, and symlinks — never a
silent omission. Files outside declared roots require another named root.
Inventory records are sorted by logical ID; ordered execution arrays
(sources, include roots, build arguments) retain order and duplicates.

**Compatibility**: the opt-in must not alter an existing request that does
not ask for identity collection. No `options.evidence` → no `run_identity`
in the report, byte-for-byte today's behavior.

## The `run_identity` block (proposed shape)

A future additive block on the functional-verification report envelope:

```jsonc
{
  "schema_version": 1,
  "input_manifest": { "/* files, recipe, toolchain, closure */": "..." },
  "input_sha256": "a408dd6f…",           // 64 hex, see hashing below
  "input_locators": {                     // NON-identity, never hashed
    "roots": {"design": "/abs/request/dir"},
    "build_dir": "/abs/build",
    "created_utc": "2026-09-22T07:05:54Z"
  }
}
```

The `input_manifest` has **exactly** these identity-bearing sections:

1. **`files`** — sorted array of
   `{logical_id, roles, byte_length, sha256}` (raw-byte SHA-256) for every
   recorded input, declared and automatic.
2. **`recipe`** — requested/effective HDL tops, testbench module and
   logical entry file, selected testcase(s), ordered user/generated source
   IDs, ordered include search roots, typed parameters/defines, requested
   and effective build arguments, timescale, requested/effective seed,
   coverage/trace switches, original/effective SDF file IDs plus
   corner/annotation configuration, and the declared environment records
   (sorted by name; set entries carry a `value_sha256`, unset entries
   `null`).
3. **`toolchain`** — engine name/version, cocotb version, Python
   implementation/version, and KLT's `build_identity.build_version()` +
   `identity()` fields (`git_commit`/`git_tag`/`dirty`/`is_release`,
   per #2090/#2102 — never package `__version__`). Unknown values are
   `null`. `dirty: true` or unknown identity is a *disclosure*, not an
   exact source-byte identity.
4. **`closure`** — `{"mode": "declared", "status": "partial",
   "reasons": [...]}`. The baseline reasons every v1 declared manifest
   must name: `python_native_imports_uncollected`,
   `ambient_environment_not_isolated`, `tool_binary_bytes_not_bound`.
   Added as applicable: `effective_seed_unobserved`,
   `klt_tool_identity_unknown`, `klt_build_dirty`.
   **A v1 declared manifest cannot claim `complete`; the reference checker
   rejects any such claim outright.**

## Hash semantics and the versioned canonical profile

File digests are raw-byte SHA-256. The run digest is
domain-separated from every other hash role in the repo (layout/netlist/
DEF provenance per #2027/#2058, report pins, deck hashes):

```
input_sha256 = SHA-256( "klt-functional-verification-input-v1" || 0x00 || canonical_manifest_bytes )
```

The canonical profile is **explicitly versioned** by the prefix string
(a future v2 changes prefix and `schema_version` together): UTF-8 JSON,
sorted object keys, no insignificant whitespace (`separators=(",",":")`),
no ASCII escaping (`ensure_ascii=False`), finite JSON numbers only;
arrays retain order; strings receive no Unicode normalization; and
integer/float JSON forms remain distinct (`1` vs `1.0` — tested).

Duplicate JSON keys, NaN/Infinity literals and malformed/non-finite values
are rejected **before hashing**. (`json.loads` turns a literal `1e400`
into an infinite float without tripping `parse_constant`, so the validator
also walks for non-finite floats.)

Literal golden vectors are published in
`tests/fixtures/functional_verification_identity_v1/manifests/`:

    canonical_manifest_sha256 = c8adf49a845034dda1f9982a33a844d630e5c67ea4083f8cb5a41395aae985cb
    input_sha256              = a408dd6ff75a61add23e40c1f793cc6c9c6eb277a38cf1e7ef00e82c7c60299a

including an equivalent-object-key-order vector (pretty-printed,
`\u`-escaped, reversed key insertion) that must hash identically. The
canonical fixture file is stored in exact canonical bytes, so the golden
BYTE vector is the file itself.

## Relocation, rename, and embedded-path rules

- **Physical root locations, timestamps, output directories and diagnostic
  paths live in the non-identity `input_locators` block.** Moving
  identical ordinary inputs together with the same logical structure keeps
  identity stable (tested).
- **Renaming a logical path** (`rtl/counter.sv` → `rtl/counter_renamed.sv`,
  same bytes) changes identity (tested).
- **Source/include/build-argument order is identity**: ordered arrays are
  never sorted to stabilize a hash; swapping two entries changes the
  digest, and duplicates are retained (tested).
- **A path embedded in literal build arguments or generated SDF shim bytes
  is still real input**: relocation may legitimately change identity
  there. The contract rewrites no strings to pretend equivalence; both
  cases are tested and disclosed (issue #2097's own wording).

## Pre-/post-execution reads

Input bytes are read and hashed before execution and re-read afterward;
an observed change refuses a bound result. This catches observed mutation
only — **not** change-and-restore races and **not** unrecorded inputs (the
undeclared-dynamic-input control proves the second point by construction).
Stronger execution-byte guarantees belong to Candidate B, if ever ratified.

## Output integrity is separate

An additive artifact inventory binds the exact `results.xml` bytes and any
trace/coverage/build-derived files cited, with role, logical path under
the artifact root, byte size and digest. Missing or unreadable required
artifacts are nonqualification, **never an empty digest**. Output mutation
fails the integrity check without changing the recorded input digest
(tested).

**No report self-hash.** A report's own final byte hash never lives inside
that report; the report-byte pin is held externally by a caller's
citation/detached receipt after serialization, and command-backed evidence
hashes exact stdout bytes before parsing. Pretty-print changes may alter
the report-byte pin while leaving input identity unchanged (tested).

## Consumer policy (proposed)

An opt-in citation extension with a caller-owned `expected_manifest`, an
explicit current input-root mapping, `allow_partial_inputs: true|false`
(default **false**), an optional externally held report-byte pin, an
artifact-root mapping and required artifact roles. A verifier reports —
separately, never compressed into an "all dependencies fresh" claim:

- recorded identity matches the intended manifest;
- recorded declared files match currently supplied bytes (re-hashed from
  the caller's roots — a digest copied from the report is not evidence);
- required output/report bytes match their pins;
- input closure remains partial or unknown.

If partial coverage was not explicitly accepted, the policy refuses
qualification. Missing legacy identity (a report with no `run_identity`)
cannot satisfy the new opt-in policy; with no new policy, legacy evidence
keeps existing behavior and #2096's nonempty-execution/count checks and
#2094's metric checks still apply independently.

The reference checker encodes this policy in `verify_citation()` with
seven independent checks (shape, metric domain, nonzero coverage,
intended-manifest match, current declared bytes, output integrity,
partial-coverage policy); the decision-table test proves each check can
fail alone and that no passing check overrides a failing one.

The digest stays **out of `provenance.input`** in v1: #2027/#2058's roles
are layout/netlist/DEF, and a functional-verification run manifest is
none of those. It must never be compared with a DRC GDS hash or between
two unrelated verification runs merely because they share a command kind
(domain separation + the verifier's signature make this structural, and
both are tested).

## Ambiguities resolved

The ratified body said the manifest has "exactly these identity-bearing
sections: files, recipe, toolchain, closure" while also ratifying an
`environment` declaration surface (set/unset state + value digest). The
resolution implemented here: declared environment records live **inside
`recipe`** as a sorted array of `{name, state, value_sha256}` entries —
`recipe` is the run-configuration section, the four top-level sections
stay exactly four, and only explicitly named variables are ever recorded.
Flagged in the PR for reviewer attention.

Second, minor: the body allows unknown toolchain values to remain
"null/unknown"; the checker accepts only `null` (the literal string
`"unknown"` is rejected as fabrication rather than disclosure). The
producer follow-up maps unknown → `null` and names the gap in
`closure.reasons`.

## Follow-up drafts (producer and consumer)

**Producer (separately scoped; not authorized by this increment).** Wire
manifest construction into `functional_verification.py` behind opt-in
`options.evidence`: build the inventory after `_resolve_sources` /
`_resolve_testbench` / SDF wrapper-shim generation, snapshot before
`_run_build`, finalize the effective seed after parsing `results.xml`,
re-read declared inputs after execution (refuse on observed mutation),
and attach `run_identity` + artifact inventory at envelope assembly. Reuse
`build_identity.py` identity fields and `_provenance.py`'s streamed
`sha256_file`; do not use the generic combined-file hash. The
canonicalization/hashing/validation core already exists as the reference
checker and moves to `src/` with tests at that point.

**Consumer (separately scoped).** Extend the functional-verification
citation path in `signoff.py` with the caller-policy objects above
(expected manifest, root mapping, `allow_partial_inputs`, report pin,
artifact mapping/roles), grade identity separately from status/metrics,
and surface the four verdicts plus toolchain disclosures as diagnostics —
preserving #2094/#2096's independent checks and #2027/#2058's
layout/netlist/DEF provenance roles. `cli/functional_verification_cmd.py`
owns serializer/output questions for any optional detached receipt;
library-returned dictionaries do not have final serialized report bytes.

Neither follow-up may: implement a production dependency collector,
change signoff policy, or alter the always-`partial` closure label.

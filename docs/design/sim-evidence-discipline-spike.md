# Spike: `klt sim` evidence discipline

**Status:** decision record. This is a scoped design pass for one item of
[#347](https://github.com/2AMLogic/klayout-tools/issues/347) ("both public
canary repos hand-roll corner harnesses"), not a new-capability spike in the
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive" sense
— `klt sim` already exists and ships the corner runner
([spice-corner-runner-spike.md](spice-corner-runner-spike.md)); this
document only decides how *evidence discipline* — record-keeping around
already-shipped `klt sim` runs — should be layered on top of it. It follows
the same survey → decision → contract-or-convention shape as its
predecessor spikes so the decision is auditable, not because a new engine
or capability is being proposed.

## Scope

#347 originally listed four things both `gf180-bandgap`'s
`sim/harness/`+`sim/run_corners.py`/`run_suite.py` and `sky130-bandgap`'s
`sim/bin/corner-run.py` hand-rolled instead of using `klt sim`. Curation
verified each against the current contract and corrected the framing
(2AMLogic/klayout-tools#347, Curator comment, 2026-08):

- **Item 1 (spec-limit evaluation per measurement) — already shipped.**
  `klt sim` has evaluated per-measurement `limits` since its first commit
  (#96): `_evaluate_limits()` (`src/klayout_tools/sim.py:1613`) and
  `_rollup_measurements()` (`src/klayout_tools/sim.py:1649`) produce
  per-measurement `status`/`margin` and a `worst_case` binding corner,
  documented at [docs/cli/sim.md](../cli/sim.md) ("JSON schema" →
  `measurements[]`). No gap; disregard the original claim.
- **Item 2 (custom process-corner axes) — tracked by
  [#351](https://github.com/2AMLogic/klayout-tools/issues/351).** A
  `corners.process` bundle schema for gf180mcu's per-device-family `.lib`
  sections is #351's concrete, scoped fix. No action here.
- **Item 3 (evidence discipline) — this document.** Append-only records
  with supersession, netlist snapshots per record, PDK-pin (hash)
  enforcement, subset-reason requirements, spread checks. Confirmed zero
  existing infrastructure via repo-wide grep for `supersession`, `spread
  check`, `subset.reason`, `append.only` — no hits in `src/` or `docs/`.
- **Item 4 (testbench manifest reuse pre/post-layout) — already shipped.**
  `netlist_source: "schematic"|"extracted"`
  ([docs/cli/sim.md](../cli/sim.md) → "Post-layout verification") already
  gives "the same bench re-runs unedited against a post-layout extracted
  netlist" — the sim request document (netlist + measurements + limits +
  corners) *is* the testbench manifest; there is no separate
  `tb.json`/`experiment.json` convention needed. No gap; disregard the
  original claim.

This document resolves item 3 only, per #347's own framing ("a design pass
should decide which land in `klt sim` vs which become a documented
block-repo pattern") — items 1, 2, and 4 no longer need that pass.

## What already exists to build on

`klt sim`'s closest existing primitive is the shared `provenance` block
(`src/klayout_tools/_provenance.py`, added by #335): `klt_version`,
`klayout_version`, `pdk` (`{name, source, version}`), `deck`
(`{name, content_hash}` for the resolved model library), and `input`
(`{content_hash}`, unused by `sim` — it covers its own inputs via
`environment`). `sim`'s own `environment` block additionally carries
`engine`/`engine_version`, `models_lib`/`models_lib_sha256`,
`netlist_sha256`, and (when declared) `netlist_source`/`monte_carlo`. Per
`_provenance.py`'s own module docstring, this is **per-invocation
reproducibility metadata, not a record store**: it lets a single stored
result be checked against the inputs that produced it, but nothing persists
across runs, nothing chains one run to the next, and nothing fails a run
because of what a *previous* run found. That gap is exactly item 3.

Two more existing facts turn out to matter for the decision below:

- **Per-corner measurement values are already fully present**, not just the
  per-measurement rollup. `corners[].measurements[]` in the response
  carries `name`/`value`/`unit`/`status`/`margin` for *every* corner, not
  only the worst case (`docs/cli/sim.md` → "`corners[]` entries"). Anything
  computable from the full per-corner matrix (e.g. a value's spread across
  corners) is already computable by a caller from the existing JSON, with
  no new field required.
- **The generated per-corner deck is already written to disk, but not
  referenced.** `_run_corner()` writes the exact text handed to `ngspice -b`
  — `.lib`/`.temp`/`alter` lines and all — to `<corner_dir>/corner.cir`
  whenever `options.keep_artifacts` is true (`src/klayout_tools/sim.py:1295`,
  `_write_corner_deck()` at `sim.py:1460`). But the documented `artifacts`
  object only carries `{"log", "raw", "waveform"}`
  (`docs/cli/sim.md` → "`corners[]` entries"; `sim.py:1413`) — the file
  exists on disk under `<outdir>/<corner-slug>/corner.cir` but is not a
  contract field. This is the one genuine data-completeness gap found
  below.

## Decision test

For each of item 3's five sub-items, ask which of two things is missing:

1. **A policy/verdict judgment specific to a block or repo** — "what counts
   as full corner coverage for this block," "what PDK hash do we expect
   pinned," "how much spread is suspiciously little," "how long do we keep
   superseded records and where." `klt sim` has already drawn this line
   once, for measurement limits: a measurement with no declared `limits` is
   *reported but never fails* (`docs/cli/sim.md` → `measurements[]`) —
   verdict policy is caller-declared, not tool-inferred, by design. A
   sub-item that is this kind of question follows the same precedent:
   **documented convention**, built from data the contract already emits.
2. **Data that the run already produces but the contract does not expose.**
   That is a contract-completeness gap, not a policy question, and the fix
   is a small, additive JSON field — proposed as a follow-up issue per the
   acceptance criteria, not implemented in this document.

## Disposition

| # | Sub-item | Kind | Disposition |
| - | -------- | ---- | ----------- |
| A | Append-only records with supersession chains | policy (storage/retention) | **Documented convention** |
| B | Netlist snapshot per record | data-completeness gap | **`klt sim` contract addition** — follow-up issue filed (see below) |
| C | PDK-pin (hash) enforcement | policy (expected-value + fail behavior) | **Documented convention** |
| D | Subset-reason requirements | policy (what counts as "full" coverage) | **Documented convention** |
| E | Spread checks | policy (verdict from already-present per-corner data) | **Documented convention** |

Four of five turn out to be policy questions answerable entirely from data
`klt sim` already emits — the same shape as the measurement-limits
precedent above, where `klt sim` reports and the caller judges. Only B is a
genuine gap: `corner.cir` is already written to disk when
`options.keep_artifacts` is set; it is a completeness bug in what the
contract exposes, not a policy decision, so it does not belong in a
"convention" — a convention cannot invent a file the tool never told the
caller existed.

## B: netlist snapshot per record — contract addition (follow-up filed)

**Follow-up issue:**
[#356](https://github.com/2AMLogic/klayout-tools/issues/356). Proposed
schema, scoped to a single additive field so it needs no
`schema_version` bump (`docs/json-contract.md` → "a `schema_version` bump
is required only for a breaking change"):

```json
"artifacts": { "log": "...", "raw": null, "waveform": null, "deck": "/abs/path/corner.cir" }
```

- `artifacts.deck` — absolute path to the exact ngspice deck (`.lib` and/or
  `.temp` and `alter` lines and all) used for this corner, mirroring
  `log`/`raw`/`waveform`: `null` unless `options.keep_artifacts` is true.
  No new file is written — `_write_corner_deck()` already produces this
  file at `deck_path` (`sim.py:1295`); the change is exposing the
  already-written path in the response, the same way `log`/`raw` already
  do.
- This is what "netlist snapshot per record" actually needs: today a
  stored `klt sim` result pins the *original* netlist file by SHA-256
  (`environment.netlist_sha256`), but the SHA-256 of the source file is not
  the same artifact as the exact per-corner deck text ngspice consumed
  (process-corner `.lib` selection, `.temp`, and `alter` supply lines are
  synthesized per corner, not present in the source file at all). A record
  store that wants "the exact text that produced this result" for audit
  needs the synthesized deck, not just a hash of the unexpanded source.

## Documented conventions (A, C, D, E)

The convention below is a repo-level pattern layered **unmodified** on top
of `klt sim`'s existing JSON output — no field of the existing contract is
read or interpreted differently than documented. It is illustrative, not
normative: a block repo may implement the same guarantees differently, as
long as append-only-ness, supersession, PDK pinning, subset-reason, and
spread checks all hold.

### Storage shape: the evidence record wrapper

Each `klt sim` run's raw JSON is wrapped, unmodified, inside a small
per-run **evidence record** and committed to git:

```
evidence/sim/<block>/<corner-scope-slug>/<recorded_at>-<request_sha>.json
```

- `<block>` — the design under test (e.g. `bandgap`).
- `<corner-scope-slug>` — a stable name for *what* was run (e.g.
  `full-pvt`, `bjt-mismatch`), so unrelated evidence scopes don't share one
  supersession chain.
- `<recorded_at>` — UTC timestamp of when the record was written, e.g.
  `20260802T193000Z`. `klt sim`'s own JSON carries no run timestamp (a
  repo-wide grep of the schema confirms it: no `timestamp`/`generated_at`
  field exists anywhere in `docs/json-contract.md` or `docs/cli/sim.md`) —
  the wrapper, not `klt sim`, is the source of truth for *when* a record
  was captured. This is a deliberate design choice, not an oversight: `klt
  sim` is a stateless, single-invocation command by construction (see "What
  already exists to build on" above), and a wall-clock stamp is
  operator/CI metadata about the *recording* event, not the simulation
  itself — the same reasoning that already keeps corner-matrix policy
  (what's "full"?) and pass/fail thresholds for spread out of the tool.
- `<request_sha>` — short SHA-256 of the exact request JSON that was run
  (the file already passed to `klt sim <request.json>`), so two records for
  the same scope with different inputs never collide, and a diff of two
  request files explains why two evidence records for the same scope
  differ.

Record wrapper shape:

```json
{
  "schema": "evidence-record/1",
  "recorded_at": "2026-08-02T19:30:00Z",
  "block": "bandgap",
  "scope": "full-pvt",
  "request_path": "sim/bandgap/full-pvt.request.json",
  "request_sha256": "9f2c1a3...",
  "subset_reason": null,
  "pdk_pin": { "name": "sky130A", "expected_deck_content_hash": "sha256:71ad0b2..." },
  "supersedes": ["evidence/sim/bandgap/full-pvt/20260715T101500Z-71ac0b2.json"],
  "result": { "...": "the unmodified klt sim JSON response" }
}
```

`result` is the untouched `klt sim` output — the wrapper never edits,
subsets, or reinterprets it, so a stored record can always be re-validated
byte-for-byte against a fresh `klt sim` invocation using
`request_sha256`/`environment.netlist_sha256`/`provenance.deck.content_hash`.

### A — Append-only + supersession

- **Append-only**: never edit or `git rm` an existing record file. A new
  run always writes a new file — the `<recorded_at>-<request_sha>` name
  makes every record unique by construction, and git history is itself an
  append-only log of every record ever committed (deleting a committed
  file is still visible in history; the convention's rule is simply "don't
  do it").
- **Supersession**: the new record's `supersedes` array names the file(s)
  it obsoletes for the same `<block>/<scope>`. Nothing is deleted — a
  three-month-old passing record stays on disk and in git history even
  after being superseded, so a signoff claim from that date remains
  auditable. A small `evidence/sim/<block>/<scope>/HEAD` file (one line:
  the current record's filename) lets tooling find "the current record"
  in O(1) without walking every file's `supersedes` chain; the chain
  itself is only walked when auditing history.
- CI enforcement: a merge that adds a new record for an existing
  `<block>/<scope>` and omits `supersedes` (or a merge that modifies an
  existing record file's bytes) fails a pre-merge check —
  `git diff --name-status origin/main...HEAD -- evidence/sim/` rejects any
  `M` status under that path; only `A` (added) is allowed.

### C — PDK-pin (hash) enforcement

`provenance.pdk` and `provenance.deck.content_hash` already report exactly
what a pin check needs — `klt sim` deliberately never fails a run over a
PDK/deck mismatch it cannot itself judge (there is no "expected" hash in
the request contract, matching the measurement-limits precedent). The
convention makes reporting into enforcement one layer up:

```bash
# scripts/check-pdk-pin.sh <evidence-record.json>
expected="$(jq -r '.pdk_pin.expected_deck_content_hash' "$1")"
actual="$(jq -r '.result.provenance.deck.content_hash' "$1")"
if [ "$expected" != "$actual" ]; then
  echo "PDK pin mismatch: expected $expected, got $actual" >&2
  exit 1
fi
```

Run as a required CI step before an evidence record is trusted (i.e.
before it can be referenced by a signoff aggregation, #309). A record
whose `pdk_pin.expected_deck_content_hash` doesn't match its own
`result.provenance.deck.content_hash` never reaches `main` with a green
check.

### D — Subset-reason requirements

`corner_count` in the response and the block's own committed
full-matrix-size policy are all a caller needs — `klt sim` cannot own "what
counts as full coverage" itself, because that is block-specific (a bandgap
with no BJTs has no `bjt_ff`/`bjt_ss` axis at all; a full matrix for it is
smaller by design, not a subset). The convention:

- Each block commits its own full-matrix policy, e.g.
  `evidence/sim/bandgap/full-pvt/full_matrix_size.txt` containing the
  integer corner count a *complete* sweep for that scope produces.
- The evidence record's `subset_reason` must be non-null whenever
  `result.corner_count` is less than the committed full-matrix size, and
  must be null when it equals it — enforced by a CI check that reads both
  and fails the merge on either violation (present-but-not-subset is
  disallowed too, so `subset_reason` can't be padded onto a full run and
  ignored later).

### E — Spread checks

Every corner's own measurement value is already in
`result.corners[].measurements[]` — spread is a pure function of data the
response already carries, no different in kind from how a measurement's
own `limits` are a caller-declared derived check on `value`. Worked
example (Python, using only the stored record's `result` block):

```python
import json
from collections import defaultdict

record = json.load(open("evidence/sim/bandgap/full-pvt/20260802T193000Z-9f2c1a3.json"))
by_name = defaultdict(list)
for corner in record["result"]["corners"]:
    for m in corner["measurements"]:
        if m["value"] is not None:
            by_name[m["name"]].append(m["value"])

# Repo-declared minimum expected spread per measurement, e.g. in the same
# full_matrix_size.txt policy file or a sibling spread_policy.json.
min_spread = {"vref": 1e-4, "iq": 1e-8}

failures = []
for name, values in by_name.items():
    spread = max(values) - min(values)
    threshold = min_spread.get(name)
    if threshold is not None and spread < threshold:
        failures.append(
            f"{name}: spread {spread:.3g} below {threshold:.3g} — corner sweep may not be reaching this measurement"
        )

if failures:
    raise SystemExit("\n".join(failures))
```

A spread below the declared threshold most often means a corner axis isn't
actually reaching the measured node (e.g. a `.temp` card that a subcircuit
ignores, or a supply `alter` targeting the wrong net) — the same class of
silent-plausible-wrong-answer failure item 3's original framing named for
PDK metadata. Like limits, an un-declared measurement in `min_spread` is
never checked — reported, not failed, by omission, the same
opt-in-per-measurement shape `klt sim`'s own `limits` field already uses.

## Follow-up issues filed

- **B (netlist snapshot per record)** →
  [#356](https://github.com/2AMLogic/klayout-tools/issues/356): expose
  `artifacts.deck` (already written to disk when `keep_artifacts` is set,
  just not referenced in the response).

## Non-goals

- This document does not change `klt sim`'s JSON contract. B's
  `artifacts.deck` field is a proposal for its own follow-up issue, not
  implemented here.
- The evidence-record wrapper shape above is illustrative, not a `klt`
  subcommand or schema `klt sim` validates — no code in this repository
  reads or writes `evidence-record/1` files. A block repo is free to name
  fields differently as long as the four guarantees (append-only,
  supersession, pin enforcement, subset-reason, spread) hold.

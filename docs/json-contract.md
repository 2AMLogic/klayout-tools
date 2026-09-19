# JSON output contract

Every `klt <verb>` command supports `--format text|json` (default: `text`).
**JSON is the API; text is a courtesy rendering of the same data** (CLAUDE.md).
This document defines the shared envelope every command emits through, so
verbs conform from day one rather than being harmonized after the fact.

All commands emit through `src/klayout_tools/cli/output.py`'s
`emit_success()` / `emit_error()` helpers — no `*_cmd.py` module hand-rolls
`json.dump`/`json.dumps`.

## Design: additive envelope, not a wrapping envelope

Each command's JSON payload stays **flat** at the top level (e.g. `klt
layers`'s `file`, `dbu_um`, `layer_count`, `layers` fields), rather than being
nested under a new key like `{"result": {...}}`. The only envelope-level
addition is a `schema_version` field alongside the command's existing fields.
This keeps the contract additive: an already-shipped command's documented
fields are never renamed, removed, or nested as part of adopting this
contract.

## Success shape

```json
{
  "schema_version": 1,
  "...": "the command's own top-level fields, unchanged"
}
```

- `schema_version` (integer) — starts at `1`. **Versioned per command**, not
  globally: `klt layers` and `klt cells` evolve independently, so a
  breaking change to one command's JSON shape does not force a version bump
  on another. A command bumps its own `schema_version` only when it makes a
  non-additive (breaking) change to its own payload; adding new fields does
  not require a bump.
- All other top-level fields are defined by the individual command's own
  documentation (e.g. `docs/cli/layers.md`).
- Written to **stdout only**, as indented JSON with a trailing newline.

### Pre-1.0 caveat: value sets within an unchanged shape can grow

The `schema_version`/`klt --version` policy above covers the *shape* of a
payload (which top-level fields exist), not the full *set of values* a field
can take on. Before `klt` reaches `1.0`, an additive change can introduce a
new enum-like value — most notably a new `mismatches[].category` (and
therefore a new `category_counts` key) for `klt lvs`, or a new `violations[]`
rule id for `klt drc` — without a `schema_version` bump and without changing
the reported `klt --version` string. Such a change is a deliberate, additive
behavior improvement (e.g. a new class of finding the tool did not previously
surface), not schema drift, but it does mean two runs of the identical `klt`
binary and identical `0.1.0` version string are **not** guaranteed to report
the same category set for the same input across time, or across two builds
snapshotted on different days. `CHANGELOG.md` is the source of truth for
*which* categories/rule ids exist as of a given date — check it first before
assuming a category change is a bug.

If a downstream project needs to pin exact reproducibility (e.g. golden
acceptance data keyed on `category_counts`), key that pin off the shared
`provenance` block's `deck` (`sha256:` content hash) and `klayout_version`
fields below — not `klt --version` — until `klt` reaches `1.0` and this
caveat is retired.

The same rule applies on the **request** side for the commands that take a
JSON request document (`klt lvs`, `klt place-and-route`, `klt synthesize`,
…): a new accepted value for an already-shipped request enum — e.g. `klt
lvs`'s `request.reference.form` gaining `"gate-level-verilog"` alongside
`"plain-element"`/`"subckt-call"` (issue #1336), together with the optional
`reference.library`/`reference.pdk`/`reference.pdk_root` fields that value
requires — is additive and earns no `schema_version` bump: every request
document that was valid before is still valid, and means exactly what it
meant before. A request document written against the newer value set is
simply rejected (exit 1, with the usual error envelope) by an older `klt`,
never silently mis-executed. Each such addition is recorded in
`CHANGELOG.md` and documented in that command's own
`docs/cli/<verb>.md` — for the example above, `docs/cli/lvs.md`'s "Digital
gate-level LVS" section and its `request` field table.

This caveat is narrow: it covers a field's *value set* growing (a new enum
member appearing), not what an existing, unchanged-type field's value
*means*. Redefining the semantics of an already-shipped field — e.g. `klt
precheck`'s `layer_whitelist[].shapes` moving from "summed once per cell
definition" to "weighted by placement multiplicity across the full cell
hierarchy" (issue #452) — is a breaking change and does earn a
`schema_version` bump on that command, even though the field's name and
JSON type (`integer`) never changed. See `docs/cli/precheck.md` for that
concrete precedent.

**A new `devices[].class` bringing its own, disjoint `params`/`nets` key set
is likewise additive, not a breaking change** — `klt extract`'s
`devices[].params`/`.nets` shape is already documented as varying *by device
class* (MOS: `w_um`/`l_um`/...; MiM capacitor: `c_f`/`area_um2`/
`perimeter_um`; drawn resistor: `w_um`/`l_um`/`r_ohm`; see
`docs/cli/extract.md`'s `devices[]` schema table), so a new class simply
adding its own key combination to that per-class map earns no
`schema_version` bump. The concrete precedent: issue #1466's MoM capacitor
devices (`cap_cmomi`/`cap_cmomf`) report only `w_um`/`l_um` — deliberately
**no** `c_f`/`area_um2`/`perimeter_um` at all, since the real device's
capacitance comes from its SPICE/Verilog-A model, not from LVS geometry —
the same "some device classes simply have fewer of the common
param/net keys than others" precedent bipolar devices (empty `params`) and
MOS devices (no `r_ohm`/`c_f`) already established, extended to a
capacitor-shaped device with no computed value at all. See
`docs/cli/extract.md`'s "MoM capacitor devices" section for the full
derivation.

**A new opt-in flag adding `null`-by-default fields to an already-shipped
per-entry shape is likewise additive** — the field exists on every entry
regardless of whether the flag was given, but only carries a real value when
the caller asked for the extra computation, so an unaware consumer never
sees a member appear or disappear. The concrete precedent: issue #1704's
`klt extract --parasitics-top-cell-only`, which adds
`resistance_ohm_top_cell`/`capacitance_ff_top_cell` to every
`parasitics.nets[]` entry — `null` unless the flag is given, a computed
number when it is. See `docs/cli/extract.md`'s "Top-cell-only hierarchy
split" section for the full derivation.

**A new *qualifier* field next to an existing numeric field is additive — and
is strictly preferable to retyping that field to `number | null`.** When an
engine reports a sentinel instead of a measurement, the honest fix looks like
two options: replace the sentinel with `null` (breaking — a retype, earning a
`schema_version` bump), or add a sibling field that says what the number is.
The second is additive: the numeric field keeps its documented type and value
for every existing consumer, and a consumer that wants the distinction keys on
the new field instead of on a magic value. The concrete precedent: issue
#1865's `timing_status` (`"constrained"` | `"unconstrained"` | `null`) on `klt
place-and-route` and `klt sta` — OpenSTA reports the worst slack of a design
with no constrained startpoint/endpoint as `1e+39`, a *positive* number that a
naive `worst_slack_ns >= 0` gate reads as "timing closed" on a design that was
never timed. `worst_slack_ns` still reports `1e+39` verbatim; `timing_status`
reports `"unconstrained"` alongside it, and neither command's
`schema_version` moved. See `docs/cli/place-and-route.md`'s "I/O timing
constraints ... and `timing_status`" section for the full derivation.

**Not every field a verb reports is a cross-build contract at all** — some
are extractor/engine-internal bookkeeping with no meaning outside the one
run that produced them (e.g. `klt extract`'s `net_id`/anonymous `$N`
net-name spellings/`parasitics.nets[]` ordering, all assigned inside an
opaque native KLayout call this repo does not control). `docs/cli/extract.md`'s
"Field classes: content, bookkeeping, tool metadata" section (issue #1559)
names this explicitly for `extract`'s own schema — a **content**/
**bookkeeping**/**tool metadata** partition every `drc`/`lvs`/`extract`
consumer of the `--check`/`--rerun` machinery below should apply, not just
`extract`'s.

## Requested-but-unperformed analysis

When a request asks for an analysis or capability, its response must make
clear whether that work was performed. An absent or `null` result must not
silently accompany an unqualified `"status": "ok"`. The command must
either refuse with its documented error response or emit a warning that
names the skipped capability and the reason it was skipped. A warning may
qualify a successful partial result, but it does not establish that the
requested analysis passed. A consumer requiring that analysis must treat
it as unverified.

This rule applies when a request implies a result, including a capability
normally required by the requested flow; it does not turn every optional
`null` field into a warning. Each verb documents which request fields
require which results, when it refuses, and when it can return useful
partial output with a warning. For example, a requested clock target with
no timing result needs an explanation; merely echoing the target does not
show that it was checked.

### Shared warning entry

Use this entry shape for warnings about skipped requested capabilities:

| Field | Type | Meaning |
|-------|------|---------|
| `category` | string | Stable, machine-readable category documented by the verb, such as `unsupported_cell_library`. Consumers branch on this value, not message wording. |
| `count` | positive integer | Number of occurrences represented by this entry; `1` for a single capability warning. This is not the number of analyses completed or skipped. |
| `text` | string | Explanation naming the affected capability or capabilities, why they were skipped, and the affected result fields. Include the relevant request value when it identifies the limitation. |

The shared contract is the **entry**. Adoption preserves both a verb's
existing warning container and its element types. A `warnings` array whose
documented entries already have this object shape can use it directly.
An existing `warnings: array<string>` must retain string elements: adding
objects, including a mixture of strings and objects, breaks that contract.
Such a verb exposes structured capability entries in a separate, additive,
documented field, or introduces an explicitly versioned breaking migration
with a `schema_version` bump.

`klt synthesize` retains its existing object
`warnings: {total, by_category, representatives}` and places them in
`warnings.representatives[]`. Its `by_category` map exposes the category
and count for consumers; its `representatives` entries explain them.
Existing fields, counts, and engine diagnostic categories keep their
documented meaning. Additive adoption must not rename, remove, or
restructure an already-shipped field, change its element types, or change
`warnings` from an object into an array.

Each verb documents its capability categories and the results they
qualify. A consumer can mechanically detect a known missing capability by
category, without parsing `text`; an unfamiliar category must not be taken
as proof that requested work completed. New categories remain additive
under the value-set rule above. When warnings are aggregated or capped,
the disclosure of skipped requested capabilities must remain visible.
The shared output helpers do not infer missing analyses: the verb knows
which work actually ran and is responsible for emitting the warning.

### Worked example: synthesis without a verified library mapping

The synthesis pilot implements this contract for missing library
capabilities (issues #2088/#2099). Consider a resolved library named
`acme_sc_hd` whose Liberty file is available but which has no verified
constraint or tie-cell mapping. The relevant request fields are:

```json
{
  "pdk": {"cell_library": "acme_sc_hd", "corner": "tt_025C_5v00"},
  "constraints": {"clock_period_ns": 81.38}
}
```

Synthesis can still produce a mapped netlist. With no engine warnings,
the corresponding response excerpt is:

```json
{
  "status": "ok",
  "timing": null,
  "warnings": {
    "total": 1,
    "by_category": {"unsupported_cell_library": 1},
    "representatives": [
      {
        "category": "unsupported_cell_library",
        "count": 1,
        "text": "cell_library 'acme_sc_hd' has no verified mapping for: ABC -constr (driving cell/output load), load-driven sizing/buffering and ABC timing (timing is null); setundef -zero/hilomap (bare 0/1/x constants may remain unroutable in place-and-route). These capabilities were skipped."
      }
    ]
  }
}
```

The warning identifies both the skipped work and its cause. The netlist
exists, but this response does not verify the requested 81.38 ns target or
the constant mapping required for routing. A partially supported library
names only its missing capabilities. See the
[synthesis warning contract](cli/synthesize.md#warnings-engine-diagnostics-and-missing-library-capabilities)
for aggregation and the existing engine categories.

This first contract increment confirms that synthesis pilot; it does not
claim every existing verb has been audited or add warnings to every
response. Further verb adoption preserves each verb's existing envelope.

## Shared `provenance` block

Verbs whose verdict depends on the exact tool build, PDK release, and rule
deck — currently `drc`, `lvs`, `extract`, `sim`, `size`, `precheck`, `erc`,
`gen`, and `gen-compose` — emit a shared top-level `provenance` block so a
"clean"/"pass"/"match" result is auditable and reproducible later. Two runs
made against different deck revisions or PDK releases are otherwise
indistinguishable in the output, so a signoff claim can't be checked or
reproduced. This block is **additive** (see above): adopting it required no
`schema_version` bump on any verb.

`klt erc` (issue #1968) is a partial exception on `pdk`: its `--pdk` selects
a built-in antenna-ratio limit table baked into `erc.py` itself (see
[`docs/cli/erc.md`](cli/erc.md)'s "Sky130 antenna-ratio limits"), not an
installed PDK directory resolved via `klayout_tools.pdk.find_pdk` the way
every other verb below populates this field — there is no `--pdk-root` for
`klt erc` to resolve against. Its `provenance.pdk.source` is therefore always
`"built-in"` rather than a filesystem-resolution method name, and `version`
is always `null` (no `SOURCES` stamp to read); `provenance.pdk` itself is
still `null` when `--pdk` was omitted, matching every other verb's
conditional population.

`klt erc` also carries one **extra** key no other verb emits (issue #2036):
`provenance.spec`, shaped `{content_hash}` exactly like `input` below and
likewise `sha256:`-prefixed. An ERC run is validated against *two* inputs,
not one — the layout (`provenance.input`) and the stackup/vias/nets/ties
spec file — and its verdict is only meaningful relative to the declarations
it was run with, so pinning the layout alone leaves a committed report
re-verifiable only halfway: edit the spec (drop a `ties` entry, re-point a
`layer`) and the report goes on asserting a verdict for declarations it
never saw. The top-level `spec` field is the spec's *path*; this is its
content. It is a verb-local field, not a `build_provenance()` parameter —
the same choice `klt lvs` made for its own second input
(`environment.reference_sha256`).

`klt wave build`/`klt wave query` (Epic #1585) also emit this block, but
for a different reason: neither resolves a PDK nor applies a rule deck (a
waveform trace has neither), so `pdk`/`deck`/`klayout_version` are always
`null` for both verbs — `klt_version` and `input.content_hash` (the trace
file's hash for `build`, the store file's hash for `query`) are what
still earn the block its keep, the same reproducibility role it plays for
every other verb below. See
[`docs/design/waveform-query-contract-spike.md`](design/waveform-query-contract-spike.md)
section 2 and [`docs/cli/wave.md`](cli/wave.md).

`klt gen`/`klt gen-compose` (issue #2035) are a partial exception for the
same reason, one field narrower: a generator or compose request is
*parameters* (plus, for compose, the blocks' own generator sub-reports) —
there is no rule/model deck and no single input layout stream to pin — so
`provenance.deck`/`provenance.input` are always `null` for both verbs.
Unlike `klt wave`, both do resolve a PDK, and unconditionally (a generator
cannot draw geometry without one), so `provenance.pdk` is always populated
and mirrors the identity the report's own top-level `pdk` field carries.
`klt_version` and `klayout_version` are what these two verbs gained the
block for: a consumer that commits generated geometry plus its report as
evidence and re-runs the generator later can otherwise not tell a real
geometry change from a klt/KLayout upgrade. See
[`docs/cli/gen.md`](cli/gen.md) and
[`docs/cli/gen-compose.md`](cli/gen-compose.md).

```json
"provenance": {
  "klt_version": "0.4.2",
  "klayout_version": "0.29.8",
  "pdk": {"name": "sky130A", "source": "volare", "version": "<stamp>"},
  "deck": {"name": "sky130", "content_hash": "sha256:<hex>", "released": true},
  "input": {"content_hash": "sha256:<hex>"}
}
```

- `klt_version` — the running `klt` package version
  (`klayout_tools.__version__`). This is the plain, static package version —
  deliberately *not* the build-identity string (issue #1202), which a
  post-tag source build reports as `X.Y.Z+g<sha>`; that one is available from
  `klt version --format json` as `version`, alongside `git_commit` and the
  tri-state `is_release`. Record it beside a report when "which build
  produced this?" has to remain answerable later; `klt_version` alone cannot
  tell a release from a source build made after that release's tag. See
  `docs/cli/version.md`.
- `klayout_version` — the KLayout Python engine build (`klayout.__version__`),
  or `null` if unresolvable.
- `klayout_version_mismatch` (issue #1490, `klt drc`/`klt lvs` only — see
  below) — `true` when this run's `klayout_version` differs from
  `klayout_version_expected` (`klt version --format json`, next section):
  the version this `klayout-tools` build/commit was tested against, else
  `false`. Always a plain boolean, never `null`: an unresolvable expected
  version (e.g. an editable/dev checkout with no build-time record) renders
  as `false` — "no *confirmed* mismatch" — not a fabricated signal. A `true`
  result also prints a one-line warning to stderr, so the drift is visible
  even without inspecting the JSON. See "Pinning the KLayout engine version"
  below.
- `pdk` — the resolved PDK, as `{name, source, version}` (`name` is the
  variant, `source` is how it was found, `version` is the `SOURCES` stamp), or
  `null` when the run resolved no PDK. `klt drc` (issue #1901) populates it
  when `--pdk`/`--pdk-root` is given (either engine), `null` when omitted;
  `klt lvs` (issue #1901) populates it only for a `"gate-level-verilog"`
  reference whose `reference.pdk`/`reference.pdk_root` resolve — a plain
  SPICE-vs-SPICE compare is topological and resolves no PDK, so `pdk` stays
  `null` there. See `docs/cli/drc.md`/`docs/cli/lvs.md` for each verb's exact
  condition.
- `deck` — the rule (or model) deck the run used, as
  `{name, content_hash, released}`. `content_hash` is a `sha256:`-prefixed
  hex digest of the deck file actually used, so "clean against *this exact*
  rule set" is a checkable claim. The whole block is `null` when no deck was
  involved (e.g. `lvs` against a pre-extracted netlist). `klt extract`
  additionally carries an `options` key (issue #595) when `--deck-option`
  selected a non-default flavour of a shared-geometry device family (e.g.
  gf180mcu's `{"poly_res": "2k"}`) — omitted entirely when no such option was
  given, so the block is otherwise unchanged. See `docs/cli/extract.md`'s
  "Selecting a shared-geometry resistor flavour". `klt drc --engine klayout`
  carries the same `options` key (issue #1306) when one or more `--deck-var
  NAME=VALUE` flags were passed, recording the extra `-rd` script globals
  that were threaded into the deck for that run (e.g. `{"feol": "true"}`) —
  likewise omitted entirely when no `--deck-var` was given. See
  `docs/cli/drc.md`.
  A pinned `content_hash` can be turned back into the klayout-tools git
  tag/PyPI version that shipped it with `klt deck resolve --content-hash
  <hash>` (issue #623) — a resolve-only lookup against a generated
  hash/version history table, not an in-process fetch of the historical
  deck; see `docs/cli/deck.md`. Note that `klt_version`/`klt --version`
  alone is *not* sufficient to confirm two runs used the same rule set (a
  rebuild of the same version can carry a different deck) — `content_hash`
  is the field that actually pins the rule set. The same hash can be obtained
  with no layout file and no check run at all from `klt deck hash --deck
  <name>` (issue #1202), whose payload is
  `{schema_version, deck, content_hash, released}` — the same `content_hash`
  and `released` values this block carries, so a consumer can gate on the
  deck a build will use *before* doing any work.
  - `released` (issue #1193) is that same `klt deck resolve` lookup run
    automatically, at generation time, so a "clean" report doesn't silently
    accumulate evidence against a deck revision no release ever ships. A
    tri-state signal, not a plain boolean: `true` when `content_hash` matches
    a released `klayout-tools` version; `false` when the deck history table
    loaded fine but confirms no release ships this hash (an unreleased dev
    checkout, an uncommitted deck edit, or a deck added after the last tag);
    `null` when the question is unanswerable — `content_hash` itself is
    `null`, or the generated history table
    (`src/klayout_tools/decks/_history.json`) is missing, unreadable, or
    malformed. `null` is deliberately distinct from `false`: a broken or
    absent history table must never be reported as a confirmed
    "this deck is unreleased" claim. `false` is a **non-fatal** signal — it
    never fails the run; it only makes an otherwise-invisible reproducibility
    gap visible in the output. No warning (`released: true`) is emitted for
    an ordinary run against a deck unchanged since its last release.
    Both `content_hash` and `released` require a run against an actual input
    layout to see; `klt deck info` (issue #1209) reports this install's own
    deck `content_hash` plus its structural device-class coverage
    (`ExtractionDeck.device_classes`) directly, with no input layout needed —
    see `docs/cli/deck.md`.
- `input` — the input layout stream the run was made against, as
  `{content_hash}` (same shape as `deck`). `content_hash` is a
  `sha256:`-prefixed hex digest of the file, so a stale committed report is a
  one-line diff against a freshly computed hash instead of being
  byte-identical to a current run. Populated by `drc`, `extract`, and (since
  issue #1969) `lvs`, which records the hash of the layout side it compared —
  the same file and the same digest its own `environment.layout_sha256`
  records, in the `sha256:`-prefixed form this block uses. `lvs` deliberately
  populated `null` here until #1969, on the reasoning that
  `environment.layout_sha256`/`reference_sha256` already covered it; that was
  wrong for `klt signoff --manifest`, whose staleness gate reads
  `provenance.input.content_hash` generically across every check kind and
  cannot see an LVS-only field, so every `content_hash`-pinned "LVS clean"
  citation graded `stale_evidence`. `environment.layout_sha256` is unchanged
  (still a bare hex digest, no `sha256:` prefix). `input` is still `null` for
  a verb with no single input stream to pin this way.

Fields that can't be resolved are `null` per the envelope convention — never
silently fabricated. The block is built once in
`src/klayout_tools/_provenance.py`; each verb's `docs/cli/<verb>.md` notes only
which of `pdk`/`deck`/`input` it populates.

**Verifying a committed report still reproduces.** A committed
`--format json` report (e.g. cited as `klt signoff` manifest evidence) is
only trustworthy for as long as its `provenance.deck.content_hash`/
`provenance.input.content_hash` still match the current deck/input — a
caller previously had to hand-roll that diff themselves. `klt drc`, `klt
lvs`, and `klt extract` (issue #1149, closing the gap the first two verbs'
issue #1106 left in the third) all support `--check <report.json>` (cheap:
re-hash and compare, no engine re-run) and `--check <report.json> --rerun`
(full: re-run and diff every verdict-bearing field, excluding
`klt_version`/`klayout_version`/`pdk.version`), reporting `status: "match"`
or `"drifted"` — see each verb's own `docs/cli/<verb>.md`, "`--check`
/ `--rerun`". This is what surfaces a curated deck rebuild that silently
changed device-recognition behavior (e.g. gf180mcu's substrate/well-tap
derivation) underneath a previously-committed report: since a deck is a
plain Python module, any byte change to it — cosmetic or
device-recognition-affecting — changes `content_hash`, and `--check`/
`--rerun` are what turn that into a caller-visible signal instead of a
silent mismatch.

`klt extract --check <report.json> --rerun` additionally normalizes every
**bookkeeping** field (issue #1559 — see `docs/cli/extract.md`'s "Field
classes: content, bookkeeping, tool metadata") before diffing, so a
committed/fresh pair differing only in `net_id`, an anonymous `$N` net-name
spelling, and/or `parasitics.nets[]` ordering no longer reports `status:
"drifted"` — only genuine **content** drift does.

### Pinning the KLayout engine version (issue #1490)

`klayout_version` above records which KLayout engine build a report was
generated against, but until this issue there was nothing to pin it *to*: a
`klayout-tools` git-commit-SHA install (the documented, recommended way to
reproduce a committed report byte-for-byte) resolves whatever `klayout` PyPI
release is current on the day of install — `pyproject.toml`'s own
`klayout>=0.30` dependency is deliberately an unbounded floor, not an exact
pin (see `docs/design/klayout-engine-version-pin.md` for the full rationale,
including why a hard pin was rejected).

`klt version --format json` reports both halves of the comparison:

```json
{
  "...": "... git_commit / git_tag / dirty / is_release, unchanged ...",
  "klayout_version": "0.30.12",
  "klayout_version_expected": "0.30.10"
}
```

- `klayout_version` — the `klayout` engine actually resolved into this
  process (same value as `provenance.klayout_version` above).
- `klayout_version_expected` — the `klayout` version this build/commit was
  tested against, recorded from the checkout's own `uv.lock` at build time
  (`uv sync --locked` is how this project's own CI installs, so the
  `klayout` entry `uv.lock` pins at a given commit *is* the version that
  commit's test suite ran against). `null` when unresolvable — a build made
  before this field existed, or a checkout with no reachable `uv.lock`.

A caller committing `klt drc`/`klt lvs` output as evidence does not need to
call `klt version` separately to detect drift: `provenance.klayout_version_mismatch`
(previous section) carries the same comparison inline on every report, with
a stderr warning when it is `true`. To reproduce the exact engine a
committed report was generated against:

```bash
uv tool install "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<sha>" \
  --with klayout==<klayout_version_expected>
```

`uv`'s own `--with` flag overrides the resolved `klayout` version for that
install without any `klt`-specific flag — see
`docs/design/klayout-engine-version-pin.md` for why this, rather than a new
override mechanism, is the documented fix.

## Declared metric namespace (`metrics` block, issue #247)

Every verb's numeric fields are named ad hoc today (`violation_count`,
`device_count`, `instance_count`, ...), with no declared aggregation or
polarity semantics — nothing states how a metric rolls up across blocks
(sum? max?), whether a larger value is better, or which metrics are critical
enough to gate signoff. `src/klayout_tools/metrics.py` is a **data-only
registry** — declared name -> `{aggregator, higher_is_better, critical}` —
that answers those questions mechanically, modeled on LibreLane/OpenROAD's
METRICS2.1 convention (hierarchical, double-underscore names such as
`design__instance__count`, `drc__error__count`). Full rationale, the
additive-vs-rename decision, and the naming grammar:
[`docs/design/metric-namespace.md`](design/metric-namespace.md).

**The convention is additive, never a rename.** A verb that adopts this
registry adds a parallel top-level `metrics` object re-keying a subset of
its *own already-emitted* fields under their declared name — it never
renames, removes, or retypes an existing field, and earns no
`schema_version` bump. `metrics` is omitted entirely when it would
otherwise be empty (mirroring this document's general omit-absent
convention).

```json
{
  "schema_version": 1,
  "...": "the command's own existing top-level fields, unchanged",
  "metrics": {
    "design__instance__count": 120,
    "drc__error__count": 0
  }
}
```

- Each key is a name declared in `klayout_tools.metrics.REGISTRY` — see that
  module (or `klt`'s own source) for the full registry, including each
  entry's `aggregator` (`"sum"`/`"min"`/`"max"`/`"mean"` — how per-block
  values roll up to a parent), `higher_is_better` (`true`/`false`/`null` —
  `null` for a purely structural/descriptive count with no declared quality
  polarity, not a placeholder), and `critical` (whether a failing value
  should mechanically gate signoff).
- **Adoption is per-verb and incremental.** `klt layout-metrics` was the
  pilot integration (see
  [`docs/cli/layout-metrics.md`](cli/layout-metrics.md)'s `metrics` field);
  `klt drc` adopted it next (issue #1847; see
  [`docs/cli/drc.md`](cli/drc.md)'s `metrics` field); `klt sim` adopted it
  after that (issue #1849; see [`docs/cli/sim.md`](cli/sim.md)'s `metrics`
  field), followed by `klt extract` (issue #1848; see
  [`docs/cli/extract.md`](cli/extract.md)'s `metrics` field — device/net/pin
  counts always, plus eight `--parasitics`-only entries). Each remaining verb
  adopts the registry — and grows its own `metrics` block — via its own
  follow-on issue; a verb that has not yet adopted it simply has no `metrics`
  field at all, same as any other not-yet-shipped optional field.
- A caller-supplied, per-run name (e.g. `klt sim`'s `measurements[].name`,
  which a request spec defines, not `klt` itself) is out of scope for this
  registry — it cannot be declared ahead of time because `klt` does not own
  the name.

## Output-artifact path fields: envelope vs. plain string (issue #2073)

`klt` reports an output-artifact path (a file it just wrote — a netlist, a
DEF, a GDS, a script) in one of two shapes today, and the two coexist
deliberately rather than by oversight:

- **`{path, scope}` envelope** — `{"path": "<repo-relative POSIX path>",
  "scope": "repo"}` when the artifact lives inside the invoking repo,
  `{"path": null, "scope": "external"}` when it does not, `{"path": null,
  "scope": "absent"}` when the field has no artifact to report. Built by
  `env_provenance.repo_relative_path()` (`src/klayout_tools/env_provenance.py`)
  — see that module's docstring for the full rationale (a repo-relative path
  is committable evidence; a machine-specific absolute path is not).
  `scope` is not decoration: the resolution rule differs per value — a
  `"repo"` path must be joined to *the invoking repo's* root (the cwd `klt`
  was run from), `"external"`/`"absent"` carry no usable path at all.
- **Plain string** — a bare absolute filesystem path (or `null` before the
  artifact exists), with no `scope` alongside it. The historical shape every
  path field used before the envelope existed.

**Resolved disposition (issue #2073): document the split, do not force a
migration.** A consumer chaining stages together needs to know *which* shape
a given field uses; it does not need every field to use the *same* shape.
Migrating the "plain string" column below to the envelope would touch 12
modules' `SCHEMA_VERSION` at once for fields whose consumers (`klt sta`,
`klt lvs`, downstream tooling) already parse them as plain strings — a much
larger blast radius and test-churn cost than documenting the existing,
stable split. The table below is the durable fix: it is the one place a
consumer (or a future migration) checks to know a field's shape without
probing the value's Python type at runtime.

**`{path, scope}` envelope fields**, as of `712a0219` (2026-09-18):

| Command | Field(s) | `SCHEMA_VERSION` | Originating issue |
|---|---|---|---|
| `synthesize` | `netlist_path`, `script_path`, `run_script_path`, `restructuring.restructured_netlist_path`, `arithmetic.candidates[].measured.{netlist_path,script_path}` | 2 | #1844 |
| `pex` | `layout`, `netlist`, `reference_netlist`, `testbenches[].{request,schematic_netlist}` | 2 | #1261 |
| `sim` | `netlist`, `environment.models_lib`, `environment.resume.checkpoint_path` | 3 | #1261 (`models_lib` additionally #1274) |
| `size` | `models_lib` | 2 | #1274 |

**Plain-string fields** — intentionally unchanged by this issue:

| Command | Field(s) | `SCHEMA_VERSION` |
|---|---|---|
| `place-and-route` | `def_path`, `unrouted_def_path`, `gds_path`, `verilog_path` | 1 |
| `sta` | `def_path`, `verilog_path`, `spef_path` | 1 |
| `place-and-route` (nested `spef_sta` block) | `spef_path`, `sdf_path` | (parent's `SCHEMA_VERSION` = 1) |
| `extract` | `netlist_path`, `spef_path`, `abstracted_cells[].lef_path` | 3 |
| `equiv` | `artifacts.{script_path,netlist_path,log_path}`, `artifacts.stage2_{script_path,log_path}` | 1 |
| `gen` | `gds_path` | 1 |
| `gen-compose` | `gds_path` (top-level and per-block) | 1 |
| `draw` | `gds_path` | 1 |
| `layout-plan-execute` | `gds_path` | 1 |
| `lef-abstract` | `output` | 1 |
| `arith-gen` | `verilog_path`, `reference_path`, `testbench_path`, `techmap_path`, `equiv_request_path`, `output_dir` | 1 |
| `functional-verification` | `info_path`, `log_path`, `options.trace.path` | 1 |
| `yield-campaign` | `campaign.sim_report_path`, `campaign.request_path` | 1 (`yield_analysis.py`) |

A field moving from this table to the envelope table above is a breaking
change to that field (a retype, `dict` in place of `str`) and earns a
`SCHEMA_VERSION` bump on its own module, following the `synthesize.py:246` /
`pex.py:160` / `sim.py:127` precedent — the same rule "Design: additive
envelope, not a wrapping envelope" states for the top-level envelope applies
per-field here too. There is no tool-wide signal for "which shape does field
X use today" beyond this table; re-check it (and this table's own `git log`)
before writing a consumer that assumes one shape tool-wide.

## Vacuous-verdict convention (`coverage.nothing_checked`, issue #1996)

Several verbs can report a passing top-level verdict on a run that checked
**nothing**, and until this convention existed there was no field a reader
could consult to tell that apart from an earned pass:

- `klt drc` (KLayout engine) — a PDK-native deck whose whole rule set is
  gated behind a feature-toggle global set via `--deck-var` that the caller
  never set. The deck runs to completion and writes a well-formed, **empty**
  report: `status: "clean"`, `violation_count: 0`.
- `klt sim` — a PVT corner matrix that expanded to zero corners, or a request
  whose every `measurements[].limits` object used keys `klt sim` does not
  apply (only `min`/`max` are read, so a typo'd bound passes everything).
- `klt pex` — a run that produced no `delta[]` row at all, so no
  schematic-vs-extracted comparison was ever performed.

Those verbs now say so in their own `coverage` block, using two keys declared
once in `src/klayout_tools/coverage.py` and spelled identically by every verb
that adopts them:

```json
{
  "schema_version": 1,
  "coverage": {
    "...": "the verb's own coverage fields, unchanged",
    "nothing_checked": true,
    "nothing_checked_reasons": ["deck_reported_no_rules"]
  }
}
```

- `nothing_checked` (boolean) — `true` **only** when the run performed zero
  actual checks, so its own `status` says nothing about the design. It is
  never a synonym for *partial* coverage: a run that checked one rule out of
  eighty reports `false` (that gap is what the verb's own coverage fields —
  `klt drc`'s `rules_skipped` / `layers_in_stream_without_rules` — are for).
- `nothing_checked_reasons` (array of string) — why, using the stable reason
  codes declared in `klayout_tools.coverage` (`deck_has_no_rules`,
  `all_rules_skipped`, `deck_reported_no_rules`, `empty_corner_matrix`,
  `unrecognized_limit_keys`, `no_delta_rows`). Always a list; empty exactly
  when `nothing_checked` is `false`. More than one code can apply to one run,
  and the set of codes can grow additively (see the pre-1.0 caveat above).
- **Additive, no `schema_version` bump.** Adding these keys to a verb's
  existing `coverage` block — or adding a `coverage` block to a verb that had
  none — adds fields without renaming, removing, or retyping any existing
  one.
- **Absence is not evidence.** An envelope with no `coverage` key, or one
  predating this convention, makes *no coverage statement*: a consumer must
  read it as neither a gap nor a guarantee of full coverage.
- **The producing verb's own `status` is unchanged** in every case. The
  convention makes the emptiness legible; it does not restate the verdict.

`klt signoff` is the first consumer that acts on it (see
[`docs/cli/signoff.md`](cli/signoff.md)): an envelope reporting
`nothing_checked: true` never counts as a passing check, and never backs a
`"met"` tier-report item.

## Error shape

Under `--format json`, errors are also JSON — not a plain-text line — written
to **stderr**, with **stdout left empty**. This means a caller never needs to
inspect stdout content to tell success from failure under `--format json`;
the exit code alone is authoritative.

```json
{
  "schema_version": 1,
  "error": {
    "command": "layers",
    "message": "file not found: missing.gds"
  }
}
```

- `error.command` — the subcommand name (e.g. `"layers"`).
- `error.message` — a concise, human-readable description of what went wrong.
  No Python traceback is ever emitted.

Under `--format text` (the default), errors remain the pre-existing
plain-text stderr line: `klt <command>: <message>`. Text is a courtesy
rendering, not the contract, so this shape is not versioned.

## Exit codes

| Exit code | Meaning                                                                                     |
| --------- | -------------------------------------------------------------------------------------------- |
| `0`       | Success. The documented success payload was written to stdout.                               |
| `1`       | Application-level error (e.g. missing/unreadable file). Documented error shape on stderr.     |
| `2`       | Usage error (missing required argument, invalid `--format` choice, etc.) — usually raised by argparse before a command's handler runs; a command may also detect one itself (see the carve-out below). |

Codes `0`/`1`/`2` mean the same thing for every verb. A command may define
**additional** codes above `2` for outcomes that are neither success nor
tool failure, documented in its own `docs/cli/<verb>.md` — e.g. `klt drc`
exits `3` when the deck ran successfully but found violations (a successful
run, so the documented success payload is still on stdout). Extensions never
redefine `0`/`1`/`2`.

**Carve-out:** exit code `2` and its accompanying stderr output are argparse's
own behavior, produced before any subcommand's `run()` executes. They are
deliberately out of scope for the shared `output.py` helper — argparse always
writes plain text for usage errors, in both `--format text` and `--format
json` modes, since format-specific handling would require parsing the
arguments before the parser itself has rejected them.

**The carve-out is about argparse, not about exit code `2`.** A usage error a
command detects *itself*, inside its own `run()` — e.g. `klt gen` rejecting a
generator name passed alongside `--pdk-pcell` — is past the parser, so
`args.format` is known and the documented envelope is owed: emit it with
`return output.emit_error(name, message, args.format,
exit_code=output.EXIT_USAGE_ERROR)`. The envelope shape and the exit code are
independent — `emit_error` writes the same envelope either way, and returns
whatever code the caller asks for (default `ERROR_EXIT_CODE`, `1`). A
plain-text `print()` + bare `return 2` in a command's `run()` is a contract
violation (issue #2029).

## `--format text` vs `--format json`

- `text` (default) — a human-readable rendering. Its exact layout is **not**
  part of the contract and may change between releases without notice.
- `json` — the stable API. Breaking a JSON field (renaming, removing, or
  retyping it) is a breaking change per CLAUDE.md; adding a field is not.

## Adding a new command

1. The library function backing the command returns a plain dict payload
   including `schema_version` (see `layers_report()` in
   `src/klayout_tools/layers.py` for the pattern) — not the CLI layer, so the
   version travels with the payload wherever it's reused (e.g. a future MCP
   server, per `docs/ARCHITECTURE.md`).
2. The `*_cmd.py` module's `run()` calls `output.emit_success(payload,
   args.format, text_renderer)` on success and `return output.emit_error(name,
   message, args.format)` on the documented error path.
3. Document the command's fields in `docs/cli/<verb>.md`, including its
   `schema_version`.

See `src/klayout_tools/cli/layers_cmd.py` and `docs/cli/layers.md` for a
worked example.

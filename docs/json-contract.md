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
  (`klt sim --report-out PATH` additionally writes the same bytes to a file,
  gzip-compressed for a `.gz` path; stdout is unchanged. `klt drc -o/--output
  PATH` does the same.)

### Pre-1.0 caveat: value sets within an unchanged shape can grow

The `schema_version`/`klt --version` policy above covers the *shape* of a
payload (which top-level fields exist), not the full *set of values* a field
can take on. Before `klt` reaches `1.0`, an additive change can introduce a
new enum-like value — most notably a new `mismatches[].category` (and
therefore a new `category_counts` key) for `klt lvs`, or a new `violations[]`
rule id or `violations[].check` kind (e.g. `"net_space"`, issue #3572) for `klt drc` — without a `schema_version` bump and without changing
the reported `klt --version` string. Such a change is a deliberate, additive
behavior improvement (e.g. a new class of finding the tool did not previously
surface), not schema drift, but it does mean two runs of the identical `klt`
binary and identical `0.1.0` version string are **not** guaranteed to report
the same category set for the same input across time, or across two builds
snapshotted on different days. `CHANGELOG.md` is the source of truth for
*which* categories/rule ids exist as of a given date — check it first before
assuming a category change is a bug. Record new JSON-visible changes in
[`changelog.d/`](../changelog.d/README.md) for inclusion in `CHANGELOG.md`
at release time. Check those fragments for changes pending on `main`.

If a downstream project needs to pin exact reproducibility (e.g. golden
acceptance data keyed on `category_counts`), key that pin off the shared
`provenance` block's `deck` (`sha256:` content hash) and `klayout_version`
fields below — not `klt --version` — until `klt` reaches `1.0` and this
caveat is retired.

Growing value sets are not the only pre-1.0 drift this caveat covers: the
**count under an already-shipped key can change what it counts**. A
category's own rules can consolidate or split the entries one event emits —
e.g. issue #4729 (`v0.9.0`) folded the geometry-derived `as`/`ad`/`ps`/`pd`
parameter differences into the same `device.property` entry as the primary
`w_um` difference they duplicated, so a width-only difference that
reported `category_counts["device.property"] == 5` before that release
reports `1` after, with `status` unchanged. Under this repo's policy such a
change is additive: it earns no `schema_version` bump, and it is recorded in
`CHANGELOG.md`. A caller comparing `category_counts` (or `error_count`)
values across versions therefore needs the same `provenance.deck` /
`provenance.klayout_version` pin described above.

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

The same holds for a **new optional request field** that widens what an
already-shipped field can express, as long as a request valid before is still
valid and still means what it meant. `klt sim`'s `measurements[].expr` (issue
#2533) is the reference case: a `measurements[]` entry used to require
`spice` (a verbatim `.meas` card) and now takes exactly one of `spice` or
`expr` (an ngspice expression evaluated after the analysis). Every pre-#2533
request is unchanged — byte-identical deck, byte-identical response — and the
*response* shape is untouched, so there is no `schema_version` bump: an
`expr` measurement is reported as an ordinary
`corners[].measurements[]` entry. See [`docs/cli/sim.md`](cli/sim.md)'s
"Measurements that are not `.meas` cards (`measurements[].expr`)".

When a `klt sim` measurement declares a supply-relative `limits.min` or
`limits.max`, its per-corner result gains `limits_resolved` containing
the numeric bounds for that corner. The aggregate measurement retains the
declared `limits`, including its `{"frac_of": "<rail>", "fraction": k}`
object. A measurement with only literal bounds keeps its existing shape.

`measurements[].optional` (issue #2925) is an opt-in boolean (default
`false`) that extends the per-measurement `status` vocabulary with an
additive value, `"absent"` (`value: null`, `margin: null`, info diagnostic
`measurement_absent`), for a `spice`/`expr` measurement that legitimately has
no value; it never makes its corner `error`. A request that omits the field is
byte-identical, so no `schema_version` bump -- but a consumer that switches
exhaustively on measurement `status` must treat `"absent"` as "no value, not a
failure". `klt pex` `delta[].schematic_status` / `extracted_status` echo it
(issue #3620): `"absent"` on exactly one side makes the row `status: "error"`;
on both sides, `"pass"`. For a schematic-only observation, `delta[].status`
is `"skipped"` unless the schematic measurement errored. See
[`docs/cli/sim.md`](cli/sim.md)'s "Optional measurements".

`measurements[].crossings` (issue #2868) is the third form and the first
whose result is a **series**, not a scalar. It does *not* retype `value`:
`value` stays a number (the count of crossings found), so every consumer that
reads `corners[].measurements[].value` as a number — the rollup, Monte Carlo
statistics, coverage, `limits` grading — is unaffected. The ordered crossing
times ride a new optional `crossings` object on that entry only, present only
when the series was extracted (absent on every other entry and on an errored
one) — the "new optional member, absent when not applicable" rule above.
No `schema_version` bump. See [`docs/cli/sim.md`](cli/sim.md)'s
"Threshold-crossing time series (`measurements[].crossings`)".

`measurements[].noise.top_n` (issue #5558) follows the same shape for a noise
measurement's largest contributors. `value` stays the scalar the entry would
return without `top_n` (graded, `monte_carlo`-aggregated and `limits`-checked
unchanged); the ranked list rides an additive `contributors` member of the
entry's `noise` object -- `[{name, value, mechanism}, ...]`, largest first --
present only when `top_n` was requested and the list was computed. A
`measurements[].noise.mechanism` entry already carries a `noise` object
(issue #5553); `contributors` joins it, and an entry without `mechanism` gains a
`noise` object holding only `contributors`. No `schema_version` bump. See
[`docs/cli/sim.md`](cli/sim.md)'s "Largest contributors (`noise.top_n`)".

`measurements[].search` (issue #4121) is a scalar form like `snm`: `value` stays
a number (the midpoint of the converged bisection bracket) and is graded by
`limits`, `monte_carlo` statistics and `klt yield` unchanged. The response
entry gains one optional `search` object (`source` or `param`, `direction`, `iterations`,
`lo`, `hi`, `converged`), present on a converged entry and, with
`converged: false`, on an errored one that ran probes; absent otherwise. A
failure is `status: "error"` with `value: null` and a `code: "measurement"`
diagnostic led by a `[search_*]` token. Additive; no `schema_version` bump. See
[`docs/cli/sim.md`](cli/sim.md)'s "Bisection / threshold search
(`measurements[].search`)".

`analysis.args` is optional for `analysis.kind: "op"` (and an `analysis_steps[]`
`op` step; issue #5078) and defaults to `""`; every other kind still requires it.
Additive; no `schema_version` bump.

`analysis.kind: "twoport"` (issue #3338) is a new accepted value of the
already-shipped `klt sim` `analysis.kind`, with its own `ports`/`freq` fields.
Every request valid before is unchanged; the *response* gains only an optional
per-corner `twoport` object and an optional `artifacts.twoport_s2p` path,
present only for a two-port request. Issue #3339 adds, inside that `twoport`
object, `stability` (`k`, `delta_abs`, `mu` arrays) and, only when the request
carries `analysis.noise`, a `noise` object (`source_port`, `t0_k`,
`noise_representation`, `nf_db`). Additive; no `schema_version` bump. See
[`docs/cli/sim.md`](cli/sim.md)'s "Two-port S-parameters".

`measurements[].transfer` (issues #2923, #3088) gains three `metric` tokens —
`phase_margin_min_deg`, `gain_margin_min_db`, `encirclement_count` — that reduce
the AC ratio at **every** crossing. Additive only: the six earlier tokens and
their entries are byte-identical, a margin entry's `transfer` sibling gains
optional `margins` (the full per-crossing report: `phase_margins[]`,
`gain_margins[]`, `band`, `reasons`, `ambiguity[]`) and `verdict`
(`pass`/`fail`/`ambiguous`) members, and an ambiguous AC band grades the entry
(and its corner) with the **existing** `status: "inconclusive"` (exit `4`)
plus a new `severity: "warning"` marker diagnostic `code: "margin_ambiguous"` —
the same one-status-many-reasons shape as `implausible_solution`, so the
`passed + failed + errored + inconclusive == corner_count` invariant holds. No
`schema_version` bump, and `docs/schemas/sim-set-report.schema.json` (the
`--batch-set` aggregate, which only references per-request reports by path) is
unchanged. See [`docs/cli/sim.md`](cli/sim.md)'s "AC return-ratio metrics".

`klt sim --check <report>` (issue #5821) is the reader half of the two netlist fields below: it emits the shared `--check` payload plus additive `diagnosis`/`note` members.

`klt sim` also gains an additive `environment.netlist_identity` (normalised,
comment/whitespace/path-insensitive netlist-closure hash; `null` plus
`netlist_identity_error` when not computable) and an optional request field
`expect_netlist_identity`: a value differing from the computed identity is
refused before simulating (exit 1, usual error envelope) with both stamps in
the message. No `schema_version` bump. See [`docs/cli/sim.md`](cli/sim.md)'s
"`environment.netlist_identity`".

`klt sim` also gains an additive `environment.device_multiplier` (issue
#5779): the device-multiplier lint's findings for instances whose vendor
`mult` parameter and SPICE `m=` drive multiplier disagree on a subcircuit
that declares `mult` but never forwards it to drive — a new finding class,
warning-only. Issue #6684 extends the scan from the netlist's own cards to
DUT cards in the scanned `.include` closure. An included fragment inherits the
scope of its include site. The scan covers calls inside parameterized DUTs
and omits included calls between sky130 primitive-library members
(`sky130_fd_pr__` caller and callee). A closure-file finding carries the
additive `file` field (the repo-relative scanned-closure label), while a
finding for the netlist's own card keeps the exact #5779 shape. `findings`
carry the instance, target subcircuit, both raw
values and a note; a `note` (never an error) discloses the netlist's own
instances whose subcircuit text is absent from the scanned include
closure, `unevaluated` lists the netlist's own non-numeric
`m=`/`mult=` values that cannot be compared, and
`not_scanned` discloses closure parts that could not be read — the
`note`/`unevaluated` disclosures are scoped to the netlist's cards so an
included library's parametric internals cannot add noise. The field is
absent when there is nothing to disclose, the exit code and every existing
field are unchanged, and the note recommends retaining both forms because
some models use `mult` to scale Monte Carlo mismatch. No
`schema_version` bump. See [`docs/cli/sim.md`](cli/sim.md)'s "Device
multiplier warnings".

`klt sim --job-status` (issue #4291) passes an optional `progress` object
(e.g. `sim_time_s`, `stop_time_s`) through from the fleet's `status.json` when
the runner publishes one. Additive and absent otherwise; the existing fields
and `schema_version` are unchanged. See [`docs/cli/sim.md`](cli/sim.md)'s
"Asynchronous submit, status and cancel".

`klt sim` on a **sharded** `batch` run (issue #5522) adds two top-level
roll-ups to `environment.remote` beside the existing `fleet[]` array:
`job_ids` (every non-`null` shard's `job_id`, in shard order) and a worst-of
`runner_compatibility` (`mismatch` > `unknown` > `match`, so a lost or
unknown shard never reads as clean). Each `fleet[]` member is typed
`object | null`: `null` is the slot of a shard that returned no remote block
(lost mid-run, or submitted but never launched), at its own index -- the
array is positional (index *i* is shard *i*, in the shard map's order), so a
client iterating it must skip the `null` slots and take identity and skew
from the roll-ups above. Additive, and narrower than the
opt-in-flag rule above: they appear only where `fleet[]` already does, so an
unsplit single-job report is byte-identical, the `fleet[]` entries are
unchanged, and the SSH `remote` backend gets neither. No `schema_version`
bump. The motivation is a reader, not a writer: splitting moved job identity
and the runner-skew check inside `fleet[]`, where a consumer written against
the single-job shape read `None` rather than failing -- and `None` is
indistinguishable from `"match"` in the obvious skew check. See
[`docs/cli/sim.md`](cli/sim.md)'s "Fleet sharding".

`measurements[].on_unreached` (issue #3108) is the same shape: an optional
request field (`"error"` default, `"null"` opt-in) whose default reproduces
today's response byte-for-byte. Opting in adds a new measurement `status`
member, `"not_reached"` (with `value: null`), an `info` diagnostic
`measurement_not_reached`, and a top-level `not_reached` count present only
for a request that declared the field. The same opt-in on a step-scoped
`analysis_steps[].measurements[].spice` card (issue #7072) reports the
identical members under the qualified `<step>.<name>` result, which also
carries its `step` provenance field. Consumers that switch on measurement
`status` already must tolerate unknown members per the rule above. No
`schema_version` bump. See [`docs/cli/sim.md`](cli/sim.md)'s "Unreached
`.meas` targets (`measurements[].on_unreached`)".

`stimulus` (issue #2977) is a new optional **request** block (declarative
timed edges, expanded by `klt sim` into PWL sources) with two new optional
**response** members under `environment`: `environment.stimulus` (the schedule
as applied, present only when the request declared `stimulus`) and
`environment.backend` (`{resolved, source, requested}`, present on every
corner-sweep report). Every pre-existing request is unchanged and no existing
response member changes type or meaning, so there is no `schema_version` bump.
An older `klt` or fleet runner that predates the key ignores it rather than
rejecting it; see [`docs/cli/sim.md`](cli/sim.md)'s "Declarative timed
stimulus (`stimulus`)" for the compatibility caveat.

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

**And a new `params` key on an *existing* `devices[].class` is additive by
the same reasoning**, provided it is a *new measurement* rather than a
redefinition of an existing key: `devices[].params` is already a per-class
key map that a consumer must read by name, so an unaware one that reads
`w_um` keeps reading exactly the `w_um` it always did. The concrete
precedent: issue #2435 adding `mmin`/`mmax` — the inclusive 1-based
metal-index range a MoM capacitor's fingers are drawn on — to the
`cap_cmomi`/`cap_cmomf` classes that had carried only `w_um`/`l_um` since
#1466. No existing key changed name, type, or meaning; the new pair is
measured from drawn geometry that was simply not read before, so **no
`schema_version` bump**. The same limit as above applies: had #2435 instead
*redefined* what an already-shipped key measures, that would have been
breaking regardless of the key's name and type staying put. Recorded in
`CHANGELOG.md` and documented in `docs/cli/extract.md`'s "MoM capacitor
devices" section, as every additive change is.

**A new optional *top-level* device member is additive too.** Issue #2445
added `devices[].feed` (`"none"`/`"same"`/`"double"`) to `cap_cmomi` devices
whose PCell feed variant was measured. It is a string, and `params` is an
object of numbers, so it sits beside `params` rather than inside it; it is
absent (not `null`) when unmeasured and always absent on `cap_cmomf`. No
existing member changed, so **no `schema_version` bump**. See
`docs/cli/extract.md`'s "MoM capacitor devices" section.

**A whole new *engine*, whose report adds fields and removes none, is
additive.** Issue #5481 added `klt extract --engine klayout-runset`, which
runs a PDK's own KLayout LVS runset instead of a curated `--deck`. Its report
carries a top-level `engine`, `deck_file` and `deck_vars`, plus
`provenance.runset` (the same block `klt lvs` already emits); `deck`,
`parasitics` and `pdk` are `null` because no curated deck, parasitics pass or
PDK resolution ran. Every field the default engine documents keeps its
meaning, and a **default-engine report is byte-identical** to one produced
before the flag existed — it does not even carry `engine`. So **no
`schema_version` bump** (`klt extract` stays at 4). A consumer that wants to
tell the two apart reads `engine`, whose absence means the curated engine.
See `docs/cli/extract.md`'s "Engine" section.

**An opt-in echo field, absent when its flag is, is additive.** Issue #5513
added `klt extract --engine klayout-runset --device-param`, which carries a
device class's measured parameters onto its `X` call. When the flag is given
the report carries a top-level `device_params`
(`{class: {PARAM: {"name", "suffix"}}}`, sorted), next to `device_models`; when
it is not, the key is **absent** rather than `null`, so a report without the
flag is byte-identical to one produced before it existed. `warnings[]`'s
per-class dropped-parameter entries then name only what is *still* not
carried. Every existing field keeps its meaning: **no `schema_version` bump**
(`klt extract` stays at 4). See `docs/cli/extract.md`'s "Carrying measured
parameters".

**A new terminal key on one device class's `nets` map is additive by the
same per-class reasoning.** Issue #2862 added an `"s"` (substrate) key to
`devices[].nets` for a bipolar device class of a deck entry that declares
the optional four-terminal `substrate` layer — absent (not `null`) on every
three-terminal entry, i.e. every deck shipped today, whose `nets` maps are
byte-identical to before. No existing key changed, so **no
`schema_version` bump**. See `docs/cli/extract.md`'s "Bipolar (BJT) device
recognition" section.

**A conditional hierarchy census is additive.** In a curated FinFET
extraction, a selected circuit with subcircuit instances gains three top-level
fields. `device_count_scope` is `"selected_circuit"`, `instantiated_device_counts`
counts each device class across the instantiated hierarchy, and
`instantiated_device_count` gives their sum. For example, a top cell with two
wrapper instances, each containing two one-NMOS leaves, reports
`{"nmos_rvt": 4}` and a total of 4. Flat selected circuits omit all three
fields. The existing `device_count`, `device_counts`, `devices[]` and
`metrics.extract__device__count` still describe devices directly in the
selected circuit. The extract report retains `schema_version` 4.
[Extraction report](cli/extract.md).

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

**An opt-in flag may also add a whole new object key to an already-shipped
per-entry shape, absent unless asked for.** Issue #2982's `klt extract
--resistance-graph` adds `resistance_graph` (`status`, `terminals`,
`pair_resistance_ohm`, `legacy_resistance_ohm`, `nodes`, `edges`,
`diagnostics`, and the additive scalar-selection provenance
`selected_resistance_ohm`, `resistance_source`, `resistance_source_reason`;
issue #2983) to each `parasitics.nets[]` entry. Without the flag the key does
not exist and the output is byte-identical to before; with it, every existing
field (`resistance_ohm`, `terminals[]`, `by_layer`, the ladder) keeps its
meaning and value. A consumer must treat the key as optional. No field is
removed, renamed or retyped, so **no `schema_version` bump**. See
`docs/cli/extract.md`'s "Terminal-aware resistance graph" section.
Issue #5747 likewise adds `fallback_reason` (`string | null`) to each
`extraction.resistance_graph.nets[]` entry of `klt pex --resistance-graph`:
`null` when `resistance_source` is `graph`, otherwise the reason the legacy
scalar was kept (the first diagnostic `code` of an `unsupported` graph, else
`resistance_source_reason`). Output without the flag is unchanged; no
`schema_version` bump.
Issue #6964 likewise adds `caps` (`{max_terminals, max_cells, max_nodes,
max_edges}`, integers) to `extraction.resistance_graph` of `klt pex
--resistance-graph`: present only when a `--resistance-graph-max-*` flag was
given, holding all four effective caps (the builder's default for any flag
omitted), and absent otherwise -- so an existing graph-enabled envelope is
unchanged. `klt extract` adds no field for the caps: its per-net
`graph_too_large` diagnostic names the cap that was exceeded and the value
applied. No `schema_version` bump.

**A *derived* per-entry identity field, present on every entry and
`null` when not derivable, is additive the same way — flag-gated or not.**
`klt extract` now annotates every `nets[]` entry with `structural_id`
(issue #5735): the net's numbering-independent `$anon<...>` structural
handle, the same identity `netlist_normalized_sha256` (issue #3407) and
`--check --rerun` already derive to canonicalize anonymous `\$N` spellings,
now emitted per net so a testbench probing an unlabeled internal node can
address it across re-extractions. It is `null` exactly when no identity is
derivable — a named net (its `name` is the label a consumer already joins
on; where names collide across internally-repeated sub-cells no unique
per-net identity would be sound, so none is minted) or an anonymous net
with zero device terminals — so no consumer ever sees the member appear or
disappear; its stability bound is exactly #3407's (equal across builds that
only renumber anonymous nets, not a promise that device instance names are
stable). Two constraints made this land without invalidating any committed
evidence: `--check --rerun`'s canonicalization strips the field alongside
`net_id` — not because it is unstable (it is *content* class, a
deterministic function of the same numbering-normalized connectivity) but
because a report committed before the field existed lacks the key and must
not diff against a fresh one on it — which also keeps
`netlist_normalized_sha256` byte-identical across the field's introduction.
No `schema_version` bump; recorded in `CHANGELOG.md` and documented in
`docs/cli/extract.md`'s `nets[]` schema table and "Anonymous net numbering"
section.

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

`klt gen res_array` additionally reports `drc_hints.length_snap` (issue #4666;
additive, absent unless a poly-body `length_um` was changed): `{requested_um,
realised_um, grid_um, resistance_delta_pct, reasons}`, where `reasons` is a
subset of `snapped_to_grid` / `lengthened_to_floor` (the latter only with
`params.short_length="lengthen"`). `realised_um` is the length actually drawn,
for comparing against the schematic/netlist `L`; `resistance_delta_pct` is
`(realised / requested - 1) * 100`. `params.snap=false` omits it and keeps the
#4203 warnings. No `schema_version` bump.

**Disambiguating an overloaded numeric field follows the same rule — add the
sibling, never retype or redefine the original.** `klt gen`'s
`drc_hints.min_spacing_um` carries two different quantities depending on the
generator: a genuine keep-out clearance (`guard_ring`'s enclosure,
`mos_array`'s same-layer margin) for most, but for a generator taking an
inter-unit `spacing_um` (`cap_array`, `res_array`) the *intra-array pitch
actually drawn* — a number the caller chooses and may widen far past any rule.
A consumer reading it as a keep-out radius (`klt gen-compose`'s
`"explicit"`-placement clearance advisory did) is wrong on the second kind.
Issue #2638 resolved that by adding `drc_hints.foreign_clearance_um` on those
two generators only: `min_spacing_um` keeps its documented meaning, type, and
value everywhere, and a consumer that needs a clearance reads the new field
when present and falls back to `min_spacing_um` when absent — which is exactly
what every pre-#2638 report, every hand-written one, and every generator that
never grew the field continues to get. Paired with it, `klt gen-compose` gained
an optional request field (`blocks[].abuts`, a list of block ids a placement is
intentionally flush with or inside) and echoes it back as `blocks[].abuts`:
additive on both sides under the request-field rule above — a request valid
before is still valid and still means what it meant. Neither command's
`schema_version` moved. See `docs/cli/gen-compose.md`'s "Which hint the
advisory reads, and the two legitimate violations (#2638)".

**A change to extracted *values* is disclosed by a revision field, not a
`schema_version` bump.** `klt extract --parasitics` reports an integer
`parasitics.model_revision` (issue #2957), bumped whenever the R or C model
changes the values extracted from identical input. It is additive;
`schema_version` is unchanged. See `docs/cli/extract.md`'s `parasitics` field
table.

**An opt-in flag that changes reported *values* adds its disclosure fields,
never retypes the old ones.** `klt extract --rename-declared-pins` (issue
#2854) renames surviving promoted pin nets to their declared names, which
changes what `nets[].name`/`devices[].nets[...]`/the written `.SUBCKT`
report for those nets — but only when the flag is given, and without
touching any field's name, type, or meaning. The disclosure is additive on
three surfaces at once: a `warnings[]` entry per aggregate (renames) and
per finding (the `declared_pin_short`/`declared_pin_rename_collision`
cases), the `null`-unless-given `pin_renames` block, and the
always-present boolean `rename_declared_pins` echo (so `--check --rerun`
can replay the rename from it plus the `pin_renames.declared_pins`/
`pin_renames.def_pins` echoes of the two original declared sets). `klt pex`
echoes the passthrough as `extraction.rename_declared_pins` (`false` when
the flag was not given), and `klt lvs` gained the optional request field
`layout.rename_declared_pins` plus one new `mismatches[].category` value
(`pin.declared_short`) — each additive under the rules above, no
`schema_version` bump on any of the three commands. See
`docs/cli/extract.md`'s "Renaming promoted pins to their declared name"
section.

**A new field *next to* a string-list field is how a list's element type
changes without breaking it.** Issue #3092: `klt deck rules`' per-rule
`layers` stays `list[str]` (deck name, or the raw `"L/D"` string when unnamed);
the stream layer/datatype behind each entry arrives as a new parallel
`layer_refs` list, plus top-level `layer_table`, `ambiguous_names` and
`unresolved`. `klt layers` gained an opt-in `--deck` that fills `name` and adds
a `deck: {name, content_hash}` block; without it the output is byte-identical.
No `schema_version` bump on either command. See `docs/cli/deck.md` and
`docs/cli/layers.md`.

Issue #3815 likewise adds, with no `schema_version` bump, `klt drc`'s
`coverage.marker_blind_layers_in_stream` and `klt deck rules`' top-level
`marker_blind_layers`: area-marker layers (sky130 `81/2`) the curated deck does
not model rule-switching for. Informational only; `status` and the exit code
are unchanged. See `docs/cli/drc.md` and `docs/cli/deck.md`.

Issue #2952's `klt lvs` category `reference.unresolved_subckt_call` is
likewise additive (no `schema_version` bump), but unlike the diagnostic-only
`pin.declared_short` it **forces** `status: "mismatch"`, so a request whose
inputs are unchanged can flip from `"match"` to `"mismatch"` on upgrade —
callers gating on `status` should expect that (see `docs/cli/lvs.md`,
"`reference.unresolved_subckt_call`").

`klt lvs`'s `body_verification` block gains an `inspected_count` integer
(issue #5749): how many MOS devices the body check actually examined, across
every eligible class (generic and `compare_mos_flavour`-flavoured), present
in every branch (`0` for `"unchecked"`) so a `"verified"` states its own
coverage instead of being inferred from the absence of findings. The same
issue also stops the block from rendering the *vacuous* `"verified"`: a run
that reached the check (a `layout.deck` was given) but found zero eligible
MOS devices — a passives-only layout — now reads `"unchecked"` with a
`reason`, and the check now sees flavoured classes, so a flavoured-only
layout is inspected (and honestly `"unverified"` when a body tie is missing)
rather than silently skipped. Both halves are additive-plus-honesty: no
field is removed or retyped, no `status` value is added or removed, and
`status`/`error_count`/`category_error_counts` are untouched (the block
does not feed grading) — but the previously-skipped flavoured warning now
fires, so on a `compare_mos_flavour: true` run over a layout with untied
flavoured bodies `mismatch_count` increases by one per newly-seen class
and `category_counts` gains a `device.body_unverified` key. A consumer
that read `body_verification.status: "verified"` as "this run inspected
bodies" must now also read `inspected_count`, because a pre-#5749
`"verified"` may have inspected nothing. No `schema_version` bump;
`docs/schemas/lvs-response.schema.json`'s `body_verification` definition
describes the new field. See `docs/cli/lvs.md`'s `body_verification` field
table.

**A new key in a documented open map, and a new member on one class of that
map's entries, are additive.** `klt functional-verification`'s
`environment.sdf.dropped` is documented as an open map keyed by class name,
each value `{count, reason}`. Issue #2885 added the
`iopath_ifnone_edge_modpath` class (`IOPATH` entries onto cell arcs whose
only modpaths Icarus discards at elaboration) and, on that class's entry
only, an `arcs` list (`{celltype, from, to}` per distinct dropped arc) so a
caller can see which arcs ran with no module-path delay. Every other class's
entry is byte-identical to before (no `arcs` key). Issue #5296 added the
`iopath_cond_expression` class (`COND`-wrapped `IOPATH` entries whose
condition is an operator expression Icarus rejects), which carries `arcs` the
same way. A run with no such
arc never carries the class, so **no `schema_version` bump**. A consumer
must keep treating `dropped` as an open map and read `arcs` as optional. See
`docs/cli/functional-verification.md`'s "SDF back-annotation" section.

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

**A run-measurement object is additive and not evidence.** `klt extract`'s
top-level `timing` object (issue #3181: per-stage wall seconds, `total_seconds`,
`unattributed_seconds`) is new and optional-to-read; no existing field changed,
so no `schema_version` bump. It varies run to run by nature, so `--check
--rerun` excludes it (`VOLATILE_PROVENANCE_PATHS`). Progress lines for the same
run go to stderr only; stdout stays the envelope.

**`klt pex --engine klayout-runset` adds fields, never changes one** (issue
#6559). The `extraction` block gains `engine`, `parasitics_carried`,
`params_not_carried`, `device_counts` and `extra_circuits` — present **only**
under that engine, so a default-engine envelope is byte-identical to before —
and
`body_bias.status` gains the value `"not_assessed"` (only ever emitted by that
engine). `klt extract --engine klayout-runset` reports gain a top-level
`params_not_carried` (`{class: [param, …]}`; `{}` when nothing is dropped),
`instantiated_device_counts` (`{class: count}` across the instantiated
hierarchy) and `extra_circuits` (`[circuit, …]`; `[]` when flat).
For `klt extract`, read the top-level `engine` field to identify the engine.
A runset report has `engine: "klayout-runset"`. A curated report omits `engine`.
`instantiated_device_counts` is always present under `klayout-runset`. On the
curated engine it appears for a FinFET selected circuit that instantiates at
least one subcircuit, alongside `device_count_scope` and
`instantiated_device_count` (issue #7615). Under the runset
engine `klt pex`'s `extraction.device_counts`/`device_count` follow the
instantiated hierarchy, which for a flat runset is the selected circuit's own
count.
Consumers that switch on `body_bias.status` already must tolerate an unknown
value per the rule above. No `schema_version` bump on either verb. See
[`docs/cli/pex.md`](cli/pex.md)'s "Runset engine".

## Same-name response field on two verbs: `cell_exclusions`

`klt synthesize` and `klt place-and-route` both report a `cell_exclusions`
block for cell exclusions, and deliberately differ in **presence** (issue
#7388): synthesize's is always present (issue #2382), while place-and-route's
appears only when the request supplied `library.dont_use` — the safer
additive choice for a verb whose whole block describes a request-supplied
list. Within the block, the shared keys mean the same thing on both verbs,
including `validated`: `true` when each requested pattern was matched
against the resolved Liberty's cell names, `null` when that cell list could
not be established and the zero-match check was skipped — the check-performed
disclosure of the "Requested-but-unperformed analysis" rule below, so a
caller mirroring one `dont_use` list between the two verbs gets the same
signal from each. (`validated` cannot take the third value it can on
synthesize, "nothing to check": place-and-route validates the request list
non-empty before emitting the block.) Synthesize additionally reports `mode`
and `engine_supports_dont_use`, which have no place-and-route analogue.
Per-verb details: [`docs/cli/synthesize.md`](cli/synthesize.md) and
[`docs/cli/place-and-route.md`](cli/place-and-route.md).

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

## Wrapping an external tool

A verb that wraps an external EDA tool inherits the
[requested-but-unperformed rule](#requested-but-unperformed-analysis): a
zero exit status from the tool is not evidence that it did what was asked.
Any new wrapper meets these four requirements, each a checkable statement
(operator ruling on [#2093](https://github.com/2AMLogic/klayout-tools/issues/2093)):

1. **Requested vs achieved.** The response carries both the requested value
   and the value the tool actually delivered, and flags a mismatch beyond a
   documented tolerance (a warning entry, or the verb's documented error)
   rather than returning an unqualified `"status": "ok"`. Upstream case: a
   PLL generator answering a different frequency than requested at exit 0
   ([YosysHQ/prjtrellis#270](https://github.com/YosysHQ/prjtrellis/issues/270)).
2. **Stage identity.** Every reported figure names the flow stage it comes
   from (for example a post-placement estimate vs final timing). A figure
   parsed from an unlabelled tool print is not reported as a final result.
   Upstream case: an unlabelled first Fmax line
   ([YosysHQ/nextpnr#1808](https://github.com/YosysHQ/nextpnr/issues/1808)).
3. **Actual tool identity.** The tool name and version captured from the
   invocation itself go into [`provenance`](#shared-provenance-block). An
   empty or unparseable version is reported as unknown (`null`), never
   fabricated or filled from a pin or a default.
4. **Retained failure evidence.** The stdout/stderr of a failed invocation is
   kept and referenced from the error or warning. Existing artefacts combined
   with a missing metrics object are not read as stage success. Tracked for
   the OpenROAD IR-drop case in [#2124](https://github.com/2AMLogic/klayout-tools/issues/2124).

### Wrapper audit checklist

When reviewing a new or existing wrapper, each answer must be yes:

- [ ] Does every requested value that the tool can silently adjust appear
      next to its achieved value, with a mismatch flagged beyond tolerance?
- [ ] Does every reported metric name the stage it was measured at?
- [ ] Is the tool name and version taken from the actual invocation, with
      `null` when it cannot be parsed?
- [ ] On a non-zero exit, or a missing metrics object, is the captured
      stdout/stderr retained and referenced from the error or warning?

In-tree implementations to compare against: [#2091](https://github.com/2AMLogic/klayout-tools/issues/2091)
and [#2124](https://github.com/2AMLogic/klayout-tools/issues/2124).

## Shared `provenance` block

Verbs whose verdict depends on the exact tool build, PDK release, and rule
deck — currently `drc`, `lvs`, `extract`, `sim`, `size`, `precheck`, `erc`,
`power`, `economy`, `ring-check`, `gen`, `gen-compose`, and `array-check`
— emit a
shared top-level `provenance` block so a "clean"/"pass"/"match" result is
auditable and reproducible later. Two runs made against different deck
revisions or PDK releases are otherwise indistinguishable in the output, so a
signoff claim can't be checked or reproduced. This block is **additive** (see
above): adopting it required no `schema_version` bump on any verb. The
HDL-flow verbs `synthesize`, `equiv`, and `functional-verification` (issue
#5550), and `klt sta`, emit the same block with `pdk`/`deck` as their run
resolved them — for those, `input.content_hash` (role `source`) pins the
design *sources* the run checked, binding a committed report to the RTL it
was built from rather than to its own bytes.

**Build drift in `klt signoff` (issue #4223).** `klt signoff` compares each
cited check's `provenance.klt_version` with the running build and reports an
additive, report-only `checks[].build_drift` object
(`state` = `current` | `stale` | `unverifiable`, plus `cited_klt_version` and
`running_klt_version`). See
[`cli/signoff.md`](cli/signoff.md#build-drift-a-verdict-from-a-different-klt-build-issue-4223).

**Client vs. worker scope (issue #2834).** `provenance` (and
`environment.client_klt_version`) describes the *submitting client*. For
`klt sim --backend batch`, what the executing worker actually resolved --
simulator binary and full version banner, PDK `SOURCES` stamp, CPU model,
`OMP_NUM_THREADS`, a worker-local image manifest -- is reported separately and
additively as `environment.remote.worker_environment` (and in each
`environment.remote.fleet[]` member), never merged into `provenance`. Missing
observations are `null` (never the client's values) and `capture` is `ok`,
`absent` or `malformed`. See [`docs/cli/sim.md`](cli/sim.md) "Worker-observed
environment" for the field names and limits. The worker's own PDK install
evidence is reported there too (issue #8007): `worker_environment.pdk.install`
and `.store_layout` are that worker's marker record and layout, read by the
worker — never the submitter's `provenance.pdk` values — so two hosts at one
revision show their respective recorded bundles. An older worker's capture
carries neither, and both read as `null`.

For sharded batch simulation, `environment.remote.run` records whole-run
completion, the original host count, observed rounds, distinct jobs with launch
evidence and the observed launch-to-terminal span. `environment.remote.fleet`
covers jobs harvested or dispatched during the reporting invocation. The
`history_coverage` field identifies partial history from older artifacts.
See the [simulation report contract](cli/sim.md#whole-run-batch-summary) for
the fields and their null semantics.

Under `options.stage_model_inputs` the worker's `pdk.stamp_*` still describes
the image tree; `pdk.stamp_scope` (`image` / `executed`) says so, and
`pdk_stamp_comparison` gains `basis: "staged_model_inputs"` (plus optional
`client_stamp`, `differing`) comparing the staged file digests. `image` also
arises without that option (issue #7981): on a deck-list batch job whose
`decks[]` holds a unit whose `.include`/`.lib` closure stages one of that
unit's declared `inputs[]`, the engine ran client-supplied bytes, so the image
stamp is not evidence about them. That condition alone leaves `models_staged`
at its worker-observed `false` and does not move `pdk_stamp_comparison` to the
digest basis -- only the option does, on corner-mode runs. A caller that
can name the revision behind those staged models sets `options.pdk_revision`
and gets it back as `environment.pdk_revision_asserted` (`{value, scope:
"caller-asserted"}`, issue #5325) -- a *claim*, kept out of the
worker-observed `worker_environment.pdk` so the two are never confused;
`cli/sim.md`'s option and `environment` tables are the field-level record.
Additive fields only; no `schema_version` bump (issue #4020).

`klt sim`'s `environment.ngspice_init` (issue #5648) records the ngspice init
file that was **in effect** -- always present on corner-mode runs, `null` for
`engine: "xyce"` -- as `{source, lines, sha256}`. `source` is a path-free label
(the same rule as `provenance.pdk.ambiguous_sources`: committed evidence must
not carry the resolving machine's `$HOME`): `"request"` (klt wrote a
`.spiceinit` including declared `options.ngspice_init` lines), `"thread_pin"`
(klt wrote only the `set num_threads=N` pin), `"home"` (klt wrote none and
`$HOME/.spiceinit` exists), `"none"`, or `"runner"` (off-host backends
`remote`/`batch`: the worker resolves the file, which the client cannot
observe). `lines` is the written file's lines for `request`/`thread_pin`, else
`null`; `sha256` is `sha256:<hex>` of the effective file's bytes, `null` for
`none`/`runner`. Additive; no `schema_version` bump.

For corner-mode `batch` runs, the worker's own on-host report supplies
`environment.remote.worker_environment.ngspice_init` (and the same field on
each `fleet[]` member, issue #6045). It has the same `{source, lines, sha256}`
shape and path-free labels; `home` always has `lines: null`. The client's
`environment.ngspice_init` remains `source: "runner"`. Missing, legacy,
Xyce or malformed worker observations are `null`; no client value is used
as a fallback. This field comes from the collected simulation report,
independently of the preflight capture's `capture` status. Additive; no
`schema_version` bump.

When a collected corner-mode ngspice batch request declares
`options.ngspice_init`, top-level `diagnostics[]` includes a warning with
`code: "batch_ngspice_init_unverified"`, `feature: "options.ngspice_init"`
and `simulator_application: "unverified"`. Its `jobs[]` contains one record
per job, in fleet order. Each record has `job_id` (string or null),
`fleet_index` (zero-based integer for a fleet, null for a single job) and
`request_identity`. The two identity values are `"digest_reported"` when
the worker reports the requested lines and their matching digest, including
any worker thread pin, and `"unverified"` otherwise. The digest identifies
the reported contents. Application by ngspice remains unverified for every
job. This warning preserves the simulation grade and is absent when the
request omits the init lines.

The same worker block carries `threads_per_worker`, the positive integer pin
from the worker report's `environment.threads_per_worker`. Each shard,
including a recovered failed job, reports its own value. An unpinned run or
a missing, legacy or malformed observation yields `null`. The client request
cannot supply a missing value. The sibling `omp_num_threads` records the
ambient variable at worker preflight, so a successful capture can report
`omp_num_threads: null` alongside `threads_per_worker: 2`.

`environment.remote` (and each `fleet[]` member) also gains the additive
`runner_models_lib_sha256` (string or `null`) and `models_lib_comparison`
(`status` `match` / `mismatch` / `not_comparable`); no `schema_version` bump
(issue #5321).

For a request that declares no `models.lib` because every selected section
names its own library (issue #2522), that hash pair is `null` on both sides by
construction, so `environment.remote` (and each `fleet[]` member) additionally
gains `runner_corner_section_libs` (`[{name, sha256}]` or `null`) and —
**only when the client or the runner reported per-section libraries** —
`corner_section_libs_comparison` (`status` `match` / `mismatch` with `names` /
`not_comparable` with `reason`), comparing by `(name, sha256)` pair
membership. `models_lib_comparison` gains the `no_models_lib` reason for that
case, previously reported as `runner_hash_unavailable`. A consumer that reads
a reason string must treat the reason set as open. Additive fields and a new
enum member on an existing optional field; no `schema_version` bump (issue
#5457). Field-level record: [`docs/cli/sim.md`](cli/sim.md) "Worker-observed
environment".

`klt sim --backend batch --dry-run`'s `published_runner.images[]` entries
(issue #5851) gain the additive `ngspice_version` and `pdk_snapshot` fields
each AMI-manifest image publishes, `null` for a legacy entry — the same
identities the new pre-submit `engine_version` pin refusal reads. No
`schema_version` bump. See [`docs/cli/sim.md`](cli/sim.md)'s "`engine_version`
pin (#5851)".

**No bucket in batch reports (issue #3777).** `environment.remote` (and each
`fleet[]` member) no longer carries `bucket` on any batch verb. The bucket name
embeds the fleet's AWS account number and reports are committed as evidence.
`job_id` and `region` remain; see [`docs/cli/sim.md`](cli/sim.md) "Reports omit
the bucket".

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

`klt ring-check --pdk` (issue #5650) is the same kind of exception: its
`--pdk` selects a seal-ring rule table baked into `seal_ring_rules.py`
(see [`docs/cli/ring-check.md`](cli/ring-check.md)), not an installed PDK
directory resolved via `find_pdk`, and there is no `--pdk-root` for it to
resolve against either. In `--pdk` mode `provenance.pdk` is therefore always
`{"name": "<pdk>", "source": "built-in", "version": null}`; in `--layers`
mode (no `--pdk`) it is `null`, matching the conditional-population
convention above.

`klt erc`'s `provenance.deck` (issue #2204) is, by contrast, populated
exactly the way every other `--deck`-taking verb below populates it (the
same `{name, content_hash, released}` shape) once `--deck` selects a curated
deck — see "Deck-driven device-marker auto-detection" in
[`docs/cli/erc.md`](cli/erc.md). Before that issue this field was always
`null` for `klt erc` (it applied no rule/model deck at all); `--deck` is
optional and independent of `--pdk`, and `provenance.deck` stays `null`
when it is omitted, matching this field's own conditional-population
convention everywhere else.

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

`klt lvs` carries its own extra key on the same footing (issue #2693):
`provenance.pin_order_sources`, present **only** when `reference.form` is
`"gate-level-verilog"`, is one `{library, path, cells}` record per
`.spice`/`.cdl` pin-order file the run actually read, in read order
(`library: null` for a `reference.pin_order_files` entry, which belongs to no
named library). That form resolves each instantiated cell's pin order from a
PDK library rather than from the reference netlist, and a macro-backed design
resolves **several** libraries — the standard cells' aggregate file plus one
file per instantiated hard macro — so `provenance.pdk` alone cannot answer
"where did this cell's pin order come from". Like `klt erc`'s `spec`, it is a
verb-local field rather than a `build_provenance()` parameter, and it is
omitted entirely (never an empty list) for every reference form that reads no
pin-order source. A file under the resolved PDK root is recorded as the
string `"$PDK_ROOT/<relative>"` (issue #5812) rather than an absolute host
path, so a committed report does not leak where the PDK is installed (the PDK
identity is in `provenance.pdk`); a `reference.pin_order_files` entry outside
the PDK root stays absolute unless the request sets `portable_paths: true`.
Same field and type, no `schema_version` bump. See
[`docs/cli/lvs.md`](cli/lvs.md)'s "Digital gate-level LVS".

`klt signoff` emits **no** `provenance` block of its own — it resolves no
PDK, applies no deck, and reads no single input stream; the evidence it
aggregates carries its own. Its two doc-parsing modes (`--manifest`,
`--fleet`) do, since issue #2176, carry the half of that block they *can*
answer, under a separate top-level `build` key: `{version, package_version,
git_commit, git_tag, dirty, is_release}`, the same identity `klt version
--format json` reports and the same `build_identity` source
`provenance.klt_version` is resolved from. A tier report is committed and
read back later, and "which build graded this" bounds what the verdict could
have checked at all — those modes grade a doc-derived item list against
rules compiled into the running build, and `--tiers-doc`/`$KLT_TIERS_DOC`
lets the two be different versions (each T1 item's `graded_by_build` says
whether this build had rules for it; a cited item that it did not renders
`unmet`/`ungradeable_by_build` rather than borrowing a pass). Both fields
are additive — new keys, and a new value in the existing `reason` enum —
and earned no `schema_version` bump on either mode. See
[`docs/cli/signoff.md`](cli/signoff.md)'s "An overridden doc can outrun the
build" and "Which build graded this".

Issue #2202 closes the reverse direction of the same skew with one more
additive key, `build_t1_item_count` (top-level in `--manifest`, per
`blocks[]` row in `--fleet`): how many T1 rows this build's *own* shipped
doc would have rendered, beside `t1_item_count`'s how many the parsed doc
did. A doc *older* than the build produces no wrong verdict — every
rendered row is correctly graded — but the report is silent about the items
it never listed, so `tier: "T1"` on a 9-item checklist reads exactly like
`tier: "T1"` on an 11-item one. `null`, never a fabricated number, when this
build cannot read its own doc, for the same reason `graded_by_build` claims
no divergence it cannot prove. A new key on an unchanged shape: no
`schema_version` bump on either mode. See
[`docs/cli/signoff.md`](cli/signoff.md)'s "A doc the build has outrun".

Issue #2718 lets T1 items 1, 2, 9 and 10 accept an artifact-anchored
`generic` envelope. The additions are: an optional `t1_item` key on the
generic envelope; a conditional `citation.artifact_binding` object, present
only on such a citation; and two new values in the existing `reason` enum,
`unanchored_evidence` and `wrong_item`. A native citation for those items
grades and renders exactly as before. A bare generic envelope cited for
them was already `unmet`, and its `reason` changes from `wrong_kind` to the
more specific `unanchored_evidence`. No `schema_version` bump. See
[`docs/cli/signoff.md`](cli/signoff.md)'s "Items 1, 2, 9, and 10: bind them
to an audited artifact".

Issue #2844 adds `klt signoff --attest {1,9,10}`, which produces that
envelope, and restricts items 1, 9 and 10 to it. A native citation for those
three now renders `unmet`/`wrong_kind`, an existing `reason` value; this is a
**grading-rule change**, recorded in `CHANGELOG.md`, not a shape change, so
no `schema_version` bump (the `build.grading_ruleset_id` hash moves, which
is what it is for). `citation.artifact_binding` gains an optional
`attestation` object when the cited envelope came from `--attest`. Item 2 is
unchanged in grading; issue #4188 adds a producer for it,
`klt signoff --attest 2 --input STREAM [--generator CMD --regenerated-hash H
[--hash-kind {bytes,geometry}]]`, emitting the same `generic` envelope with
`attestation.checks[]` entries `stream_readable`, `top_cell_non_empty` and
`dbu_on_grid`, `provenance.klayout_version` populated, and an optional
`provenance.regeneration` (additive; no `schema_version` bump). The
inventory-driven producers — `klt signoff --attest ITEM` for items 1, 8, 9
and 10 (issue #5690) — populate `provenance.klayout_version` the same way:
the `klayout` build resolved into the attesting process, which is the engine
a declared `checks[].argv` command that imports `klayout.db` actually ran
against (`null` when `klayout` is not importable). Like `klt_version`, it is
a version string in the `--check`/`--rerun` exclusion set, so committed
evidence re-checked across a klayout upgrade still reports `match`. The inventory `--attest` reads is its own versioned contract —
see "A versioned *input* contract" below.

Issue #3044 adds an optional `provenance.input.paths` set form to that
envelope and, on its citation, an additive `artifact_binding.members` list
(`[{path, content_hash, status}]`; the item's `detail.artifact_binding.members`
when unmet). Single-path citations are byte-identical to before. No
`schema_version` bump.

Issue #2887 adds an optional `provenance.regeneration` object
(`{generator, regenerated_hash}`) to an item-1/2 artifact-anchored
`generic` envelope. `klt signoff` never executes `generator`. On a matching
claim the citation's `artifact_binding` gains a `regeneration` object
(`{generator, regenerated_hash, executed_by_klt: false}`). A mismatched
claim renders the new `reason` value `stale_artifact`, and a malformed one
renders the existing `unanchored_evidence`. An envelope that makes no
claim grades and renders exactly as before. No `schema_version` bump.

Issue #5640 adds the item-1 coverage floor to the manifest grader: an
item-1 `generic` citation whose envelope enumerates at least one `present`
file in `attestation.checks[]` but declares no `provenance.input.paths`
(the pre-0.9.0 producer's inventory-only binding) renders the new `reason`
value `provenance_narrower_than_attested` with `detail` naming the breadth
gap (`missing`, `attested_members`, `bound_paths`). Post-#4764 envelopes,
hand-written envelopes without an `attestation` block, attestations with no
`present` check, and items 2, 8, 9 and 10 (single-artifact binding by
design) are unaffected. A new value in the existing `reason` enum, additive
like #2718's: no `schema_version` bump. See
[`docs/cli/signoff.md`](cli/signoff.md)'s "Item 1's coverage floor".

Issue #3743 adds three additive, optional fields (no `schema_version`
bump; absent they change nothing): `provenance.regeneration.hash_kind`
(`"bytes"` default, or `"geometry"` to compare `regenerated_hash` with the
timestamp-independent layout geometry digest; echoed in the disclosure only
when `"geometry"`; unknown values are `unanchored_evidence`); `local_only`
(boolean) on an attestation-inventory command, also echoed on the emitted
`attestation.checks[]` entry; and `skipped_checks[]` in the
`klt signoff --attest --check` response, listing `local_only` commands that
were not run. See
[`docs/cli/signoff.md`](cli/signoff.md)'s "Items 1 and 2: attesting that
the artifact was regenerated".

Issue #5460 adds an optional `check` object to a `drift[]` entry of the
`klt signoff --attest --check` response, naming the `attestation.checks[]`
entry the entry is about: its `role`, then the check's own kind (its `check`
value — `present`, `command`, `stream_readable`, …) when it carries one,
then whichever identity keys that check kind carries — `path` (a `present`
file check), `target` / `name` / `testbench` (a `command` or `recorded_hash`
check), `row` (an item-8 citation check), `corner` + `metric` (an item-8
metric check, one per corner × metric). The kind comes first so checks of
one role that carry no other identity key stay pairwise distinct — the three
`layout` stream checks of item 2, for example, are named by their kind alone.
A check with no identity key beyond its kind — item 9's `pdk` / `tools` pins
(`{"role": …, "check": "pin"}`), a top-level `cold_start`
(`{"role": "cold_start", "check": "command"}`) — stops there. **Additive; no
`schema_version` bump**: `field` stays the dotted positional pointer
(`attestation.checks.17.content_hash`) it has always been, and `committed` /
`fresh` are untouched, so a consumer that never reads `check` sees the bytes
it saw before. The key is **absent** on an entry that names no single check —
a whole-list `attestation.checks` entry (the index-by-index diff only applies
when both lists have the same length, so an inventory that *gained or lost* a
file still yields one whole-list entry), a `status` entry, anything outside
`attestation.checks`, and every `drift[]` entry the other `--check` verbs
produce. `--format text` renders it as a bracketed label after the pointer
(`attestation.checks.17.content_hash  [testbench present
sim/bin/sim_common.py]:`) so
a CI log names the file that moved without a second lookup — the index alone
is not even stable, since file-presence checks are sorted by path. See
[`docs/cli/signoff.md`](cli/signoff.md)'s "CI usage: `--check`".

Issue #5463 adds two more additive, optional fields on the same principle (no
`schema_version` bump; absent they change nothing): `documented_argv` on an
item-9 `cold_start` in an attestation inventory — the full invocation that
reproduces a bench whose declared `argv` only validates it, recorded and never
executed — and, on the emitted `attestation.checks[]` entry for such a command,
`reproduces: "validates"` plus the `documented_argv` verbatim. A cold start
that does not declare it emits the entry it always did, so committed envelopes
show no `--attest --check` drift, and item 9's `status` and T1 grade are
unchanged either way. The two fields also appear on the matching
`skipped_checks[]` entry, taken from the current inventory rather than the
committed envelope. The key is refused (exit `1`) on any non-item-9 command.
See [`docs/cli/signoff.md`](cli/signoff.md)'s "A cold start that only
validates its bench".

Issue #5641 lets an item-9 `testbenches[]` entry declare its `cold_start` as a
**list** of named commands as well as the single object it always took
(additive; no `schema_version` bump): each member `{name, argv, ...}` — `name`
a non-empty string, unique within its entry; anything else refused, exit `1` —
yields its own `command` check (`role: "cold_start"`) carrying `testbench` and
`name` alongside the usual `argv`/`cwd`/`exit_status`/`passed`, and the entry's
file is still inventoried exactly once (one `present` check). An entry with a
single-object `cold_start` — or none, under a top-level one — emits the checks
it always did, with no `name` key, so a committed envelope re-checks `match`
unchanged. Because `name` joins the carried-check identity, `--attest --check`
and `--skip-local-only` deliver a `local_only` member's committed outcome to
the right member even when two members under one path declare identical
`argv`. Members are emitted in `name` order after benches sorted by path, so
check order does not depend on inventory listing order. See
[`docs/cli/signoff.md`](cli/signoff.md)'s "Several cold starts over one file".

Issue #5563 adds the optional `resolved_from` field to an item-9 `pin` entry
in `attestation.checks[]` when at least one pin in that check uses a reference.
It maps each referenced leaf name (`name` or `revision` for the `pdk` role,
the tool name for the `tools` role) to its declared `{"from", "key"}` object.
On a passing check, the corresponding leaf in `value` holds the resolved
string. A literal-only check omits `resolved_from`. This field is additive
and requires no `schema_version` bump. See
[A pin declared by the file that owns it (issue #5563)](cli/signoff.md#examples).

Issue #5963 adds two additive, optional keys to a **file-backed**
`evidence[]` entry of the `klt signoff --manifest` request (no
`schema_version` bump; absent they change nothing): `artifact_manifest`
(repo-relative path to a committed artifact-store `*.manifest.json`) and
`artifact_member` (the manifest entry's `name`). For a `yield` entry whose
samples document is not on disk, the grader takes the recorded `sha256` of
the named member — `artifact_member`, else the entry whose `name` matches
the report's own `samples` reference (exact, then a single basename match)
— as the live hash the `content_hash` pin compares, reading only the
committed manifest: no broker, no network, no subprocess, so the grade
stays a pure function of the git tree. The citation discloses the basis
(`content_hash_basis: "artifact_manifest"`, additive like issue #4893's
`"evidence_file"`) and reports `input_verified: null` — the bytes were not
re-hashed. A store-resolved hash never supplies a verdict (operator ruling
2026-11-17, option `a`): the envelope itself stays committed and
file-backed. An unreadable/invalid manifest, a missing, duplicate-named or
basename-ambiguous member leaves the hash unresolved with a named reason in
`content_hash_unresolved` (`manifest_unreadable`, `manifest_invalid`,
`member_missing`, `duplicate_member`, or `ambiguous_member`). For any of
these failures, a pinned unmet item carries the same object in
`detail.content_hash_unresolved`. A non-string store-pin value or either
store-pin key on a command-backed entry yields `invalid_evidence` for that
item. A samples file present on disk always wins; entries that name no `artifact_manifest` are byte-identical to
before. See [`docs/cli/signoff.md`](cli/signoff.md)'s "Citing an object in
the artifact store".

`klt erc` since issue #2183 also carries a second verb-local key,
`provenance.devices`: one entry per `devices[]` spec declaration (`{name,
body_layer, on, body_area_um2}`), `[]` when none were declared. A
`devices[]` entry subtracts a drawn device body's region from a declared
conductor role before connectivity is registered — it changes which nets
exist, and so which findings are reachable — and `body_area_um2` reports
the area each declaration *actually* removed — the marker intersected
with the declared role's own conductor region, not the marker layer's own
area (issue #2226) — so a declaration that subtracted nothing is
distinguishable from one that bit, whether it named the wrong role or
matched no geometry at all. Same rationale
as `provenance.spec` above: a report whose verdict depends on a
transformation of its input has to say what the transformation did. See
[`docs/cli/erc.md`](cli/erc.md)'s "Device bodies are not wires".

When `--deck` selects a curated deck (issue #2204), every
`provenance.devices` entry — both hand-declared and deck-detected — gains
two additional keys: `source` (`"declared"` vs `"deck"`) and
`superseded_by` (the hand-declared device name that pre-empted a
deck-detected carve-out for the same role, `null` otherwise). A
deck-detected device whose conducting-body layer matches no declared role
is still listed (`"on": null`), so a mis-scoped spec is visible rather than
silently invisible. Both new keys are **additive and conditional**: a run
that omits `--deck` produces `provenance.devices` entries with the pre-#2204
4-key shape, byte-identical to before this issue — no `schema_version`
bump, matching this block's own additive-envelope convention. See
[`docs/cli/erc.md`](cli/erc.md)'s "Deck-driven device-marker
auto-detection".

Issue #2524 adds a third verb-local key on the same principle,
`provenance.net_exclusions`: one entry per `nets[]` spec declaration that
carried `unlabelled_allowed_boxes` (`{net, boxes, excluded_islands,
excluded_area_um2}`), `[]` when none did. That key subtracts caller-declared
regions — legitimate unlabelled fill on a role a net declared it owns — from
the unlabelled remainder the `erc.unlabelled_conductor` finding is graded
on, so a declaration *suppresses a finding*, and the two `excluded_*` values
report what it actually removed. Same reasoning as `provenance.devices`'
`body_area_um2`, one level up: without the echo, two runs of the same layout
disagree about a finding with nothing in either payload to say why, and a
box over empty space is indistinguishable from one that bit. See
[`docs/cli/erc.md`](cli/erc.md)'s "The remainder gates".

Issue #4374 is additive on the same surface: an `erc.unlabelled_conductor`
finding now populates `islands[]` (previously `null` for that rule) with
`{bbox, layer, shape_count, kind}` entries, `kind` being `"device_connected"`
or `"floating"`; the optional spec key `nets[].unlabelled_allow_device_connected`
(default `false`) excuses the former; and a `provenance.net_exclusions` entry
gains `allow_device_connected` / `excluded_device_connected_islands` only when
that key is declared. Nothing is renamed or removed and `schema_version` is
unchanged.

Issue #2415 adds a fourth, `provenance.label_layers`: one entry per
`stackup` role that declares a `label_layer`, in stackup order (`{name,
label_layer, label_text_count}`), `[]` when no role declares one. Always
present on a successful report. `label_text_count` counts text
*occurrences* on that layer across the analysed top-cell hierarchy (each
repeated string and each child-cell instance separately), before any
conductor-overlap filtering. A declared label layer carrying no text
otherwise surfaces in the JSON only as `erc.unconnected_net` and `null`
gate nets — indistinguishable from a real supply defect — while its
issue #2401 warning goes to stderr, which a committed report does not
capture. `0` means no text was present (usually, not provably, a
mis-transcribed layer/datatype); a positive count does not show that the
labels touch the intended conductor. Evidence only — no finding, status
or exit-code change. Additive, no `schema_version` bump; a report
predating #2415 lacks the key, and a reader must treat that absence as
"evidence not recorded", not as an empty array. See
[`docs/cli/erc.md`](cli/erc.md)'s `provenance.label_layers` row.

`klt power` (issue #2349) emits the block for the same two-input reason
`klt erc` does — its IR/EM verdict is a joint function of the layout
*and* the spec's `stackup`/`vias` sheet-resistance and EM declarations,
its `pads`, and its `current_model`, none of which a committed report
could otherwise be re-verified against. `provenance.pdk`/`provenance.deck`
are always `null` (the verb resolves no installed PDK and applies no deck
— the spec declares its electrical numbers directly); the layout is
pinned under `provenance.input` and the spec under the same verb-local
`provenance.spec` `{content_hash}` shape `klt erc`'s issue #2036 added,
so a caller reads both verbs identically. It adds one verb-local key of
its own, `provenance.top_cell`: the solved network is `LayoutToNetlist`'s
output *for one cell*, and a stream can hold several. See
[`docs/cli/power.md`](cli/power.md)'s "Provenance".

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
`klt gen` also reports an additive top-level `subcell_naming_scheme` (issue
#5808): a string id of its sub-cell naming (`null` when none remains) — see
`docs/cli/gen.md`.
`klt_version` and `klayout_version` are what these two verbs gained the
block for: a consumer that commits generated geometry plus its report as
evidence and re-runs the generator later can otherwise not tell a real
geometry change from a klt/KLayout upgrade. **Generator geometry itself is
not guaranteed stable across releases before `1.0`** — only this block's
shape is covered by the additive-envelope guarantee above; see
[`docs/cli/gen.md`](cli/gen.md)'s "Semantics and guarantees" section for the
stability statement, concrete precedents, and drift-detection guidance, and
[`docs/cli/gen-compose.md`](cli/gen-compose.md).

```json
"provenance": {
  "klt_version": "0.4.2",
  "klt_git_commit": "<full sha> | null",
  "klt_reinstall_spec": "klayout-tools==0.4.2 | null",
  "klayout_version": "0.29.8",
  "pdk": {"name": "sky130A", "source": "volare", "version": "<stamp>"},
  "deck": {"name": "sky130", "content_hash": "sha256:<hex>", "released": true},
  "input": {"content_hash": "sha256:<hex>", "role": "layout"}
}
```

- `klt_version` — the running `klt` build identity (issue #2090), resolved by
  `build_identity.build_version()` exactly like `klt --version` and
  `klt version --format json`'s `version`: `X.Y.Z` for a confirmed clean
  release, `X.Y.(Z+1).devN+g<12-char sha>` for a post-tag build (or
  `X.Y.Z.devN+g<sha>` before the base version is tagged), an additional
  `.dirty` for uncommitted changes, or `X.Y.Z.dev0+unknown` when no git
  provenance can be recovered. Since issue #2823 this is the built
  distribution's own metadata version, so it equals
  `klayout_tools.__version__` and `klt version`'s `package_version` for every
  built distribution; editable installs recompute it live from their
  checkout. Reports from before #2090 carry only the static package version
  and cannot distinguish releases from post-tag builds; reports from builds
  between #2090 and #2823 carry the older `X.Y.Z+g<sha>` / `X.Y.Z+unknown`
  forms. **Contract change (issue #2823):** `package_version` (in `klt
  version` and `klt signoff`'s `build` block) is no longer the static base
  version on non-release builds — it carries the same dev form as `version`.
  Field names, types, and the definition (`klayout_tools.__version__`) are
  unchanged, so no `schema_version` bump; see `docs/cli/version.md`.
- `klt_git_commit`, `klt_reinstall_spec` (issue #4412, additive, no `schema_version` bump) — the
  full commit behind this build and the `uvx --from`/`pip install` spec that
  re-obtains it, with the same values as `klt version --format json`'s
  `git_commit` and `reinstall_spec`. `klt_git_commit` is `null` only when no
  commit is recoverable (tarball install, `X.Y.Z.dev0+unknown`) and stays
  populated on a dirty tree; `klt_reinstall_spec` is `null` when the tree is
  dirty or the commit is unrecoverable. Never fabricated. A `.devN+g<sha>`
  `klt_version` is not published to an index, so `uvx --from
  "klayout-tools==<version>"` fails by design; use `klt_reinstall_spec`. The
  spec re-obtains the **klt axis only**; the canonical both-axes
  reconstruction of a recording adds `--with "klayout==<klayout_version>"`
  (issue #5884) — see [`docs/cli/version.md`](cli/version.md)'s
  "Reproducing a recorded pin". Both are
  excluded from `--rerun`/`--check` drift like `klt_version`.
- `klayout_version` — the KLayout engine that actually produced the run.
  For `klt drc --engine klayout` (issue #5660) this is the standalone
  application's own `-v` version — the binary the deck ran under, tracked
  through `--klayout-binary`/`$KLT_KLAYOUT_BINARY`, probed once per run even
  without `--min-klayout-version`, `null` when the probe fails softly and no
  floor was declared — with the pip wheel's version recorded beside it as
  the additive `klayout_module_version` (`--engine klayout` only); the
  `--with "klayout==<klayout_version>"` pin (issue #5884) reproduces the
  *wheel* axis of a recording, not that engine — see
  [`docs/cli/drc.md`](cli/drc.md), 'Engine' -> 'klayout'. For every other
  engine (`--engine curated`, `klt lvs`, `klt extract`, ...) this is the
  KLayout Python engine build (`klayout.__version__`), or `null` if
  unresolvable — the wheel genuinely is the engine there, and reproducing a
  recording byte-for-byte passes this recorded value to `uv`'s
  `--with "klayout==<klayout_version>"` next to `klt_reinstall_spec`
  (issue #5884) — see [`docs/cli/version.md`](cli/version.md)'s
  "Reproducing a recorded pin".
- `klayout_module_version` (issue #5660, additive, `--engine klayout` only)
  — `klayout.__version__`, the pip `klayout` wheel in klt's own environment,
  recorded next to `klayout_version` so the wheel's version is not lost
  without presenting it as the engine that ran the deck. Absent for every
  other engine, so their provenance is byte-identical to before.
- `klayout_version_mismatch` (issue #1490, `klt drc`/`klt lvs`/`klt extract`
  — see below) — `true` when this run's `klayout_version` differs from
  `klayout_version_expected` (`klt version --format json`, next section):
  the version this `klayout-tools` build/commit was tested against, else
  `false`. Always a plain boolean, never `null`: an unresolvable expected
  version (e.g. an editable/dev checkout with no build-time record) renders
  as `false` — "no *confirmed* mismatch" — not a fabricated signal. A `true`
  result also prints a one-line warning to stderr, so the drift is visible
  even without inspecting the JSON — for `--engine klayout` runs the warning
  names the standalone application and its remedy, not `--with klayout==`
  (issue #5660: pinning the wheel does not pin that engine); for `klt
  extract` (default in-process engine, issue #7824) the consequence the
  warning names is the extracted netlist's own device parameters and
  parasitic R/C, not DRC/LVS report counts alone. Under
  `--format json` that warning follows the "Error shape" delivery rule
  above (issue #6935): still on stderr ahead of a report — any report,
  including a violations report's exit `3` — but dropped when the
  invocation ends in the error envelope, which keeps that stderr one
  parseable JSON document. See "Pinning the KLayout engine version" below.
- `pdk` — the resolved PDK, as `{name, source, version}` (`name` is the
  variant, `source` is how it was found, `version` is the `SOURCES` stamp), or
  `null` when the run resolved no PDK. `klt drc` (issue #1901) populates it
  when `--pdk`/`--pdk-root` is given (either engine), `null` when omitted;
  `klt lvs` (issue #1901) populates it only for a `"gate-level-verilog"`
  reference whose `reference.pdk`/`reference.pdk_root` resolve — a plain
  SPICE-vs-SPICE compare is topological and resolves no PDK, so `pdk` stays
  `null` there. See `docs/cli/drc.md`/`docs/cli/lvs.md` for each verb's exact
  condition.
  - `ambiguous_sources` (issue #2564) — present **only** when more than one
    candidate install on the resolving host held the resolved variant; the
    `source`-style search-order labels of the ones the first-match-wins
    resolution skipped, e.g. `["search root: ~/.volare"]`. Deliberately the
    path-free labels and not the absolute roots: this block is committed
    evidence, and a raw store path carries the resolving machine's home
    directory (the same reasoning behind the `{path, scope}` shape used for
    paths elsewhere). The absolute roots are named in the accompanying
    stderr warning instead. Absent in the ordinary single-install case, so
    an unambiguous run's `provenance` is byte-identical to what it was
    before this field existed and a stored-report diff cannot drift on it.
  - `compatibility` (issue #3427) — present **only** when the resolved
    variant's `SOURCES` stamp differs from the declared open_pdks pin
    (`klt pdk`'s `compatibility.status == "mismatch"`):
    `{status: "mismatch", package, expected}`. Absent when the status is
    `match`/`unknown`/`not_declared`, so a matching run's `provenance` is
    byte-identical to before.
    Populated by `klt sim` today; the underlying detection lives in
    `klayout_tools.pdk.find_pdk` (`ambiguous_roots`) and is available to
    every verb that resolves a PDK — see `docs/cli/pdk.md`.
  - `alternative_variants` (issue #3814) — additive field on `klt pdk find`'s
    payload (not part of `provenance`): sorted names of the other variants in
    the chosen root when no variant was selected (`--pdk`/`$PDK` unset);
    `[]` otherwise. Resolution and exit code are unchanged; text mode warns on
    stderr. See `docs/cli/pdk.md`.
  - `commit` (issue #3786) — present **only** when the install carries a
    `.fetched-commit` record (40-hex upstream commit, written by
    `scripts/fetch-*.sh` from the tarball's pax header); absent otherwise, so
    provenance for any other install is byte-identical to before. `version`
    stays `fetched:<tag>`. Like `version`, excluded from `--rerun` drift
    comparison. Additive; no `schema_version` bump. The fleet-bundle
    `.pdk-store.json` is read, never written, here (issue #5318).
  - `install` (issue #8007) — what the nearest `.pdk-store.json` records about
    the install: `{sha256, version, via, synced}`, each the marker's own string
    or `null`, and `null` when there is no usable marker evidence — so an
    install without a marker reports its placing tool as unknown rather than
    implying one. Carried from `klt pdk find` as reported (present whenever
    that report is; absent only for a hand-built `pdk` dict without it).
    Path-free: only the four named fields travel, never the marker's store
    `key`, and nothing is inferred from directory names or tools on `PATH`.
    Excluded from `--rerun` drift comparison like `source`: it records how
    this host came by the tree.
  - `store_layout` (issue #8007) — the layout the tree was found in:
    `manager_family`, `legacy_manager` or `flat`, or `null` when it cannot be
    established (see [`cli/pdk.md`](cli/pdk.md) → "Install provenance").
    Same carriage and `--rerun` exclusion as `install`.
  - `version` (issue #3070) is a self-describing stamp, or `null` when no
    revision can be read mechanically (never guessed): an open_pdks `SOURCES`
    file (the lines joined with `"; "`, unchanged), else `fetched:<marker>`
    from the `.fetched-version` file `scripts/fetch-*.sh` writes (searched at
    the variant dir and up to two parents), else `git:<40-hex HEAD>` when the
    variant dir or its parent is a Git work tree root. The `-dirty` suffix
    marks changes in the index or to tracked files in the checkout. An
    untracked file leaves the stamp unchanged until it is added to the index.
    A variant nested inside an unrelated checkout skips the Git fallback.
    When the preceding sources yield no version, a `.pdk-store.json` string
    `version` at the real variant directory or its parent gives
    `store:<version>`. The final fallback is `null`.
  - `linked_trees` (issue #3070) — present **only** when the model library the
    run resolved (`provenance.deck`) realpath-resolves into a *different* PDK
    variant tree than `name` (a cross-variant symlink): a list of
    `{name, version}`, `version` read exactly as above. Path-free, absent
    otherwise, so a run without cross-tree resolution is byte-identical to
    before. Populated by verbs that pass a model library (`klt sim` today).
    Like `version`, it carries a revision and is excluded from `--rerun`
    drift comparison. Additive; no `schema_version` bump.
- `deck` — the rule (or model) deck the run used, as
  `{name, content_hash, released}`. `content_hash` is a `sha256:`-prefixed
  hex digest. For a built-in deck it covers the primary Python module and
  each deck-specific sibling that populates or modifies its curated DRC rules
  (including exemptions, notes and provenance IDs). Extraction-only siblings
  such as `sky130_lvt.py` fall outside the DRC pin. The hash covers all of
  `sky130_unmodeled.py` because it supplies rule provenance IDs, so edits to
  its disclosure data also change the pin. For multiple sources,
  hash the concatenation of each UTF-8 basename, a NUL byte and its raw
  32-byte SHA-256 digest, sorted by basename. A single source or an external
  deck file uses the SHA-256 of its raw bytes. An unreadable required source
  yields `content_hash: null`. The whole block is `null` when no deck was
  involved (e.g. LVS against a pre-extracted netlist).
  - **Pin migration (#6741).** Re-run reports that pin the named `sg13g2` or
    `sky130` deck and update expected pins when adopting the new identity.
    The `asap7`, `gf180mcu`, `sg13cmos5l` and external-file pins retain their
    values for identical source bytes. Historical release pins still resolve
    against the release that emitted them with `klt deck resolve`.
  - **Coverage disclosures are not guaranteed to be hashed (#6775).**
    `content_hash` identifies the rule set a run was checked against: the
    primary module plus the siblings its `DRC_RULE_SOURCES` names. The
    disclosure lists that ride in the same envelope —
    `coverage.unmodeled_rules` and `coverage.unchecked_rules` (`klt drc`), and
    `unmodeled_rules` on `klt deck info` / `klt deck rules` — record what the
    deck does *not* check, and are covered only to the extent their source
    text lives in a hashed file.
    Where a deck assembles one from a sibling module `DRC_RULE_SOURCES` does
    not name (e.g. `gf180mcu`, which declares none), adding or removing an
    entry can leave `content_hash` unchanged, so citing the hash next to such
    a list does not make that list drift-detectable. Compare the emitted list
    itself when its exact contents matter.
  - `path` (issue #3785, optional, additive) — the `{path, scope}` envelope
    (`env_provenance.repo_relative_path`) of the resolved deck file, emitted
    only by `klt sta` and `klt place-and-route` (whose Liberty is a resolved
    file). An out-of-repo file is `{"path": null, "scope": "external"}`; an
    absolute host path is never emitted. `content_hash` stays authoritative;
    no `schema_version` bump, and reports lacking it still verify.
  - `options` / `options_explicit` / `options_hash` (issues #595, #2394) —
    the deck-option flavour selection a run resolved, present together
    **whenever the deck declares at least one caller-selectable option**,
    whether or not the caller passed any, and omitted together for a deck
    that declares none (e.g. sky130). Carried by `klt extract` and `klt lvs`
    (and `klt pex`, which extracts through the former).

    ```json
    "deck": {
      "name": "gf180mcu",
      "content_hash": "sha256:<hex>",
      "released": true,
      "options": {"metal_top": "9K", "mim_cap": "cap_mim_2f0_m4m5_noshield", "poly_res": "3k"},
      "options_explicit": {"metal_top": false, "mim_cap": false, "poly_res": true},
      "options_hash": "sha256:<hex>"
    }
    ```

    `options` is the **fully resolved** set: every option key the deck
    declares, mapped to the value actually wired — *including* the ones the
    deck applied by default because `--deck-option` (CLI) /
    `request.layout.deck_options` (`klt lvs`) never mentioned them. It was
    caller-passed keys only before #2394, which made a run that silently
    took gf180mcu's `poly_res='1k'` default provenance-identical to a run
    against a deck with no selectable options at all — the stored device
    class and resistance were *one option's* answer with nothing in the
    record saying so.

    `options_explicit` maps the same key set to `true` (the caller pinned
    this value) or `false` (the deck applied its own default), so a consumer
    can tell "pinned `poly_res=1k`" from "defaulted to `1k`" with no
    deck-specific knowledge.

    `options_hash` is a `sha256:`-prefixed digest of the resolved **values**
    (never of `options_explicit`), so two records can be gated on
    option-for-option equality with a string compare instead of a structural
    dict diff. Two runs that extracted identically — one pinning a value,
    one defaulting to it — hash equal. `content_hash` deliberately does not
    fold options in: it pins the deck *source*, and is the value `klt deck
    resolve --content-hash` looks up in the released-deck history table.

    `klt extract --check --rerun` / `klt lvs --check --rerun` replay only
    the keys `options_explicit` marks `true`; re-pinning a defaulted key
    would replay over exactly the changed-deck-default drift those modes
    exist to surface. A report written before #2394 carries no
    `options_explicit` and reruns exactly as it used to (every key it
    recorded *was* caller-passed).

    See `docs/cli/extract.md`'s "Selecting a shared-geometry resistor
    flavour".
  - `klt drc --engine klayout` carries an `options` key of its own (issue
    #1306) when one or more `--deck-var NAME=VALUE` flags were passed,
    recording the extra `-rd` script globals that were threaded into the
    deck for that run (e.g. `{"feol": "true"}`) — likewise omitted entirely
    when no `--deck-var` was given. This path records **caller-passed keys
    only** and carries no `options_explicit`/`options_hash`: an external
    `.drc` file has no declared option surface klt can enumerate, so there
    are no defaults to resolve and none are invented. See `docs/cli/drc.md`.
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
    A release build (built on this version's tag from a clean tree) also
    vouches for its **own** decks (issue #2451): the history table is
    regenerated after tagging and so never lists the release that ships it,
    so a hash equal to the deck hash this build recorded *at build time*
    resolves to `true` instead of `false`. An installed distribution compares
    against those recorded hashes, not the current on-disk deck bytes, so a
    deck module edited after install does not self-identify. `klt deck resolve` reports such a hit with an additive
    `self_identified: true` field (absent otherwise). Source, dirty, or
    untagged builds never self-identify and still report `false`.
    Both `content_hash` and `released` require a run against an actual input
    layout to see; `klt deck info` (issue #1209) reports this install's own
    deck `content_hash` plus its structural device-class coverage
    (`ExtractionDeck.device_classes`) directly, with no input layout needed —
    see `docs/cli/deck.md`. `klt deck rules` (issue #2308) reports that same
    `content_hash` alongside the deck's actual rule values (`id`,
    `description`, `value_um`, `provenance`), so a constant read out of the
    deck for pre-layout arithmetic can be pinned to — and re-checked
    against — the exact deck revision it came from. `klt deck devices`
    (issue #2365) reports it alongside the *drawn-layer predicate set* each
    device class is recognised by (marker layer + per-terminal
    `requires`/`excludes`, with layer/datatype numbers), so "what must a
    layout draw for this class to extract?" is answerable without reading
    the deck module's Python source.
- `input` — the single input artifact the run was made against, as
  `{content_hash, role}` (mirroring `deck`'s `{name, content_hash,
  released}`). `content_hash` is a `sha256:`-prefixed hex digest of the file,
  so a stale committed report is a one-line diff against a freshly computed
  hash instead of being byte-identical to a current run. Populated by `drc`,
  `extract`, `pex`, and (since issue #1969) `lvs`, which records the hash of
  the layout side it compared — the same file and the same digest its own
  `environment.layout_sha256` records, in the `sha256:`-prefixed form this
  block uses. `lvs` deliberately populated `null` here until #1969, on the
  reasoning that `environment.layout_sha256`/`reference_sha256` already
  covered it; that was wrong for `klt signoff --manifest`, whose staleness
  gate reads `provenance.input.content_hash` generically across every check
  kind and cannot see an LVS-only field, so every `content_hash`-pinned "LVS
  clean" citation graded `stale_evidence`. `environment.layout_sha256` is
  unchanged (still a bare hex digest, no `sha256:` prefix). `sim` (since
  issue #2039) is populated the same way, duplicating
  `environment.netlist_sha256`'s digest of the netlist under test under
  `role: "netlist"` — `klt sim` was deliberately left `null` here by #2027
  (populating it before the `role` discriminator existed would have
  compared a netlist digest against a layout digest and refused every
  otherwise-consistent `drc` + `sim` bundle); #2039 closes that gap now that
  roles scope the comparison. `input` is still `null` for a verb with no
  single input artifact to pin this way.

  **A consumer may verify this hash against the artifact, not only against
  another claim** (issue #2196). `content_hash` is a *self-report*: it says
  what the producing run hashed, and comparing it against a manifest's
  pinned copy compares two statements about a revision, neither of which is
  the revision. Every verb that populates `input` also echoes the artifact
  it hashed under a top-level field of its own — `drc`/`extract`/`erc`'s
  `file`, `lvs`'s `layout` (the layout side; the reference netlist is pinned
  separately under `environment.reference_sha256`), `sim`'s `netlist`,
  `pex`'s `layout`, `sta`'s `def_path`/`verilog_path` — so the loop is
  closeable with no schema change: re-hash that file and compare. `klt
  signoff --manifest` does exactly this and reports the answer per citation
  as `input_verified: true | false | null` (see
  [`cli/signoff.md`](cli/signoff.md)); a verb that pins an `input` hash
  without echoing the path it covers leaves its consumers with `null` there,
  so **new verbs should echo the artifact path they hashed**. Inside a
  `klt signoff` report, a `generic`/`model-run` envelope that declares such
  a pathless hash is disclosed on its citation as the additive
  `input_verified_note: "declared_hash_unbound"` (issue #6403) — advisory,
  grading nothing — so the declared-but-never-checked case no longer reads
  as a binding.

  **That echoed path is the `{path, scope}` shape, not a bare string (issue
  #2659).** `input_verified` is only re-derivable where the echoed path still
  resolves, and a bare echo is anchored to the producing run's own
  filesystem: an absolute string names a directory that exists on exactly
  one machine, and a relative one is anchored to whatever cwd (or request
  file) that run happened to use. A committed `drc`/`extract`/`lvs` envelope
  cited by a block manifest therefore verified `input_verified: true` on the
  producing host and `null` from every other checkout, so `klt signoff
  --check` reported drift on `items.N.citation.input_verified` for a signoff
  record whose evidence was committed in full beside it. `drc`'s `file`,
  `extract`'s `file` and `lvs`'s `layout` now carry the same `{path, scope}`
  envelope `sim`'s `netlist` and `pex`'s `layout` adopted in issue #1261 —
  `scope: "repo"` is joined to the repo root `klt signoff` discovers from
  the evidence file's own location, which is what makes the citation
  portable. A **new** verb echoing an input path should emit that shape from
  the start. `lvs`'s `reference` is the one documented exception: it stays
  the verbatim request echo, because it is pinned by its own
  `environment.reference_sha256` digest rather than by `provenance.input`.
  - `role` (issue #2027) — **which kind of artifact `content_hash` covers**.
    One of:

    | `role` | Meaning | Verbs |
    |---|---|---|
    | `"layout"` | A layout stream (GDSII/OASIS, or a DEF) | `drc`, `extract`, `pex`, `erc`, `economy`, `power`, `array-check`, `ring-check`, `lef-abstract` (layout-bearing mode only; layout-free `--no-layout` hashes a `source` instead — see [`cli/signoff.md`](cli/signoff.md#provenance-consistency)), `lvs` (`layout.file` shape), `sta` (`def` request) |
    | `"netlist"` | A netlist (SPICE, or a gate-level Verilog netlist) | `lvs` (pre-extracted `layout.netlist` shape), `place-and-route`, `sta` (`verilog` request), `sim` (issue #2039), `characterize` |
    | `"source"` | HDL source, or the socket descriptor hashed by layout-free `lef-abstract` | `synthesize`, `equiv` (both its `yosys` and `yosys-sequential` engines), `formal`, `functional-verification`, `lef-abstract` (`--no-layout` only) |

    The field exists because the hash alone is kind-blind, and one verb can
    pin either kind: `klt lvs`'s `layout.file` shape hashes the original
    GDS/OASIS stream (byte-identical to what `klt drc` hashes for the same
    file), while its pre-extracted `layout.netlist` shape hashes a *SPICE
    netlist*. **A consumer comparing `content_hash` across reports must
    compare only within one `role`** — `klt signoff`'s
    `provenance_consistency` gate does, the same way it compares
    `deck.content_hash` only among checks naming the same deck. Before this
    field, that gate compared a pre-extracted LVS run's netlist digest
    against a DRC report's layout digest and rendered `refused` for a
    consistent `drc` + `lvs` pair describing one design: a false alarm, not
    caught staleness.
    Adding `role` is additive — the key appears whenever `input` is
    non-`null` and nothing else about the block changed. An envelope
    committed before #2027 carries no `role`; read it as `"layout"`, the
    field's only documented meaning at the time (`klt signoff` does exactly
    that, so archived evidence keeps participating in the layout-side
    comparison rather than being exempted from it).

    **`sim` shares the `netlist` bucket with three other verbs (issue
    #2039).** `klt lvs`'s pre-extracted (`layout.netlist`) shape,
    `place-and-route`, `sta`'s `verilog` request, and `sim` are all
    `netlist`-role, so a bundle citing `sim` alongside any one of the other
    three is compared and refused unless they pin the same netlist file.
    This is deliberate: a post-layout `sim` run of the exact netlist `lvs`
    verified should bind to that digest. A schematic-level `sim` — which
    will not share an extracted netlist's hash — should not be bundled with
    a `netlist`-role `lvs`/`place-and-route`/`sta` citation of a later design
    stage; see `docs/cli/sim.md`.

    **`klt wave build`/`klt wave query` are a deliberate, permanent
    exception (issue #2039).** Their `input` block is built by the native
    `klt-wave` Rust binary (`native/wave/src/build.rs`), not by this
    module's `_input_block`, and stays `{content_hash}` with no `role` key —
    this was decided in #2039 rather than left silently unaddressed. Neither
    existing `role` value is honest for what either verb hashes (`build`
    hashes a waveform trace, `query` a built store — neither a `layout` nor
    a `netlist`), so closing the gap would mean extending `INPUT_ROLES` with
    a new value (e.g. `trace`/`store`) and threading it through the Rust
    binary's `InputProvenance` struct plus both `wave-*-response.schema.json`
    schemas — real cost for a discriminator neither verb's envelope
    currently needs: `klt signoff` recognizes no `wave` check kind, so the
    role-scoped cross-check never sees either block regardless. If a future
    `klt signoff` check kind for waveform evidence is ever added, that is
    the trigger to revisit this decision, add the vocabulary, and update
    this note — not a reason to add it speculatively today.

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

`klt synthesize --check <report.json> --skip-deck` (issue #4315) adds an
optional top-level `skipped[]` (`{field, reason}` entries) to the cheap-mode
payload, naming checks deliberately not run; the key is absent otherwise. It is
an additive field — no `schema_version` bump. A consumer must treat a `match`
with a non-empty `skipped` as a partial verification.

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

A caller committing `klt drc`/`klt lvs`/`klt extract` output as evidence
does not need to
call `klt version` separately to detect drift: `provenance.klayout_version_mismatch`
(previous section) carries the same comparison inline on every report, with
a stderr warning when it is `true`. The warning prints the install command
for *your* build — for a released install the version-pin form, for a clean
dev checkout the git-commit form — derived from
`klt version --format json`'s `reinstall_spec` field (issue #5601). To
reproduce the exact engine a committed report was generated against:

```bash
# released install (the warning prints this when klt itself came from a pin):
uv tool install "klayout-tools==<klt version>" \
  --with klayout==<klayout_version_expected>

# dev/git checkout:
uv tool install "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<sha>" \
  --with klayout==<klayout_version_expected>
```

`uv`'s own `--with` flag overrides the resolved `klayout` version for that
install without any `klt`-specific flag — see
`docs/design/klayout-engine-version-pin.md` for why this, rather than a new
override mechanism, is the documented fix.

## Stream-stability contract: layout stream bytes are not stable across klt versions

Everything above contracts on JSON *report* payloads. The **layout streams**
(`GDS`/`OASIS`) that the write-side verbs produce are contracted separately,
and the contract is: **`klt` does not guarantee that a generated stream's
bytes are stable across `klt` versions.** The drawn geometry is meant to be
stable — every cross-version divergence on record is a reordering or a
bookkeeping change, not a geometry change — but *record order within a cell*
and the presence of non-geometry structures are KLayout-writer
implementation details that an upgrade may change.

Worked instance: #5499 (commit `7d1a262fb`, released in
[`CHANGELOG.md`](../CHANGELOG.md)'s `## 0.9.2 (2026-11-14)` section) pinned
`gen`'s layer table to a sorted order and stopped `write_layout()` from
emitting the `$$$CONTEXT_INFO$$$` structure. Streams written before and after
that commit are geometrically identical (zero XOR area on every layer) yet
differ byte-for-byte, so a consumer gate that regenerated a committed stream
and byte-compared it went red for a tool-churn reason indistinguishable from
a real generator regression.

**A raw byte hash (`sha256sum`, or `provenance.input.content_hash`) or a
`cmp` against a committed stream is therefore not a supported
reproducibility gate across a `klt` upgrade.** Within one pinned `klt` build
bytes *are* deterministic — stream write timestamps are suppressed — which is
what this repo's own within-version drift guard
(`tests/test_gen.py::test_dogbone_terminal_example_regenerates_byte_identical`)
relies on: a byte compare pins record order, which a geometry compare cannot
see. Across an upgrade, gate on one of the two supported
order-insensitive comparisons instead:

- **`klt diff a.gds b.gds`** ([`docs/cli/diff.md`](cli/diff.md)) — exit `0`/`3`
  answers "same layout?", ignoring stream timestamps and record order within
  a cell; equal `canonical_hash` with `stream_identical: false` is the
  precise "bytes reordered, layout unchanged" verdict. Two known limits, so
  the gate is not oversold: `klt diff` deliberately never ignores **cell
  names**, so a release that renames an emitted inner cell reports
  `equivalent: false` even with identical drawn geometry (open #5729; the
  live instance is #5627); and `canonical_hash` is this verb's own
  normalization — **not** interchangeable with the digest below (issue
  #5551), meaningful only against another `canonical_hash`.
- **`klt stats --format json | jq -r .geometry_digest`**
  ([`docs/cli/stats.md`](cli/stats.md) → "Geometry digest") — the
  `geometry_digest` field `klt stats` reports is a SHA-256 over the stream's
  **decoded geometry** that never reads the write timestamp, record order
  within a cell, cell-table indices or the stream format, and is the value
  `klt signoff --attest 2 --hash-kind geometry` is graded against. (Its
  computation costs runtime; #5583 proposes gating it behind a flag — phrase
  your gate around the field, which survives either way.)

Emission-affecting changes are called out in `CHANGELOG.md` as a practice,
and the #5499 entry above is the model — mechanism, every affected verb,
regenerated goldens — so a consumer pinning a version can see when an
upgrade will invalidate committed streams before taking it.

## Declared metric namespace (`metrics` block, issue #247)

Every verb's numeric fields are named ad hoc today (`violation_count`,
`device_count`, `instance_count`, ...), with no declared aggregation or
polarity semantics — nothing states how a metric rolls up across blocks
(sum? max?), whether a larger value is better, or which metrics are critical
enough to gate signoff. `src/klayout_tools/metrics.py` is a **data-only
registry** — declared name -> `{aggregator, higher_is_better, critical, domain}` —
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
  should mechanically gate signoff), plus `domain` (`"finite_number"` by
  default, or `"nonnegative_integer"` for declared critical error counts).
  Domains are independent of polarity: signed finite measurements remain
  valid values, while counts reject negatives, fractions, booleans, strings,
  nulls, arrays, and objects. A JSON `0.0` is a float, not an integer count.
- `klt signoff` fails malformed known-critical metrics with a structured
  `detail.critical_metric_blockers` entry containing `metric`, `value`,
  `higher_is_better`, `domain`, and `reason` (`expected_integer`,
  `expected_number`, `below_minimum`, or `non_finite`). The same detail is
  included on failed manifest tier items; their `citation` remains null.
  Ordinary threshold failures retain their existing blocker shape. Unknown
  optional metrics and legacy envelopes without metrics retain their grading.
- Signoff's envelope, manifest, fleet, and command-evidence readers reject
  `NaN`, `Infinity`, `-Infinity`, and numeric literals overflowing to infinity
  before grading. This includes stdin and nested fleet manifest files.
  Invalid top-level JSON produces the normal error envelope; unreadable
  manifest evidence leaves its item unmet. No nonfinite input is echoed into
  signoff output.
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

### GF180 native DRC deck selection

The native GF180 verdict exposes `native_deck.deck_selection`. Fatal deck
errors expose `error.deck_selection`. A tolerated abort carries the same
object in `engine_deck_errors.deck_selection`. A symlink to the installed
GF180 main script carries the same selection as its target.

When the parallel runner names failed workers, `error.abort.decks` lists their
distinct deck IDs. A tolerated partial report uses `engine_deck_errors.abort.decks`.
A single identified failure also supplies `abort.deck`. For the recognized GF180
3LM abort (`deck: "mslot1"`, `symbol: "metal4_drawn"`), the optional
`error.abort.hint` points to the [deck selectors and their coverage](cli/drc.md#gf180-3lm-deck-selection).
A tolerated report carries that hint at `engine_deck_errors.abort.hint`.

The parallel runner
can start several decks before reporting a failure.

| Field | Meaning |
|---|---|
| `requested` | The supplied `decks` string, or `null` for the PDK default. |
| `selected` | Registry IDs resolved by the installed PDK for this selector, in runner order. |
| `omitted` | Sorted default registry IDs excluded by selection. Explicit `all` has an empty omission array. |
| `narrowed` | Whether the resolved selector omits any default IDs. |
| `started` | Registry IDs observed starting in the full run log, in observation order. |
| `not_reached` | Selected IDs outside the observed starts when the start log accounts for the full selection or a sequential abort prefix. |

If the registry cannot be resolved, `selected`, `omitted` and `narrowed` are
`null`. Missing or incomplete start evidence leaves `not_reached` null.
The report records unknown scope in `coverage.unknown`. Known exclusions
appear in `coverage.skipped` as namespaced `native_deck` work IDs with
reason `not_selected`. Unreached selected IDs use reason `not_reached`.
Narrowed reports also expose a top-level `rule_selection` for signoff.
These arrays name native registry IDs. `coverage.rule_categories` names
RDB categories, which can differ from the deck that emits them.

## Path fields: envelope vs. plain string (issue #2073)

`klt` reports a path — an output artifact it just wrote (a netlist, a DEF, a
GDS, a script) or an input it read and hashed — in one of two shapes today,
and the two coexist deliberately rather than by oversight:

- **`{path, scope}` envelope** — `{"path": "<repo-relative POSIX path>",
  "scope": "repo"}` when the artifact lives inside the invoking repo,
  `{"path": null, "scope": "external"}` when it does not, `{"path": null,
  "scope": "absent"}` when the field has no artifact to report. Built by
  `env_provenance.repo_relative_path()` (`src/klayout_tools/env_provenance.py`)
  — see that module's docstring for the full rationale (a repo-relative path
  is committable evidence; a machine-specific absolute path is not).
  `scope` is not decoration: the resolution rule differs per value — a
  `"repo"` path must be joined to *the invoking repo's* root (the cwd `klt`
  was run from), except for the request-anchored fields whose table rows
  below name their own resolution root (#6762, #6764) — those join to that
  root instead; `"external"`/`"absent"` carry no usable path at all.
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

**One class of field is exempt from "do not force a migration" (issue
#2659): an input path a verb echoes beside a `provenance.input.content_hash`
that covers it.** That field is not merely nicer as an envelope, it is the
*only* shape in which it works at all for its stated purpose — closing the
`input_verified` loop from a checkout other than the producing one (see
"Shared `provenance` block" above). So `drc`'s `file`, `extract`'s `file`
and `lvs`'s `layout` moved to the envelope column below even though #2073
left them alone, and a new verb echoing a hashed input emits the envelope
from the start rather than being added to the plain-string table.

**The exemption stops at inputs, deliberately.** It does not extend to an
*output* path in the same response — `extract`'s `netlist_path`, `spef_path`
and `abstracted_cells[].lef_path` all stayed plain strings through #2659's
bump. The envelope carries no repo root of its own, so a `scope: "repo"`
entry is resolvable only against *one* root per envelope, and for a cited
envelope that root is the input artifact's repo (the one the evidence is
committed in). An output written outside it would degrade to `path: null`
and leave a caller with no machine-readable handle on the file it just asked
`klt` to write — a real loss for an output, and no gain for `input_verified`,
which reads the input field only (`signoff.py`'s `_INPUT_ARTIFACT_FIELDS`).

**`{path, scope}` envelope fields**, as of `dbb82fbe` (2026-10-02):

| Command | Field(s) | `SCHEMA_VERSION` | Originating issue |
|---|---|---|---|
| `synthesize` | `partial.netlist_path`, `partial.stats_path`, `netlist_path`, `script_path`, `run_script_path`, `restructuring.restructured_netlist_path`, `arithmetic.candidates[].measured.{netlist_path,script_path}` | 2 | #1844 |
| `pex` | `layout`, `netlist`, `reference_netlist`, `testbenches[].{request,netlist,schematic_netlist}` | 2 | #1261 (`testbenches[].netlist` additionally #2875); additive `measurement.mode: "extract-only"` (`--extract-only`, #3739) carries `reference_netlist: {path: null, scope: "absent"}` and empty `testbenches[]`/`delta[]` |
| `sim` | `netlist`, `environment.models_lib`, `environment.resume.checkpoint_path` | 3 | #1261 (`models_lib` additionally #1274) |
| `size` | `models_lib` | 2 | #1274 |
| `em` | `artifacts.{mesh,spec,report,log}` (retained layout runs also `layout`, `mesh_report`, `mesh_log`), each with additive `sha256` — the hashed envelope #4931 defines. All of them use the repo containing the request file (#6764): same request, same report from any invoking directory; present paths outside that repo (or no repo at all) have scope `external` | 1 | #5616 |
| `place-and-route`, `sta` | New OpenROAD log fields `engine_logs[]` (P&R), `engine_log` / `corners[].engine_log` (STA): `script_path`, `metrics_path`, `directory`, `stdout_path`, `stderr_path`, `metadata_path`. Since #6764 every field resolves against the repo containing the request file (never the invoking cwd); an `outputs.dir` outside that repo makes the P&R fields `external` | 1 | #2124 |
| `characterize` | `cell.netlist`, `cells[].cell.netlist` | 1 | #2502 (`cells[]` #2503) |
| `drc` | `file` (the hashed input layout; both engines) | 3 | #2659 |
| `extract` | `file` (the hashed input layout; both response builders) | 4 | #2659 |
| `lvs` | `layout` (the hashed layout side, `schema_version` 2, #2659); `reference` (the resolved reference netlist, `schema_version` 3, #4984; the `klayout-runset` engine keeps both as plain strings) | 3 | #2659, #4984 |
| `erc` | `file` (the hashed input layout; `spec` stays a plain string) | 2 | #2703 |
| `lef-abstract` | `file` (the hashed input layout; `socket` and `output` stay plain strings; `{path: null, scope: "absent"}` under `--no-layout`, #5714) | 2 | #2703 |
| `place-and-route` | `netlist` — the input netlist `provenance.input.content_hash` pins (additive, no bump) | 1 | #3751 |
| `sta` | `def_path`, `verilog_path`, `spef_path` (`null` when the request does not use that input) | 2 | #2879 (moved from the plain-string table; schema-1 evidence carries absolute strings) |
| `sta` | `sdf` / `corners[].sdf` — a written output: the envelope plus `content_hash` (`null` unless `request.sdf: true`; additive, no bump) | 2 | #2886 |
| `functional-verification` | `environment.{results_xml,test_log,build_log}`, `environment.initial_state.runs[].results_xml`, `mutation_testing.results[].log_path` (`{path: null, scope: "absent"}` when there is none, e.g. `results_xml` on the Verilog-bench path; `--format text` still prints the real paths), and (schema_version 3, #5784) the echoed *inputs*: `environment.sdf.file`, `environment.initial_state.netlist`, and `environment.source_defines` — the last retyped from an absolute-path-keyed object to a list of `{"source": <envelope>, "defines": {...}}` (an envelope cannot be a JSON object key, and a source outside the repo has no path to key on; `[]` when absent). The `environment.{results_xml,test_log,build_log}` and `initial_state.runs[].results_xml` envelopes also carry an additive `sha256` (`sha256:<hex>` of the file as written, `null` when it was not produced, #4931). These fields and `environment.sources[]` use the repo containing the base for relative paths in the request. By default the base is the request file's directory, or the invoking directory for stdin and inline JSON, whose envelopes can therefore differ between invocations. `--base-dir` overrides either default. The same request file with its default base, or a fixed absolute `--base-dir`, keeps the classification repo stable. A relative `-o/--outdir` follows the invoking directory, so an artifact's path or scope can still change. If the base is outside every repo, present paths have scope `external` (#6762). Version 4 adds `coverage.info_path` in the same hashed envelope, anchored to the request repo | 4 | #4279 (moved from the plain-string table; schema-1 evidence carries absolute strings), #5784 (the echoed inputs; schema-2 evidence carries absolute strings) |
| `array-check` | `file` (the hashed input layout) | 2 | #5788 |

**Plain-string fields** — intentionally unchanged:

| Command | Field(s) | `SCHEMA_VERSION` |
|---|---|---|
| `place-and-route` | `def_path`, `unrouted_def_path`, `gds_path`, `verilog_path`, `library_limit_report_path` (also in the route stage and per-corner entries, null when no report is available, additive in #7588) (plain strings by default; a request with `portable_paths: true` rewrites these and the other path fields to `{path, scope}` / `$PDK_ROOT/...` so the report is committable, issue #3820 — see `cli/place-and-route.md` → Response) | 1 |
| `place-and-route` (nested `spef_sta` block) | `spef_path`, `sdf_path` | (parent's `SCHEMA_VERSION` = 1) |
| `extract` | `netlist_path`, `spef_path`, `abstracted_cells[].lef_path` | 4 |
| `equiv` | `artifacts.{script_path,netlist_path,log_path}`, `artifacts.stage2_{script_path,log_path}` | 1 |
| `gen` | `gds_path` | 1 |
| `gen-compose` | `gds_path` (top-level and per-block) | 1 |
| `draw` | `gds_path` | 1 |
| `layout-plan-execute` | `gds_path` | 1 |
| `layout-plan-check` | `gds_path` | 1 |
| `lef-abstract` | `output`, `socket` | 2 |
| `arith-gen` | `verilog_path`, `reference_path`, `testbench_path`, `techmap_path`, `equiv_request_path`, `output_dir` | 1 |
| `functional-verification` | `trace.path` (absolute waveform filename, which can trigger a provenance lint finding) | 4 (`coverage.info_path` moved to the envelope table in #6005) |
| `yield-campaign` | `campaign.sim_report_path`, `campaign.request_path` | 1 (`yield_analysis.py`) |
| `characterize` | `liberty.path`, `liberty.roundtrip.probe_netlist`, `simulation.{testbench,request,report}`, `cells[].simulation.{testbench,request,report}`, `comparison.{ours,reference}` | 1 |

A field moving from this table to the envelope table above is a breaking
change to that field (a retype, `dict` in place of `str`) and earns a
`SCHEMA_VERSION` bump on its own module, following the `synthesize.py:246` /
`pex.py:160` / `sim.py:127` precedent — the same rule "Design: additive
envelope, not a wrapping envelope" states for the top-level envelope applies
per-field here too. There is no tool-wide signal for "which shape does field
X use today" beyond this table; re-check it (and this table's own `git log`)
before writing a consumer that assumes one shape tool-wide.

### Repo-relative provenance in *committed* artifacts (issue #2224)

The table above describes what `klt` **emits**. The rule for what a consumer
**commits** is stricter, and it is one rule:

> A committed artifact must contain no absolute host path. Reference an input
> by repo-relative path (`{path, scope}`) or by content hash
> (`provenance.input.content_hash` / `provenance.deck.content_hash`) — never
> by where it happened to live on the machine that produced it.

This is not style. A committed record embedding `/Users/<user>/…` regenerates
to *different bytes* on every other checkout, so any CI job that re-derives
the artifact and byte-compares it fails for a reason that has nothing to do
with the design — the failure mode 2AMLogic/gf180-surge#39 hit, and again in
a sibling repo the same week (SXT-013). A plain-string field from the table
above is the usual carrier: it is absolute by construction, so a consumer that
commits the envelope verbatim commits the absolute path with it.

`klt env-provenance lint-envelope FILE…` enforces the rule mechanically — it
walks a JSON envelope and fails (exit `3`) on any string field carrying an
absolute host path, naming the offending field by dotted path, with
`--allow-prefix` for genuinely machine-wide PDK/tool install roots. It is
deliberately broader than `klt env-provenance scan`, which answers the
*disclosure* question (home-shaped paths only): `/opt/build/out.def` names
nobody and breaks reproduction just as thoroughly. See
[`cli/env-provenance.md`](cli/env-provenance.md) → `lint-envelope` for the
full rule set, the comparison table, and the CI wiring. Since #2230 this
repo gates its own tree with it — `git ls-files 'examples/**/*.json' | xargs
klt env-provenance lint-envelope`, with an **empty** allow-list, in
`.github/workflows/ci.yml`'s `Lint (ruff)` job.

### Pinned derived artifacts: `pinned: true` (issue #2279)

Path hygiene is necessary but not sufficient. A committed artifact can be
path-clean and still carry bytes that depend on the *generating host's*
floating-point environment: a value computed through `math.pow`/`math.exp`
(or numpy `power`/`float_power`, whose `np.float32` result diverges from
double-pow-then-round under NEP-50) legitimately differs in the last ulp
between conforming libms, so the same generator on macOS/py3.14 and
ubuntu/py3.12 produces different committed bytes (the gf180-surge PR #49 /
PR #45 incidents). The rule this adds to the one above:

> An evidence-generating code path must be **integer-exact**, read its
> floats from a **committed pinned table** (a decimal literal parses to the
> same double on every host), or commit a **pinned artifact** — bytes
> written once, with the generator's identity + inputs recorded, so drift
> detection compares against the pin rather than silently regenerating.

A pinned artifact whose envelope is JSON carries:

| Field | Meaning |
|---|---|
| `pinned` | `true`. The committed bytes are the reference; nothing regenerates them silently. |
| `generator` | Repo-relative path of the generator script that produced the bytes. |
| `generator_sha256` | sha256 of that script's source at pin time — proves the committed generator is the one that produced the committed bytes. |
| `regenerate` | The command a human runs (on a trusted host) to regenerate before re-pinning. Recorded, never executed by tooling. |

The enforcement point is the artifact-determinism check
([`scripts/check_artifact_determinism.py`](../scripts/check_artifact_determinism.py)):
a manifest entry declares `float_discipline: "pinned-artifact"` plus the same
pin (`generator_sha256`, `regenerate`, per-artifact `artifact_sha256`); the
check then never regenerates that generator, verifies the pin instead, and
flags `stale_pin` (generator source moved after pinning) / `pin_drift`
(committed bytes moved after pinning) at exit `1`. Generators that do *not*
declare the escape hatch are statically scanned for host-libm float
computation and flagged (`host_float_op`). Full rules, the flagged/not-flagged
operator lists, and the negative controls:
[`guides/golden-artifact-determinism.md`](guides/golden-artifact-determinism.md)
→ "Float provenance rules for committed evidence".

## Path-only (non-result) keys (issue #2723)

`klt drc` and `klt extract` echo caller-supplied filesystem paths. Two analyses
of identical inputs run from different directories therefore never produce
byte-identical reports, even when every result is identical. A consumer that
hashes, diffs, or dedupes reports should drop the keys below rather than
hard-code names from observation. The lists are also importable constants in
`src/klayout_tools/_report_verify.py` (`DRC_PATH_ONLY_PATHS`,
`EXTRACT_PATH_ONLY_PATHS`, `PATH_ONLY_KEYS_BY_VERB`) with a helper
`strip_path_only_keys(report, verb)`; a test fails if a path-bearing key is
added without being registered. Paths use dotted notation, `*` standing for
any list index. The wildcard is meaningful only on this canonicalization
side: drift exclusion in `--rerun`/`--check` verdicts
(`diff_verdict_fields(exclude=...)`) matches by exact path, so a `*` entry
in an `exclude` set is rejected with a `ValueError` rather than silently
ignored (issue #5586) — a per-list-entry exclusion belongs in a
`strip_path_patterns` set passed via `committed_for_diff`/`fresh_for_diff`.

| Verb | Key | Meaning |
|---|---|---|
| `drc` | `file` | input layout, as provided by the caller |
| `extract` | `file` | input layout, as provided by the caller |
| `extract` | `netlist_path` | written SPICE netlist (explicit `--output`, else derived from `file`); `null` only under `--no-netlist` (issue #2850), where `netlist_sha256` is `null` too |
| `extract` | `netlist_normalized_sha256` | additive (issue #3407): SHA-256 hex of the flat `devices[]`/`nets[]` connectivity with anonymous-net (`$N`) numbering normalized away, so it is stable across `klayout` builds where `netlist_sha256` (written-bytes hash) is not; `null` exactly when `netlist_sha256` is; no `schema_version` bump |
| `extract` | `spef_path` | written SPEF (`--spef`), else `null` |
| `extract` | `subcircuit.path` | written sub-circuit netlist (`--subcircuit`) |
| `extract` | `abstracted_cells.*.lef_path` | LEF file an abstracted cell's pins came from |
| `extract` | `pdk.root` | `{path, scope}` of the PDK install (never absolute, but host-dependent) |
| `extract` | `timing` | run-scoped, not a path (issue #3181): wall-clock seconds for this run, differs on every run |

Separately, `provenance.klt_version`, `provenance.klayout_version`, and
`provenance.pdk.version` (`VOLATILE_PROVENANCE_PATHS`) differ across tool
builds even when every path matches, so reports are comparable across
checkouts only for the same tool build; `strip_path_only_keys(...,
include_volatile_provenance=True)` drops those too. The default report shape
is unchanged: these fields are still emitted as before (no `schema_version`
change). A `--path-root` option that records them relative to a caller-given
root is tracked separately.

## Run-specific fields (issue #5182)

Some envelope fields describe the run or the machine, not the layout/input.
A consumer that commits an envelope as evidence and compares it with a fresh
run should drop these before comparing; `--check --rerun` already does
(`_report_verify.VOLATILE_PROVENANCE_PATHS` is the single source of truth, and
a test keeps this table equal to it). Dotted paths:

<!-- run-specific-fields:begin -->
| Run-specific path | Varies with |
| ----------------- | ----------- |
| `timing` | wall-clock of this run (issue #3181) |
| `provenance.klt_version` | tool build |
| `provenance.klt_git_commit` | tool build (issue #4412) |
| `provenance.klt_reinstall_spec` | tool build (issue #4412) |
| `provenance.klayout_version` | KLayout build |
| `provenance.pdk.version` | PDK release stamp |
| `provenance.pdk.commit` | PDK upstream commit (issue #3786) |
| `provenance.pdk.linked_trees` | host PDK install layout |
| `provenance.pdk.ambiguous_sources` | other PDK roots on this machine (issue #2564) |
| `provenance.pdk.source` | how the PDK was resolved — search roots vs `$PDK_ROOT` vs `--pdk-root` (issue #5863) |
| `provenance.pdk.install` | store marker's record of what placed the PDK tree on this host (issue #8007) |
| `provenance.pdk.store_layout` | layout the PDK tree was found in on this host (issue #8007) |
<!-- run-specific-fields:end -->

No field is added or removed by this list; `schema_version` is unchanged.
Verb-specific extras (e.g. `engine_version` for the flow verbs) are
documented with their verb.

**Do not copy this table into a consumer.** `klt evidence canonicalize
--kind <verb> REPORT` emits the report with these paths (and the verb's own
extras and bookkeeping keys) already removed — the *static* view `--rerun`
diffs, as a document to commit and diff whole. `--rerun` additionally
tolerates directional, pair-dependent differences a single report cannot
express — see the caveats in
[`cli/evidence.md`](cli/evidence.md#klt-evidence-canonicalize) (issue
#5541).

## Verifying committed evidence: `--check` / `--rerun`

Eight verbs let a consumer verify that a previously committed `--format json`
report still reproduces, built on one shared implementation
(`src/klayout_tools/_report_verify.py`):

| Verb | Invocation | Issue |
|---|---|---|
| `drc` | `klt drc --check REPORT [--rerun]` | #1106 |
| `lvs` | `klt lvs --check REPORT [--rerun]` | #1106 |
| `extract` | `klt extract --check REPORT [--rerun]` | #1149 |
| `synthesize` | `klt synthesize REQUEST --check REPORT [--rerun]` | #2224 |
| `place-and-route` | `klt place-and-route REQUEST --check REPORT [--rerun]` | #2224 |
| `signoff` | `klt signoff --manifest\|--fleet M --check REPORT` | #2249 |
| `equiv` | `klt equiv REQUEST --check REPORT [--rerun]` | #2280 |
| `erc` | `klt erc --check REPORT [--rerun]` | #3060 |

**Seven of those eight also take `--rerun`** — every verb in the table except
`signoff`, which is in it as a `--check`-only verb because re-grading a
manifest *is* the whole verification (the `klt signoff` bullet below). So a
consumer asking "does my verb have a regenerate-and-diff mode?" reads yes for
`drc`, `lvs`, `extract`, `synthesize`, `place-and-route`, `equiv` and `erc`,
and no for `signoff`. A boolean `--check` elsewhere in the CLI is a different
mechanism and is deliberately absent above: `klt netlist --check` diffs a
regenerated netlist against the committed artifact at `-o`, and `klt evidence
render --check` is a doc-staleness flag — neither verifies a committed
`--format json` report.

All eight share one contract:

- **Cheap mode (`--check`)** re-derives the recorded input/deck content hashes
  and compares them. No engine runs.
- **Full mode (`--check … --rerun`)** re-runs the analysis and diffs
  verdict-bearing fields against the committed report.
- **The view full mode diffs is available on its own** (issue #5541): `klt
  evidence canonicalize --kind <verb> REPORT` emits a report reduced to the
  static fields full mode compares, so a consumer that commits a report and
  guards it with its own regenerate-and-diff check does not re-implement
  the exclusion list. The same definition feeds both, so they cannot
  diverge. Equal canonical views imply no drift; the converse does not
  hold, because full mode's comparison is directional and pair-dependent
  (committed-only `provenance` leaves are `not_compared`, issue #5866;
  `drc` across the #5660 version boundary; the `lvs` pair tolerance) —
  documented with the sub-verb in
  [`cli/evidence.md`](cli/evidence.md#klt-evidence-canonicalize).
- **`klt signoff` has one mode, not two** (issue #2249): re-grading the
  manifest *is* the whole verification — there is no engine run to hold back
  — so `--check` alone produces the full-mode `{status, drift, fresh}` shape
  under `mode: "check"`, and it takes its manifest the way the flow verbs
  take their request (a tier report echoes verdicts, not the manifest).
- **`status` is two-valued**: `"match"` (exit `0`) or `"drifted"` (exit `3`,
  naming which hash moved or which fields changed). A report that cannot be
  verified at all — missing, unparseable, missing the field needed to re-run —
  is exit `1` with an error envelope. **A missing recorded value is never a
  pass**: `hash_check()` renders `expected: null` as `match: false`, so a
  report predating the field it would be checked against renders `"drifted"`,
  never a false `"match"`.
- **A `{path, scope}` input field re-hashes from any checkout (issue
  #2659).** `drc`/`extract`'s `file` and `lvs`'s `layout` are the
  `{path, scope}` envelope, and both modes resolve `scope: "repo"` against
  the repo root discovered from **the committed report's own directory** —
  the identical resolution `klt signoff`'s `input_verified` gate applies to
  the same shape — so a report committed beside its inputs verifies from any
  clone and from any working directory. A `scope: "external"`/`"absent"`
  entry names no path by construction: `--check` reports it as the same
  unverifiable `actual: null` an absent field gets, and `--rerun` as the
  same clean "no `file`/`layout` field to rerun" error, never a traceback.
  A report predating #2659, whose field is a bare string, keeps the old
  anchoring described in the next bullet exactly as before.
- **An explicit input restores the non-repo workflow (issue #2699).** For
  `drc`/`extract`/`lvs`, the caller may supply the echoed input alongside
  `--check`/`--rerun` — `klt drc <file> --check REPORT`, `klt
  extract <file> --check REPORT`, `klt lvs --check REPORT --layout PATH` —
  for use when the report cannot locate the input (`scope: "external"`) or
  the grading clone is laid out differently. **Precedence is one rule for all
  three: the explicit input always overrides the report's recorded value**
  (not only when that value is unresolvable); it is hashed and compared to
  the recorded `content_hash`/`layout_sha256`, so a file that disagrees
  reports `drifted`. With no explicit input the behaviour is unchanged
  (additive; no `schema_version` bump, no new response field).
- **An unresolvable input path is named, not silently `null` (issues #2595
  for `lvs`, #6466 for `drc`, #6655 for `erc`).** A
  committed report that echoes an input back exactly
  as the request gave it re-hashes a *relative* echoed path against the
  **current working directory** of the `--check` invocation — a deliberate,
  shared convention, not an oversight, and unchanged here. Since #2659 above
  this covers only the echoes that are still a bare string: `erc`'s `spec`,
  the `reference` and `deck_file` of a runset-engine `lvs` report, and any
  pre-normalisation report's bare-string field. A non-runset `lvs` report
  echoes its `reference` as the `{path, scope}` object, like its `layout`,
  and `erc`'s `file` is that object too (#2703), so those re-anchor to the
  repo root like `drc`'s `file`. But a
  path that resolves to no existing file
  re-hashes to `null`, which alone reads as "the recorded hash moved". `klt
  lvs --check` (its layout, reference and runset-deck lines), `klt drc
  --check` (its `provenance.input.content_hash` line, on a top-level report
  and on each `reports.curated.`/`reports.native.` side of an `--engine both`
  envelope) and `klt erc --check` (its `provenance.input.content_hash` and
  `provenance.spec.content_hash` lines) therefore attach an
  **additive, optional** `input_not_found`
  block to such a `checks[]` entry. The block has five keys on `drc` and
  `erc` (`path`, `resolved`, `found_relative_to_report`, `resolved_against`,
  `hint`); each of `lvs`'s three entries (layout, reference, runset deck)
  adds a sixth, `resolved_repo_root` (#5845): the repo root a repo-scoped
  echo resolved against, `null` for a bare-string echo or an explicit
  override. `expected`/`actual`/`match`/`status` are
  unchanged, and the key is absent on a genuine content mismatch, on an
  echo that names no path (`scope: "external"`/`"absent"`, or no field) and
  on a path that resolves to an existing file. An explicit input override
  (`klt drc <file> --check`, `klt lvs --layout`/`--reference`) still wins and
  is the path diagnosed when *it* is missing. `hint` is `null` unless the
  input sits beside the report. When set, it always names the report's
  directory to run `--check` from, and adds an "or pass …" clause only
  where the verb has an override: `lvs`'s layout and reference and `drc`'s
  input. `lvs`'s runset-deck line (`--deck-file` is refused with `--check`)
  and both `erc --check` lines (`--check` is mutually exclusive with ERC's
  positional inputs) have none, so their hints name only the report's
  directory. `klt drc --check`'s other echoed-path line,
  `provenance.waivers.content_hash`, and `klt erc --check`'s deck line carry
  no such block (they are not routed through the same resolution; the deck
  line resolves packaged decks by name, and DRC's deck line has its own
  `deck_not_found` block). Additive by the envelope
  rules above — no `schema_version` bump, and a consumer that never reads the
  key sees the bytes it saw before. See
  [`cli/lvs.md`](cli/lvs.md)'s "`--check` / `--rerun`",
  [`cli/drc.md`](cli/drc.md) and
  [`cli/erc.md`](cli/erc.md).
- **Tool identity is excluded from the diff**, and only tool identity:
  `provenance.klt_version`, `provenance.klayout_version`,
  `provenance.pdk.version`, plus (flow verbs only) `engine_version` — the
  Yosys/OpenROAD build string, which stands in the same relation to a flow
  verb that the KLayout engine build does to `klt drc`. The engine *identity*
  (`engine`) is not excluded: swapping engines is a different run. For
  `signoff` the same rule lands on the field *its* reports carry tool
  identity in — the whole `build` block (issue #2249), which `--check`
  additionally reports (report-only, never touching `status`) as the
  additive `build_skew` field: `same`/`build_only`/`ruleset`/
  `unverifiable`/`unknown` over the block's fields, with the difference
  states naming what moved (issues #5868, #5852).
- **A provenance field the fresh run does not emit is `not_compared`, not
  drift (issue #5866).** A committed report may carry `provenance` keys the
  re-running `klt` never writes — spliced in by a downstream wrapper
  (`provenance.gds_sha256`), or emitted by a newer/older version of the
  tool. Such a key is collected into an additive, optional `not_compared`
  list of `{field, committed}` entries (dotted field names exactly like
  `drift[]`'s; emitted only when non-empty, so every existing payload
  stays byte-identical) and influences neither `status` nor the exit code:
  only a field both reports actually carry can drift. The rule is
  deliberately provenance-only and one-directional — a verdict-bearing key
  missing from the fresh report (`violation_count`, `violations`,
  `coverage`, …) still drifts, because dropping a verdict-bearing field is
  exactly the regression the diff exists to catch; a key the *fresh*
  report grew (schema evolution in the other direction) still drifts; a
  fresh report missing its entire `provenance` block still drifts; and
  the carve-out covers scalar keys only — a committed `provenance`
  *sub-block* (a dict or list, e.g. a `regeneration` claim the fresh run
  declined to adopt) absent from fresh is one absent verdict-bearing
  structure and still drifts. The `provenance.regeneration` subtree
  (`klt signoff --attest`'s caller-supplied claim) is excluded from the
  carve-out entirely — its `hash_kind` leaf is written only when it
  differs from the default (`bytes`), so absence there encodes a default
  *value*, not an unproducible field, and any leaf disagreement inside a
  declined claim still drifts (the explicit `--hash-kind` cross-check of
  issue #5572). One further case files a field a report *does* carry
  there: `klt drc --check --rerun` reports a committed
  `provenance.klayout_version_mismatch` as `not_compared` when the
  committed `--engine klayout` report predates issue #5660 (no
  `provenance.klayout_module_version`) — that change redefined the flag's
  input from the pip wheel to the standalone binary, so a pre- and a
  post-change value are not comparable. The
  `--format text` renderer lists the skipped field names in a
  `not compared:` block, so a non-zero exit always means "something that
  could be compared did not match".
- **`provenance.pdk.source` is excluded as resolution provenance (issue
  #5863).** It records *how* the PDK install was found (search roots vs
  `$PDK_ROOT` vs `--pdk-root`), in the shared
  `VOLATILE_PROVENANCE_PATHS` set: the identical install (same
  `provenance.pdk.version` hash) resolved through a different mechanism on
  another host must not read as drift — the same rationale one field over
  for `provenance.pdk.ambiguous_sources` (issue #5182).
- **Flow-verb output paths compare by file name (issue #5863).** A consumer
  commits lint-clean evidence with output paths rewritten repo-relative or
  to `~/`/`$PDK_ROOT/...`-form (what `env-provenance lint-envelope`
  requires), while the fresh side of a `--rerun` diff echoes absolute host
  paths — the directory is host- and scrub-shape-specific and never
  verdict-bearing. `klt place-and-route --check --rerun` therefore reduces
  every output-path field (`netlist`/`provenance.deck.path`/`layer_map.path`
  `{path, scope}` envelopes, `def_path`/`unrouted_def_path`/`gds_path`/
  `verilog_path` at the top level, under `power.placed` and inside every
  `corner_runs[]` entry, `corner_runs[].output_dir`, `macros[].lef`/`.lib`,
  `spef_sta.spef_path`/`sdf_path`) to its file name on **both** sides
  before diffing. The identity check stays: a different file *name* drifts,
  and so does an artifact going `null` ↔ path (missing or newly appearing).
  Message prose is not reduced, so it must not carry a path in the first
  place: the DEF-derived `power.placed.unavailable_reason` strings (and the
  `warnings[]` that quote them) name the DEF by file name only (issue
  #6146), so a lint-clean envelope reproduces exactly. The other flow
  verbs (`synthesize`) and the analysis verbs
  (`drc`/`lvs`/`extract`/`equiv`/`erc`) have not needed this and are
  unchanged; a follow-up issue covers them if reproduced.
- **`klt signoff` excludes one more surface, and reports it separately**
  (issue #2526): the report's verbatim quotation of the checklist doc — each
  `items[]` entry's `title`/`text`/`notes`, and `source_doc_content_hash`.
  A tier report inlines that prose, and a `klt` version pin does not pin the
  doc, so an upstream *rewording* alone used to render `"drifted"` —
  indistinguishable from a real grading change, which is the one distinction
  the mode exists to make. It is excluded from `status` only: every excluded
  field that moved is still reported, in a `doc_drift_fields` list (same
  `{field, committed, fresh}` shape as `drift`) with a `doc_drift` boolean,
  so a consumer can warn on "the yardstick was reworded" while failing only
  on "the evidence moved". A checklist that *gained, lost or renumbered* an
  item still fails, via the counts and per-item `id`s that stay compared.
  See [`cli/signoff.md`](cli/signoff.md)'s "A reworded checklist is not
  evidence drift".
- **`klt signoff --check` also carries a report-only `build_skew`** (issue
  #5868; additive, no `schema_version` bump): `{state, fields}` where `state`
  is `"same"`, `"build_only"`, `"ruleset"` (`build.grading_ruleset_id`
  differs) or `"unknown"` (the committed report has no `build` block), and
  `fields` lists each differing `build.*` field in the `{field, committed,
  fresh}` shape of `drift`. It never affects `status` or the exit code.

**Build identity is route-dependent, so committed reports must not be
byte-compared (issue #2249).** A `klt signoff` tier/fleet report's `build`
block (and `provenance.klt_version` elsewhere) records the state of the tree
the running install was *built from*, at build time — not a property of the
commit. Two byte-legitimate installs of the **same pinned commit** can
therefore emit different bytes for identical evidence: a
`git+https://…@<sha>` install records real git facts (`.devN+g<sha>`), while a
`pip install` of a source tarball of that same `<sha>` has no `.git` at build
time and honestly records none (`.dev0+unknown`, `git_commit: null`,
`is_release: null`). The second case cannot be fixed by tightening `dirty` (issue #2248) —
the facts were never present to record. So "does this committed evidence
still hold" is answered by `--check`, which excludes exactly the identity
surface, **not** by `diff`-ing a committed report against a fresh render. See
[`cli/signoff.md`](cli/signoff.md)'s "`build` describes the *install*, not
only the commit".

**The flow verbs take the request back.** `drc`/`lvs`/`extract`/`erc` echo
their own inputs into the report, so `--check REPORT` is self-sufficient. A
`synthesize`/`place-and-route`/`equiv` report echoes *outputs* and
provenance hashes but never the request document or the source paths it
resolved — so those three keep their positional `REQUEST` argument
alongside `--check`, and the question answered becomes "does this
committed record still reproduce *from this request*". Run-scoped
bookkeeping is canonicalized out of the `--rerun` diff on both sides
(`synthesize`'s per-run `run_id` and the artifact paths under it;
`place-and-route`'s `engine_logs[]`, keyed by a fresh `uuid4` per
OpenROAD invocation; `equiv`'s `elapsed_s` and its `resume` block,
issue #2280) — without that, `--rerun` would report `"drifted"`
unconditionally. `place-and-route --resume` (issue #4411) adds an optional
`resume: {resumed_stages: [...]}` object, present only when the flag was
given (see [`cli/place-and-route.md`](cli/place-and-route.md), "Resuming an
interrupted run"). `klt signoff` is the same shape one level up: a
tier/fleet report echoes graded verdicts, never the manifest, so
`--check` sits beside the `--manifest`/`--fleet` argument it re-grades.
Per-verb detail:
[`cli/drc.md`](cli/drc.md), [`cli/lvs.md`](cli/lvs.md),
[`cli/extract.md`](cli/extract.md),
[`cli/synthesize.md`](cli/synthesize.md),
[`cli/place-and-route.md`](cli/place-and-route.md),
[`cli/equiv.md`](cli/equiv.md),
[`cli/erc.md`](cli/erc.md),
[`cli/signoff.md`](cli/signoff.md).

## Functional-verification run identity (**proposed** — not yet emitted, issue #2097)

> **Status: contract ratified, producer not implemented.** The v1 pilot
> ([#2097](https://github.com/2AMLogic/klayout-tools/issues/2097),
> Candidate A, operator-ratified 2026-09-22) ships the contract, reference
> fixtures and contract tests — *not* a producer. `klt
> functional-verification` emits no `run_identity` field today, and this
> document must not pretend otherwise. The authoritative contract record
> is
> [`docs/design/functional-verification-run-identity-v1.md`](design/functional-verification-run-identity-v1.md).

When the producer follow-up lands, `run_identity` will be an **additive,
opt-in** sibling block on the functional-verification envelope
(`schema_version: 1`, `input_manifest`, `input_sha256`, plus a
non-identity `input_locators` block): by the envelope rules above it earns
no `schema_version` bump on the verb, because every request/response valid
before is still valid and means what it meant — the block appears only
when a request explicitly asks for identity collection via the proposed
`options.evidence` object, exactly like the pre-existing additive
`options.trace`/`environment.sdf` fields (issue #1002's precedent).

Compatibility boundaries fixed by the ratified contract:

- **Request side**: no `options.evidence` → byte-identical behavior today;
  a request using the new value set is rejected (exit 1, error envelope)
  by an older `klt` rather than silently mis-executed.
- **Hash roles stay disjoint**: `input_sha256` is domain-separated by the
  `klt-functional-verification-input-v1` prefix and must never be
  compared with a `provenance.input` layout/netlist/DEF content hash
  (#2027/#2058's roles), a deck hash, or another run's digest; it is not
  added to `provenance.input` in v1.
- **Closure is structurally `partial` in v1** and the manifest's
  `closure.reasons` name the uncovered classes (Python/native imports,
  ambient environment/external reads, unbound tool binaries, plus
  missing-seed/tool-identity/dirty-build disclosures as applicable). A
  declared-mode `complete` claim is rejected by the contract checker, not
  scored.
- **Consumer verification is separately scoped**: a caller-owned expected
  manifest, current input-root re-hash, externally held report/artifact
  byte pins, and explicit `allow_partial_inputs` acceptance (default
  false). Missing legacy identity cannot satisfy the opt-in policy;
  #2094/#2096's independent checks are not replaced by it.

The reference checker (`tests/helpers/identity_contract_v1.py`) and the
golden vectors it validates are the executable form of this section until
the producer follow-up moves that core into `src/` with tests.

## Checked-work coverage (`coverage.schema_version: 1`)

The [checked-work coverage contract](coverage-contract.md) defines required
common fields, stable work identities/reasons, applicability, unknown engine
coverage and Phase 1 refusal semantics. The machine schema is
[coverage.schema.json](schemas/coverage.schema.json); Python validation also
enforces identities being disjoint across work categories.

Curated DRC, external KLayout DRC, ERC antenna, power EM, simulation sweeps
and PEX comparisons adopt it in #2108. They preserve their existing coverage
fields alongside the common version. Known zero checks produce
`status: "not_checked"` (exit 4) unless an actual failure/error already wins.
Uninstrumented external KLayout reports have unknown execution, regardless
of whether RDB categories exist: absent findings their status is
`coverage_unknown` (exit 4), never `clean`. DRC envelope v2 versions the
correction to `coverage.rules_checked`; category declarations move to
`coverage.rule_categories`.

**Caller-vouched rule-category assertions** (issues #2697, #2806) are the one
opt-in exception: the additive top-level `coverage_assertion` field, present
only when the caller supplied an assertion, records which one was used and
whether it held. Two variants, distinguished by `kind`:

- `{"kind": "expected_rule_categories", "expected": <int>, "observed":
  <int>, "satisfied": <bool>}` — `--expect-rule-categories N` (unchanged).
- `{"kind": "expected_rule_category_names", "expected_names": [<str>],
  "observed_names": [<str>], "missing_names": [<str>], "unexpected_names":
  [<str>], "satisfied": <bool>}` — `--expect-rule-categories-file PATH`. All
  name lists are sorted and deduplicated. This variant never carries the
  integer `expected`/`observed` keys, so a consumer must dispatch on `kind`
  rather than read `expected` blindly.

A satisfied assertion of either kind marks the declared categories checked
and clears the `unmeasured_rule_execution` sentinel; it is caller-supplied
evidence, not proof of execution (see `docs/cli/drc.md`, "Engine" →
`"klayout"`).

For `--engine klayout`, optional `violations[].values` contains the
verbatim XML character data of each non-geometry RDB value in document
order, including any tag such as `[#RATIO]`. Only findings with such
values carry the field. `--engine both` retains it in
`reports.native.violations[]`. `--check --rerun` compares the strings.
They provide evidence only and do not affect the coverage verdict,
`coverage_unknown`, or `--expect-rule-categories`.

**Deck stdout as additive evidence** (issue #6613): every `--engine
klayout` DRC report carries a top-level `deck_output` object —
`{"stdout": <str>, "truncated": <bool>, "line_count": <int>}` — holding
the deck's own captured stdout (tail-capped at 500 lines, temp work-dir
paths scrubbed to a placeholder; `line_count` counts the whole stdout).
Like `coverage_assertion` it is additive and optional from a consumer's
perspective, but unlike it the field is always present on a klayout-engine
report that produced a verdict. It is execution evidence to cite, never a
status upgrade: a zero-finding, zero-category run stays `coverage_unknown`
(exit 4), `--expect-rule-categories 0` remains rejected, and klt parses
nothing from the log. `--rerun` excludes it from the drift diff.

`nothing_checked` and `nothing_checked_reasons` remain available. In a v1
coverage block, zero means **known** zero actual checked work. It is not a
synonym for partial, unknown, attempted, or inapplicable work. Real errored
runs can have zero checked work, while retaining their error verdict.
Missing legacy coverage does not assert full coverage. Old explicit
`nothing_checked: true` remains nonqualifying, and old external KLayout
category lists do not establish execution. Optional Verilator coverage
percentages remain distinct from executed functional-verification tests.

`klt signoff` refuses zero, unknown and malformed common coverage in plain,
numbered and compound evidence while preserving actual failure precedence.
The full [compatibility mapping and public-path inventory](coverage-contract.md)
also records which per-path adapters remain outstanding.

**One envelope may carry more than one scope** (issue #2179). `klt erc`
reports an antenna scope at `coverage` and a connectivity scope at
`erc_coverage`, each with its own roll-up (`status` and `erc_status`
respectively) — it performs two independent bodies of checked work, and only
the antenna one needs a PDK limit table. The refusal above is a question
about the *artifact*, so a consumer reads every scope an envelope declares
before calling it empty; the exit code still follows the top-level `status`
alone. See [`cli/erc.md`](cli/erc.md)'s "Two verdicts".

### The common rollup rule (#2109)

One decision table maps a producer's own outcome plus its coverage state onto
eight rows — `errored`, `failed`, `malformed`, `unknown`, `zero`, `partial`,
`legacy`, `full` — each with a stable `reason` code and an exit code (`0`
success, `3` findings, `4` no verdict reached). Errors and real failures are
decided before coverage, so a defect is never masked by a coverage gap and a
gap is never dissolved by a clean status.

Successful checks alongside a nonempty list of skipped *requested* work are
`partial`: a real, exit-0 result that is **not** the verb's unconditional
success token. Adapters report `f"{success}_partial"` (`clean_partial`,
`pass_partial`) for it. Curated DRC is the first adopted adapter (#2110):
`klt drc --engine curated` derives its `status` and exit code from this
table, reports `clean_partial` for the partial row, and classifies a rule
skipped for an absent input layer as `inapplicable` when running it could
not have reported a violation — see
[the contract](coverage-contract.md)'s producer mapping. Only `full` asserts positively established complete
applicable coverage; missing common coverage (`legacy`) keeps grading by the
verb-specific rules that always governed it and is never counted as proof of
completeness. Optional Verilator code-coverage percentages are `legacy` and
are never a requested-check claim. The
[full table, field names and adapter migration contract](coverage-contract.md)
are normative.

## A success payload that is itself an input document (`klt yield-samples`, issue #2563)

Most payloads are reports a person or tool reads. `klt yield-samples`'
payload is also a **document another verb consumes**: its `--format json`
output is a `klt yield` sample-set document, redirected to a file and passed
to `klt yield` unchanged. It needs no exception to this contract, because
the envelope is additive and flat:

```json
{
  "schema_version": 1,
  "measurements": [ "... sample-set entries, see docs/cli/yield.md ..." ],
  "derivation": { "nominal": {"path": "...", "content_hash": "sha256:..."},
                  "negative_control": {"path": "...", "content_hash": "sha256:...", "description": null},
                  "measurements": [ "... per-measurement control disclosures ..." ] }
}
```

- The success payload is emitted through `emit_success()` like every other
  verb; `schema_version` (this command's own, `1`) sits beside the
  sample-set's `measurements`, and `derivation` is an additive audit block.
  `klt yield`'s sample-set reader reads `measurements` and ignores both
  extra top-level keys, so **no result wrapper and no extraction step** sits
  between the two commands.
- Errors use the shared error shape below (`error.command:
  "yield-samples"`) on stderr with stdout empty, exit `1`; a missing
  `--negative-control` is an argparse usage error, exit `2`. Because stdout
  is empty on failure, `klt yield-samples ... --format json > samples.json`
  leaves an empty file rather than a partial sample set — still, check the
  exit code before using the file.
- The output is deterministic (no timestamps), so a CI job can re-derive and
  byte-compare a committed copy. Field-level documentation, the matching
  rules, and the signoff freshness consequence are in
  [`cli/yield.md`](cli/yield.md#deriving-a-sample-set-from-two-klt-sim-reports-klt-yield-samples).

## A versioned *input* contract: the attestation inventory (`klt signoff --attest`, issue #2844)

`klt signoff --attest {1,9,10} --inventory FILE` reads a document a block
repo authors and commits: the **attestation inventory**
(`"kind": "t1-attestation-inventory"`). Unlike a request document consumed
once, it is a public, versioned contract in its own right, because the
envelope `--attest` emits pins its raw-byte hash and a fleet of block repos
depends on its meaning staying fixed:

- The inventory carries its own `schema_version` (currently `1`),
  independent of `klt signoff`'s output `schema_version`. A build refuses
  (exit `1`) any version it does not read rather than guessing at its
  semantics.
- Within a version, the inventory is **closed**: unknown top-level fields
  are refused, so a typo cannot silently drop a check. Adding a new optional
  field or a new item therefore lands as a new inventory `schema_version`,
  not as an additive change. A build that adds version `2` keeps reading
  `1`.
- The emitted envelope is an ordinary `generic` evidence envelope plus an
  additive `attestation` block (`producer`, `inventory_schema_version`,
  `hash_algorithm`, `check_count`, `passed_count`, `checks[]`). Its fields
  follow this document's usual additive rules. The tier record's flat
  `artifact_binding.attestation.inventory_schema_version` is resolved from
  that key when present, else from `attestation.inventory.schema_version`
  (the item-2 `--input`+`--inventory` shape, issue #5571), so a committed
  envelope in either shape discloses its inventory contract without a
  re-attest.
- Hashes are `sha256:<hex>` of raw bytes (`hash_algorithm:
  "sha256-raw-bytes"`). Introducing any normalisation would be a new named
  algorithm recorded in the envelope, never a silent change to this one —
  the one such algorithm is `sha256-canonical-yaml-v1` (issue #5862): an
  inventory `hygiene` entry `{"attest": "yaml", "select"?}` records it,
  with `attest` and `select`, beside that check's own `content_hash` (the
  digest of the canonical parse — type-tagged keys, sequences recursed
  element-wise, sorted, compact — of the whole file or the `select`
  subtree; a node with no deterministic canonical form, e.g. `!!set`, is
  an error naming the path) plus an informational
  `raw_content_hash`, which the `--check` drift diff excludes and reports
  as a live note instead. The envelope-level `attestation.hash_algorithm`
  stays `sha256-raw-bytes` for every other check, and no inventory
  `schema_version` bump carries the entry: within version 1 the per-entry
  accepted keys are part of the `hygiene` entry's documented shape, and a
  build that does not read `attest: "yaml"` refuses it explicitly (exit
  `1`, naming the accepted values) rather than byte-hashing it.

Field-level schema, path rules, examples and exit codes:
[`cli/signoff.md`](cli/signoff.md#attesting-items-1-9-and-10---attest-issue-2844).

Two behaviours worth knowing when running `--attest` or grading a manifest:

- **Declared commands see the caller's `PATH` — except that `klt` stays the
  attesting `klt`.** Inventory commands run with the invoking environment,
  except that when `klt` itself runs inside a venv
  (`sys.prefix != sys.base_prefix`, as under `uvx`/pipx) that venv's `bin`
  directory is removed from `PATH`, so a bare `python3` resolves to the
  repo's interpreter, not the tool's. The one name the stripping must not
  remove is restored: a run-scoped shim directory holding a single `klt`
  symlink to the venv's entry point is **prepended** to the declared
  command's `PATH`, so `klt` on it is always the `klt` running the
  attestation — the venv's when attesting from a venv, the host's when not.
  The shim is prepended and so takes precedence over a `klt` the caller
  placed earlier on `PATH` (issue #5565). No other executable from the
  venv's `bin` (`python`, `python3`, `pip`) is re-exposed; the per-run shim
  path never enters the envelope. Outside a venv the environment is
  unchanged.
- **Manifest evidence paths are cwd-relative.** A relative `file` evidence
  path resolves against the process cwd, not the manifest's directory. When a
  file-backed entry grades `unreadable_evidence`, the item's `detail` carries
  `path` (the resolved absolute path) and `cwd` (the process cwd) — additive
  fields; no existing field changes.

## Error shape

Under `--format json`, errors are also JSON — not a plain-text line — written
to **stderr**, with **stdout left empty**. This means a caller never needs to
inspect stdout content to tell success from failure under `--format json`;
the exit code alone is authoritative.

**Warning delivery (issues #6935, #7203).** The CLI defers `klt`'s
human-readable warning lines for the duration of a `--format json`
invocation; every emitter that can fire ahead of an application error
routes through the one deferral (`klayout_tools._warn`): the engine-version
mismatch warning (see [shared `provenance`](#shared-provenance-block)); the
`klt sim` advisories for ambiguous PDK installs and an ambient ngspice init
file; the `klt sim` batch-planning warnings (runner-version,
runner-feature and job-ceiling checks under `warn`, features the client
could not verify, an over-the-serial-bound job split, and a batch receipt
that could not be written); the `klt sim` request-lint and disclosure
warnings (`--meas-at` lint echoes, the local worker-pool clamp notice,
unseeded transient-noise sources, and the device-multiplier findings
summary); the `klt extract` job-ceiling warning; `klt netlist` batch
warnings; and the `klt erc`/`klt power` stackup and `devices[]` warnings
(an empty `label_layer`, a body layer whose carve-out subtracted nothing,
and a stackup no antenna-rule layer matches). When a command emits a JSON
error, the CLI discards these warnings so the
caller can parse the whole error stream as one JSON value. When a command
produces a report, the CLI sends the warnings to stderr, including for a
violations report with exit code `3`. Under `--format text`, and for
library callers that never arm the deferral, the warnings print as they
happen, byte-identical to before, ahead of the plain-text error line.

Not deferred, by design: streaming batch progress/heartbeat lines and
`klt sim: notice:` lifecycle narration print as they happen (they are
progress, not one-shot warnings), and so do the `--resume` recovery hints
(`_warn_unlaunched`, `apply_unrun_signal`). One residual: a resume-path
hint can fire just before an error envelope while an exception propagates;
it stays direct on purpose, because dropping it would lose the only pointer
to the resume path for a sweep whose process died mid-submit. A consumer
parsing stderr as one JSON document after such a `klt sim` failure may
therefore still see that one hint line ahead of the envelope.

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
- `error.code` — **optional**, a stable machine-readable classification of the
  failure, present **only** when the command classified it (absent — not
  `null` — otherwise). It lets a caller tell, e.g., a retryable infrastructure
  condition from a request error without parsing `error.message`, whose prose
  is not part of the contract. Added additively (issue #2721): every error
  without a code keeps exactly the two-field shape above, and
  `schema_version` is unchanged. Consumers must ignore codes they do not
  recognize. Codes defined today:

  | Command | `error.code` | Meaning |
  |---------|--------------|---------|
  | `sim` | `engine_version_unmet` | The request's `engine_version` pin (`"MAJOR"` or `"MAJOR.MINOR"`) is not satisfied by the engine that would run it, or the version could not be determined (fail closed). Raised before any corner simulates on the `local`/`local-parallel` backends. On `remote`/`batch` the pin travels with the job: the runner's own `local-parallel` `klt sim` raises it there before any corner simulates (0.8.0+ runners), surfacing per unit as `batch_job_failed` with `runner_code: "engine_version_unmet"` and then as this top-level code when the submitter's post-run check finds no report version; the submitter's post-run check against the report's `environment.engine_version` remains the fail-closed backstop for a runner too old to enforce the field. The *tool* is unavailable. See [docs/cli/sim.md](cli/sim.md) "Pinning the simulator version". |
  | `sim` | `pdk_version_unmet` | The request's `models.pdk_version` pin is not satisfied by the installed PDK (`mismatch` or `unknown` identity), the PDK could not be resolved, or the pin was given with neither `models.pdk` nor `models.pdk_root`. Raised before any corner simulates on `local`/`local-parallel`; `remote`/`batch` reject the field with an uncoded request error (worker-side check tracked in #3462). See [docs/cli/sim.md](cli/sim.md) "Pinning the PDK revision". |
  | `sim` | `unsupported_analysis` | The request names a periodic analysis (`pss`, `pnoise`, `pac`) that `klt sim` does not implement. The *capability* is absent regardless of simulator build. Raised before any corner runs. See [docs/cli/sim.md](cli/sim.md) "Supported analyses". |
  | `sim` | `batch_no_capacity` | `--backend batch`: 2am's fleet launcher refused the job for a Spot pool shortage or a temporary capacity limit. Exit `75` covers the shared vCPU budget (`BATCH_MAX_VCPU`) and profile-specific vCPU slices. The fleet instance cap has its own code below. Returned after any configured `batch.capacity_wait_s` budget is spent. See [docs/cli/sim.md](cli/sim.md) "Batch backend". |
  | `sim` | `batch_concurrency_cap` | `--backend batch`: 2am's fleet launcher refused the job because the shared fleet is at `BATCH_MAX_CONCURRENT_INSTANCES` (after any `batch.capacity_wait_s` budget was spent). Transient slot contention, distinct from a Spot shortage; retrying later is reasonable. See [docs/cli/sim.md](cli/sim.md) "Batch backend". |
  | `sim` | `batch_fleet_status_unreadable` | `klt sim --list-jobs` could not obtain a valid inventory from the fleet provisioner. A successful empty inventory returns `jobs: []` and the fleet capacity figures. |
  | `sim` | `batch_cancel_nothing_to_cancel` | `klt sim --cancel JOB_ID`: the fleet's cancel verb found no pending or running instance for that job (already finished or cancelled, or unknown to the fleet tag) and wrote nothing (issue #4351). Not an infrastructure failure. See [docs/cli/sim.md](cli/sim.md) "Asynchronous submit, status and cancel". |
  | `sim` | `batch_job_cancelled` | `--backend batch`: polling (a blocking run or `--attach`) observed `status.json` state `cancelled` — the job was cancelled through the fleet; nothing is re-launched and no results are coming (issue #4351). See [docs/cli/sim.md](cli/sim.md) "Asynchronous submit, status and cancel". |
  | `sim` | `batch_runner_version_mismatch` | `--backend batch` under `batch.runner_version_check: "enforce"` (the default): the AMI manifest publishes a runner `klt` whose major.minor differs from this client's, for every image the request could boot. Refused once, before any upload or launch, so no report and no per-corner diagnostics exist (issue #2851). Not retryable without a matching runner image or client. A mismatch found only on the runner (refused with exit `87`) keeps reporting through per-corner `batch_job_failed` diagnostics with `runner_code` and, additively, one top-level `diagnostics[]` entry with this code (severity `error`, `accepted_client_release`, remedy in `message`; issue #5061). See [docs/cli/sim.md](cli/sim.md) "Pre-launch check from the AMI manifest". |
  | `netlist` | `batch_no_capacity` | `--backend batch`: the fleet launcher refused the job for a temporary capacity limit, including Spot availability, the shared vCPU budget, or a profile’s vCPU slice. Usually transient; retrying later is reasonable. Nothing ran locally. See [docs/cli/netlist.md](cli/netlist.md#batch-backend). |
  | `sim` | `batch_runner_feature_unsupported` | `--backend batch` under `batch.runner_version_check: "enforce"`: the request uses a feature (currently `analysis_steps`) that every runner image the AMI manifest publishes predates. Refused once, before any upload or launch (issue #3249). The same code appears as `runner_code` on per-corner `batch_job_failed` diagnostics when the runner-side preflight finds it. Those diagnostics also carry additive `missing_features[]` (the request-feature keys the runner predates; issue #4863). See [docs/cli/sim.md](cli/sim.md) "Request-feature capability check". |
  | `sim` | `batch_runner_request_feature_predated` | A **warning** (top-level `diagnostics[]` entry, not an error code): `--backend batch` under `batch.runner_version_check: "warn"` and the request uses fields (`options.osdi_preload`, `measurements[].expr`, ...) the fleet runner `klt` predates. Lists `features[]`, `runner_klt_version`, `source` (`manifest` before launch, `runner` post-run). See [docs/cli/sim.md](cli/sim.md) "Request-feature capability check" (issue #2901). |
  | `netlist` | `batch_concurrency_cap` | `--backend batch`: the fleet launcher refused the job because the shared fleet is at `BATCH_MAX_CONCURRENT_INSTANCES`. Usually transient; retrying later is reasonable. Nothing ran locally. See [docs/cli/netlist.md](cli/netlist.md#batch-backend). |
  | `sim` | `batch_runner_lacks_decks` | `--backend batch` with a deck-list (`decks[]`) request: the AMI manifest publishes only runner images whose `klt` predates `decks[]` support, so the request was refused once, before any upload or launch (issue #3303). Also the `error.code` of a runner-side preflight envelope (exit 87) when the runner's own `klt --version` lacks `decks[]`; `runner_version_check: "warn"` does not relax it. Not retryable without a rebuilt runner image. See [docs/cli/sim.md](cli/sim.md) "Runner support for `decks[]` on the batch backend". |
  | `place-and-route` | `batch_runner_lacks_place_and_route_backend` | `--backend batch` (a `pdk.rebuild_corners` request): the runner's `klt` predates `place-and-route --backend` (older than `0.7.1.dev1091`, which includes `0.7.0`), so every corner's job would fail on the argument (issue #4124). Raised once as an error envelope before any upload or launch when the AMI manifest publishes only such images; otherwise the runner-side preflight exits 87 with an envelope carrying this code, recorded as each `corner_runs[].error_code`. `runner_version_check: "warn"` does not relax it. Not retryable without a rebuilt runner image. See [docs/cli/place-and-route.md](cli/place-and-route.md) "Runner support for `--backend`". |
  | `netlist` | `batch_runner_version_mismatch` | `--backend batch`: the runner's `klt` version differs from this client's; the job was refused before netlisting, or before submission when the AMI manifest already publishes the mismatch (issue #2851). Nothing ran locally. See [docs/cli/netlist.md](cli/netlist.md#batch-backend). |
  | `netlist` | `batch_runner_version_unknown` | `--backend batch`: the runner's `klt` version could not be read; the job was refused before netlisting. Nothing ran locally. See [docs/cli/netlist.md](cli/netlist.md#batch-backend). |
  | `netlist` | `batch_poll_timeout` | `--backend batch`: the job did not finish within the client's poll budget. It may still finish on the runner; nothing was collected. See [docs/cli/netlist.md](cli/netlist.md#batch-backend). |
  | `sim` | `batch_job_interrupted` | `--backend batch` (a per-unit diagnostic code, not an `error.code`): the job stayed `interrupted` (Spot-reclaimed, no re-launch seen) longer than `batch.interrupted_grace_s`, so polling stopped before `batch.poll_timeout_s`. See [docs/cli/sim.md](cli/sim.md) "Batch backend". |
  | `netlist` | `batch_job_interrupted` | `--backend batch`: the job stayed `interrupted` past the default grace; nothing was collected. It may still be recoverable on the runner. See [docs/cli/netlist.md](cli/netlist.md#batch-backend). |
  | `netlist` | `batch_job_failed` | `--backend batch`: the job failed, or left no usable report or netlist. When the runner's own `klt netlist` reported an error, its message is quoted and, if that error carried an `error.code`, the runner's code is passed through in place of `batch_job_failed`. Nothing ran locally. See [docs/cli/netlist.md](cli/netlist.md#batch-backend). When the runner left no klt error envelope, `error` also carries `remote_stderr_tail` (last 2 KiB of the runner's `stderr.log`, sanitized), `remote_exit_code`, `remote_state` and `harness_log` (additive; absent otherwise). |
  | `netlist` | `batch_job_timeout` | `--backend batch`: the fleet harness stopped the job at its `timeout_seconds` (`--timeout-s` plus headroom). See [docs/cli/netlist.md](cli/netlist.md#batch-backend). |
  | `netlist` | `batch_input_too_large` | `--backend batch`: the input tree's uncompressed size is above the batch cap. Raised before any archive is built or anything is uploaded. Not retryable without shrinking the tree. See [docs/cli/netlist.md](cli/netlist.md#batch-backend). |
  | `functional-verification` | `missing_optional_dependency` | The `functional-verification` extra (cocotb) is not importable in the interpreter running `klt`. The message names that interpreter and the remedy per install route: put `klayout-tools[functional-verification]` on the install spec for `uv tool install` / `pipx install` / `uvx --from`, or `uv sync --extra functional-verification` for a source checkout. See [docs/cli/functional-verification.md](cli/functional-verification.md#requirements). |
  | `extract` | `finfet_malformed_geometry` | A FinFET deck (`--deck asap7`) found device geometry it refuses to approximate (no crossing fin, clipped fin, wrong gate length, missing select, VT conflict, ...). `error.message` lists each finding with its bounding box. Not retryable: the layout must change. See [docs/cli/extract.md](cli/extract.md) "ASAP7 FinFET extraction". |
  | `extract` | `finfet_dbu_mismatch` | A FinFET deck was run on a layout whose database unit differs from the deck's; it is refused rather than rescaled. See [docs/cli/extract.md](cli/extract.md) "ASAP7 FinFET extraction". |
  | `pex` | `schematic_flatten_cycle`, `schematic_flatten_pin_count_mismatch`, `schematic_flatten_unsupported_card`, `schematic_flatten_wrapper_pin_unconnected`, `schematic_flatten_malformed` | `--flatten-schematic` (issue #2889): the schematic DUT could not be flattened faithfully — a subcircuit instantiation cycle, an `X` call whose node count differs from the subcircuit's pin count, a card the flattener cannot rename safely, unbalanced/duplicate `.subckt` definitions, or (issue #4625) a fragment DUT with no net for a pin of the synthesized `.subckt <top>` wrapper. Raised before anything simulates; no partial flatten is written. Not retryable without changing the DUT netlist. See [docs/cli/pex.md](cli/pex.md) "Flattening a hierarchical schematic DUT". |
  | `drc` | `klayout_unavailable` | `--engine klayout` (issue #3076): the standalone `klayout` application binary is missing or could not be launched (run or `--min-klayout-version` probe) — the second opinion could not run. Exit `1`, never a verdict; not a bad-input error. See [docs/cli/drc.md](cli/drc.md) "Machine-readable failure codes". |
  | `drc` | `klayout_version_too_old` | `--engine klayout`: the `klayout` application (`--klayout-binary`, `$KLT_KLAYOUT_BINARY` or `PATH`; the resolved binary is recorded as `environment.klayout_binary` on every run, issues #4950/#5844) is older than `--min-klayout-version`. Exit `1`; the deck is never launched. See [docs/cli/drc.md](cli/drc.md) "Machine-readable failure codes". |
  | `drc` | `negative_control_failed` | `--engine klayout --negative-control`: the known-bad fixture produced no violations (or missed a `--negative-control-categories` entry) under the same deck, so a clean verdict cannot be trusted. See [docs/cli/drc.md](cli/drc.md) "Negative control". |
  | `pex`, `sim`, `place-and-route`, `netlist --output-dir` | `outdir_locked` | Another cooperating `klt` run owns this run's persistent output directory (or an ancestor/descendant of it). Exit `1`, empty stdout. Transient: retry after the owner finishes. See "Output-directory ownership" below. |
  | `pex`, `sim`, `place-and-route`, `netlist --output-dir` | `outdir_lock_unavailable` | Ownership could not be established for a reason other than contention (unwritable lock registry, no `flock` on this platform or filesystem). Not retryable without changing the environment. Exit `1`, empty stdout. |

Under `--format text` (the default), errors remain the pre-existing
plain-text stderr line: `klt <command>: <message>`. Text is a courtesy
rendering, not the contract, so this shape is not versioned.


### Output-directory ownership (`outdir_locked`, issue #2770)

`klt pex`, `klt sim` and `klt place-and-route` (and the `run_pex` /
`run_sim` / `run_place_and_route` library entry points) take a non-blocking,
OS-backed advisory lock on each persistent artifact root for the run's whole
lifetime. A second cooperating run fails fast instead of interleaving
artifacts:

```json
{
  "schema_version": 1,
  "error": {
    "command": "pex",
    "message": "output directory '/w/.klt/pex' is owned by another run (pid 4242 since 2026-10-12T10:00:00+00:00); wait for it to finish and retry",
    "code": "outdir_locked"
  }
}
```

`klt pex` additionally merges the lock's structured owner into `error`
(issue #4692; additive): `owner_pid` and `owner_acquired_at` (`null` when
unknown), `locked_directory` and `relation` (`same`/`ancestor`/`descendant`).

- **Exit code and streams.** Contention is an application error without a
  report: exit `1`, stdout empty, the envelope above on stderr (`--format
  text`: `klt <command>: <message>`). The exit-code table is unchanged; tell
  contention apart by `error.code`, not by a new exit code. Library callers
  get `klayout_tools.output_lock.OutputDirLockedError` (attributes
  `directory`, `locked_directory`, `relation` = `same`/`ancestor`/
  `descendant`, `owner_pid`, `owner_acquired_at`) or
  `OutputLockUnavailableError`; both derive from `OutputLockError` (`.code`).
- **Diagnostics are best effort.** PID and acquisition time come from an
  owner record that may be missing, malformed or momentarily unreadable, in
  which case the message says `owner unknown`. The record is never used to
  decide whether a lock may be broken: ownership is the OS lock, so a killed
  owner releases it automatically and no PID-liveness or PID-reuse guesses
  exist. A refused run changes nothing under the protected directory.
- **Which roots.** `pex`: `--outdir` (default `.klt/pex/` beside the layout).
  `sim`: `--outdir`/default `.klt/sim/` beside the request, but only
  for runs that write there (`options.keep_artifacts`, `--resume`, `--plot`
  (the plot directory), or the `remote`/`batch` backends that collect results
  locally); a temporary-only run takes no lock. `place-and-route`: the
  existing `.klt/place-and-route/` beside the request (no new flag).
- **Identity and policy.** Paths are canonicalised (relative, `..` and
  symlink aliases contend). Same root: exclusive. Ancestor and descendant
  exclude each other (a `klt sim` on `<pex outdir>/<testbench>/schematic`
  is refused while `klt pex` owns `<pex outdir>`). Unrelated roots, and
  distinct siblings under a common ancestor, run concurrently. `run_pex`'s
  own `run_sim` calls inherit its ownership and never self-contend; within
  one thread, re-entry on an owned root or its descendants is free, while
  owning a descendant and then requesting its ancestor is refused (acquire
  outermost first). Acquisition never blocks, so it cannot deadlock. A child
  process does not inherit a lock, with one additive exception (#3291):
  `klt pex --measure-command` exports `KLT_OUTDIR_LOCK_INHERIT=<canonical
  $KLT_PEX_ARTIFACTS_DIR>`, and a `klt` run whose output directory is that
  directory or a descendant is covered by the parent's ownership. Ancestors,
  siblings and unrelated roots are refused as usual; `error.code` values and
  `schema_version` are unchanged.
- **Scope and limits.** Cooperating `klt` writers on one host that resolve
  the same lock registry (`$KLT_LOCK_DIR`, default `/tmp/klt-locks-<uid>`)
  on a filesystem where `flock` works. Other tools, writers that do not use
  `klt`, and distributed remote writers are not covered; there is no
  cross-host lock service. Lock files live in the registry, never in the
  artifact root, and are never deleted (so all processes lock the same
  inode). If `fcntl.flock` is unavailable (Windows) or the registry is
  unusable, the run fails with `outdir_lock_unavailable` rather than running
  unprotected. A `fork()` without `exec` shares the lock descriptor; such
  workers outliving a killed owner keep the lock until they exit.

### Aggregate (set) envelopes: `klt sim --batch-set` (issue #2869)

A verb that submits several independent requests in one invocation emits one
aggregate payload with its own `schema_version` (versioned like any command
payload), a rollup `status`, and a `requests[]` array of per-request entries
(`status`, `exit_code`, `job_id`, `error.{message,code}`). A request-level
failure is an entry, not a command error: the command exits with the **worst
per-request code** (a partial set exits `4` for `klt sim`), which is `>= 3`,
so the "a report was emitted" rule below holds. See
[`docs/cli/sim.md`](cli/sim.md) "Submitting a set of requests" and
`docs/schemas/sim-set-report.schema.json`.

`klt sim-merge` (issue #3045) emits a single `klt sim` report over the union of
several per-request reports, plus an additive `merge` block; it defines the
refusal exit codes `5`-`10` (a set that is not exactly the planned one; stdout carries a
`status: "refused"` payload whose `refusal.code` names the class) alongside `klt sim`'s
`3`/`4` verdicts. See [`docs/cli/sim.md`](cli/sim.md) "Merging per-request
reports" and `docs/schemas/sim-merge-report.schema.json`.

`klt sim-plan` (issue #3423) writes the shard requests and `sim-merge` manifest
beside a request and prints `{schema_version, axis, manifest, shard_count,
shards[]}` (`id`, `request`, `request_sha256`, `corner_count`). It exits `0` or
`1` (error envelope on stderr) only. See [`docs/cli/sim.md`](cli/sim.md)
"Planning shards".

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

`klt synthesize` exits `4` with `status: "inconclusive"` and
`reason: "abc_timeout"` when its optional generic ABC budget expires.
The `partial` block identifies the retained generic netlist and statistics.

**The exit-code contract is additive, and a consumer must treat it that way.**
A command may ship a new code above `2` in a later release for an outcome
that previously did not exist — e.g. `klt lvs` adding exit `4`
(`status: "inconclusive"`) alongside its original `0`/`1`/`2`/`3` (issue
#1370). A script that allowlists a fixed set of "acceptable" codes (e.g.
`returncode not in (0, 3)`) treats every new code as a crash, because the
allowlist itself is not additive even though the codes it is checking
against are. This broke a real downstream pipeline mid-run when `4` shipped
(issue #2172) — the fix is architectural, not a one-time allowlist update:

- **There is a stable "a report was emitted" class.** Any exit code `3`
  and above means the command ran to completion and its documented JSON
  payload (with a `status` field or equivalent verdict) is on stdout — the
  new code is a *new verdict*, not a new failure mode. Only exit `1` means
  no report was written (`2` is a usage error from argparse — see the
  carve-out below — and also writes no report). A consumer that wants to
  keep working across new codes should branch on "`1`: no report, handle as
  a failure" vs. "anything else: a report was written, parse it" instead of
  enumerating every code it currently knows about.
- **`status` is the verdict of record; the exit code is a shortcut.** Every
  command's JSON payload carries a `status` (or equivalently-named verdict)
  field that is the authoritative outcome — the exit code is a
  process-level convenience for shell scripting (`if klt lvs ...; then`),
  derived from the same `status`, not an independent signal. A consumer
  that gates CI/pipeline behavior on outcome should read `status` from the
  parsed JSON, not maintain its own exit-code allowlist; `status`'s
  possible values are documented per-verb and grow the same way exit codes
  do (a new named value, never a redefinition of an existing one), so
  reading it directly does not need updating each time a verb adds an
  outcome.

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
  part of the contract and may change between releases without notice. A
  character the stdout encoding cannot represent prints as a `\uXXXX` escape
  rather than aborting the render with a traceback (a stdout error handler the
  caller chose, e.g. `PYTHONIOENCODING=cp1252:replace`, is honoured); the JSON
  keeps the original string.
- `json` — the stable API. Breaking a JSON field (renaming, removing, or
  retyping it) is a breaking change per CLAUDE.md; adding a field is not.

## Response schemas (`docs/schemas/<verb>-response.schema.json`)

A verb's success payload may ship a machine-readable JSON Schema (Draft
2020-12) alongside its prose in `docs/cli/<verb>.md`. Today `klt layers`,
`klt cells`, `klt stats`, `klt drc` and `klt lvs` have one:
[`layers-response`](schemas/layers-response.schema.json),
[`cells-response`](schemas/cells-response.schema.json),
[`stats-response`](schemas/stats-response.schema.json),
[`drc-response`](schemas/drc-response.schema.json) (verdict reports from both
engines plus the non-verdict `--list-rules` and `--list-rule-categories`
listings; its `coverage` block
`$ref`s [`coverage.schema.json`](schemas/coverage.schema.json)) and
[`lvs-response`](schemas/lvs-response.schema.json).

- **Naming.** `docs/schemas/<verb>-response.schema.json`, one file per verb.
- **Required means always present.** Every field the verb always emits is in
  `required`; fields that appear only under a flag (e.g. `klt layers
  --flattened`) are declared in `properties` but not required. Nullable
  fields are typed explicitly (`["string", "null"]`).
- **Version pin.** The top-level `schema_version` is pinned with `const`.
- **Extensibility.** Object boundaries keep `additionalProperties: true`, so
  additive pre-1.0 fields stay compatible. That does not excuse leaving a
  documented field out of `properties`.
- **Version-bump rule.** Renaming, removing or retyping a field, or making an
  optional field always-present, changes the shape: bump the verb's
  `schema_version` and the schema's `const` together. Adding an optional
  field does not bump it, but the schema must be updated to describe it.
- **Testing.** The verb's existing `test_json_*` module loads the shipped
  schema, runs `Draft202012Validator.check_schema` on it, validates real CLI
  output for each option-dependent shape, and asserts that deleting a
  required field raises `jsonschema.ValidationError`.

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
4. If the command wraps an external tool, meet the four requirements in
   [Wrapping an external tool](#wrapping-an-external-tool).

See `src/klayout_tools/cli/layers_cmd.py` and `docs/cli/layers.md` for a
worked example.

### `klt pex --flatten-schematic` (issue #2889)

`klt pex` adds the always-present boolean `flatten_schematic` echo and the
`schematic_flatten` block (`null` unless the flag is given; otherwise the
flattened schematic DUT's `{path, scope}` `netlist`, its `netlist_sha256`
under the issue #2875 `sha256:<hex>` convention, and an inlining summary).
Both are additive under the opt-in-flag rule above: no existing field
changes name, type or meaning — `reference_netlist` and
`testbenches[].schematic_netlist*` still name the caller's own DUT — so
**no `schema_version` bump**. See [`cli/pex.md`](cli/pex.md).

### `klt pex` schematic-side content digests (issue #2875)

`klt pex` adds `testbenches[].{request_sha256,netlist_sha256,
schematic_netlist_sha256}`, `testbenches[].netlist` and
`reference_netlist_sha256` (`sha256:<64 lowercase hex>`). This is an
**additive** change, so `SCHEMA_VERSION` stays `2`: no existing field changed
shape, and consumers must already ignore unknown fields. These are distinct
roles from `provenance.input.content_hash` (the layout) and must not take
part in layout-consistency comparisons; `klt signoff` verifies them per role
(`citation.pex_inputs`). Pre-change envelopes omit them and stay readable.
See [`cli/pex.md`](cli/pex.md).

### `klt signoff` `citation.sim_closure` (issue #7860)

A `klt sim` citation gains `sim_closure`: `{status, includes, stale[],
unverifiable[]}` (or `{status: "not_recorded", reason}` when the cited report
records no `environment.netlist_closure`), the re-hash of the report's recorded
`.include` closure. This is an **additive** change to the signoff output, so no
`schema_version` changes; the key is absent on every other kind. No `klt sim`
report field is added and `provenance.input.content_hash` keeps meaning the top
deck only. A stale include sets `citation.input_verified` to `false` without
changing the item's status. See [`cli/signoff.md`](cli/signoff.md).

### `klt pex --dry-run` (issues #5464, #5511)

`klt pex --dry-run` emits a **different payload** from a graded run, not a
superset of one: `dry_run: true`, `status: "valid"`, `layout`, `deck`,
`top`, `tops` (`null` on a zero- or one-`--top` dry run, or
`[{"top": "<resolved cell name>"}, ...]` in `--top` order when two or more
`--top` values are given; issue #5511), `deck_options`, `declared_pins`,
`legs`, `reference_netlist`, `testbenches[]` (each `{request,
schematic_netlist, sim}`, where `sim` is the nested `klt sim --dry-run`
report verbatim) and `not_checked[]`. It rides the same envelope at the same
`SCHEMA_VERSION` (`2`) — **no bump**.

This is additive under the opt-in-flag rule above: the payload exists only
when `--dry-run` is given, no field of the graded report changes name, type
or meaning, and a consumer that never passes the flag sees a byte-identical
report. `dry_run` is the discriminator — it is absent from every graded
report, so a reader can tell the two apart without looking at the argv.
`not_checked[]` is a closed set of fixed string codes (new codes are
additive); it exists so a dry run states what it did not verify rather than
letting a `"valid"` status imply more than it earned. See
[`cli/pex.md`](cli/pex.md)'s "Validating an invocation without running it".

### `klt pex --resistance-graph` echo fields (issue #5894)

`extraction.resistance_graph.nets[]` entries — present only with
`--resistance-graph` — additively gain `terminals` and `pair_resistance_ohm`,
copied verbatim from `klt extract`'s per-net `resistance_graph`.
`terminals` lists the sorted geometry-derived names available when the
calculation stopped. A skipped net with one terminal retains that name.
An unsupported solve can retain both terminal names, while a graph that
failed before terminal discovery has an empty list.
`pair_resistance_ohm` is the symmetric effective-resistance matrix. A
solved matrix uses `null` for a disconnected pair. A `skipped` or
`unsupported` net has an empty matrix (`[]`). This closes an honesty gap in the
extraction-to-simulation loop: the lumped `resistance_ohm` a `legacy` net
keeps is a series sum of *all* the net's fragments, so a tree-routed net's
total overstates the terminal-to-terminal path (stub arms to terminals
carrying no DC current are summed in), and nets with three or more terminals
always keep `legacy` — the pair matrix is what a caller reads for the path
resistance between two named terminals there. No existing field changes
name, type or meaning, output without the flag stays byte-identical, so
**no `schema_version` bump**. See [`cli/pex.md`](cli/pex.md) and
[`cli/extract.md`](cli/extract.md)'s "Terminal-aware resistance graph"
section.

### `klt pex --resistance-network` per-net network disclosure (issue #6899)

`--resistance-network` (requires `--resistance-graph`) writes the
Kron-reduced terminal network of the solved graph in place of the star for
every net whose graph solved `ok` with three or more terminals and whose
device terminals map one-to-one onto its graph terminals, so a tree-routed
net's stub arms stop adding series resistance to the current path. The
additive fields, all absent without the flag (byte-identical output):

- `parasitics.nets[].resistance_graph.network` (from `klt extract`, echoed
  verbatim into `extraction.resistance_graph.nets[].network`):
  `{written, resistor_count, fallback_reason}`. `written` is `reduced`
  (the network was written; `resistor_count` pair resistors, at most
  `T(T-1)/2` = 2016 at the graph's 64-terminal cap), `scalar` (a
  two-terminal graph net kept issue #4037's `selected_resistance_ohm`) or
  `legacy`. `fallback_reason` reuses issue #5747's vocabulary
  (`resistance_source_reason`, first `unsupported` diagnostic `code`) and
  adds only `terminal_mismatch` (the one-to-one device-terminal mapping
  failed, or position cannot decide it: two terminals of one device on the
  net share that device's single position), `mom_rlc_override` (a
  `--mom-rlc-resistance-ohm` substitution won) and `parasitic_variant` (a
  `--parasitic-variant` drop kept the star), which it cannot express. A
  written reduced network reproduces every `pair_resistance_ohm` entry within
  1e-6 relative tolerance. A disconnected filtered network reports
  `pair_resistance_unavailable`, and an inaccurate connected reduction reports
  `singular_system`. Both keep the existing fallback model. The net's ground
  capacitance splits equally across its terminal nodes (the documented
  first-increment rule; per-node capacitance from the graph itself is the
  roadmap's open question).
- `extraction.resistance_graph.resistance_network`: `true` when the request
  enables the reduced network. The field is absent otherwise.
- `parasitics.model.resistance_network` (echoed in
  `extraction.model`): a string stating the mode, so a
  `klt signoff --manifest` item-7 citation can tell a graph-derived
  network extraction from a series-sum one.
- `parasitics.nets[].rc_model` gains a third value, `"reduced"`, beside
  `"lumped"` and `"distributed"` — only for nets that took the network;
  their `integrity.open` runs the real terminal-connectivity check.

No existing field changes name, type or meaning, so **no
`schema_version` bump**. See [`cli/pex.md`](cli/pex.md)'s
`--resistance-network` and [`cli/extract.md`](cli/extract.md)'s "Reduced
terminal networks" paragraphs.

### `klt corner-matrix check` coverage verdict (issue #5865)

`klt corner-matrix check` adds the always-present boolean
`coverage_complete` next to `complete`: true iff `missing[]`, `extra[]` and
`duplicate[]` are all empty — *did the run exercise the declared matrix,
once each*. `complete` keeps its published meaning (coverage **and**
outcome quality: no `not_simulated[]`, no `non_finite[]`, no
`out_of_bounds[]`), so a report that covered its matrix but recorded one
legitimate failing corner is `coverage_complete: true` with
`complete: false`. The field is emitted in both modes; in `--request` mode
`coverage_complete == complete` by construction. Exit codes are unchanged,
which is the point: the exit status follows `complete` and cannot serve as
a coverage gate. Additive — no existing field changes name, type or
meaning, so **no `schema_version` bump** (the command's success payload
carries none today). See [`cli/corner-matrix.md`](cli/corner-matrix.md).

## Static typing of the envelope

The envelope modules (`klayout_tools.cli.output`,
`klayout_tools.signoff_envelopes`) are held to `disallow_untyped_defs` under
mypy and the package ships `py.typed`. The rest of the code is covered by a
baseline ratchet; see [`type-checking.md`](type-checking.md).

In report mode, `errored[]` contains the expected corner IDs whose error row
records an engine attempt, in matrix order. It is a subset of
`not_simulated[]`. A report without attempted errors emits an empty array.
Request mode omits this field. Both verdicts and the exit status retain
their existing meanings (#7008).

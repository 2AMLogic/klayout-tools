# Checked-work coverage, version 1

`klt` distinguishes completed checks, requested work it skipped, work that
was inapplicable, and execution it cannot measure. This is Phase 1 of
[#1988](https://github.com/2AMLogic/klayout-tools/issues/1988), implemented in
[#2108](https://github.com/2AMLogic/klayout-tools/issues/2108). Phase 1
changed zero-check success and unknown execution; the common partial-success
rollup rule that reads this schema is
[#2109](https://github.com/2AMLogic/klayout-tools/issues/2109), documented in
"The common rollup rule" below and consumed by the six per-path adapters.

The machine schema is [coverage.schema.json](schemas/coverage.schema.json).
The independent `coverage.schema_version` is `1`; the producing command's
own `schema_version` remains independently versioned. Existing verb-specific
coverage fields remain alongside the common fields.

```json
{
  "coverage": {
    "schema_version": 1,
    "known": true,
    "checked": ["met1.width.1"],
    "skipped": [{"id": "met2.width.1", "reason": "absent_input_layer"}],
    "inapplicable": [],
    "unknown": [],
    "nothing_checked": false,
    "nothing_checked_reasons": []
  }
}
```

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`, never a boolean. Unsupported versions are not qualifying evidence. |
| `known` | Boolean: the producer can account for this coverage scope's execution. Exactly equivalent to an empty `unknown` array. |
| `checked` | Unique stable string identities for work actually checked, including checks that failed. A declaration or an attempted engine launch alone does not count. |
| `skipped` | Requested work not checked, each `{id, reason}`. Reasons are nonempty stable codes matching `[a-z][a-z0-9_]*`. |
| `inapplicable` | Work outside this invocation's applicable assessment, with the same identity/reason shape. These entries do not make otherwise complete work partial. |
| `unknown` | Execution whose extent cannot be measured, with the same shape. Unknown is neither zero nor full coverage. Actual findings can coexist with unknown coverage. |
| `nothing_checked` | Exactly `known && checked.length == 0`. An unknown run must report `false`, even if it cannot prove a single check. |
| `nothing_checked_reasons` | Nonempty unique reason codes only for known zero checked work; otherwise `[]`. |

All eight fields are required in a versioned block. Identities are disjoint
across the four work arrays. The Python validator additionally checks this
cross-array constraint, which the JSON Schema cannot express. Producers sort
identities deterministically; identity is not a count or free-form prose.
DRC uses its rule IDs. Other adapters use `domain:` followed by a compact JSON
array of scope components (for example
`limit:[0,"tt/1.800V/27C","vout","max"]`). JSON quoting prevents names
containing separators from colliding; a leading occurrence index (a corner's
or row's own position in the producer's report) additionally guards against
two distinct occurrences that happen to share the same *display* label (e.g.
a supply voltage rounded for `corner_id`) colliding as one identity. These
are identities within a report, not content hashes or identities across
design revisions; provenance provides the latter connection.

`coverage_state()` derives `full`, `partial`, `zero`, `unknown`, `malformed`,
or `legacy`. Full and partial require known, nonempty checked work; partial
also has requested skips. A full scope does not assert physical signoff for
every possible rule or analysis: curated decks still disclose their limited
rule scope, and antenna/EM adapters identify their scope explicitly.

## The common rollup rule, version 2109

This is Phase 2's single decision table, implemented once as
`coverage_rollup()` in `klayout_tools/coverage.py` and applied identically by
every producer and every consumer. Its purpose is that a partial result can
never be rendered as an unconditional success by one path while another
refuses it.

Two inputs: the producer's own non-coverage outcome (`errored`, then
`failed`), and `coverage_state()`. Eight rows, in precedence order:

| Row | When | `reason` | Exit | `unconditional` | `complete` |
| --- | --- | --- | --- | --- | --- |
| `errored` | The run could not complete | `check_errored` | 4 | no | no |
| `failed` | An executed check found a defect | `check_failed` | 3 | no | no |
| `malformed` | The `coverage` block is present and structurally invalid or self-contradictory | `malformed_coverage` | 4 | no | no |
| `unknown` | Execution extent cannot be measured (`known: false`) | `coverage_unknown` | 4 | no | no |
| `zero` | Known zero checked work | `nothing_checked` | 4 | no | no |
| `partial` | Every executed check succeeded **and** requested work was skipped | `partial_coverage` | 0 | no | no |
| `legacy` | No common coverage block — the envelope makes no claim | `coverage_not_reported` | 0 | yes | no |
| `full` | Known, nonempty checked work with no skipped request | — | 0 | yes | yes |

**Real failures retain precedence.** `errored` and `failed` are decided
before coverage is consulted at all, so a run that found a defect is reported
as that defect and never masked by a coverage gap — and conversely a
coverage gap is never dissolved by a clean-looking status. `errored` outranks
`failed`: a run that did not complete cannot vouch for the findings it emitted.

**Zero-check results cannot become success.** `zero`, `unknown` and
`malformed` all exit 4 and reach no verdict. Nothing in this table promotes
them; `unknown` in particular is not evidence of either zero or complete
execution.

**Unconditional success requires positively established complete applicable
coverage.** Only `full` is `complete`. Successful checks plus a nonempty
skip list are explicitly `partial`: a real, exit-0, reportable result that is
*not* the verb's unconditional success. Inapplicable work is not a skipped
request, so it never makes an otherwise complete run partial — the rule can
be strict about skips precisely because it does not punish a verb for work
the invocation never asked for.

### Result, reason and status fields

`CoverageRollup` exposes `result` (the row name), `reason` (the stable code
above, `None` only for `full`), `exit_code`, `unconditional`, `complete`, and
the derived `successful` (exit 0) / `reached_verdict` (not exit 4). Those
names are the machine-readable contract; the human rendering is not.

`rollup_status(rollup, success=..., partial=..., failure=...)` maps a row
onto the verb's own status vocabulary. Verbs differ in what they call success
(`clean` for `klt drc`, `pass` for most others) but must not differ in *when*
they may say it, so the vocabulary is a parameter and the mapping is not.
`partial` defaults to `f"{success}_partial"`, reproducing the `pass_partial`
token #1997 already shipped rather than competing with it. The non-verdict
rows keep the tokens already fixed for them: `not_checked` and
`coverage_unknown`, both exit 4.

A producer building its block through `build_check_coverage()` can never
reach the `malformed` or `legacy` rows — that helper validates what it emits
and always emits the common fields. Both rows exist for consumers reading
external, hand-edited or pre-contract evidence.

### Legacy and unknown policy, stated explicitly

Missing common coverage (`legacy`) keeps grading exactly as the verb-specific
rules that always governed it say, and is **never** counted as proof of
completeness: `unconditional` is true, `complete` is false, and the row
carries its own reason code `coverage_not_reported`. This is the migration
policy, not an inference — it exists because retrofitting a refusal onto
every historical artifact would invalidate evidence whose coverage nobody
ever claimed, and it is why `complete` exists as a separate question from
`unconditional`. A consumer that must assert positively established coverage
reads `complete`, which no legacy envelope can satisfy. Each adapter shrinks
the legacy set for its own path; none of them relabels it.

`unknown` is the opposite policy and is unchanged from Phase 1: uninstrumented
execution stays unknown rather than becoming either a gap or a guarantee.
Neither policy permits renaming inapplicable work into skips, or skips into
inapplicable work, to reach a nicer row.

Optional HDL code-coverage percentages (Verilator's `line_pct`/`branch_pct`)
share the `coverage` key name and nothing else. They classify as `legacy`
and are never read as a complete, partial or zero *requested-check* claim.

### Signoff qualification

`klt signoff` applies two distinct gates from the same table:

| Gate | Helper | Rows it stops |
| --- | --- | --- |
| Hard refusal — the evidence states no usable verdict | `coverage_refusal_reason()` | `zero`, `unknown`, `malformed` |
| Qualification — the evidence is not an unconditional success | `coverage_qualification_reason()` | those three **and** `partial` |

The hard refusal is unchanged by Phase 2: a partial run did check something,
so refusing it outright would discard a real result rather than qualify it.
What Phase 2 adds on the consumer side is that partial evidence can never be
read as complete, and is never *silent*:

- A producer that has adopted the rollup reports its partial token
  (`clean_partial`, `pass_partial`, …). `klt signoff` does not count it as a
  passing check, and names the reason `partial_coverage` rather than
  `check_failed` — the cited run did not find a defect, it skipped requested
  work. This is how the operator's distinction reaches signoff: the
  producer's verdict carries it, and signoff grades that verdict.
- Until a verb's adapter lands, its status can still be the unconditional
  success word on a run whose common coverage says `partial`. Those
  citations now carry `coverage_qualification` (the reason plus the skipped
  work) on both the `checks[].detail` and `"met"`-citation paths, so the gap
  is visible in the report instead of inferred by re-opening the envelope.
  Item 3's verdict itself remains `status` alone, as
  [design-evidence-tiers.md](design-evidence-tiers.md) item 3 specifies —
  changing which item text grades on what is that document's decision, not
  this contract's.

### Migration contract for the per-path adapters

Each of the six Phase 2 adapter issues (#2110, #2111, #2115, #2116, #2117,
#2118) owns one public path and does the same four things:

1. Build the v1 block with `build_check_coverage()`, classifying each piece
   of work as `checked`, `skipped` (a *requested* check that did not run),
   `inapplicable` (outside this invocation's assessment) or `unknown`. Do
   not move work between those categories to reach a nicer row — but do
   settle the classification deliberately, because it decides the row.
   Curated DRC's `absent_input_layer` was the open one, and #2110 settled
   it: a skipped rule is `inapplicable` when running it with the absent
   layer empty *could not have reported a violation* (every primitive in
   `drc.py` returns nothing for an empty input region, so the check is a
   provable no-op — a rule that constrains a drawn pair has no instance to
   constrain), and a `skipped` request when it could have (today exactly
   `check="antenna"`, whose primitive faults a zero-area protection
   region). Phase 1 had recorded all of them as skipped requests, which
   would have made an ordinary sky130 run on a small block partial (7 of 57
   rules checked on `examples/design-pipeline/06-layout.gds`) and a bare
   standard cell partial for declining to check the via rules of a cell that
   draws no vias. See [cli/drc.md](cli/drc.md)'s "`coverage.skipped` vs.
   `coverage.inapplicable`".
2. Derive the verdict with `coverage_rollup(envelope, failed=…, errored=…)`,
   passing the path's own existing failure/error determination. Do not
   re-derive precedence locally.
3. Report `rollup_status(...)` as the top-level status and `rollup.exit_code`
   as the exit code, keeping the verb's existing success/failure words as
   the `success=`/`failure=` arguments. A verb whose partial token is
   already shipped (`klt power`'s `pass_partial`, #1997) passes it
   explicitly rather than renaming it.
4. Close with the path's exact false-pass regression **and** a checked-success
   control, per #1988's completion rule.

Adapters do not change this table, the reason codes, the exit codes or the
signoff gates. A path that needs a row this table does not have is a change
to this document first.

## Producer and compatibility mapping

| Public path | Checked work and exclusions | Phase 1 result and CLI |
| --- | --- | --- |
| `drc --engine curated` | Executed rule IDs. An absent input layer is an `absent_input_layer` skip only when running the rule could still have reported a violation; otherwise it is `no_applicable_geometry` inapplicable work (#2110). Deck scope and uncovered stream layers remain in legacy fields, and `rules_skipped` keeps listing both categories. | Violations retain `violations`/3; known zero becomes `not_checked`/4; a nonempty skipped-request list becomes `clean_partial`/0; otherwise `clean`/0. Empty-deck and all-skipped reason codes remain. |
| `drc --engine klayout` | RDB categories are declarations, not proof of execution. `rule_categories` retains them; `rules_checked` and common `checked` contain only rule IDs with actual findings. The rest is `unmeasured_rule_execution`, so `known=false`. | Findings retain `violations`/3; otherwise `coverage_unknown`/4. **DRC envelope v2** versions the corrected `rules_checked` meaning for this engine. Neither an empty RDB nor a nonempty category list proves clean execution. There is no instrumentation interface claiming known full coverage in this release. |
| `erc` antenna | One ID per actual gate/non-gate level compared to a limit. Missing PDK/limit are skips. The gate reference level is inapplicable. `scope="antenna"`; connectivity findings remain independently authoritative. | Any antenna/connectivity finding retains `violations`/3; zero graded antenna levels becomes `not_checked`/4; a clean run with skipped requested levels (e.g. a full sky130 stack whose met3-5 roles have no antenna-ratio limit) is `clean_partial`/0 (#2115); otherwise `clean`/0. A per-gate `pass_partial` remains disclosed independently (#1997). |
| `erc` connectivity — **second scope, issue #2179** | One ID per subject actually checked by the `erc_findings` rules, which need no PDK: per discovered gate (`erc.floating_gate`), per declared `nets[]` entry (`erc.net_connectivity`), per declared `ties[]` entry (`erc.missing_tie`). An undeclared rule is *inapplicable* (`no_nets_declared`/`no_ties_declared`, or — when the spec's top-level `ties_disclosure` was given — `ties_disclosed_unexpressible` (issue #2234) or `ties_disclosed_tool_limitation` (issue #2247, `ties_disclosure.kind: "tool_limitation"` — the tap is expressible, but the build the evidence had to be produced on cannot grade a declared tie safely) in place of the latter), never a skip; a *declared* tie whose tap region cannot be told apart from an ordinary source/drain contact is a real skip (`degenerate_tap_declaration`, issue #2199 — requested work that could not be performed), whether that region was derived from `tap_requires`/`tap_is_dedicated` or asserted via `tap_boxes` (issue #2234 — the same geometric falsifiability test applies to all three forms); so is a tie whose caller-asserted *substrate region* (`well_layer: null` + `well_boxes`, issue #2255 — the native-substrate form, for a block that draws no well/tub layer) cannot be told apart from the whole top-cell extent (`degenerate_well_assertion`); and so is a tie whose declared *well-side class selection* (`well_requires`/`well_excludes`, issue #2339 — which merged shapes of a drawn `well_layer` this entry is about, for a tub layer carrying two differently-biased well classes) kept every shape of that layer or none of them (`degenerate_well_selection`); and so is a tie naming a *drawn* `well_layer` with no geometry at all in the stream — a typo'd layer/datatype, a PDK whose tub layer number changed, a GDS written without the tub layer (`empty_well_region`, issue #2377 — the unselected form of the same zero-iteration state, never applicable to an asserted `well_boxes` region, which cannot be empty after spec validation). The well tests are applied first when more than one would hold, since the tap narrowing is measured inside the well. Reported at `erc_coverage`, `scope="connectivity"`, beside the antenna scope at `coverage`, plus two additive assertion lists — `checked_by_assertion` (issue #2234): the subset of `checked` whose *tap* region came from a caller assertion (`tap_boxes`) rather than pure PDK-marker narrowing, and `checked_by_well_assertion` (issue #2255): the subset whose *well* region was itself asserted. Both are purely informational, neither is one of the four common-contract lists, and they stay separate because they are different claims (which drawn geometry is the tap vs. where the substrate is). A well-side *selection* adds no third list: it narrows drawn geometry, exactly as `tap_requires` does, so a tie graded through one is an ordinary geometrically-derived pass. | Rolled up separately into the additive `erc_status`, with `erc_finding_count` (and *not* antenna violations) as the `failed` input: `violations` for any finding, else `clean_partial` when a degenerate tie was skipped, else `clean`. It drives no exit code of its own — `status` and the exit code still answer the antenna question, unchanged — so a table-less PDK reports `status: "not_checked"`/4 alongside a real `erc_status`. |
| `power` EM | Net/island/edge IDs with both solved current and declared limit. Missing limit/current are skips. Without a requested solve the EM work is inapplicable. `scope="electromigration"`. | Additive top-level `status` mirrors `em_verdict.status`, or `not_checked` without a solve. `fail` exits 3, `not_checked` exits 4; existing `pass` and `pass_partial` exit 0. Network/IR data remain available on refusal. Existing signoff rules continue to reject `pass_partial`. |
| `sim` sweep — **Phase 2 adopted, issue #2117** | Applied min/max bounds per actual corner/measurement; unknown keys, null bounds, missing measurements, empty matrix and no requested measurements are disclosed skips. Deliberate characterization counts a produced observation as checked and its omitted limit as inapplicable. | Top-level `status` is `coverage_rollup()`/`rollup_status()`'s own row, applied directly rather than a bespoke check: `error`/4 and `fail`/3 (including a failing Monte Carlo sigma window) retain precedence over coverage; with neither, known zero checked work is `not_checked`/4, successful checks alongside a nonempty skip list are `pass_partial`/0 (never the unconditional `pass`), and complete coverage is `pass`/0. A launched corner or parsed declaration alone is insufficient. Legacy counters still count their original corner/measurement declarations. |
| `pex` comparison | Actual pass/fail delta pairs; error rows are unavailable comparisons. A testbench with no corners or measurements is a named skip. | Existing `error`/4 and `fail`/3 win; no comparable pairs becomes `not_checked`/4. A real comparison with identical values remains `pass`/0. Legacy row counts include errors; common checked IDs do not. |

The common fields and enum additions are additive for ERC (envelope v1),
power (v1), sim (v3), and PEX (v2), under the pre-1.0 enum policy in
[json-contract.md](json-contract.md). The new coverage version defines actual
checked work more precisely than the old two-field convention: an errored
PEX row or unavailable simulation measurement can now coexist with
`nothing_checked=true`. That disclosure never replaces its real error.
DRC v1 reports remain readable, but new DRC output is v2. Old explicit
`nothing_checked=true` remains nonqualifying; legacy external KLayout reports
also remain unknown even if their old `rules_checked` names categories.

Completed reports, including refusal reports, remain on stdout in the
requested format. Exit 1 still means an invocation/application error and
uses the existing stderr error envelope; exit 2 remains argparse usage.
New statuses and exit 4 are intentional automation compatibility changes:
consumers must require the documented qualifying status, not merely parse a
report or look for a zero violation count. `drc --check/--rerun` keeps its
existing match/drift verification exit contract; matching an old report is
not a new claim that its checks qualify.

## Signoff consumption

Plain, numbered and compound signoff evidence cannot qualify with known
zero work, unknown execution or malformed common coverage. The common gate
runs after actual failures/errors and critical metric validation. Thus a
real failed check still reports `check_failed`, rather than being masked by
its coverage gap. A status-only apparent pass with a common coverage refusal
gets `nothing_checked`, `coverage_unknown`, or `malformed_coverage` in a
numbered citation. Native `not_checked`/`coverage_unknown` statuses are also
non-successful in their own right. Plain check details expose
`coverage_state`, zero reasons and validation errors when present.

One artifact may carry more than one scope, and the zero-work refusal is
then a question about the artifact, not about one scope (issue #2179). A
`klt erc` envelope reports an antenna scope at `coverage` and a connectivity
scope at `erc_coverage`; `klt erc` carries an antenna-ratio table for sky130
only, so on every other PDK the antenna scope is known-zero for every layout
while the connectivity rules run to completion. Signoff therefore reads both
before refusing such a report as empty, and envelope-aggregation mode grades
a `not_checked` ERC check on `erc_status` when — and only when — the
connectivity scope is present and reached a verdict. An envelope carrying no
second scope, including every one written before #2179, grades exactly as it
did: absence of a scope is absence of evidence, never a pass. Nothing here
widens a *failing* verdict, and no exit code changes.

Missing legacy coverage is readable and never relabeled full. This migration
does not retroactively require versioned coverage from every historical
artifact. Optional functional-verification `coverage` percentages retain
their separate Verilator meaning; executed test counts and all-skipped
refusal (#2096/#2107) remain authoritative independently. Common partial
coverage adds no new *hard refusal* — see "Signoff qualification" above for
the qualification gate and disclosure Phase 2 adds instead — and existing
stricter verb-specific gates remain intact.

## Public skip-capable inventory and remaining adapters

This inventory distinguishes public checking paths from ordinary generators
that skip an irrelevant candidate during construction. The first six rows
above adopt v1 here. Remaining public analysis/checking paths retain their
existing contracts pending their own adapters and Phase 2 policy:

| Public path | Existing evidence / remaining work |
| --- | --- |
| `functional-verification` | `tests[]` and passed/failed/skipped counters, all-skipped refusal; optional Verilator percentages are a separate namespace. Do not overwrite that `coverage` object with this contract without a versioned migration. |
| `lvs` | `power_connectivity` can be unchecked and `body_verification` unverified/unchecked; layout comparison and body/power coverage need separate identities. |
| `extract` (including parasitics/SPEF) | Ignored layers, abstracted cells and missing R/C coefficient coverage; extraction success does not assert every parasitic mechanism was modeled. |
| `sta`, post-route timing in `place-and-route` | Constrained/unconstrained timing, optional hold paths/corners and stage-dependent reports. A positive unconstrained sentinel is already nonqualifying. |
| `synthesize`, partial `place-and-route` | Missing library capabilities and optional timing, selected stop stage and omitted physical checks. Generation success alone does not establish physical/timing coverage. |
| `precheck`, `socket-check` | Named pass/fail/skipped checks with skip reasons for optional grid, layer, pin and reserved-region inputs. Must distinguish omitted optional checks from requested unavailable work. |
| `sim --op-lint` | Unknown rails can skip drain/body checks with a warning; operating-point status has a separate contract from simulation sweeps. |
| `yield` | Measurements without spec limits can be skipped (explicitly requesting one refuses); no eligible measurement already refuses. Missing yield targets are deliberate statistical reporting, not zero sample work. |
| `congestion` | Nets lacking enough usable located pins are omitted from estimation; the estimator needs a modeled-net scope before adopting checked-work identities. |
| `gen-compose` rule audit and `layout-plan execute` verification | Candidate routing skips are generation details; optional/unsupported verification and generically modeled primitive checks are separate potential assessment scopes. |
| `signoff`, `report`, report verification (`--check`/`--rerun`) | Consumers/orchestrators. They must retain producer coverage, applicability and failure precedence; report integrity checks do not manufacture design checks. |

Read-only metadata/discovery (`deck`, `pdk`, `kb`), geometry conversion and
LEF abstraction do not assert a checking verdict merely because they omit
irrelevant data. `equiv`'s unconstrained initial-state reasoning is a formal
proof model, not skipped execution. Those word matches are not coverage
adapters. `ring-check` already refuses an empty/uncheckable ring; its optional
scopes can adopt the common format separately. Phase 1 did not claim to
complete all inventory adapters; the rollup rule above is the Phase 2 success
policy, and the six per-path adapters remain outstanding against it.

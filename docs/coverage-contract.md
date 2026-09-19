# Checked-work coverage, version 1

`klt` distinguishes completed checks, requested work it skipped, work that
was inapplicable, and execution it cannot measure. This is Phase 1 of
[#1988](https://github.com/2AMLogic/klayout-tools/issues/1988), implemented in
[#2108](https://github.com/2AMLogic/klayout-tools/issues/2108). The contract
supports Phase 2's partial-success policy (#2109); Phase 1 changes zero-check
success and unknown execution, without imposing that later policy.

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
array of scope components (for example `limit:["tt/1.800V/27C","vout","max"]`).
JSON quoting prevents names containing separators from colliding. These are
identities within a report, not content hashes or identities across design
revisions; provenance provides the latter connection.

`coverage_state()` derives `full`, `partial`, `zero`, `unknown`, `malformed`,
or `legacy`. Full and partial require known, nonempty checked work; partial
also has requested skips. A full scope does not assert physical signoff for
every possible rule or analysis: curated decks still disclose their limited
rule scope, and antenna/EM adapters identify their scope explicitly.

## Producer and compatibility mapping

| Public path | Checked work and exclusions | Phase 1 result and CLI |
| --- | --- | --- |
| `drc --engine curated` | Executed rule IDs; absent input layers produce `absent_input_layer` skips. Deck scope and uncovered stream layers remain in legacy fields. | Violations retain `violations`/3; known zero becomes `not_checked`/4; otherwise `clean`/0. Empty-deck and all-skipped reason codes remain. |
| `drc --engine klayout` | RDB categories are declarations, not proof of execution. `rule_categories` retains them; `rules_checked` and common `checked` contain only rule IDs with actual findings. The rest is `unmeasured_rule_execution`, so `known=false`. | Findings retain `violations`/3; otherwise `coverage_unknown`/4. **DRC envelope v2** versions the corrected `rules_checked` meaning for this engine. Neither an empty RDB nor a nonempty category list proves clean execution. There is no instrumentation interface claiming known full coverage in this release. |
| `erc` antenna | One ID per actual gate/non-gate level compared to a limit. Missing PDK/limit are skips. The gate reference level is inapplicable. `scope="antenna"`; connectivity findings remain independently authoritative. | Any antenna/connectivity finding retains `violations`/3; zero graded antenna levels becomes `not_checked`/4; otherwise `clean`/0. A per-gate `pass_partial` remains disclosed; Phase 2 owns the overall partial policy. |
| `power` EM | Net/island/edge IDs with both solved current and declared limit. Missing limit/current are skips. Without a requested solve the EM work is inapplicable. `scope="electromigration"`. | Additive top-level `status` mirrors `em_verdict.status`, or `not_checked` without a solve. `fail` exits 3, `not_checked` exits 4; existing `pass` and `pass_partial` exit 0. Network/IR data remain available on refusal. Existing signoff rules continue to reject `pass_partial`. |
| `sim` sweep | Applied min/max bounds per actual corner/measurement; unknown keys, null bounds, missing measurements, empty matrix and no requested measurements are disclosed skips. Deliberate characterization counts a produced observation as checked and its omitted limit as inapplicable. | Existing `error`/4 and `fail`/3 win. With neither, zero checked work becomes `not_checked`/4, otherwise `pass`/0. A launched corner or parsed declaration alone is insufficient. Legacy counters still count their original corner/measurement declarations. |
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

Missing legacy coverage is readable and never relabeled full. This migration
does not retroactively require versioned coverage from every historical
artifact. Optional functional-verification `coverage` percentages retain
their separate Verilator meaning; executed test counts and all-skipped
refusal (#2096/#2107) remain authoritative independently. Common partial
coverage alone does not add a new refusal in Phase 1, and existing stricter
verb-specific gates remain intact.

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
scopes can adopt the common format separately. This issue does not claim to
complete all inventory adapters or the Phase 2 success policy.

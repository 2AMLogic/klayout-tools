# `klt functional-verification`

Run a cocotb testbench against RTL sources through Icarus Verilog or
Verilator, and report a per-test pass/fail/skip breakdown plus optional
coverage — Phase 3 of
[Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391) ("adopt the
digital engine class — Yosys + OpenROAD — RTL→GDS as a first-class `klt`
flow"), and the hard gate behind
[`klt eval`](eval.md)'s `valid` field (issue #387).

```
klt functional-verification <request> [--mutations <proposals>] [--format text|json]
```

This is the build phase carried by two accepted Phase 1 spikes — read them
first; where this document and the code disagree with either, this document
(and the code) win:

- [`docs/design/cocotb-verification-spike.md`](../design/cocotb-verification-spike.md)
  (#398) — the engine survey, the invocation surface (cocotb 2.0's
  first-party Python `Runner` API, never a generated Makefile), the
  `results.xml`/coverage extraction recipes, and this request/response
  contract.
- [`docs/design/digital-flow-contracts-spike.md`](../design/digital-flow-contracts-spike.md)
  section 6 (#399) — the same contract restated alongside the
  synthesis/place-and-route ones, and the exit-code table.

Reviewing a testbench this verb runs — or the RTL it verifies — for false
positives, coverage gaps, or the bug classes a testbench should catch?
See [`docs/guides/digital-review/`](../guides/digital-review/README.md),
the per-concern digital review-bar guides (issue #1587): in particular
[`cocotb-tb-review.md`](../guides/digital-review/cocotb-tb-review.md) and
[`cocotb-tb-style-guide.md`](../guides/digital-review/cocotb-tb-style-guide.md)
for the cocotb testbenches this verb drives, and
[`rtl-bugs.md`](../guides/digital-review/rtl-bugs.md) /
[`rtl-protocol-cdc.md`](../guides/digital-review/rtl-protocol-cdc.md) /
[`rtl-spec.md`](../guides/digital-review/rtl-spec.md) for the RTL side. The
index's evidence table maps each checklist item that depends on tool
evidence to the response field on this page that supplies it.

Like `klt lvs`/`klt sim`/`klt synthesize`, this verb takes a **request
document** — RTL sources plus a testbench module plus engine/coverage
options is richer than a flag line carries cleanly — not positional file
args.

- `<request>` — a path to a request JSON file, `-` to read the request from
  stdin, or an inline JSON object string (the same three forms `klt lvs`
  accepts). Relative paths inside the request (`sources`, and the
  `testbench.module` file — or, if given, `testbench.search_path`) resolve
  against the **request file's own directory**; for the stdin/inline forms,
  against the current working directory.
- `--format` — `text` (default, a human-readable summary) or `json`.
- `--mutations <proposals>` — optional, issue #1592: run mutation testing
  against the same `<request>` — see "Mutation testing: `--mutations`"
  below.

## Engines

`request.engine` selects the simulator: `"icarus"` (default) or
`"verilator"`. Both are invoked *through cocotb*, never directly — the
testbench is simulator-agnostic Python, which is the whole point of cocotb
as the harness layer (the spike ran the identical `test_gcd.py` against both
and got the same pass/fail structure).

| Engine | Why you'd pick it | Cost (spike's worked example) |
| --- | --- | --- |
| `icarus` | The CI-friendly default — an interpreter, so there is no native compile step. | ~0.7 s build + run |
| `verilator` | Required for coverage; a compiler, so it wins once a design runs enough cycles for per-cycle interpretation overhead to dominate. | ~8 s build + run |

The Verilator tax is fixed per invocation, so it **compounds** inside a
design-space-exploration loop (N candidates × that build cost) in a way it
does not for a single per-PR run — budget it accordingly.

### Requirements

- **cocotb** (`pip install cocotb`). It is deliberately *not* a `klt`
  dependency: cocotb 2.0 refuses to run on Python 3.14+, while this repo
  supports Python 3.10+ with no upper bound, so pinning it would break `klt`
  installs that never verify anything. A missing install is a clear,
  actionable error (exit 1), never a traceback — the same posture `klt
  synthesize` takes toward a missing `yosys` binary.
- **`iverilog`** or **`verilator`** (plus `verilator_coverage` for coverage
  runs) on `$PATH`, matching the requested engine.

## Never trusting an exit code

The spike observed the *same* deliberately-failing regression exit `2`, `1`,
and `0` through three different invocation paths — a `make`-wrapped run
reports Make's own convention, not the simulator's, and cocotb's own
`Runner.test()` may either `sys.exit()` or return normally on a run with
failing tests. So this verb:

- invokes cocotb's Python `Runner` API (`build()` → `test()`), never a
  generated Makefile;
- **discards** whatever exit code the simulator process produced;
- parses the run's own `results.xml` itself for every count and the final
  verdict — a `<testcase>` with no child is a pass, a `<failure>` child is a
  failure, a `<skipped>` child is a skip.

cocotb's `get_results()` helper is deliberately *not* used as the source of
truth: its `num_tests` counts skipped tests too, so it cannot produce the
passed/failed/skipped split this contract reports.

Two related consequences:

- **A run that produced no `results.xml` is exit 1, not a pass.** Verified
  live: a testbench module that fails to import leaves cocotb exiting `0`
  with no results file at all.
- **A regression that registered zero `@cocotb.test()` functions is exit 1,
  not a vacuous pass.** It verified nothing, and `status: "pass"` there
  would hand `klt eval` a `valid: true` for a design nothing ever checked.

## Artifacts

Everything a run produces is written to `.klt/functional-verification/` next
to the request file — the same "next to the input" default `klt sim`/`klt
synthesize` already use — and kept, never deleted:

| Artifact | What it is |
| --- | --- |
| `results_<engine>.xml` | The raw cocotb results file every count in the response is derived from (echoed as `environment.results_xml`). |
| `sim_build_<engine>/` | cocotb's build directory (the compiled model / `.vvp`). |
| `build_<engine>.log` / `test_<engine>.log` | The engine's own transcripts. These are captured to files rather than inherited, so simulator chatter can never corrupt `--format json`'s stdout. |
| `coverage.info` | lcov-format coverage, on coverage runs only (see below). |
| `klt_sdf_annotate.v` | The generated `$sdf_annotate` elaboration root, on `options.sdf` runs only — kept, so the exact annotation call a run used is inspectable after the fact. |
| `klt_sdf_dut_wrapper.v` | The generated transparent pass-through wrapper the DUT is nested under, on `options.sdf` runs only — works around Icarus's inability to resolve a bare top-level-port `INTERCONNECT` entry against a module elaborated as its own `-s` root (issue #1056). Kept alongside `klt_sdf_annotate.v` for the same reason. |

## Coverage

`options.coverage: true` **requires `engine: "verilator"`** — Icarus has no
coverage path through this flow at all, so the combination is rejected with
exit 1 rather than silently ignored.

A coverage run adds Verilator's `--coverage --trace` build args, then
post-processes the emitted `coverage.dat` with two `verilator_coverage`
passes: `--write-info` (the portable lcov `.info` artifact the response
references **by path**, matching the "artifacts are paths, not inlined
blobs" rule `klt sim`'s waveforms and `klt extract`'s netlists already
follow) and `--report summary` (the line/toggle/branch/expr percentages).
Coverage requested but not producible (no `coverage.dat`, a
`verilator_coverage` failure) is exit 1 — never a silent `coverage: null`,
which would be indistinguishable from "not requested".

Functional coverage (`cocotb-coverage`-style bins defined in the testbench)
is out of scope for this contract; `coverage` is structural coverage only.

## Compile-time defines, build args, and includes

A PDK's own behavioural Verilog cell models commonly gate their content
behind compile-time `` `ifdef ``s the caller is expected to define -- e.g.
`USE_POWER_PINS` (whether cell modules carry supply pins at all) and
`FUNCTIONAL` (zero-delay behavioural models vs. SDF-annotatable timing models
under the same guard). `options.defines`, `options.build_args`, and
`options.includes` forward straight through to cocotb's own
`Runner.build(defines=..., includes=...)` (and the accumulated `build_args`
list) -- exactly the knobs Icarus/Verilator's own compile-time invocation
already exposes, with no translation in between.

Without these fields, the only way to define such a macro was a tiny
"defines" Verilog file listed *first* in `request.sources`, relying on the
fact that a `` `define `` set while compiling one file in a multi-file
`iverilog`/Verilator invocation stays in effect for every file compiled
afterward (no per-file preprocessor scoping unless something calls
`` `resetall ``). That still works, but it is a preprocessing-order
accident -- the defines file must sort/list before anything that consumes
the macro, and the requirement is invisible from the request schema itself.
`options.defines` makes the same thing an explicit, order-independent request
field.

`options.build_args` **composes with**, rather than replaces, the fixed
`--coverage --trace` args a `options.coverage: true` run already adds (see
"Coverage" above) and the SDF back-annotation args an `options.sdf` run
already adds (see "SDF back-annotation" below): the effective build args are
always `[<coverage args>] + [<sdf args>] + options.build_args`, in that
order, so a user-supplied flag is appended last and can still override an
earlier default if the two conflict.

`options.includes` resolves each entry relative to the request file's own
directory -- the same convention `sources` and `testbench.module` already
use -- for a cell library split across multiple files with `` `include ``
directives.

## Reproducibility: `random_seed`

cocotb's regression manager seeds its own `random` module per run and logs
the value it used in `results.xml`'s `<property name="random_seed">`
element (verified live, `docs/design/cocotb-verification-spike.md` section
4) — but does not accept one back in as a request-level input by default.
`options.random_seed` closes that gap end to end:

- **Request → `Runner.test()`.** `options.random_seed`, when given, is
  forwarded to `Runner.test()`'s own `seed` parameter, which sets
  `COCOTB_RANDOM_SEED` in the simulator subprocess's environment — a pinned
  seed reproduces cocotb's own seeded `random` module state run-to-run, not
  merely the same *logged* value after the fact.
- **`Runner.test()` → response.** Whether pinned or left to cocotb's own
  generator, the *effective* seed is always echoed back in
  `environment.random_seed`, read from `results.xml`'s own `<property>`
  element — the same artifact-derived-truth discipline every other count in
  this contract already follows. An unpinned run still gets a
  randomly-generated seed this way, worth capturing to reproduce that
  *specific* run later by feeding it back in as `options.random_seed`.

This is the same reproducibility bar `klt sim`'s Monte Carlo seeding and
`klt lvs`'s `environment` hashes already set (issue #423) — a stored CI
result's `environment.random_seed` is enough to reproduce it exactly.

## SDF back-annotation: `options.sdf`

`options.sdf` re-runs a **gate-level** regression with real post-route delays
back-annotated from an SDF file — the delay-annotated leg of
[`docs/design/post-route-sta-survey.md`](../design/post-route-sta-survey.md)
§4.3. The natural producer of that file is [`klt
place-and-route`](place-and-route.md)'s own `post_route_sdf` field
(`spef_sta.sdf_path`), but any IEEE-1497 SDF works:

```json
{
  "sources": ["gcd_route.v", "/pdk/sky130A/libs.ref/sky130_fd_sc_hd/verilog/primitives.v",
              "/pdk/sky130A/libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v"],
  "hdl_toplevel": "gcd",
  "testbench": { "module": "test_gcd" },
  "options": { "sdf": { "file": ".klt/place-and-route/gcd_route.sdf", "corner": "typ" } }
}
```

The **same** testbench the zero-delay gate-level check already used runs
unmodified — that is the point: the coverage signal §4.3 asks for is *"does
the testbench's own pass/fail outcome change once real delay is present?"*.
Everything below was verified live against Icarus Verilog 13.0
([`docs/design/sdf-annotate-feasibility-spike.md`](../design/sdf-annotate-feasibility-spike.md),
issue #962).

**Icarus only.** `options.sdf` with `engine: "verilator"` is rejected with
exit 1 — Verilator has no `$sdf_annotate` path at all, so silently ignoring
the block would report a zero-delay verdict as if it were annotated. This is
the mirror image of `options.coverage`'s Verilator-only rejection.

**Icarus 13.0 or newer.** `-ginterconnect` — mandatory here, since a
post-route SDF's net delays are entirely `INTERCONNECT` entries — does not
exist before Icarus 13.0. On 12.0 (Ubuntu noble's distro package, for
instance) `iverilog` rejects the flag outright with `Unknown/Unsupported
Language generation interconnect` and exit 255. This verb probes the resolved
`iverilog -V` version and rejects the request up front with that reason
(issue #1004), rather than letting a raw compiler error surface from four
layers down. A version string it cannot resolve or parse is *not* treated as
a failure — the probe is a courtesy, and a real incompatibility still fails
the build.

**Not with `FUNCTIONAL` cell models.** `options.sdf` together with a
`FUNCTIONAL` entry in `options.defines` is exit 1. A PDK puts its zero-delay
behavioural models and its SDF-annotatable *timing* models in the two branches
of the same `` `ifdef FUNCTIONAL `` guard, and only the non-`FUNCTIONAL`
branch carries the `specify` blocks an SDF's `IOPATH` entries annotate — so
the combination asks for two incompatible things. At best the annotation
matches nothing; on Icarus 12.0 the run silently mis-simulates outright (every
flop samples `x`, no error raised — issue #1004). Either way the verdict is
quietly wrong, which is what this feature exists to prevent, so it is rejected
at request-validation time rather than run. Drop `FUNCTIONAL` to re-simulate
with real delays, or drop `options.sdf` to keep the zero-delay run.

**How the annotation is wired.** `$sdf_annotate` is a Verilog system task and
must be called from an `initial` block inside some elaborated module — and a
cocotb regression has no such module, since `hdl_toplevel` *is* the DUT. So
this verb generates a second, otherwise-empty **elaboration root** carrying
the call (the same idiom cocotb's own Icarus runner uses for its waveform-dump
module). A bare top-level-port `INTERCONNECT` entry (one endpoint a primary
input/output rather than an internal `<inst>.<pin>`) additionally needs the
DUT to be a *nested child instance*, not its own `-s` root — Icarus cannot
resolve a bare port identifier against a module elaborated as its own root at
all (issue #1056). So a second generated module, a transparent pass-through
wrapper, re-declares `hdl_toplevel`'s exact port list, instantiates the real
(unmodified) DUT as a nested child, and becomes the new elaboration root in
`hdl_toplevel`'s place:

```verilog
module klt_sdf_dut_wrapper (
  input a,
  ...
);
  gcd klt_sdf_dut (
    .a(a),
    ...
  );
endmodule

module klt_sdf_annotate();
  initial $sdf_annotate("/abs/path/gcd_route.sdf", klt_sdf_dut_wrapper.klt_sdf_dut);
endmodule
```

The wrapper's ports carry the DUT's exact names and widths, so the DUT's own
hierarchy is untouched and every `dut.<port>` handle in the Python testbench
keeps resolving exactly as before, now via the wrapper. The embedded path is
always **absolute**: `vvp` runs with its own working directory, and a relative
path there is one `SDF WARNING` away from a silent zero-delay run. The build
gains `-gspecify -ginterconnect -s klt_sdf_annotate -T <corner>` plus a second
`-s klt_sdf_dut_wrapper` root (from `hdl_toplevel` itself becoming the
wrapper) — `-gspecify`/`-ginterconnect` are both mandatory (without
`-gspecify`, Icarus discards the annotation call itself and runs at zero
delay; without `-ginterconnect`, every `INTERCONNECT` entry — which is exactly
what a post-route SDF carries — fails at run time).

**Not with `request.parameters`.** cocotb's own Icarus parameter-override
syntax is always `-P<hdl_toplevel>.<name>=<value>`; once `hdl_toplevel`
becomes the generated wrapper above, that would silently target the wrapper
(which declares no parameters) instead of the real DUT. `options.sdf`
together with a non-empty `parameters` is therefore rejected at
request-validation time (exit 1) rather than risking an override that looks
applied but was not.

**Corner selection is a compile-time flag.** `options.sdf.corner`
(`"min"`/`"typ"`/`"max"`, default `"typ"`) maps to `iverilog -T`, which picks
one member of each SDF `min:typ:max` triplet. It is *not* an `$sdf_annotate`
argument: Icarus ignores every argument past the second ("`$sdf_annotate`
currently only uses the first two argument", its own wording), so a corner
passed there would be silently ignored.

**Every SDF failure mode is non-fatal, so the transcript is scanned.** Icarus
reports an unopenable file, an unmatched instance, an `IOPATH` the cell's
`specify` block does not declare, or an `INTERCONNECT` without
`-ginterconnect`, and then **carries on**: `vvp` exits `0`, and cocotb duly
reports the zero-delay verdict as a clean pass. This verb therefore scans both
engine transcripts for `SDF WARNING`/`SDF ERROR` and fails the run (exit 1) on
any line found — the difference between "the design was re-verified with real
delays" and "the design was re-run at zero delay and nobody noticed". One
class is exempt: `TIMINGCHECK not supported`, which fires on *correct* input
(a real `write_sdf` emits `TIMINGCHECK` sections and Icarus implements SDF
delays but not SDF timing checks) and does not affect the delays it does
apply.

Only a **well-formed** diagnostic counts — one shaped
`SDF WARNING:`/`SDF ERROR:` followed by Icarus's own `<file>:<line>:`
locator. A marker-bearing line without that shape is a *corrupted* line, not
a diagnostic, and is ignored: Icarus's C-level diagnostic output and cocotb's
Python logging share one stdout file descriptor, so under a non-tty capture
(a caller piping this verb's output without `PYTHONUNBUFFERED=1`) a flush
from one can land mid-line inside a not-yet-flushed line from the other and
splice them together. Before this check, such a splice could eat the
`TIMINGCHECK` substring off a benign warning and fail a run whose annotation
had applied perfectly. Ignoring the spliced line cannot hide a real problem:
Icarus emits one diagnostic *per failing SDF entry*, so a real failure
arrives in volume while a splice corrupts only the single line the
interleaved flush landed in.

**Known limitation, inherited from Icarus**: no setup/hold violation
*reporting* on this path — it models delays only, so `$setuphold`-driven `X`
propagation on a violated flop does not happen and this path is *less*
pessimistic than a commercial gate-level sim. Slack numbers remain OpenSTA's
job ([`klt place-and-route`](place-and-route.md)'s `spef_sta`). A genuine
timing failure still surfaces here as a *functional* failure the testbench
catches, which is the signal this feature is scoped around.

An annotated run is identifiable from the JSON alone: `environment.sdf` is
`null` on an ordinary run and an object on an annotated one.

**`annotated: true` alone does not mean "every class in the SDF applied"**
(issue #1102). The exempted `TIMINGCHECK` class above is the normal case on
Icarus — every real `write_sdf` output emits `TIMINGCHECK` sections, and
Icarus drops every one of them, so `$setup`/`$hold`/`$width` checks that run
during the regression use the cell library's own Verilog placeholder timing,
not the characterised limits in the SDF, even though `annotated: true`.
`environment.sdf.partial` and `environment.sdf.dropped` make that
machine-readable instead of requiring a transcript hand-count:

```json
"sdf": {
  "file": "gcd_route.sdf",
  "corner": "typ",
  "annotated": true,
  "partial": true,
  "dropped": {
    "timingcheck": {
      "count": 708,
      "reason": "Icarus Verilog implements SDF delay annotation (IOPATH/INTERCONNECT) but not SDF TIMINGCHECK -- every TIMINGCHECK section in the SDF is dropped, so $setup/$hold/$width checks run against the cell library's own placeholder timing, not the characterised limits in the SDF"
    }
  }
}
```

`partial` is `false` and `dropped` is `{}` on a run where no benign
diagnostic class was filtered out of the transcript scan (delays *and* every
timing check applied cleanly). Both keys are additive, alongside the existing
`file`/`corner`/`annotated`.

## Mutation testing: `--mutations`

```
klt functional-verification <request> --mutations <proposals> [--format text|json]
```

Coverage measures what a testbench *executed*, not what it would *notice
broken* — "all tests pass, coverage 95%" is exactly the verdict a weak
testbench paired with a correct design produces, and also the verdict a weak
testbench paired with a buggy design produces. `--mutations` closes that gap:
it applies a set of hand- or agent-authored single-point RTL mutations, one
at a time, to an isolated build+test of the same design, and reports how many
the testbench actually caught. Whoever authors the `<proposals>` document
should read
[docs/guides/rtl-mutation-testing.md](../guides/rtl-mutation-testing.md)
first (issue #1593) — the proposer-side companion to
[`docs/guides/digital-review/`](../guides/digital-review/README.md)'s
reviewer-side guides. This is
[docs/design/mutation-testing-spike.md](../design/mutation-testing-spike.md)'s
own contract (issue #1592), ported from
[`boldaxolotl/booley`](https://github.com/boldaxolotl/booley)'s (Apache-2.0)
byte-exact mutation seam
(`src/klayout_tools/_vendor/mutation_variants.py`) — no HDL parsing anywhere
in this path; a proposal is a byte-exact source slice and its replacement,
validated against the pristine source, never inferred from Verilog structure.

**`<request>` is reused unchanged.** The exact same request document already
used for an ordinary pass/fail run — see "Request" below — is the request
used for a mutation run. No new field is added to that schema; `--mutations`
is purely additive at the CLI-flag layer.

**`options.coverage` and `options.sdf` cannot currently be combined with
`--mutations`** — both are exit 1. Coverage-per-mutant multiplies an
already-per-invocation-fixed Verilator cost by `N+1` (see "Engines" above)
in a way that has not been measured yet; `options.sdf` swaps in a generated
wrapper module as the elaboration root (see "SDF back-annotation" above)
that this path's isolated per-mutant build does not replicate, so running it
would silently produce a mutant build that diverges from the baseline's own.
Neither is designed yet — see
[docs/design/mutation-testing-spike.md](../design/mutation-testing-spike.md),
"Open questions".

### The baseline-must-pass gate

Before any proposal runs, `--mutations` runs `<request>` exactly as an
ordinary (non-mutation) invocation would. **If that baseline itself reports
`status: "fail"`, the whole run aborts with exit 1 before touching any
proposal.** Comparing mutant behavior against an already-broken baseline
cannot distinguish "the mutation broke it" from "it was already broken", so
a `mutation_score` computed there would be meaningless, not merely
incomplete.

### The `<proposals>` document

A second, separate file — `scope` plus `proposals[]`, booley's own
byte-exact shape:

```json
{
  "schema": "klt.functional_verification.mutation_proposals/1",
  "scope": ["modexp.v"],
  "proposals": [
    {
      "index": 1,
      "category": "boundary",
      "file": "modexp.v",
      "line": 75,
      "original_code": "mm_sum >= mm_m2",
      "mutated_code": "mm_sum > mm_m2",
      "detectability_argument": "Skips the first modular subtraction exactly when the pre-reduction sum equals 2*mod, leaving the partial product unreduced by one modulus."
    }
  ]
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Contract identifier + major version, matching this repo's request-side convention. Not validated. |
| `scope` | array\<string\> | Every RTL source file a proposal is allowed to name. **Must be a subset of `<request>.sources`** (the exact same strings `request.sources` uses) — a whole-document error (exit 1) if not, checked *before* the baseline ever runs. May be empty only alongside an empty `proposals[]`. |
| `proposals[].index` | integer | 1-based, unique, positive. A duplicate or non-positive index is a whole-document error (exit 1), before any build runs — the same "fail the whole request, don't run half a plan" posture `options.sdf` validation already takes. |
| `proposals[].category` | string | Free-text label (e.g. `operator_change`, `boundary`, `constant`, `polarity`, `bit_select`, `fsm_next_state`) or any caller-chosen value — echoed back in the response, never validated against a fixed enum. |
| `proposals[].file` | string | Must be a member of `scope`. A proposal naming a file outside `scope` is `status: "rejected"` for **that one proposal only** — not a whole-document failure, the same "one bad entry doesn't poison the batch" posture `klt drc`'s per-violation reporting already takes. |
| `proposals[].line` | integer | 1-based line `original_code` must anchor on. |
| `proposals[].original_code` | string | The **exact** byte-for-byte source slice being replaced — not a pattern, not trimmed/normalized. Must occur **exactly once** on the declared `line`; zero or multiple occurrences is `status: "rejected"` for that proposal (`"not found"` / `"ambiguous"`). |
| `proposals[].mutated_code` | string | The exact replacement text. Must be non-empty and byte-different from `original_code` — `status: "rejected"` otherwise. This catches a byte-identical no-op mutation, but **not** a semantically-equivalent-but-textually-different one (e.g. Verilog's `2'b1` and `1'b1` are both the integer `1`) — proposal authorship staying a semantically-aware task (human or agent), not a `klt`-internal generator, is exactly why automatic mutation generation is out of scope for this verb. |
| `proposals[].detectability_argument` | string | Optional. Free-text rationale, echoed back unmodified — never validated or acted on programmatically. |

### Response: the additive `mutation_testing` block

The baseline run's own response fields (`status`, `test_count`, `tests[]`,
`coverage`, `environment`, ...) are exactly as documented below in
"Response", unmodified — plus one new top-level block:

```json
{
  "mutation_testing": {
    "schema_version": 1,
    "proposal_count": 2,
    "valid_count": 2,
    "rejected_count": 0,
    "killed_count": 1,
    "survived_count": 1,
    "mutation_score": 0.5,
    "results": [
      {
        "index": 1,
        "category": "boundary",
        "file": "modexp.v",
        "line": 75,
        "status": "survived",
        "first_killing_test": null,
        "log_path": ".klt/functional-verification/mutants/mutant_1.log"
      },
      {
        "index": 2,
        "category": "bit_select",
        "file": "modexp.v",
        "line": 134,
        "status": "killed",
        "first_killing_test": "test_modexp_known_vectors",
        "log_path": ".klt/functional-verification/mutants/mutant_2.log"
      }
    ]
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `mutation_testing.schema_version` | integer | Versioned per *feature*, independent of the outer envelope's own `schema_version` — the same way `klt extract`'s `parasitics` sub-block could evolve independently of `devices[]`. |
| `mutation_testing.proposal_count` | integer | Total entries in `<proposals>.proposals[]`, before validation. |
| `mutation_testing.valid_count` | integer | `proposal_count - rejected_count`. The score's denominator. |
| `mutation_testing.rejected_count` | integer | Proposals that failed anchor validation (bad `file`/duplicate anchor/`original_code` not found or ambiguous on `line`/empty or identical text) or failed to build in isolation (a syntax error the mutation itself introduced) — never run against the testbench at all. |
| `mutation_testing.killed_count` / `survived_count` | integer | Of the `valid_count` proposals actually run: how many produced at least one test failure the baseline did not (`killed`) vs. reproduced the baseline's own all-passing structure exactly (`survived`). **A timed-out variant counts as `killed`** — ported verbatim from booley's own rule: "a timed-out mutant counts as detected because the mutation can wedge the design." |
| `mutation_testing.mutation_score` | number \| null | `killed_count / valid_count`, or `null` when `valid_count == 0` (every proposal was rejected, or the proposal set was empty) — never silently reported as a perfect or zero score. |
| `mutation_testing.results[]` | array\<object\> | One entry per proposal, in the order `<proposals>.proposals[]` declared them. `status` is `"killed"`/`"survived"`/`"rejected"`. `reason` (string) is present **only** on `"rejected"`, naming the validation/build failure verbatim. `first_killing_test` (string \| null) is the first `@cocotb.test()` name whose failure the baseline did not have, `null` on `"survived"`/`"rejected"`/a timed-out `"killed"`. `log_path` is the isolated variant's own captured build+test transcript (`null` for a proposal rejected before any build ran — an out-of-scope `file` or an anchor mismatch) — the same "artifacts are paths, kept, never deleted" convention this document's "Artifacts" section already establishes, extended per-mutant under `.klt/functional-verification/mutants/`. |

### Isolation and the per-mutant subprocess timeout

Each resolved proposal gets its **own build directory, from scratch** — no
incremental compile, no shared state with the baseline or with any other
mutant's build (each mutates the same source file path to different bytes in
sequence, restoring the pristine bytes immediately after that one mutant's
isolated build+test completes; the next mutant never sees a previous
mutant's bytes). This is a genuine simplification relative to booley's own
design: booley's `mutation_lock.py` caches per-mutant build directories
across warm re-runs to amortize a *live LLM creator agent's* repeated
proposal-generation cost, which does not apply here, since `klt` reads a
fixed, already-authored `<proposals>` document and does not itself generate
or cache anything — it was deliberately not ported (see the attribution
header in `src/klayout_tools/_vendor/mutation_variants.py`).

Every isolated build+test is bounded by a `klt`-level subprocess timeout
(600 seconds), **independent of and in addition to** whatever cycle bound
the testbench's own code has. cocotb 2.0.1's own `Runner.test()` has no
timeout parameter at all — every simulator invocation is a bare, unbounded
`subprocess.run(...)` — so a mutation that wedges the DUT's FSM, paired with
a testbench that `await`s an event with no cycle bound, would otherwise hang
the whole `--mutations` run forever. When this timeout fires, the variant is
killed outright and classified `"killed"` (per the rule above), never
`"rejected"`.

Cost model: total wall time is `(N + 1) × per-run cost`, sequential (each
isolated build is independent, so parallelizing them is a real, low-risk
future optimization — not attempted here). See "Engines" above for
per-engine per-run cost; `--mutations` defaults to `engine: "icarus"` the
same way the base contract does, for exactly this reason.

### Worked example: a real killed mutant and a real surviving mutant

[`examples/functional-verification/proposals-modexp.json`](../../examples/functional-verification/proposals-modexp.json)
reproduces `docs/design/mutation-testing-spike.md`'s own live-measured §2
finding on the same `modexp.v` worked example this document's "Worked
example" section below also uses — one mutation that survives the committed
`test_modexp.py` unnoticed, one that is correctly killed:

```console
$ klt functional-verification examples/functional-verification/request-modexp.json \
    --mutations examples/functional-verification/proposals-modexp.json
engine: icarus 13.0
hdl_toplevel: modexp
testbench: test_modexp
status: pass
tests: 2  passed: 2  failed: 0  skipped: 0

[passed] test_modexp_known_vectors  23750.0 ns
[passed] test_modexp_random  180480.0 ns

results_xml: .../.klt/functional-verification/results_icarus.xml
random_seed: 1

mutations: 2 proposed  2 valid  1 killed  1 survived  0 rejected  score: 0.50
  [survived] #1 boundary modexp.v:75
  [killed] #2 bit_select modexp.v:134  (killed by test_modexp_known_vectors)
$ echo $?
3
```

Mutation #1 (`mm_sum >= mm_m2` → `mm_sum > mm_m2`, the interleaved modular
multiplier's reduction-step boundary) is a real, reachable weakening of the
design that the committed testbench's randomized stimulus does not happen to
exercise — exactly the "all tests pass, coverage looks fine, and a real
correctness bug ships anyway" failure mode this feature exists to catch.
Mutation #2 (`exp_r[WIDTH-1]` → `exp_r[WIDTH-2]`, a wrong exponent bit) is
caught immediately by the directed known-vectors test. Both numbers were
captured live, not assumed — running the command above reproduces them
exactly.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The baseline passed **and** every valid proposal was killed (`survived_count == 0`), including `proposal_count == 0` (an explicitly empty proposal set is not an error). |
| `1` | Any of the base contract's own exit-1 causes, **plus**: the `<proposals>` document is malformed/unparseable, a `<proposals>.scope` entry is outside `request.sources`, `options.coverage`/`options.sdf` combined with `--mutations`, the baseline itself failed (the baseline-must-pass gate), or every proposal ended up `"rejected"` (`valid_count == 0` with `proposal_count > 0`) — mirrors the base contract's own "a regression that registered zero tests is exit 1, not a vacuous pass": a mutation run that tested nothing must never look like a clean `mutation_score: null` success. |
| `2` | Usage error — from argparse. |
| `3` | Ran successfully; the baseline passed but at least one valid proposal `"survived"` (`survived_count > 0`) — the same "ran fine, found violations" convention [`klt drc`](drc.md) already uses. |

### Out of scope

- **Automatic mutation generation.** Proposal authorship stays outside
  `klt`, by hand or by agent — a naive syntactic generator would produce
  false-positive-looking noise (e.g. flipping `1'b1` to `2'b1`, both the
  integer `1`) without taking on real HDL semantics.
- **A named evidence-tier threshold.** `mutation_testing` ships as an
  optional response field only; no change is made to
  [docs/design-evidence-tiers.md](../design-evidence-tiers.md) T1 item 9's
  wording. See
  [docs/design/mutation-testing-spike.md](../design/mutation-testing-spike.md)
  section 4 for the reasoning.
- **Parallelizing the isolated per-mutant builds.** Each is already
  independent — a real, low-risk future speedup, not attempted here.
- **`options.coverage` + `--mutations` together.** See above.

## Request

```json
{
  "schema": "klt.functional_verification.request/1",
  "engine": "icarus",
  "sources": ["gcd.v"],
  "hdl_toplevel": "gcd",
  "testbench": { "module": "test_gcd", "testcase": null },
  "options": {
    "coverage": false,
    "timescale": ["1ns", "1ps"],
    "random_seed": 1785780800,
    "defines": { "USE_POWER_PINS": null, "FUNCTIONAL": "1" },
    "build_args": ["-Wall"],
    "includes": ["cells"],
    "sdf": { "file": "gcd_route.sdf", "corner": "typ" }
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Request contract identifier + major version. Not validated — user-authored input, never emitted by this tool. |
| `engine` | string | `"icarus"` (default) or `"verilator"`. An unsupported value is an application error (exit 1). |
| `sources` | array\<string\> | RTL source file paths, resolved relative to the request. Required, non-empty. May point at original RTL **or** at `klt synthesize`'s `netlist_path` — a gate-level equivalence re-check against the same testbench needs no contract change, only a different `sources` value. |
| `hdl_toplevel` | string | The DUT module name. Required. |
| `testbench.module` | string | The Python test module **name** (`"test_gcd"`, not `"test_gcd.py"`), resolved as `<search dir>/<module>.py` where `<search dir>` is `testbench.search_path` if given, else the request's own directory. Required — this verb does not synthesize testbenches; the module is human- or generator-authored. |
| `testbench.search_path` | string | Optional. Directory to resolve `testbench.module` against, instead of the request's own directory — absolute, or relative to the request (the same convention `sources` entries already use). Lets one unmodified testbench module be shared by several requests (e.g. RTL, gate-level netlist, and layout-extracted views of the same design) that live in different directories. Omitted: unchanged default behavior — the module is resolved next to the request. |
| `testbench.testcase` | string \| array\<string\> \| null | Optional testcase-name filter; `null`/omitted runs every `@cocotb.test()` in the module. Filtered-out tests still appear in the report as `skipped`. |
| `options.coverage` | boolean | Defaults to `false`. `true` requires `engine: "verilator"` (see "Coverage"). |
| `options.timescale` | `[string, string]` | `[unit, precision]`, defaulting to `["1ns", "1ps"]`. Passed to **both** the build and test steps — Icarus elaboration otherwise fails the moment a testbench's `Clock(..., unit="ns")` meets an unset (default 1 s) simulator precision. |
| `options.random_seed` | integer \| null | Optional. Pinned to `Runner.test()`'s own `seed` parameter (`COCOTB_RANDOM_SEED`) when given; omitted/`null` lets cocotb generate its own. Either way the seed actually used is echoed in `environment.random_seed` (see "Reproducibility: `random_seed`"). |
| `options.defines` | object | Optional. String key -> string \| null value, forwarded unchanged to `Runner.build(defines=...)`. A `null` value defines the macro with no value (e.g. `` `define USE_POWER_PINS ``). Defaults to `{}` (see "Compile-time defines, build args, and includes"). |
| `options.build_args` | array\<string\> | Optional. Extra Icarus/Verilator build args, appended **after** the fixed `--coverage --trace` args a coverage run already adds and after any SDF back-annotation args (composed, not replaced — see "Coverage" and "SDF back-annotation"). Defaults to `[]`. |
| `options.includes` | array\<string\> | Optional. `-I` include directories, resolved relative to the request (same convention as `sources`). Forwarded to `Runner.build(includes=...)`. Defaults to `[]`. |
| `options.sdf.file` | string | Optional. Path to an IEEE-1497 SDF file, resolved relative to the request like every other path field, and back-annotated onto the design through Icarus's `$sdf_annotate` (see "SDF back-annotation"). Requires `engine: "icarus"` at version 13.0 or newer — `options.sdf` with `engine: "verilator"`, against a pre-13.0 `iverilog` (no `-ginterconnect`), or alongside a `FUNCTIONAL` entry in `options.defines`, is exit 1, never a silent no-op. A missing/unreadable file is exit 1 (issue #1002). |
| `options.sdf.corner` | string | Optional, one of `"min"`/`"typ"`/`"max"`, default `"typ"`. Selects one member of each SDF `min:typ:max` triplet, via the compile-time `iverilog -T` flag. Only valid inside an `options.sdf` block; an unknown key inside that block is exit 1 rather than silently ignored. |
| `parameters` | object | Optional. String key -> scalar value (integer, float, string, or boolean), forwarded unchanged to both `Runner.build(parameters=...)` and `Runner.test(parameters=...)`. Overrides Verilog `parameter` (or VHDL `generic`) values at elaboration time -- e.g. `{"WIDTH": 8}` to elaborate a design's `#(parameter WIDTH = 16)` at 8 bits instead of its default. cocotb's own per-engine backend translates each entry into the right flag (Icarus: `-P<toplevel>.<name>=<value>`; Verilator: `-G<name>=<value>`) -- this verb never needs to know that syntax itself. Omitted/empty is a no-op, identical to today's behavior. Non-empty together with `options.sdf` on Icarus is exit 1 (see "How the annotation is wired") -- the SDF top-level-port workaround elaborates a generated wrapper as the new `-s` root, and cocotb's parameter-override syntax would then silently target that wrapper instead of the real DUT. |

## Response

```json
{
  "schema_version": 1,
  "engine": "icarus",
  "hdl_toplevel": "gcd",
  "testbench": "test_gcd",
  "status": "fail",
  "test_count": 3,
  "passed_count": 2,
  "failed_count": 1,
  "skipped_count": 0,
  "tests": [
    { "name": "test_gcd_known_pairs", "status": "passed", "sim_time_ns": 520.0, "real_time_s": 0.0051 },
    { "name": "test_gcd_random_pairs", "status": "passed", "sim_time_ns": 4720.0, "real_time_s": 0.0471 },
    {
      "name": "test_gcd_deliberately_wrong_expectation",
      "status": "failed",
      "sim_time_ns": 130.0,
      "real_time_s": 0.003,
      "error_type": "AssertionError",
      "error_message": "gcd(48, 18): got 6, want 999 (deliberate failure)"
    }
  ],
  "coverage": null,
  "environment": {
    "engine": "icarus",
    "engine_version": "13.0",
    "cocotb_version": "2.0.1",
    "results_xml": "/abs/path/.klt/functional-verification/results_icarus.xml",
    "random_seed": 1785780800,
    "sdf": null
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per [`docs/json-contract.md`](../json-contract.md). |
| `engine` | string | Echo of the request's engine. |
| `hdl_toplevel` / `testbench` | string | Echo of the request's DUT / testbench-module identifiers. |
| `status` | string | `"pass"` (`failed_count == 0`) or `"fail"` (`failed_count > 0`). Never `"error"` in-band — a run that failed to *run* emits no envelope at all (see "Exit codes"). |
| `test_count` / `passed_count` / `failed_count` / `skipped_count` | integer | Derived from `results.xml`'s own `<testcase>`/`<failure>`/`<skipped>` structure. `test_count` includes skipped tests, so `passed + failed + skipped == test_count`. |
| `tests` | array\<object\> | One entry per `@cocotb.test()`, in the order cocotb ran them. `status` is `"passed"`/`"failed"`/`"skipped"`; `sim_time_ns`/`real_time_s` are `null` when the simulator did not report them. `error_type`/`error_message` are present **only** on `"failed"` entries, taken verbatim from the `<failure>` element's attributes. |
| `coverage` | object \| null | `null` unless `options.coverage: true`; otherwise `line_pct`/`toggle_pct`/`branch_pct`/`expr_pct` (numbers, or `null` for a category `verilator_coverage` did not report) plus `info_path`, an absolute path to the lcov `.info` artifact. |
| `environment` | object | Reproducibility block: `engine`, `engine_version` (the simulator's own version token, `null` if unresolvable), `cocotb_version`, `results_xml` — the absolute path to the raw evidence this report was derived from, so a stored verdict can be re-checked against it — and `random_seed` (the effective seed cocotb used, `null` only if `results.xml` lacked the property; see "Reproducibility: `random_seed`"), plus `sdf` (issue #1002) — `null` on an ordinary run, an object on an SDF-annotated one, so an annotated verdict is never mistakable for a zero-delay one from the JSON alone: `file`, `corner`, `annotated: true`, plus `partial` and `dropped` (issue #1102) — `partial` is `true` when any benign diagnostic class (currently only `TIMINGCHECK`) was filtered out of the transcript scan, and `dropped` names each such class with `{count, reason}`; see "SDF back-annotation" for the full shape. |

There is no shared `provenance` block: this verb's verdict depends on no PDK
and no rule deck (see `docs/json-contract.md` → "Shared `provenance`
block"), and `environment` is the contract's own reproducibility surface.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every test passed (`status: "pass"`). |
| `1` | Failed to run — bad request, unresolvable RTL source or testbench module, coverage requested on an engine that has none, `options.sdf` on an engine (or an Icarus older than 13.0) that has no usable `$sdf_annotate` path, `options.sdf` alongside a `FUNCTIONAL` define, an unresolvable/unreadable SDF file, an SDF annotation that did not fully apply (`SDF WARNING`/`SDF ERROR` in the transcript), missing cocotb/simulator install, build or elaboration error, simulator crash, no `results.xml` produced, or a regression that registered zero tests. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |
| `3` | Ran successfully; at least one test failed (`status: "fail"`). |

This is the same `0`/`1`/`2`/`3` trichotomy [`klt lvs`](lvs.md) and
[`klt drc`](drc.md) use — **not** `klt sim`'s four-way split. A cocotb
regression has no analogue of "this corner's simulator errored but the rest
of the batch is still trustworthy": either the build+run pipeline produced a
`results.xml` to report from, or it did not.

`--mutations` extends this table additively — see "Mutation testing:
`--mutations`" → "Exit codes" above.

## Composing into `klt eval`

`status: "pass"` → exit `0` → `valid: true`; `status: "fail"` → exit `3` →
`valid: false`. That is exactly [`klt eval`](eval.md)'s existing convention
(issue #387), so a gate needs no adaptation:

```json
{
  "gates": [
    { "check": "functional-verification",
      "args": { "request": "verify.json" } }
  ],
  "objective": { "check": "synthesize", "metric": "area_um2",
                 "polarity": "minimize", "args": { "request": "synth.json" } }
}
```

A partial result (2 of 3 tests passing) still collapses to one boolean at
the gate: `klt eval` reports that gate as `status: "fail"`, contributing one
`false` to `valid`, with the per-test detail available in this verb's own
`tests[]` for whoever wants to know *which* test failed. A run that never
produced evidence surfaces as `klt eval`'s own exit 1 — an optimizer must
never read "crashed" as "scored badly".

## Worked example

[`examples/functional-verification/`](../../examples/functional-verification)
holds the GCD worked example from the spike verbatim — an iterative-subtractor
GCD core (`gcd.v`), a cocotb testbench with three tests, one of them
deliberately failing (`test_gcd.py`), and two requests (Icarus; Verilator
with coverage):

```console
$ klt functional-verification examples/functional-verification/request.json
engine: icarus 13.0
hdl_toplevel: gcd
testbench: test_gcd
status: fail
tests: 3  passed: 2  failed: 1  skipped: 0

[passed] test_gcd_known_pairs  520.0 ns
[passed] test_gcd_random_pairs  4720.0 ns
[failed] test_gcd_deliberately_wrong_expectation  130.0 ns
    AssertionError: gcd(48, 18): got 6, want 999 (deliberate failure)

results_xml: .../.klt/functional-verification/results_icarus.xml
random_seed: 1785780800
$ echo $?
3
```

The identical testbench under `engine: "verilator"` reproduces the same
`TESTS=3 PASS=2 FAIL=1 SKIP=0` structure and adds coverage:

```console
$ klt functional-verification examples/functional-verification/request-verilator-coverage.json
...
coverage:
  line_pct: 100.0
  toggle_pct: 60.0
  branch_pct: 100.0
  expr_pct: 100.0
  info_path: .../.klt/functional-verification/coverage.info
```

Toggle coverage at 60% is correct, not a defect: the random test only drives
values up to 500 through a 16-bit datapath, so many high-order bits never
toggle — a real signal a designer (or an agent widening stimulus ranges)
would act on.

### `parameters`: overriding a Verilog `parameter` at elaboration time

[`examples/functional-verification/request-modexp-parameters.json`](../../examples/functional-verification/request-modexp-parameters.json)
elaborates the RSA-style `modexp.v` core (`#(parameter WIDTH = 16)`) at
`WIDTH=8` instead of its RTL default, via `request.parameters`:

```json
{
  "sources": ["modexp.v"],
  "hdl_toplevel": "modexp",
  "testbench": { "module": "test_modexp_parameters" },
  "parameters": { "WIDTH": 8 }
}
```

```console
$ klt functional-verification examples/functional-verification/request-modexp-parameters.json
engine: icarus ...
hdl_toplevel: modexp
testbench: test_modexp_parameters
status: pass
tests: 1  passed: 1  failed: 0  skipped: 0
```

The companion testbench (`test_modexp_parameters.py`) asserts
`len(dut.result) == 8` before running its stimulus — proof the override
reached elaboration itself, not just the request/response envelope — then
re-runs `test_modexp.py`'s own width-adaptive randomized cross-check against
Python's `pow`.

### `testbench.search_path`: one testbench, several views of the same design

The natural way to show that an implementation still satisfies its spec is to
run the **same, byte-for-byte unmodified testbench** against successively
lower-level views of a design — behavioural RTL, then a synthesized netlist,
then a netlist implied by layout extraction. Each view is a separate request
(different `sources`, different scratch directory) that would otherwise need
its own copy (or symlink) of the testbench. `testbench.search_path` lets both
requests point at one shared testbench directory instead:

```
repo/
  testbenches/
    test_gcd.py
  rtl-check/
    request.json          # sources: ["../gcd.v"]
  netlist-check/
    request.json          # sources: ["../synth/gcd.netlist.v"]
```

```json
// rtl-check/request.json
{
  "sources": ["../gcd.v"],
  "hdl_toplevel": "gcd",
  "testbench": { "module": "test_gcd", "search_path": "../testbenches" }
}
```

```json
// netlist-check/request.json
{
  "sources": ["../synth/gcd.netlist.v"],
  "hdl_toplevel": "gcd",
  "testbench": { "module": "test_gcd", "search_path": "../testbenches" }
}
```

Both requests resolve `test_gcd` via `testbenches/test_gcd.py` — the identical
file, never copied or symlinked — while each still uses its own `sources` and
its own `.klt/functional-verification/` scratch directory (per "Request",
`sources` and `testbench.search_path` resolve the same way: absolute, or
relative to the request's own directory).

## Out of scope

- **Testbench generation.** `testbench.module` is an input. This verb never
  synthesizes Verilog or Python; who authors testbenches (hand-written per
  block, or eventually generator-assisted the way `klt gen` assists layout)
  is a separate question.
- **Functional coverage.** Structural (line/toggle/branch/expr) coverage
  only — see "Coverage".
- **Waveform inspection / interactive debug.** Batch pass-fail + coverage is
  the contract; `--trace` is enabled on coverage builds as a side effect of
  Verilator's coverage recipe, but no waveform artifact is contracted.
- **Commercial simulators.** cocotb supports several; open-tooling posture
  keeps them out (the same reasoning that excludes Calibre/HSPICE-class
  tools from the LVS and SPICE contracts).

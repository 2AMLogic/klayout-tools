# Spike: mutation testing as a test-quality gate for `klt functional-verification`

**Status:** spike / proposal. Nothing here is scheduled, and nothing here
authorises implementation. Per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) →
"How capabilities arrive," a major capability arrives by spiking a design
first — candidate/prior-art survey, proposed JSON contract, wrap/build (here,
port/adopt/build) decision — and this document is that spike for issue #1586.
It follows the same structure the accepted digital-flow spikes already use,
most directly
[docs/design/cocotb-verification-spike.md](cocotb-verification-spike.md) and
[docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md) (survey →
contract → wrap/build decision → open questions).

**Forcing function:** `klt functional-verification`
([docs/cli/functional-verification.md](../cli/functional-verification.md))
reports pass/fail and, on Verilator, structural (line/toggle/branch/expr)
coverage — but coverage measures what a testbench *executed*, not what it
would *notice broken*. In this fleet the testbench is usually agent-written
in the same sweep as the RTL, so "all tests pass, coverage 95%" is exactly
the verdict a weak testbench paired with a correct design produces, and also
the verdict a weak testbench paired with a buggy design produces — coverage
alone cannot tell those two cases apart. `docs/design-evidence-tiers.md` T1
item 9 ("Testbenches shipped") currently accepts a testbench on presence
alone; there is no test-quality gate anywhere in the digital path today.

**Everything below was run, not recalled.** cocotb 2.0.1 (installed via this
repo's own `functional-verification` extra), Icarus Verilog 12.0 (this
sandbox's apt package), and Verilator 5.020 (this sandbox's apt package) were
installed and driven directly against `examples/functional-verification/`'s
real `modexp.v` RSA-style modular-exponentiation core and its committed
cocotb testbench (`test_modexp.py`) — the closest thing this repo has today
to "sky130-modexp's suite" as a committed, runnable fixture (the full
`sky130-modexp` canary itself lives in a separate downstream repo per
`docs/design/sky130-modexp-canary-signoff-status.md`; `modexp.v` is the same
RTL, vendored into this repo's own worked example). Four real single-point
source mutations were hand-applied to `modexp.v` with a byte-exact
find/replace script (mirroring the mechanism proposed below), each run
through `klt functional-verification` against the committed testbench, and
restored — §2 quotes the actual results, including one real surviving
mutant. `boldaxolotl/booley`'s cited source files were fetched live from
GitHub (`raw.githubusercontent.com`, `main` branch, 2026-09-09) and are
quoted verbatim in §1a; its repository metadata (license, activity) was
fetched via the GitHub REST API, the same verification discipline
[docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md) §1 and
[docs/design/cocotb-verification-spike.md](cocotb-verification-spike.md)'s
own opening paragraph established.

## 1. Survey

### 1a. `boldaxolotl/booley`'s mutation seam — the issue's named prior art

[boldaxolotl/booley](https://github.com/boldaxolotl/booley) ("The
open-source agentic RTL IDE") is Apache-2.0 licensed (confirmed against its
`LICENSE` file and the GitHub API's `license.spdx_id`, `"Apache-2.0"`), and
its default branch was active as recently as this spike's own research
(`pushed_at` reflects ongoing commits at fetch time). It is not a published
package — it is an internal module of an agentic RTL IDE product, invoked
through its own MCP tool surface (`src/booley/specialists/mutation_tester.py`,
~2,860 lines: the full agent-driving orchestration, not a general-purpose
library) — so "depend on it" was never really on the table; the operator's
own framing in the issue ("port this design and any useful code... not to
depend on the project") matches what the source confirms.

**The seam itself (`src/booley/dev_support/mutation_variants.py`, quoted in
full below since it is short and is the actual deliverable worth porting) is
~200 lines with no HDL-aware logic anywhere in it:**

```python
"""Materialize exact, isolated RTL mutation variants without parsing HDL.

The creator proposes one exact source replacement per mutation.  This module
owns the mutation seam: it validates those proposals against the pristine
source snapshot, applies one proposal at a time, and restores the original
bytes after the caller builds or simulates the variant.

No SystemVerilog or Verilog structure is inferred here.  Syntax and language
semantics belong to the project's configured compiler; this module deals only
in byte-exact source spans.
"""
```

Its `MutationVariantPlan.resolve()` classmethod is the entire validation
seam: for each proposal it (1) rejects a non-unique or non-positive `index`,
(2) rejects a `file` outside the declared scope, then (3) calls
`_resolve_one()`, which rejects an empty or byte-identical
`original_code`/`mutated_code` pair, finds every byte offset of
`original_code` in the pristine source, keeps only the offsets that fall on
the declared `line`, and raises `MutationVariantError` unless **exactly one**
such offset exists ("not found" vs. "ambiguous" as the two distinct error
messages). `applied(index)` is a context manager: write the mutated bytes,
`yield`, then **always** restore the pristine bytes in a `finally` block —
`__exit__` runs even if the caller's build/run raises. `_replace_exact()`
additionally re-checks that the pristine bytes at the resolved span are still
what was hashed at `resolve()` time before writing the mutant, so a mutation
plan resolved once and reused across many isolated runs can never silently
apply against source that changed underneath it. `source_fingerprint()`
hashes the whole pristine scope (`sha256:`-prefixed, the same convention this
repo's own `content_hash` fields already use) — a plan-level integrity check
independent of any one mutation's own anchor check.

The companion proposer's guide
(`src/booley/data/refs/rtl-mutation-testing.md`, also quoted from a live
fetch) states the workflow and the output contract:

> 1. Read every authorized RTL file and understand the datapath, control
>    logic, and externally observable behavior. Do not read testbench
>    sources.
> 2. Select N single-point mutations that a reasonable testbench should
>    detect.
> 3. Return each mutation as an exact source slice and replacement. Do not
>    edit files, run commands, add selector muxes, or insert marker
>    comments.
> 4. Booley checks that the exact original bytes occur once on the declared
>    line.
> 5. Booley builds and runs the untouched source as the baseline.
> 6. Booley applies one replacement alone, builds it in an independent
>    directory, runs the Target's complete test suite, and restores the
>    pristine bytes.

and names the mutation categories to prefer — "arithmetic or logical
operator changes ... comparison-boundary changes ... constant or
reset-value changes ... condition or polarity changes ... bit-select
changes ... FSM next-state changes ... signal substitutions or branch
swaps" — plus a rejection list (equivalent mutants, dead-code targets,
multi-fault combinations, overly broad slices) that maps directly onto §2's
own findings below.

**One design element in booley that does *not* transfer, and why:**
`src/booley/dev_support/mutation_lock.py` persists a `lock.json` plus
per-mutant build directories under a runtime dir, keyed on scope-file content
hashes, so a *live LLM "creator" agent's* expensive proposal-generation round
is cached and reused across warm re-runs. That machinery exists because
booley regenerates proposals itself, in-process, on every cold start. `klt`'s
proposed shape (§3) never regenerates proposals — the committed
`proposals.json` *is* the durable artifact, authored once outside `klt` — so
there is nothing to lock or cache: each `--mutations` invocation reads a
fixed proposal list and does a from-scratch isolated build per proposal,
exactly like `klt lvs`/`klt sim` re-run their own inputs from scratch on
every invocation. This is a genuine simplification the port earns by not
carrying booley's "agent-in-the-loop" concern into `klt`'s "agent-authors-a-
file-once, `klt` executes it deterministically" model — the same
authoring/execution split the issue's own "Proposal authoring stays outside
klt" line draws.

**Classification and kill semantics worth porting verbatim
(`src/booley/specialists/mutation_tester.py`):** a mutant is `detected`
(**killed**) when the isolated variant's simulation run reports at least one
test failure the baseline did not; `not_detected` (**survived**) when the
variant's tests all pass exactly like the baseline's; and `invalid`
(**rejected**) when the proposal itself failed anchor validation or the
isolated variant failed to build/elaborate at all — an `invalid` mutant is
excluded from both the numerator and the denominator of the score, matching
the guide's "Missing, malformed, skipped, or otherwise unresolved ... results
are inconclusive and never count as a kill." One more rule worth porting
exactly: **"A timed-out mutant counts as detected because the mutation can
wedge the design"** — a mutation that hangs the simulator has, by definition,
changed observable behavior (`done` never asserts), which is exactly the kind
of change a testbench with any liveness expectation would (or should) catch.

### 1b. YosysHQ MCY (Mutation Cover with Yosys) — adopt-as-dependency candidate, considered and rejected

[YosysHQ/mcy](https://github.com/YosysHQ/mcy) ("Mutation Cover with Yosys")
is real, maintained (commits as recently as 2026-08-05 per its GitHub API
`pushed_at`), and ISC licensed — and it is directly relevant here because
this repo already wraps Yosys for `klt synthesize`
([docs/design/yosys-synthesis-spike.md](yosys-synthesis-spike.md)), so "just
use the tool built by the same upstream project we already depend on"
deserves a real look, not a dismissal on name recognition alone. Its own README
(fetched live) states the actual mechanism: *"Given a self checking
testbench, MCY generates 1000s of mutations by modifying individual signals
in a post synthesis netlist. These mutations are then filtered using Formal
Verification techniques..."* Three concrete, load-bearing differences from
what this issue asks for:

1. **It parses/elaborates HDL by design** — the whole point of routing
   mutations through Yosys is that Yosys reads and elaborates the RTL to
   produce the netlist MCY mutates. The issue's explicit design constraint
   ("nobody parses HDL") is not a minor style preference here; MCY's formal-
   filtering step (SymbiYosys, a SAT-backed equivalence check that prunes
   "unwitnessable" mutants before ever running the testbench) is *only*
   possible because Yosys already has a full structural model of the design.
   Adopting MCY means accepting HDL parsing as a dependency, exactly the
   thing the issue rules out.
2. **Mutation locations are netlist signal names post-synthesis, not `(file,
   line)` in the original RTL.** The issue's proposed proposal shape is
   `(file, line, original_code, mutated_code)` against the RTL an agent read
   — a human- or agent-readable, source-anchored claim. MCY's mutation
   targets are wires/cells in an elaborated (`prep`-ped) design, which do not
   map cleanly back to a single RTL source line once synthesis has
   flattened, renamed, or optimized structure away.
3. **Heavier, GUI-first dependency chain.** MCY requires SymbiYosys plus a
   SAT/BMC solver backend as a mandatory filtering stage before the
   testbench ever runs, and its primary interfaces are `mcy gui`
   (browser-based exploration) and `mcy dash` — neither a natural fit for
   `klt`'s headless, `--format json`-first contract. `klt
   functional-verification` today needs only cocotb + iverilog/verilator;
   MCY would add a third, independent EDA toolchain (Yosys + SBY + solver)
   purely to get mutation information, when the existing simulator toolchain
   this verb already drives is sufficient for byte-exact source mutation.

**Verdict: rejected as a dependency to wrap.** MCY's formal-pruning idea —
discard mutants no test could ever witness, before paying a simulation cost
on them — is a genuinely good idea worth a forward-looking footnote once
`klt` has a formal-equivalence backend of its own (it does not today; `klt
lvs` is topological, not formal-equivalence), but adopting it now would mean
taking on HDL parsing and a second heavy EDA dependency chain to solve a
problem the byte-exact approach already solves without either.

### 1c. `mutpy` / `mull` — rejected, wrong target language

Both fetched live via the GitHub API for license/activity, the same
diligence §1a/§1b applied:

- **`mutpy/mutpy`** — Python-source AST mutation testing. License field is
  `"Other"` (no SPDX-recognized license asserted in the repo's own metadata);
  last pushed 2024-04-23 (over two years stale relative to this spike).
  Structurally wrong target regardless: it mutates *Python* source, and the
  only Python in this verb's input is the **testbench** (`test_modexp.py`),
  never the RTL under test. Mutating the testbench does not test the
  testbench's ability to detect an RTL bug — it is backwards from what T1
  item 9 needs, which is evidence the *testbench* would notice a *design*
  defect.
- **`mull-project/mull`** — Apache-2.0, actively maintained (pushed
  2026-07-31), LLVM/Clang IR-level mutation testing for C/C++. No
  Verilog/SystemVerilog frontend exists or is planned; the only way to point
  it at this repo's RTL would be through a Verilator-generated C++ model
  reinterpreted as the mutation target — a materially larger toolchain
  commitment (a Clang/LLVM plugin pipeline) for no advantage over the direct
  RTL-source-level approaches in §1a/§1b, and it would mutate the
  *simulation model*, several transformation steps removed from the RTL an
  agent actually wrote and would need to fix.

### Survey verdict

Port booley's seam design (§1a) — not booley itself, which is not
distributable as a dependency, and not MCY (§1b), which solves a related but
structurally different problem at the cost of exactly the HDL-parsing
dependency this issue rules out. `mutpy`/`mull` (§1c) target the wrong
language layer entirely and are not reconsidered further.

## 2. Measured rebuild cost on `modexp.v`'s suite (this environment)

Environment: this sandbox's apt-provided Icarus Verilog 12.0 and Verilator
5.020, cocotb 2.0.1 installed via `uv sync --extra functional-verification`.
Every number below is a real wall-clock measurement (`time`/`/usr/bin/time`),
not an estimate.

### Baseline (unmutated) full rebuild + run, Icarus, `modexp.v`

Three consecutive clean runs (`.klt/` removed before each, so every run pays
a full build, matching what an isolated per-mutant build directory would
also pay — Icarus has no incremental-compile path to shortcut):

| Run | Wall time |
| --- | --- |
| 1 | 1.25 s |
| 2 | 1.27 s |
| 3 | 1.27 s |

For comparison, the existing `examples/functional-verification/request.json`
(GCD, the smaller of this repo's two worked examples) reproduced
`docs/cli/functional-verification.md`'s own documented "~0.7 s" Icarus figure
closely: 0.83 s / 0.77 s / 0.79 s across three runs in this environment — the
small difference from the documented number is consistent with this being a
different (older, apt-packaged) Icarus build on different hardware, not a
regression in the number itself.

**`modexp.v` costs roughly 1.6x the GCD example's Icarus build+run** (a
larger core: a full interleaved modular multiplier with an outer/inner FSM,
vs. GCD's iterative subtractor) — still well under 1.5 s per isolated run.

### Verilator: blocked by a local cocotb/Verilator ABI mismatch, not a design blocker

`docs/cli/functional-verification.md`'s own documented Verilator figure
("~8 s build + run," §"Engines" table) was measured against Verilator 5.050
([docs/design/cocotb-verification-spike.md](cocotb-verification-spike.md)
§1, "Everything below was run" note). This sandbox's apt package is
Verilator 5.020. Attempting a Verilator run of `modexp.v` here fails at
compile time with:

```
error: 'evalNeeded' is not a member of 'VerilatedVpi'
```

— cocotb 2.0.1's bundled VPI shim (`cocotb/share/lib/verilator/verilator.cpp`)
calls a `VerilatedVpi` method that Verilator 5.020's C++ API does not have; a
version-skew problem between this sandbox's older apt Verilator and the
Verilator generation cocotb 2.0.1 was built against (5.050+, per the original
spike), not anything about the mutation-testing design. It was not chased
further (installing a newer Verilator from source is a real but orthogonal
provisioning task the original cocotb spike already flagged as a Phase 3/CI
concern, not this spike's to resolve). **The cost-model conclusion below does
not depend on resolving it** — it follows from the fixed-per-invocation-tax
structure `docs/cli/functional-verification.md` already documents, which
mutation testing simply multiplies by N+1.

### The cost model: N+1 full rebuilds, sequential

Every mutant needs its own from-scratch build (§1a: "builds it in an
independent directory" — an isolated build directory cannot share Icarus's
or Verilator's compiled state with the pristine baseline or with any other
mutant, since each carries different source bytes at the same file path).
So total wall time for `baseline + N proposals` is:

```
total ≈ (N + 1) × per-run cost
```

sequential, unless/until a follow-on parallelizes independent isolated build
directories (each is already fully independent — no shared mutable state —
so this is a real, cheap future optimization, not attempted here). On
`modexp.v`/Icarus (measured ~1.3 s/run):

| N proposals | Sequential wall time (measured basis) |
| --- | --- |
| 5 | ~7.8 s |
| 10 | ~14.3 s |
| 25 (booley's own guide's stated `MAX_COUNT`, §1a) | ~33.8 s |

— comfortably inside a per-PR CI budget on Icarus. The same table on
Verilator, using the *documented* (not locally re-measured, per the ABI note
above) ~8 s/run figure: N=10 → **~88 s**, N=25 → **~208 s**. This is the
sharpest instance yet of the compounding cost
`docs/cli/functional-verification.md`'s own "Engines" section already warns
about ("The Verilator tax is fixed per invocation, so it compounds inside a
design-space-exploration loop ... in a way it does not for a single per-PR
run") — mutation testing *is* exactly that N-candidates loop, every time it
runs. **Recommendation: `--mutations` runs default to `engine: "icarus"`,
the same documented default the base contract already uses**, and a caller
opting into `engine: "verilator"` for coverage-plus-mutation in the same
pass should expect the N+1 multiplier to dominate wall time.

### A real killed mutant and a real surviving mutant, on `modexp.v`

Four single-point mutations were hand-applied to `modexp.v` (each verified
to anchor exactly once, applied, run against `test_modexp.py` unmodified via
`klt functional-verification examples/functional-verification/request-modexp.json`,
then restored):

| # | File:line | `original_code` → `mutated_code` | Category (§1a's list) | Result |
| - | --- | --- | --- | --- |
| 1 | `modexp.v:75` | `mm_sum >= mm_m2` → `mm_sum > mm_m2` | comparison-boundary | **survived** — `status: "pass"`, 2/2 tests passed |
| 2 | `modexp.v:76` | `mm_sum >= mm_m1` → `mm_sum > mm_m1` | comparison-boundary | **survived** — `status: "pass"`, 2/2 tests passed |
| 3 | `modexp.v:134` | `exp_r[WIDTH-1]` → `exp_r[WIDTH-2]` | bit-select | **killed** — `status: "fail"`, both tests failed: `modexp(2,10,1000): got 576, want 24` |
| 4 | `modexp.v:152` | `outer <= outer - 1'b1` → `outer <= outer - 2'd2` | arithmetic/FSM-next-state | **killed** — `status: "fail"`, both tests failed with `did not assert done within 20000 cycles` (the FSM's terminal-count check, `outer == 1`, is unreachable once `outer` only ever takes even values — a real hang, correctly reported as a **test failure**, not a process hang, because `test_modexp.py`'s own `run_modexp()` helper bounds its wait with `timeout_cycles=20000` and raises; see "Open questions" below for why `klt` must not rely on every testbench doing this) |

Mutants #1 and #2 are **real, reproducible survivors on the committed
testbench** (re-run twice; identical `status: "pass"` both times, fixed
`random_seed`) — not equivalent mutants. Both weaken the same modular
reduction's boundary condition (`mm_red = (mm_sum >= mm_m2) ? ... : (mm_sum
>= mm_m1) ? ... : mm_sum`, the interleaved modular multiplier's core
subtract-if-too-big step). Whether `mm_sum` ever lands exactly on `mm_m1`
(`= mod_r`) or `mm_m2` (`= 2*mod_r`) is a real, reachable condition (e.g.
`mm_p == mod_r - 1` with `mm_add == 2`), but it is a low-probability event
under `test_modexp_random`'s 40 draws from `random.Random(0)` plus 7 fixed
known-vectors — exactly the "all tests pass, coverage looks fine, and a real
correctness bug ships anyway" failure mode this issue exists to close. This
is the concrete, on-this-repo's-own-canary evidence the survey's abstract
argument needed.

One accidental discovery, kept here because it is itself instructive:
an *attempted* fifth mutation, `outer <= outer - 1'b1` → `outer <= outer -
2'b1`, was **not** a real mutation at all — `2'b1` and `1'b1` are both the
Verilog integer value `1` (differing only in the literal's declared bit
width, which does not change the arithmetic result), so this "mutation"
anchored and applied cleanly but changed nothing observable, and (correctly)
survived. This is precisely the class of **equivalent mutant** §1a's guide
tells a proposer to reject ("Reject a mutation when: ... it is equivalent
for all legal inputs") — and it is a strong argument for *not* trying to
have `klt` auto-generate proposals: a naive syntactic mutation generator
(flip an adjacent bit-width literal, flip an operator) will manufacture
exactly this kind of false-positive-looking-like-a-defect noise unless it
understands enough semantics to know `2'b1 == 1'b1`, which is exactly the
HDL-parsing burden §1a/§1b's designs both go out of their way to avoid
taking on. Proposal authorship staying a semantically-aware (human or agent)
task, not a `klt`-internal generator, is reinforced by this one concrete
near-miss, not just asserted.

## 3. Proposed request/response contract

Documented in the field-table style this repo's other spikes and
`docs/cli/functional-verification.md` already use. This is a **proposed**
shape — no `klt` subcommand, dependency, or code is added by this spike.

```
klt functional-verification <request> --mutations <proposals> [--format text|json]
```

### 3a. Reused unchanged from `docs/cli/functional-verification.md`

Per AC #2, the `<request>` document is **exactly** today's
`klt.functional_verification.request/1` shape — `schema`, `engine`,
`sources`, `hdl_toplevel`, `testbench`, `options`, `parameters` — completely
unmodified. **No new field is added to that schema.** This is a deliberate
design choice, not an oversight: the alternative considered (embedding a new
`mutations.proposals[]` array inside the existing request document) was
rejected because it would force every future reader of the base
`functional_verification.request` schema to reason about an optional block
almost no caller uses, for a capability that is opt-in per invocation, not
per design. Keeping `<request>` byte-for-byte reusable means **the same
request file already used for an ordinary pass/fail run is the same file
used for a mutation run** — nothing about the base contract changes, and
`--mutations` is purely additive at the CLI-flag layer.

### 3b. New: the `<proposals>` document

A second, separate file — booley's own byte-exact shape (§1a), ported with
minimal changes (an added `schema` field, for this repo's request-schema
convention; everything else is what §1a's guide already specifies):

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
| `scope` | array\<string\> | Every RTL source file a proposal is allowed to name — **must be a subset of `<request>.sources`** (rejected at request-validation time, exit 1, if not: a proposal targeting a file the baseline build never compiles cannot be meaningfully tested). Mirrors booley's own `scope_files` check (§1a). |
| `proposals[].index` | integer | 1-based, unique, positive. Duplicate or non-positive indices are rejected (whole document, exit 1) before any build runs — the same "fail the whole request, don't run half a plan" posture `options.sdf` validation already takes in the base contract. |
| `proposals[].category` | string | Free-text label from §1a's guide's list (`operator_change`, `boundary`, `constant`, `polarity`, `bit_select`, `fsm_next_state`, ...) or any caller-chosen value — echoed back in the response, never validated against a fixed enum (new categories are a value-set growth, not a schema break, per `docs/json-contract.md`'s pre-1.0 caveat). |
| `proposals[].file` | string | Must be a member of `scope`. A proposal naming a file outside `scope` is `status: "rejected"` for that one proposal (not a whole-document failure — see §3d), the same "one bad entry doesn't poison the batch" posture `klt drc`'s per-violation reporting already takes. |
| `proposals[].line` | integer | 1-based line `original_code` must anchor on. |
| `proposals[].original_code` | string | The **exact** byte-for-byte source slice being replaced — not a pattern, not trimmed/normalized. Must occur **exactly once** on the declared `line` (§1a's `_resolve_one`). |
| `proposals[].mutated_code` | string | The exact replacement text. Must be non-empty and different from `original_code` (catches the `2'b1`/`1'b1` near-miss in §2 only when the two literal strings are byte-identical; a *semantically* equivalent-but-textually-different pair, like that near-miss, is **not** catchable by this schema alone — flagged honestly here, not glossed over, since §2 demonstrated it live). |
| `proposals[].detectability_argument` | string | Optional. Free-text rationale, echoed back unmodified — never validated or acted on programmatically; a human/reviewer aid, matching booley's own guide field. |

### 3c. Response — additive on top of the existing `functional_verification` response

The baseline run's response is **exactly** today's documented
`klt.functional_verification` response (`status`, `test_count`, `tests[]`,
`coverage`, `environment`, ...), unmodified, plus one new top-level block:

```json
{
  "schema_version": 1,
  "engine": "icarus",
  "hdl_toplevel": "modexp",
  "testbench": "test_modexp",
  "status": "pass",
  "test_count": 2,
  "passed_count": 2,
  "failed_count": 0,
  "skipped_count": 0,
  "tests": [ "...the baseline run's own tests[], unchanged shape..." ],
  "coverage": null,
  "environment": { "...unchanged..." },
  "mutation_testing": {
    "schema_version": 1,
    "proposal_count": 4,
    "valid_count": 4,
    "rejected_count": 0,
    "killed_count": 2,
    "survived_count": 2,
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
        "category": "boundary",
        "file": "modexp.v",
        "line": 76,
        "status": "survived",
        "first_killing_test": null,
        "log_path": ".klt/functional-verification/mutants/mutant_2.log"
      },
      {
        "index": 3,
        "category": "bit_select",
        "file": "modexp.v",
        "line": 134,
        "status": "killed",
        "first_killing_test": "test_modexp_known_vectors",
        "log_path": ".klt/functional-verification/mutants/mutant_3.log"
      },
      {
        "index": 4,
        "category": "fsm_next_state",
        "file": "modexp.v",
        "line": 152,
        "status": "killed",
        "first_killing_test": "test_modexp_known_vectors",
        "log_path": ".klt/functional-verification/mutants/mutant_4.log"
      }
    ]
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `mutation_testing.schema_version` | integer | Own version, independent of the outer response's (per `docs/json-contract.md`'s "versioned per command" — this block is versioned per *feature*, the same way `klt extract`'s `parasitics` sub-block could evolve independently of `devices[]`). |
| `mutation_testing.proposal_count` | integer | Total entries in `<proposals>.proposals[]`, before validation. |
| `mutation_testing.valid_count` | integer | `proposal_count - rejected_count`. The score's denominator. |
| `mutation_testing.rejected_count` | integer | Proposals that failed anchor validation (bad `file`/duplicate `index`/`original_code` not found or ambiguous on `line`/empty or identical text) or failed to build in isolation (syntax error the isolated variant introduced) — never run against the testbench at all. |
| `mutation_testing.killed_count` / `survived_count` | integer | Of the `valid_count` proposals actually run: how many produced at least one test failure the baseline did not (`killed`) vs. reproduced the baseline's own pass/fail structure exactly (`survived`). A timed-out variant counts as `killed` (§1a). |
| `mutation_testing.mutation_score` | number \| null | `killed_count / valid_count`, or `null` when `valid_count == 0` (every proposal was rejected — see "Exit codes" below; never silently reported as a perfect or zero score). |
| `mutation_testing.results[]` | array\<object\> | One entry per proposal, `status: "killed"\|"survived"\|"rejected"`. `reason` (string, present only on `"rejected"`) names the validation/build failure verbatim. `first_killing_test` (string \| null) is the first `@cocotb.test()` name whose failure differed from the baseline, `null` on `"survived"`/`"rejected"`. `log_path` is the isolated variant's own captured build+test transcript — the same "artifacts are paths, kept, never deleted" convention `docs/cli/functional-verification.md`'s "Artifacts" section already establishes for `build_<engine>.log`/`test_<engine>.log`, extended per-mutant under `.klt/functional-verification/mutants/`. |

### 3d. The baseline-must-pass gate

Ported directly from booley's own cold-start check (§1a: it verifies the
*pristine* source and testbench pass together before ever spending a mutant
round): **if the baseline run itself is `status: "fail"`, `--mutations`
aborts before running any proposal** — comparing mutant behavior against a
baseline that is already broken cannot distinguish "the mutation broke it"
from "it was already broken," so a `mutation_score` computed there would be
meaningless, not merely incomplete. This is exit 1 (an application error,
matching how `docs/cli/functional-verification.md`'s own "Never trusting an
exit code" section already treats "verified nothing" states — see next
section), with the underlying `status: "fail"` baseline payload still
included for diagnosis (the same way a rejected `options.sdf` combination
still reports *why*, not just that it failed).

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Baseline passed **and** every valid proposal was killed (`survived_count == 0`), including the `valid_count == 0` edge case only when `proposal_count == 0` (an explicitly empty proposal set — a caller choosing "run no mutations" is not an error). |
| `1` | Failed to run (all of the base contract's existing exit-1 causes), **plus**: the baseline itself failed (§3d), the `<proposals>` document is malformed/unparseable, a proposal names a `file` outside `<request>.sources`, or every proposal was rejected (`valid_count == 0` with `proposal_count > 0`) — mirroring `docs/cli/functional-verification.md`'s own "a regression that registered zero tests is exit 1, not a vacuous pass": a mutation run that tested nothing must never look like a clean `mutation_score: 1.0`. |
| `2` | Usage error — from argparse. |
| `3` | Ran successfully; **either** the baseline itself would already report exit 3 under the base contract's own rules (kept, since a mutation-testing invocation is still a functional-verification invocation) **or** the baseline passed but at least one valid proposal survived (`survived_count > 0`) — the issue's own requested "exit 3 when any valid mutant survives, following `klt drc`'s convention" (compare `docs/cli/drc.md`: `"Ran successfully, violations found"`). |

This keeps the same `0`/`1`/`2`/`3` trichotomy every other `klt` verb in
this family uses — no new exit code is introduced.

## 4. Where the score binds — recommendation

**Recommendation: ship `mutation_testing` as an optional, additive field on
`klt functional-verification`'s own response only, in the first
implementation issue. Do not propose T1 item 9 wording yet.** Two reasons,
both grounded in what this spike actually found rather than a general
preference for caution:

1. **No committed proposal-set corpus exists to calibrate a threshold
   against.** §2's own four-mutation experiment already shows the score is
   extremely sensitive to *which* mutations are in the set — two comparison-
   boundary mutations on the same one-line expression both survived, while a
   bit-select and an FSM-arithmetic mutation both died instantly. A
   single project-wide numeric threshold ("mutation score ≥ X") invites
   exactly the gaming problem one level up from the one this issue exists to
   solve: since proposal authorship is deliberately kept outside `klt` (§1a's
   "nobody parses HDL" design), nothing stops a threshold-driven author from
   proposing only mutations they already know their testbench catches. T1
   item 9's current wording ("every claimed measurement's testbench
   committed, with a documented cold-start invocation a third party can
   run") already has a real third-party-reproducibility check built in
   *for the testbench*; a numeric mutation-score threshold needs an
   analogous check on the **proposal set itself** (independently authored or
   reviewed, not self-selected by whoever wants the number to clear) before
   it can be trusted as a claim, and this spike does not have that mechanism
   designed yet.
2. **The base contract's own "never fabricate a signal" discipline argues
   for shipping the field first and observing it in real use** before
   spending a T1-wording change on it — the same order `klt power`'s
   IR-drop/EM evidence took (`docs/design-evidence-tiers.md` → "Power/IR-
   drop + EM evidence (not yet a T1 item)": shipped as a `klt power` field
   well before any T1-item proposal was written for it, specifically so the
   metric's real-world shape (what values it actually takes, what a "good"
   number looks like on a real block) is known before it is asked to gate
   anything).

**The concrete follow-on step this recommends, once the field has shipped
and run against at least one real committed canary's proposal set:** a
*second*, separate proposal to add a named T1 threshold — "testbenches
shipped, mutation score ≥ X on the committed proposal set" — paired with an
explicit anti-gaming rule for what counts as an acceptable committed
proposal set (candidates worth evaluating then, not decided here: a fixed,
versioned size-budget formula like booley's own `sqrt(source_lines)` clamped
to `[3, 25]`, §1a; a human/Judge review requirement on the proposal set
itself; or requiring the proposal author to be a different agent/session
than the one that wrote the RTL/testbench being scored). This spike
deliberately leaves that second proposal unwritten — per the issue's own
"Out of scope: ... any change to T1's wording before the spike recommends
one," and per point 1 above, this spike's recommendation *is* "not yet,"
stated with reasons, not a deferred yes.

## Out of scope for this spike

No dependency was added to `pyproject.toml`, no `klt` subcommand or
`--mutations` flag was added, no mutation-testing code was written, and no
`docs/guides/` proposer guide was ported. The four mutations in §2 were
applied by a throwaway shell/Python script against a scratch copy, run, and
reverted — `git status` in this worktree confirms `examples/functional-
verification/modexp.v` is byte-identical to `origin/main` throughout. Per
the issue's own "Out of scope": automatic mutation generation inside `klt`
was not designed (§1a/§2's `2'b1`/`1'b1` near-miss is direct evidence for why
not), and no change to T1 item 9's wording is proposed (§4).

## Open questions for a follow-up implementation issue

- **A per-mutant subprocess timeout independent of the testbench's own
  bounding.** §2's mutant #4 hung the DUT's FSM entirely (`outer` never
  reaches its terminal count), and was still correctly reported as a
  **test failure** only because `test_modexp.py`'s own `run_modexp()` helper
  happens to bound its wait with `timeout_cycles=20000` and raises. A
  testbench that `await`s an event with no cycle bound at all (a plausible,
  even common, cocotb pattern for a design that is expected to always
  eventually respond) would let an FSM-wedging mutant **hang the simulator
  process itself**, not fail a test — turning one bad mutation proposal into
  a stuck `--mutations` run with no bounded worst case. §1a's own
  `default_timeout: int = 1800` / `min_timeout: int = 1200` (seconds) on
  booley's simulator subprocess invocation confirms this is a real,
  previously-solved problem, not a hypothetical — the implementation issue
  should carry an explicit `klt`-level subprocess timeout (independent of
  and in addition to whatever the testbench itself does), not assume every
  testbench author remembers to bound their own waits.
- **Parallelizing the N isolated builds.** §2's cost model is stated
  sequential because that is what was measured; each isolated build
  directory is already fully independent (no shared mutable state between
  a baseline build and any mutant's), so running them concurrently (bounded
  by CPU count) is a real, low-risk speedup worth scoping into the
  implementation issue rather than this spike, once the sequential baseline
  ships and its real-world wall time on a full canary suite is known.
- **`options.coverage` + `--mutations` together.** Not designed here.
  Coverage on the *baseline* run is unambiguous (today's existing
  contract), but whether per-mutant coverage is worth collecting (and at
  what cost, given §2's Verilator N+1 multiplier) is left for the
  implementation issue to decide, informed by real Verilator-on-`modexp`
  numbers this spike could not measure in this sandbox (§2).
- **Where the proposal-authoring guide should live.** The issue's own
  "Proposed shape" asks for a ported proposer's guide in `docs/guides/`;
  issue #1587 (open, not
  yet substantively scoped as of this spike) proposes a
  `docs/guides/digital-review/` directory for a related but distinct set of
  per-concern RTL/testbench review guides, also citing `boldaxolotl/booley`
  under the same Apache-2.0 port-not-depend direction. If `docs/guides/
  digital-review/` exists by the time the mutation-proposer guide ships,
  co-locating there is the natural choice; if not, `docs/guides/
  rtl-mutation-testing.md` at the top level is a fine starting point. Not
  resolved here since it depends on #1587's own timeline, which this spike
  does not control.
- **Attribution mechanics.** This repo has no `NOTICE` file today (checked:
  none exists at the repo root), and `boldaxolotl/booley` itself ships no
  `NOTICE` file to preserve (checked: a live fetch of its own `NOTICE` path
  404s). The implementation issue should add a short attribution header
  comment (origin repo, file, license, fetch date) to the top of each ported
  file — the seam module and the proposer's guide — which satisfies
  Apache-2.0 §4's notice-preservation requirement without inventing new
  repo-wide `NOTICE`-file machinery this repo has never needed before, and
  is fully compatible with this repo's own MIT license for its original
  code (an MIT project may incorporate Apache-2.0-licensed components
  as long as the incorporated portion's own license terms and notices are
  retained, which per-file headers accomplish directly).

## Follow-on implementation issues filed from this spike

1. **Issue #1592 — Implement `klt functional-verification --mutations
   <proposals>`** — the §3 contract in full: port `mutation_variants.py`'s
   validate/apply/restore seam (Apache-2.0 attribution header), the
   baseline-must-pass gate (§3d), the isolated-build-directory-per-proposal
   loop through the existing `Runner.build()`/`.test()` invocation this verb
   already drives, the killed/survived/rejected classification and
   `mutation_score` (§3c), the exit-code table (§3), and a `klt`-level
   per-mutant subprocess timeout (see "Open questions" above — a stated
   requirement of this issue, not a follow-on of it).
2. **Issue #1593 — Port `rtl-mutation-testing.md` into `docs/guides/`** —
   the proposer's guide (§1a), Apache-2.0 attribution header, updated only
   to reference `klt functional-verification --mutations`'s actual schema
   names (`scope`/`proposals[]`/etc., §3b) in place of booley's own internal
   MCP tool framing; location resolved per "Open questions" above (co-locate
   with #1587's `docs/guides/digital-review/` if it exists by then).

## Related

- Issue #1586 (this spike)
- Issue #1592 — follow-on: implement `klt functional-verification
  --mutations`
- Issue #1593 — follow-on: port the proposer's guide into `docs/guides/`
- [docs/cli/functional-verification.md](../cli/functional-verification.md) —
  the contract this spike's §3 is additive on top of
- [docs/design-evidence-tiers.md](../design-evidence-tiers.md) T1 item 9 —
  the "Testbenches shipped" checklist item §4 recommends not yet touching
- [docs/json-contract.md](../json-contract.md) — the additive-envelope,
  exit-code, and value-set-growth conventions §3/§4 both follow
- [docs/design/cocotb-verification-spike.md](cocotb-verification-spike.md) —
  the accepted spike this one builds directly on top of (contract shape,
  `Runner` API invocation, `results.xml`-is-truth discipline)
- Issue #1587 — the sibling "port RTL/testbench review guides" issue, same
  `boldaxolotl/booley` source, distinct (static review checklist vs. this
  issue's dynamic automated gate); cross-referenced, not absorbed, per the
  Curator's own duplicate-check comment on #1586
- [boldaxolotl/booley](https://github.com/boldaxolotl/booley) — source of
  the ported design (Apache-2.0), specifically
  `src/booley/dev_support/mutation_variants.py`,
  `src/booley/dev_support/mutation_lock.py` (informative, not ported — see
  §1a), `src/booley/specialists/mutation_tester.py`, and
  `src/booley/data/refs/rtl-mutation-testing.md`
- [YosysHQ/mcy](https://github.com/YosysHQ/mcy) — adopt-as-dependency
  candidate considered and rejected in §1b

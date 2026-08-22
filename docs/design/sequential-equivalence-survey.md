# Survey & proposal: sequential equivalence checking for `klt equiv` Phase 2

**Status:** research / proposal, no implementation. Filed for issue #1290
under [Epic #707](https://github.com/2AMLogic/klayout-tools/issues/707)
Phase 2 ("Sequential equivalence — register-correspondence +
bounded/k-induction model checking for sequential designs (the
retiming/clone cases P&R produces), meeting the P&R epic (#700) here.").
This document plays the same role for Epic #707 Phase 2 that
[`docs/design/native-routing-survey.md`](native-routing-survey.md) (#934)
and [`docs/design/post-route-sta-survey.md`](post-route-sta-survey.md)
(#944) played for Epic #700 Phases 2/3 — it is the "fresh eyes" input a
later Champion pass decomposes into dispatchable sub-issues, and it does
**not** authorise implementation of anything below.

**Phase 1 (#832/#833/#834) is closed**, on top of the already-shipped
combinational engine (#726). Unlike Phase 1, Phase 2's epic-body text names
no lettered sub-issue breakdown at all — only the one-sentence phase goal
quoted above — which is exactly the gap this issue exists to close, per
its own body: "before inventing a Phase 2 sub-issue breakdown from
scratch, produce the equivalent survey for sequential equivalence checking
first."

**This document's single most load-bearing finding, stated up front rather
than buried in §2/§3: the phase-goal sentence's own premise does not match
what this repo's own P&R output actually contains.** "Retiming" (moving a
register across a combinational boundary) is not something `klt
place-and-route`'s own OpenROAD invocation does anywhere in its pipeline —
grep-confirmed, §2 below. What the pipeline *does* do — CTS buffer
insertion, hold/setup buffer insertion, drive-strength resizing, antenna
diode insertion — never changes the **set** of state elements (flip-flops)
between a pre-P&R and post-P&R netlist; it only changes the combinational
logic between them. That is a materially easier problem than general
sequential equivalence: it reduces to **register correspondence** (cut at
every flip-flop, prove per-cut-point combinational equivalence), not to
**bounded/k-induction model checking**, which is the harder, genuinely
different technique needed only when the two sides' own register
*structure* differs (a real retiming pass, a re-timed clock-domain
restructuring, or a redesigned FSM encoding — none of which this repo's own
P&R pipeline currently performs). §4 stages the proposal accordingly: the
register-correspondence item is Priority 1, cheap, and reuses almost all of
Phase 0/1's own shipped machinery; the bounded/k-induction item is staged
later, oracle-gated, and explicitly framed as "build only if real evidence
of a retiming-shaped P&R transformation shows up" — the same "smaller,
crisper sub-problem first, riskier one staged and gated" discipline
`native/legalize/README.md` (#784, Epic #700 Phase 1's own No-go) already
established for this epic family, and the native-routing/post-route-STA
surveys (#934, #944) each already applied a second and third time.

**A second load-bearing correction, equally worth stating up front:**
Epic #707's own body states "**Language: Rust** for the miter/CNF
construction and the solver-orchestration core." What Phase 0/1 actually
shipped (#726, closed #832/#833/#834) is **pure Python**
(`src/klayout_tools/equiv.py`) orchestrating **Yosys's own built-in**
`miter`/`sat` primitives (Yosys's internal MiniSat, not an external
solver, not a Rust CNF/SAT engine of this repo's own) — the module's own
docstring states this choice explicitly (quoted in full in §1 below). No
Rust crate for equivalence checking exists anywhere in this repo today
(`native/` has no `equiv`/`miter`/`cnf`/`sat`-named crate — grep-confirmed,
§1). This is not a defect in Phase 0/1 — the shipped engine matches every
one of the epic's own functional acceptance criteria (executable
counterexample, `inconclusive`-never-`equivalent` on timeout, matches the
Yosys/SymbiYosys oracle) — but it means Phase 2 inherits an aspiration
("Rust") the actual shipped code already diverged from once, for reasons
this survey did not second-guess (reusing a mature tool's own internal SAT
solver rather than reimplementing one). §4's own native-Rust item (§4.5)
addresses this directly, and does not assume the epic's original Rust
framing should simply be re-adopted without the same wrap-vs-native
evidence bar every other native-Rust item in this epic family has had to
clear.

**Required prior art, read first, not re-derived here:**

- [`docs/design/native-routing-survey.md`](native-routing-survey.md) (#934)
  and [`docs/design/post-route-sta-survey.md`](post-route-sta-survey.md)
  (#944) — Epic #700's own Phase 2/3 survey precedents this issue's body
  explicitly asks to be mirrored; this document follows their structure,
  evidence-tier discipline, and §4 prioritized-proposal shape.
- `native/legalize/README.md` (#784) — the Epic #700 Phase 1 No-go whose
  staging discipline ("smallest, best-bounded sub-problem first; anything
  broader is a gated spike, not a commitment") this document applies a
  third time, for the identical underlying reason: a from-scratch
  reimplementation of a mature open tool's own core algorithm is the
  highest-risk item in any of this project's epics, regardless of domain.
- `src/klayout_tools/equiv.py` — the shipped Phase 0/1 combinational engine
  this document's §1/§3 build directly on; its own module docstring already
  states the Rust-vs-Python choice and the SymbiYosys deferral this
  document's introduction restates precisely.
- `docs/cli/equiv.md` — `klt equiv`'s own contract documentation, ground
  truth for §1, including its own explicit "Out of scope: Sequential
  equivalence... SymbiYosys / `sby` orchestration... left for a later
  phase" lines this issue is the direct continuation of.
- `docs/cli/place-and-route.md`'s "As-built netlist (`verilog_path`, issue
  #996)" section — the real, measured evidence (§2 below) of what P&R
  actually changes about a netlist's structure.
- [`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) — the
  evidence-tier discipline this document follows.

## Evidence-tier discipline

Following this repo's own convention (`docs/design-evidence-tiers.md`; the
Epic #700 surveys' own tiering): this task did not run SymbiYosys, Yosys's
`equiv_*` command family, or any corpus benchmark of its own — it is pure
analysis, per its own Definition of Done. Every claim below is one of:

- **[REPO]** — read directly from this repo's own source/docs, cited by
  file and line, re-verified against the current tree (commit `132efa0`,
  2026-08-22) rather than assumed unchanged from a prior survey or from the
  epic's own body text.
- **[REPO-RUN]** — a **real** result already captured by another accepted
  document or module in this repo (e.g. issue #996's own measured
  pre-CTS/post-route netlist diff), not re-derived from memory.
- **[LIT]** — a technique or finding from the published EDA-CAD/formal-
  verification literature or Yosys/SymbiYosys's own documented command
  surface, cited by name/venue/year to the best of this survey's ability
  without live network/container access in this task. Treat exact command
  flag spellings and author lists as best-effort — verify against the
  primary source (or, per this repo's own live-introspection precedent —
  issue #783's `info body clock_tree_synthesis` method — a real `sby`/
  `yosys` invocation) before citing in a paper trail that requires
  precision.
- **[PROPOSAL]** — this document's own reasoning/recommendation, not a
  claim about the world.

**Methodology note on live verification.** This task did not have a
SymbiYosys (`sby`) install available — this repo's own CI does not install
one today (`.github/workflows/ci.yml` installs a pinned Yosys and Icarus
Verilog, grep-confirmed, no `sby`/`symbiyosys` step anywhere), and no
container with it was reachable in this task's environment. Every SymbiYosys
command cited in §3/§4 below is therefore **[LIT]**-tier, from
SymbiYosys's own published documentation and Yosys's own `equiv_*` command
reference — **not independently verified live in this task**, exactly the
same posture the post-route-STA survey (#944) recorded for its own
unreachable-container commands. Every follow-on sub-issue this document
proposes must re-verify its own exact command surface live before shipping,
per that same precedent — §4.1 below proposes this as its own first,
cheapest deliverable.

## 1. Baseline: what `klt equiv`'s combinational engine does today

**[REPO]**, `src/klayout_tools/equiv.py`, re-read against the current tree.

`klt equiv` proves or refutes **combinational** equivalence between a
`gold` and a `gate` side (each an RTL/gate-level Verilog source set plus an
optional liberty file and an I/O `port_map`). The module's own docstring
states the engine choice precisely enough to quote rather than paraphrase:

> "This environment (and this repo's CI...) has a real `yosys` binary but
> no `sby`/SymbiYosys install — SymbiYosys adds essentially nothing over
> plain Yosys for the **combinational** MVP this issue scopes (its main
> value is orchestrating multi-step *sequential* proofs — BMC/k-induction —
> which is explicitly out of scope here). Rather than stub the
> orchestration behind an interface no build of this repo can exercise,
> this module orchestrates Yosys's own `equiv`-family primitives directly."

Concretely, `_write_script` (`equiv.py:424`) generates a `.ys` script that,
per side: `read_liberty`/`read_verilog`, `proc`, `hierarchy -check`,
`flatten`, an optional port `rename` (the `port_map` remap), then
`design -stash`. Both sides are then copied into a shared design as
`gold`/`gate`, a hard guard runs —

```
select -assert-none gold/t:$dff* gate/t:$dff* gold/t:$adff* ... gold/t:$mem* gate/t:$mem* ...
```

— (`_SEQUENTIAL_CELL_GLOBS`, `equiv.py:97-104`: `$dff*`, `$adff*`, `$sdff*`,
`$dlatch*`, `$sr*`, `$mem*`, `$fsm*`) which **fails the whole run** the
instant either side contains any flip-flop, latch, memory, or FSM cell —
before anything sequential-shaped is ever handed to the SAT solver. This is
today's entire "sequential" story: reject outright
(`_sequential_error_message`, `equiv.py:500`, translates the resulting
Yosys assertion failure into an actionable `EquivError`), never attempt a
combinational-only proof on a design that actually has state. `klt equiv`
then builds `miter -equiv -make_assert -make_outputs -flatten gold gate
miter`, flattens it, and runs `sat -prove-asserts -show-ports -timeout
<n> miter` — Yosys's own **built-in MiniSat** solver, not an external SAT
engine and not a Rust CNF/SAT core of this repo's own (§0's second
correction, restated: no `native/equiv`-shaped crate exists anywhere in
this repo — `find . -iname '*equiv*'` outside `.loom/worktrees/` returns
only `docs/cli/equiv.md`, `src/klayout_tools/equiv.py`,
`src/klayout_tools/cli/equiv_cmd.py`, and the test files —
**[REPO]**, grep-confirmed).

Three outcomes: `"equivalent"` (`sat` proved the miter's assert holds for
every input), `"counterexample"` (a concrete divergence found — then
independently re-run through the flattened netlist via `iverilog`/`vvp`,
never trusted on the solver's own say-so, `_confirm_counterexample`,
`equiv.py:665`), or `"inconclusive"` (an internal solver timeout, a process-
level timeout, or any unrecognized solver output — **never** silently
reported as `"equivalent"`, `_classify_sat_result`, `equiv.py:538`). This
three-way, timeout-safe classification, the executable-counterexample
discipline, and the request/response JSON shape (`docs/cli/equiv.md`) are
all **engine-independent** — nothing about them is specific to the
combinational-only miter recipe, which matters directly for §4: a Phase 2
engine extension can reuse this whole outer shell (request parsing,
timeout handling, three-way classification, counterexample confirmation)
and change only the "how is a proof obligation built and dispatched" inner
piece.

`docs/cli/equiv.md`'s own "Out of scope" section already names this
document's job precisely: "**Sequential equivalence.** See 'Scope' above —
a later phase of #707... **SymbiYosys / `sby` orchestration.** ...left for
a later phase, once sequential scope makes it worth the added dependency."

## 2. Grounding "the retiming/clone cases P&R produces" against real P&R output

**[REPO]**, `src/klayout_tools/place_and_route.py`, re-read against the
current tree. This section directly answers the issue's own third
requirement: "The retiming/clone cases P&R (#700) actually produces — what
shapes of sequential transformation need to be provable equivalent,
grounded in real P&R output rather than assumed."

### 2.1 What the pipeline actually calls, stage by stage

`_stage_script_lines` (`place_and_route.py:2532`) issues, per stage:

| Stage | Calls that change netlist structure |
| --- | --- |
| `place` | `repair_design` (fixes max-cap/max-transition/max-fanout violations — may insert buffers); `repair_timing` (untagged — setup-focused, before a clock tree exists, per the code's own comment at `place_and_route.py:2646` explaining why hold-fixing is deferred to `cts`) |
| `cts` | `clock_tree_synthesis -root_buf <buf> -buf_list <buf> -sink_clustering_enable -obstruction_aware` (TritonCTS — builds the clock buffer tree); `repair_timing -hold` (inserts hold buffers) |
| `route` | `repair_antennas <diode_cell>` (inserts diode instances on antenna-violating nets), looped `max_antenna_repair_iterations` times; `filler_placement`+`global_connect` when `request.power` is given (non-signal filler cells only) |

None of `repair_design`, `repair_timing`, `clock_tree_synthesis`, or
`repair_antennas` is a "grep hit" for retiming or gate-cloning by name —
**[REPO]**, confirmed by grep: no `retim`, `clone`, `move_register`, or
similarly-named call exists anywhere in `place_and_route.py`. What every
one of these calls does, by its own well-documented function, is add or
resize **combinational** cells (buffers, diodes) or change an existing
cell's drive-strength variant — never move a flip-flop, never split a
register's own state into two copies, never change which cycle a signal is
valid on.

### 2.2 Real, already-measured evidence of the actual diff

`docs/cli/equiv.md`'s sibling contract, `docs/cli/place-and-route.md`'s "As-
built netlist (`verilog_path`, issue #996)" section, already states a real,
measured number for exactly this question — **[REPO-RUN]**, quoted in
full because it is this survey's single most important data point:

> "This command inserts and modifies cells: TritonCTS builds a clock tree
> (`clock_tree_synthesis`), `repair_design`/`repair_timing` insert buffers
> and swap gates for higher-drive-strength variants, and `repair_antennas`
> inserts diodes... in one real run, 40 of ~720 instances (35 CTS/timing-
> repair cells plus 5 drive-strength resizes under unchanged instance
> names)."

That is a real, already-run OpenROAD flow on a real corpus design,
diffing `klt synthesize`'s pre-P&R netlist against `write_verilog`'s
post-route netlist (`place_and_route.py:2740-2758`, the same
`_merge_def_to_gds`/`write_verilog` machinery #996 wired in). **Every one
of the 40 changed instances is a combinational insertion or an in-place
drive-strength swap of an existing instance** — the diff report's own
categorisation ("35 CTS/timing-repair cells plus 5 drive-strength
resizes") names no flip-flop addition, removal, or relocation. The
**register set itself — same flip-flops, same names, same positions in the
netlist hierarchy — is unchanged** between pre-P&R and post-route in this
one real, measured run.

### 2.3 What this means for "retiming/clone," precisely

- **"Clone"** — read charitably, this most plausibly refers to the real,
  observed buffer/diode *insertion* (§2.1/§2.2): new combinational
  instances appearing in the netlist that were not there before. This is
  a real, confirmed P&R behaviour.
- **"Retiming"** in its literal EDA-CAD sense — moving a register's
  position across a combinational boundary to rebalance path delay,
  changing which pipeline stage a value is valid in — is a real, standard
  optimization family in commercial P&R tools (**[LIT]**, general
  practice), but **this survey found no evidence this repo's own OpenROAD
  invocation performs it**: none of `repair_design`/`repair_timing`/
  `clock_tree_synthesis`/`repair_antennas` is a retiming pass by its own
  documented function (OpenROAD's resizer performs buffering, gate
  sizing, and — per general OpenROAD/`rsz` documentation, **[LIT]**, not
  independently re-verified live in this pass, the same caveat this
  survey's methodology note applies throughout — possibly "gate cloning"
  for high-fanout net splitting, a distinct optimization from register
  retiming that duplicates a *combinational* driver, not a register). The
  one real measured diff this repo has (§2.2) shows zero flip-flop-count
  or flip-flop-position change on the one design it was measured on.

**Net assessment, and why it drives §4's staging:** the P&R transformations
this repo's own pipeline actually, verifiably produces are exactly the
shape that **register correspondence** (not bounded/k-induction model
checking) solves — the two sides share an identical set of state elements,
so cutting at every flip-flop boundary and proving the combinational logic
between corresponding cut points is unchanged is both correct and
sufficient. This is a materially cheaper, more directly Phase-0/1-reusable
proof obligation than the general sequential-equivalence problem the phase
name's second half ("bounded/k-induction model checking") names. This
survey does **not** conclude bounded/k-induction model checking is
unneeded forever — §4.4 stages it as a real, later item, oracle-gated
precisely because this section's own evidence is one real diff on one
design, not a corpus-wide guarantee that no P&R run will ever restructure
registers. But it is not this epic's own "smallest, best-bounded
sub-problem first" starting point, by the same reasoning
`native/legalize/README.md`'s own staging note already established for
this epic family.

## 3. External SOTA survey

**3.1 Register correspondence via cut-point induction — Yosys's own
`equiv_*` command family.** Yosys ships a purpose-built sequential
equivalence pipeline distinct from the plain `miter`/`sat` combinational
recipe `klt equiv` already uses: `equiv_make` builds an `$equiv` cell
pairing each corresponding gold/gate cell or wire (matched by name, or via
`-inames` for Yosys-internal-name-only matches); `equiv_simple` resolves as
many `$equiv` cells as it can via local, per-cell SAT (the combinational
case, structurally similar to what `klt equiv` already does); `equiv_induct`
resolves the remainder via **temporal induction over the design's own state
elements** — the literal register-correspondence technique the epic's
phase-goal sentence names — proving that if every *already-proven*
`$equiv` pair holds this cycle, every *still-unproven* pair also holds next
cycle, then generalising by induction to "holds at every cycle" without
unrolling the design an unbounded number of times; `equiv_status` reports
which pairs remain unproven (**[LIT]**, Yosys's own manual, `equiv_*`
command reference — not independently re-verified live in this pass, per
this document's own methodology note). This command family is exposed
end-to-end through SymbiYosys's own `mode equiv` (**[LIT]**, SymbiYosys
documentation) — the purpose-built, no-hand-authored-Tcl entry point a
follow-on issue should reach for first, rather than hand-assembling the
`equiv_make`/`equiv_simple`/`equiv_induct` pipeline the way `klt equiv`'s
own `_write_script` hand-assembles `miter`/`sat` today.

**3.2 Bounded model checking (BMC).** Biere, A., Cimatti, A., Clarke, E.,
Zhu, Y. "Symbolic Model Checking without BDDs" (TACAS, 1999 — **[LIT]**,
the foundational BMC paper) unrolls a sequential design's transition
relation for a fixed number of cycles `k` and asks a SAT/SMT solver whether
a property (or, for equivalence, a self-checking miter assertion) can be
violated within those `k` cycles. This proves the *absence* of a
counterexample up to depth `k` — it does not prove the property holds
forever, only that no violation exists within the bound, the reason BMC
alone is a strong *bug-finding* tool but not by itself a full proof.
SymbiYosys's own `mode bmc` (**[LIT]**, SymbiYosys documentation) exposes
this directly: `sby` builds the design (optionally two designs plus a
comparator/miter module for an equivalence-shaped check), and invokes an
external SMT-BMC engine (`yosys-smtbmc`, backed by a solver such as
`z3`/`boolector`/`bitwuzla`/`yices` — **[LIT]**, not independently verified
live in this pass) up to a configurable depth.

**3.3 K-induction — closing BMC's "up to depth k" gap into a real, unbounded
proof.** Sheeran, M., Singh, S., Stålmarck, G. "Checking Safety Properties
Using Induction and a SAT-Solver" (FMCAD, 2000 — **[LIT]**) formalised the
technique SymbiYosys's own `mode prove` exposes as `bmc`+`k`-induction
combined (**[LIT]**, SymbiYosys documentation): prove the property holds
for the first `k` cycles (the base case, itself a bounded check), then
prove — as a separate SAT/SMT query — that *if* the property held for the
previous `k` cycles from an arbitrary (unconstrained) starting state, it
also holds on cycle `k+1` (the inductive step). If both hold, the property
holds at every cycle, forever, without unrolling the whole design. This is
the general external route to a genuine unbounded sequential-equivalence
proof when the automatic per-cut-point induction §3.1's `equiv_induct`
performs is not directly applicable — e.g. an equivalence claim that is not
naturally expressible as one-`$equiv`-cell-per-corresponding-signal (a
retiming case, where no 1:1 signal correspondence exists at all, needing a
hand-authored comparator asserting equivalence only after a design-specific
number of pipeline-latency cycles have elapsed). §4.4 proposes this as this
survey's own explicitly-staged, oracle-gated "harder half" of the phase
name, reached for only once §4.2's register-correspondence item has
established there is a real case it cannot resolve.

**3.4 `mode cover` and `mode live` — named in the issue's own body, cited
for completeness, not proposed as near-term items.** SymbiYosys's `cover`
task (find a concrete trace reaching a `cover()` statement) and `live`
task (Büchi-automaton-based liveness checking, **[LIT]**) are both real
SymbiYosys task types, but neither is an equivalence-checking mode — they
answer "can this state be reached" / "does this eventually happen," not
"do these two circuits compute the same function." This survey does not
propose either as a §4 item: nothing in Epic #707's own acceptance
criteria or this repo's own P&R/synthesis correctness question needs
reachability or liveness checking, and adding either without a concrete
consumer would be scope creep beyond what this issue's own body asks for.

**3.5 Reset/initial-state handling — a known, real gap in the naive
register-correspondence recipe.** A pure structural register-correspondence
check (§3.1) implicitly assumes both sides start from a matched initial
state, or is run "free-running" (no reset assumption, proving equivalence
for *every* possible starting state pair) — general SymbiYosys/formal-
verification practice, **[LIT]**. A P&R flow that changes a design's reset
network (this survey found no evidence this repo's pipeline does, §2, but
flags it as a real risk class for a follow-on to check) or a design whose
`$dff`/`$adff` cells carry an async reset Yosys's own cell library encodes
differently pre- vs post-synthesis could produce a false "not equivalent"
purely from a reset-modelling mismatch, not a real functional bug — a
known, documented failure mode in the formal-verification literature this
survey flags explicitly so a follow-on issue's own negative-control suite
(§4.3) tests it deliberately rather than discovering it as a confusing
false-positive in production.

**3.6 Known gap, with this repo's own real evidence.** §2.2's own real,
measured 40-of-720-instance diff (CTS/timing-repair/drive-strength changes
only, zero register-count change) is not a hypothetical SOTA gap
assessment — it is this repo's own concrete evidence for exactly which
technique (§3.1's register correspondence) actually matches the real P&R
transformation shape, the same evidentiary bar the Epic #700 surveys'
own §3.4/§3.7 sections each set for their strongest priority-1 finding.
§4.2 below is this document's direct equivalent.

## 4. Prioritized proposal

Six items. Item 1 is the oracle-infrastructure prerequisite every other
item needs (installing `sby` in this repo's own pinned, from-source
convention) — it should happen regardless of which technical item is
tackled first, and costs the least of anything in this document. Items 2–4
are staged from narrowest/safest (register correspondence, directly
matching §2's real evidence and reusing almost all of Phase 0/1's own
shipped shell) to broadest/riskiest (bounded/k-induction for cases register
correspondence cannot resolve), explicitly informed by the legalizer
spike's No-go: this survey does **not** propose jumping straight to a
general BMC/k-induction engine, the sequential-equivalence-checking
equivalent of what #784 attempted (and missed QoR on) for legalization.
Item 5 is the epic's own originally-stated Rust ambition, explicitly staged
last and oracle-gated, mirroring the native-routing survey's §4.4 and the
post-route-STA survey's §4.5 treatment of their own "replace a mature open
tool's core algorithm with new Rust" items. Item 6 closes the loop back to
Epic #700, per this phase's own stated goal ("meeting the P&R epic (#700)
here").

### 4.1 Install and live-verify SymbiYosys (`sby`) in this repo's pinned-toolchain convention (Priority 1, prerequisite for everything below)

- **Technique:** add `scripts/install-symbiyosys.sh` following the exact
  pattern `scripts/install-yosys.sh`/`install-icarus-verilog.sh`/
  `install-verilator.sh` already establish via their shared
  `_install_common.sh` (pinned version tag, checksum-verified fetch,
  idempotent install-marker, `--force` to rebuild) — install `sby` itself
  (a Python entry point from the `YosysHQ/sby` project) plus one pinned
  external SAT/SMT backend for its BMC/k-induction modes (`bitwuzla` or
  `boolector` are the commonly-used lightweight options — this issue's own
  follow-on must pick and pin one, not leave it to whatever happens to be
  on `$PATH`). Wire it into `.github/workflows/ci.yml` alongside the
  existing pinned-Yosys step (`sby` needs the exact Yosys already built
  there, not a second Yosys build).
- **Why first, before any technique item:** every claim in §3 above is
  **[LIT]**-tier specifically because this task had no reachable `sby`
  install (methodology note) — the same gap the post-route-STA survey
  recorded for OpenSTA containers (#944's own methodology note) and which
  that survey's own §4.1 flagged as the first thing a follow-on must
  close. Nothing in §4.2–§4.4 below can be built, let alone oracle-
  validated, without a real `sby` to compare against.
- **QoR metric:** none (this is infrastructure) — the measurable output is
  `sby --version` (or equivalent) succeeding headlessly in CI, plus a
  live re-verification of every exact command surface §3.1–§3.3 cited
  **[LIT]** (`equiv_make`/`equiv_induct`/`mode equiv`/`mode prove` flag
  spellings), replacing this survey's own best-effort citations with
  confirmed, executed ones.
- **Risk:** low — this is the same "add a pinned, checksum-verified
  from-source tool" shape three prior scripts already established
  successfully in this exact repo; the only open question is build time
  (Yosys's own pinned build is already the CI job's single largest cost
  per `.github/workflows/ci.yml`'s own comments — a second heavy build
  step is a real, measurable CI-time addition worth reporting, not
  assumed free).

### 4.2 Register-correspondence sequential equivalence, reusing Phase 0/1's shell (Priority 2 — this survey's recommended first technique item)

- **Technique:** extend `klt equiv` with a new engine value (e.g.
  `"engine": "yosys-sequential"`, additive per `SUPPORTED_ENGINES`'s own
  precedent) that, instead of `_write_script`'s hard `select -assert-none`
  guard (§1), builds a `sby`-orchestrated (§4.1) `mode equiv` job:
  `equiv_make` pairs corresponding registers/wires between `gold`/`gate`
  by name (the same instance-name-preserving property §2.2's own real
  measurement already confirmed holds for this repo's own P&R output —
  "5 drive-strength resizes under unchanged instance names"), `equiv_simple`
  resolves the combinational cones between registers, `equiv_induct`
  resolves the sequential cut points by induction, `equiv_status` reports
  the verdict. Reuses, unchanged: request parsing/validation
  (`load_request_arg`, `_resolve_side`), the `EquivError`/three-way
  `status` classification shell, the executable-counterexample-via-
  `iverilog`/`vvp` confirmation step (a `sby`-reported counterexample is a
  concrete cycle-indexed trace, translatable into the same testbench shape
  `_build_testbench` already generates, generalised to drive inputs across
  multiple clock cycles instead of one combinational vector), and the
  documented JSON response shape (extended, not replaced, per
  `docs/json-contract.md`'s additive posture).
- **Why this, over bounded/k-induction, as the *first* sequential item:**
  mirrors exactly the reasoning the Epic #700 surveys used for their own
  first-native-item picks (§4.2 of the routing survey, §4.1 of the STA
  survey): (a) it is the narrower, more bounded problem — automatic
  per-register-boundary correspondence, not an open-ended proof over
  arbitrary state-machine reachability; (b) it has the same direct,
  unambiguous oracle every item in this family gets — `sby`'s own reported
  verdict, cross-checked; (c) it directly matches §2's own real, measured
  P&R evidence (register-set-preserving transformations only), so it is
  the item most likely to actually close this phase's own stated goal
  ("meeting the P&R epic (#700) here," §4.6) rather than building general
  machinery for a case this repo's own corpus has not yet been shown to
  produce.
- **QoR metric:** correctness only (this is a proof, not an optimization) —
  does the new engine reach `"equivalent"` on a known-good pre-P&R/post-
  route pair and `"counterexample"`/`"inconclusive"` on a seeded-broken
  pair (§4.3), matching `sby`'s own independently-computed verdict on the
  identical pair every time.
- **Rust vs. flow:** **flow/Python**, zero Rust — the same "orchestrate an
  existing mature tool's own primitives" shape `equiv.py`'s own docstring
  already chose for Phase 0/1, extended one step further (from hand-built
  `miter`/`sat` Tcl to `sby`-orchestrated `equiv_make`/`equiv_induct`) —
  not the epic's originally-stated "Rust miter/CNF construction," a
  divergence this document's introduction already names and does not
  propose reversing without evidence it is warranted (§4.5).
- **Measurement plan:** build the request pair from a real P&R run —
  `klt synthesize`'s pre-P&R netlist as `gold`, `klt place-and-route`'s
  `verilog_path` (#996, §2.2) as `gate` — on the existing `gcd`/`modexp`
  corpus fixtures already present under `tests/corpus/place_and_route/`
  and `tests/corpus/legalize/` (the same designs the Epic #700 surveys'
  own §5 harness already uses); confirm `"equivalent"`, cross-checked
  against a real `sby mode equiv` run on the identical pair.
- **Risk:** medium. Genuinely new territory for this repo (the `sby`
  orchestration itself, plus translating a `sby`-reported sequential
  counterexample trace into a multi-cycle confirmation testbench, which
  `_build_testbench`'s current single-cycle shape does not yet support) —
  but every piece of new work builds directly on an already-shipped,
  already-tested shell, the same "smaller net-new surface than it looks"
  property the routing survey's §4.2 (native DRC-repair tool) found for
  its own first-pick item.

### 4.3 Seeded-broken sequential negative controls, oracle-validated (Priority 2, ships alongside 4.2)

- **Technique:** following #832's own precedent exactly (Phase 1's "seeded
  inversion"/"dropped register" pairs, `tests/test_equiv.py`'s own
  `_REGSEL_RTL`-derived fixtures) but for the *sequential* engine: at least
  two seeded-broken pairs derived from a real pre-P&R/post-route pair —
  (a) a **buffer-insertion mutant** that also (incorrectly) drops or
  duplicates a register, simulating a hypothetical P&R bug that *did*
  change the state-element set (the negative control this phase's own
  register-correspondence engine must catch); (b) a **reset-mismatch
  mutant** (§3.5's own flagged risk class) that changes one side's reset
  polarity or async/sync reset style, to confirm the engine reports a real
  divergence rather than a spurious pass or a spurious
  `"inconclusive"`-forever hang.
- **QoR metric:** `klt equiv`'s new engine must report non-equivalence
  (never a false `"equivalent"`) on every seeded-broken pair, with an
  executable counterexample, and must report `"equivalent"` on the
  known-good pair from §4.2 — the same two-sided acceptance bar #832
  already established for the combinational case.
- **Rust vs. flow:** flow/Python, zero Rust — test-fixture authoring only.
- **Wrap-vs-native trade-off:** none — this is a test-suite item, not an
  engine item.
- **Measurement plan:** cross-check every seeded-broken pair's verdict
  against a real `sby mode equiv` run on the identical pair (§4.1's own
  oracle), exactly as §4.2's own known-good pair is cross-checked.
- **Risk:** low — the same shape #832 already shipped successfully once
  for the combinational engine.

### 4.4 Bounded/k-induction sequential equivalence for cases register correspondence cannot resolve — explicitly staged, oracle-gated (Priority 3)

- **Technique:** a `sby mode prove` (§3.3) path: a hand-authored comparator
  module asserting `gold`/`gate` output equivalence (optionally only after
  a stated pipeline-latency offset, for a genuine retiming case where no
  1:1 register correspondence exists), proven by BMC-plus-k-induction
  rather than `equiv_induct`'s automatic per-cut-point correspondence.
- **Why staged *behind* §4.2, not alongside it:** §2's own evidence is that
  this repo's real P&R output does not currently need this — register
  correspondence (§4.2) already covers every real transformation this
  survey found evidence of. Building the harder, more open-ended technique
  before there is a real case it is needed for would repeat the exact
  mistake this epic family's own precedent (`native/legalize/README.md`,
  #784) warns against: investing in a broader capability before the
  narrower one has even been shown insufficient. This item's own
  go/no-go gate should be "did §4.2's register-correspondence engine
  actually fail on a real (not synthetic) P&R output, and why" — a
  question this survey cannot answer today because §4.2 has not shipped
  yet.
- **QoR metric:** correctness only, cross-checked against `sby`'s own
  `mode prove` verdict on the identical comparator/design pair.
- **Rust vs. flow:** flow/Python, zero Rust — `sby` orchestration, the same
  shape as §4.2.
- **Wrap-vs-native trade-off:** none — `sby`'s own BMC/k-induction
  implementation (via `yosys-smtbmc` and an external SMT solver) is mature,
  actively maintained tooling; nothing here proposes reimplementing it.
- **Measurement plan:** deferred until §4.2 ships and either (a) a real
  P&R-produced pair defeats register correspondence, giving this item a
  concrete worked example to build the comparator/induction-depth
  methodology against, or (b) a corpus-wide sweep of §4.2 across every
  #520-proxy design (`gcd`/`modexp`/`mult8`, the same three-design set
  every prior survey in this repo names) finds zero such cases, in which
  case this item stays a documented, deferred capability rather than
  something built speculatively.
- **Complexity/risk:** medium-high. The comparator/induction-depth
  methodology (how many cycles of "warm-up" latency does a given
  transformation need before the equivalence assertion is even
  well-formed) is a genuinely open design question with no single correct
  answer independent of the specific transformation being proven — the
  reason this item is explicitly *not* proposed as a near-term
  implementation issue, only as a named, staged, evidence-gated future
  step.

### 4.5 Native-Rust CNF/SAT/BMC core — the epic's own originally-stated ambition, explicitly deferred (Priority 4, informational)

- **Technique:** the epic body's own literal ask — "Rust for the miter/CNF
  construction and the solver-orchestration core (SAT via a
  `varisat`/`splr`-class engine or an external solver behind the
  interface)" — applied to whatever §4.2/§4.4 end up needing: a Rust-native
  miter/CNF builder plus either an embedded Rust SAT engine or Rust-side
  orchestration of an external solver, replacing Yosys's/`sby`'s own
  internal machinery.
- **Why not proposed as a near-term item, stated plainly:** Phase 0/1
  already made — and shipped, and passed every one of the epic's own
  functional acceptance criteria with — the opposite choice (reuse Yosys's
  own mature internal SAT solver rather than build one), for reasons this
  document's introduction already restated. This item would be a
  from-scratch reimplementation of a problem class (CNF construction, SAT
  solving, and — for §4.4's harder half — BMC/k-induction orchestration)
  that mature, actively-maintained open tools (Yosys's own MiniSat,
  external solvers `sby` already knows how to drive) already solve well,
  the identical shape of undertaking `native/legalize/README.md` (#784)
  attempted for standard-cell legalization and missed QoR on — and, unlike
  legalization, this survey has **zero** evidence (not even a partial one,
  the way the native-routing survey had #785's own convergence-cliff
  finding) that Yosys's/`sby`'s own SAT/BMC performance is a real,
  measured bottleneck for this repo's own corpus today, because §4.2/§4.4
  have not shipped yet to even generate that evidence.
- **Recommendation:** do not fund this directly. Restate it here, per this
  document's introduction, so a future reader does not mistake "Phase 2
  shipped as Python/`sby` orchestration" for an oversight rather than a
  reasoned, evidence-following continuation of Phase 0/1's own already-
  accepted choice. If §4.2/§4.4 both ship and a real, measured performance
  or capability gap against `sby`/Yosys shows up on this repo's own
  corpus, *that* — not the epic's original prose — is the evidence a
  future native-Rust item should be justified against, the same
  "wait for real data, then decide" posture the native-routing survey's
  own §4.4 applied to a full native detailed router.

### 4.6 Wire the sequential engine into `klt place-and-route` as an optional acceptance gate, closing the phase's own stated goal (Priority 3, depends on 4.2)

- **Technique:** mirroring `klt synthesize --verify-equivalence`'s own
  precedent (#704 Phase 1, `docs/cli/synthesize.md#equivalence-gate`) —
  the wired, one-command version of "synthesize, then check" — add an
  analogous optional flag/field to `klt place-and-route` (or a documented,
  reusable two-command recipe, exactly as #834's own Phase 1c acceptance
  criteria phrased it: "wired as a reusable step... or a documented
  follow-on command, not a one-off manual invocation") that runs §4.2's
  sequential engine between the pre-P&R netlist (`klt synthesize`'s own
  `netlist_path`) and the post-route `verilog_path` (#996) automatically
  at the end of a `"route"`-stage run.
- **Why this is the phase's own real deliverable, not an optional add-on:**
  the phase-goal sentence's own final clause — "meeting the P&R epic (#700)
  here" — is not satisfied by a standalone `klt equiv` capability sitting
  unused; it is satisfied specifically by P&R's own output being provably
  equivalent to its own input, wired so that claim is checkable on every
  run, not only when a caller remembers to invoke `klt equiv` by hand
  afterward. This is the direct sequential-equivalence analogue of #834's
  own already-shipped `klt synthesize`↔RTL loop-closer.
- **QoR metric:** none — this is a wiring/contract item; the metric is
  "does a real corpus P&R run report `equivalent` when it should, and
  `counterexample`/`inconclusive` (never a silent skip) when a seeded-
  broken variant of the flow is forced (§4.3's own negative-control
  discipline, applied at the P&R-integration level)." This item should
  wire the sequential engine's own dependency (`sby`) as a documented,
  optional install (per `klt synthesize --verify-equivalence`'s own
  precedent for its own Yosys/Icarus dependency) rather than a hard
  requirement of `klt place-and-route` itself.
- **Rust vs. flow:** flow/Python, zero Rust.
- **Risk:** low, mechanical, once §4.2 exists — the same "compose two
  already-shipped pieces" shape the post-route-STA survey's own §4.3
  (SDF write + Icarus `$sdf_annotate`) already used successfully in this
  repo (issue #1002, shipped).

## 5. Measurement harness — common to every item above

- **Corpus.** The same three-design #520-corpus proxy every prior survey
  in this repo uses — `gcd`, `modexp` (both already present as P&R-shaped
  fixtures under `tests/corpus/place_and_route/` and
  `tests/corpus/legalize/`), and `mult8` where a P&R-shaped fixture exists
  per the native-routing survey's own §5/§6 sketch (issue #941).
- **`sby`/Yosys `equiv_*` as the oracle**, per §4.1 — every claim any item
  above makes about a proof verdict is scored against a real `sby` run on
  the identical input, never against this repo's own prior output, per
  Epic #707's own reality-grounding discipline ("Yosys `equiv`/SAT +
  SymbiYosys as ground truth" — the epic's own body, quoted in full at the
  top of this survey's "Required prior art" reading list).
- **Executable counterexamples, always.** Every negative-control item
  (§4.3) must re-run its reported counterexample through `iverilog`/`vvp`
  (extended to a multi-cycle testbench, §4.2's own scope) and confirm the
  divergence reproduces — never trust the solver's report alone, exactly
  Phase 0/1's own already-shipped discipline (`_confirm_counterexample`,
  `equiv.py:665`).
- **`inconclusive` never means `equivalent`.** Every new engine path must
  preserve Phase 1's own #833 guarantee — a `sby` timeout, an unresolved
  `equiv_status` cell, or an unexpected output shape is `"inconclusive"`,
  never silently upgraded to `"equivalent"`.
- **A/B protocol.** Same request document, same corpus design, one engine
  toggled at a time (`"yosys"` combinational vs. `"yosys-sequential"`),
  JSON response fields diffed directly, never eyeballed from logs — the
  same convention every prior survey in this family has used.

## 6. Follow-on implementation-issue sketch

### Sketch — install/verify SymbiYosys, then ship register-correspondence sequential equivalence (§4.1 + §4.2 + §4.3)

**Title:** `equiv: add SymbiYosys-orchestrated register-correspondence engine for sequential equivalence`

**Why this is the right *first* Phase 2 issue, not §4.4/§4.5 directly:**
every later item in this document (§4.4's bounded/k-induction path, §4.6's
P&R wiring) needs a real `sby` install and a working sequential engine to
build on or gate against — shipping this first is the same "cheapest,
most-certain-value item first" ordering every prior survey in this repo has
used, and unlike §4.4/§4.5 it carries **zero** open design questions this
survey could not resolve (no "how many induction cycles" ambiguity the way
§4.4 has, no "is a native rewrite even warranted" open question the way
§4.5 has) — it is a direct, evidence-matched answer to §2's own real,
measured P&R-diff finding.

**Scope:**
1. `scripts/install-symbiyosys.sh`, pinned/checksummed, wired into CI
   alongside the existing pinned-Yosys step (§4.1).
2. A new `"yosys-sequential"` `klt equiv` engine value, `sby`-orchestrated
   `mode equiv` (`equiv_make`/`equiv_simple`/`equiv_induct`/`equiv_status`),
   reusing the existing request/response shell (§4.2).
3. At least two seeded-broken sequential negative controls (§4.3), each
   cross-checked against a real `sby` run.
4. `docs/cli/equiv.md` updated: a new "Sequential equivalence" section
   (replacing the current "Out of scope: Sequential equivalence... left for
   a later phase" line), the new engine's request/response additions, and
   the `sby` dependency's own install/build documentation, following
   `docs/cli/yield.md#building-the-native-extension`'s own precedent for
   documenting an optional heavy dependency.

**Acceptance criteria:**
- `klt equiv` with `"engine": "yosys-sequential"` reports `"equivalent"` on
  a real pre-P&R/post-route corpus pair (`gcd` and/or `modexp`,
  `klt synthesize`'s netlist vs. `klt place-and-route`'s `verilog_path`),
  cross-checked against a real `sby mode equiv` run on the identical pair.
- At least two seeded-broken sequential pairs (§4.3) each produce a
  non-equivalence verdict with an executable, `iverilog`/`vvp`-confirmed
  counterexample, matching `sby`'s own independently-computed verdict.
- A solver/process timeout on this new engine path reports `"inconclusive"`,
  never `"equivalent"` — a dedicated forced-timeout test, mirroring #833's
  own Phase 1b acceptance criterion for the combinational engine.
- Existing combinational-engine tests and behaviour are entirely unaffected
  (this is an additive `engine` value, not a change to `"yosys"`'s own
  default combinational path).

**Not in scope:** §4.4's bounded/k-induction path, §4.5's native-Rust
question, and §4.6's `klt place-and-route` wiring are separate, larger
follow-on issues that consume this sketch's own shipped engine as their own
prerequisite — deliberately not bundled here, mirroring every prior
survey's own "cheapest, most-certain-value item first" sequencing.

## References

- Biere, A., Cimatti, A., Clarke, E., Zhu, Y. "Symbolic Model Checking
  without BDDs." TACAS, 1999 — the foundational bounded-model-checking
  paper.
- Sheeran, M., Singh, S., Stålmarck, G. "Checking Safety Properties Using
  Induction and a SAT-Solver." FMCAD, 2000 — the k-induction technique
  SymbiYosys's own `mode prove` exposes.
- Spindler, P., Johannes, F. M., cited here only by cross-reference — see
  the native-routing survey's own reference list; not independently used
  in this document.
- SymbiYosys (`YosysHQ/sby`) and Yosys's own `equiv_make`/`equiv_simple`/
  `equiv_induct`/`equiv_status` command reference — best-effort citation,
  primary source is each project's own documentation; not independently
  container-verified in this task (methodology note above).
- `docs/design/native-routing-survey.md` (#934) and
  `docs/design/post-route-sta-survey.md` (#944) — Epic #700's own survey
  precedents; this document's direct structural and evidentiary ancestor,
  per this issue's own body.
- `native/legalize/README.md` (#784) — the Epic #700 Phase 1 native-
  placement spike whose No-go verdict and staging discipline this document
  applies a third time (§4's own ordering, §4.5's deferral).
- `src/klayout_tools/equiv.py`, `docs/cli/equiv.md` — the shipped Phase 0/1
  combinational engine and its own contract, this document's own §1/§4.2
  baseline and direct extension point.
- `docs/cli/place-and-route.md`'s "As-built netlist (`verilog_path`, issue
  #996)" section — the real, measured pre-P&R/post-route netlist diff this
  document's own §2 central finding rests on.
- Epic #707 (this document's own parent) and Epic #700 (the P&R epic this
  phase's own goal names as the consumer of its output).

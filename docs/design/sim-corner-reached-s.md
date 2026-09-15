# Decision: `klt sim` dispatch-time fail-fast via a two-pass calibration probe

**Status:** decision record, implemented in the same PR. This document
resolves the open design question in
[#1694](https://github.com/2AMLogic/klayout-tools/issues/1694) (a follow-up
to [#1686](https://github.com/2AMLogic/klayout-tools/issues/1686)): how to
give `klt sim` a real dispatch-time fail-fast guard for an unmeetable
`options.timeout_s` budget, given that the two most obvious mechanisms for
recovering a killed corner's `reached_s` are both blocked by the same root
cause. It follows the same decision-record pattern as
[docs/design/mom-general-conductor-geometry.md](mom-general-conductor-geometry.md):
states the problem, the options considered, why one was chosen, and the
plan — but unlike that document, the chosen option (the two-pass probe
model) is implemented in `src/klayout_tools/sim.py` in the same PR that adds
this file, per issue #1694's "Revision 2026-09-15 (operator lane)" removing
the operator-decision gate and asking the Builder to pick and build.

## The problem

`klt sim` runs a per-corner wall-clock budget (`options.timeout_s`) but has
no guard against a budget that is *structurally* unmeetable for the whole
grid — only against one corner hanging. The motivating incident
([2AMLogic/sky130-pll#103](https://github.com/2AMLogic/sky130-pll/issues/103)):
a manifest widened a `tran` analysis window while raising `timeout_s` by a
smaller factor, and all 45 corners in the grid ran their full 10800s
`timeout_s` budget while covering only ~54% of the window each — ~45
wall-clock hours of saturated cores producing a response with zero usable
measurements. Issue #1686 shipped a coarse, static, advisory-only preflight
(`environment.timeout_preflight_warning`, comparing `timeout_s` against a
generous timepoints-per-second floor) but explicitly left its two more
impactful acceptance criteria open:

1. A per-corner `reached_s`/`fraction` progress diagnostic on a `timeout`
   corner — how far a *killed* corner's simulated time actually got.
2. A dispatch-time fail-fast abort, built on (1), that stops the grid once
   early corners prove `timeout_s` itself (not one slow corner) cannot
   plausibly cover the window — the guard that would have prevented the
   incident above.

Both are blocked on the same root cause, verified empirically against
ngspice-46 while building #1686 (see `docs/cli/sim.md`'s "Timeout-budget
preflight" section for the full findings, summarized here):

- **ngspice's `-r <rawfile>` incremental streaming requires "autorun"** (a
  bare top-level analysis card, no `.control` block). Every real corner
  deck (`_write_corner_deck`) unconditionally wraps its analysis in a
  `.control` block — for the `alter` supply-override cards, an optional
  waveform capture, and the Monte Carlo `.options seed=` card — and *any*
  `.control` block silently disables `-r`'s streaming, with no exception for
  one that never calls `write`. A `.control`-wrapped run killed mid-flight
  leaves **no rawfile at all**, so `-r` would report `reached_s: null` on
  every real invocation, not "most" — worse than not shipping it.
- **The `stop`/`resume` checkpoint workaround corrupts `.meas` correctness.**
  Periodic `stop when time > <t>` / `write` / `resume` checkpoints inside
  the `.control` block do let a killed run recover a graduated `reached_s`,
  but ngspice evaluates (and prints) `.meas` results tied to the *first*
  `doAnalyses` completion — including the first checkpoint pause, not the
  analysis's true final completion. A `.meas ... AT=<t>` beyond that first
  checkpoint fails "out of interval" and is never re-evaluated once the run
  actually finishes, even though the simulation itself completed correctly.
  This breaks measurement extraction for the overwhelming majority of real
  (non-timeout) sweeps — not shippable as a general mechanism.

## The options considered

1. **Redesign the corner deck to avoid `.control`** — parameterize supply
   overrides via `.param`/expression-based source cards the netlist
   references, instead of post-hoc `alter`, so a plain top-level analysis
   card (autorun) can be used and `-r` streaming works.
2. **A two-pass/probe execution model** — run a short calibration slice of
   the deck first to measure this specific corner/deck's actual
   simulated-time-per-wall-clock-second rate, then use that *measured* rate
   (not a fabricated preflight floor) to estimate whether `timeout_s` can
   cover the full window, before committing the rest of the grid to it.
3. **ngspice server-mode / shared-library integration** — spawn ngspice via
   its `-s`/server or shared-library interface and poll the live `time`
   vector out-of-band while the `.control`-based analysis runs, without any
   `stop`/`resume` interference with `.meas`.
4. **Accept the limitation and drop AC1/AC2** — keep only the coarse,
   advisory #1686 preflight; document that a real fail-fast guard is out of
   scope until one of the above is worth the investment.

## Why the two-pass probe model (option 2)

**Against option 1 (redesign the deck to avoid `.control`).** This is a
bigger, cross-cutting change than the fail-fast guard itself warrants: it
touches every corner run, the netlist convention `docs/cli/sim.md`
documents (`.param`/expression-based sources instead of the current
`alter`-based override, which every existing request written against this
contract already relies on), and possibly the Monte Carlo seed contract's
`.options seed=` ordering requirement (`_write_corner_deck`'s docstring
notes `.options seed=` must appear before any `.lib`/`.include` that
evaluates a mismatch-aware model's random functions — a `.control`-free
deck would need to re-derive an equivalent ordering guarantee without
`.control` scripting to sequence it). It would also still need its own new
validation that the incremental `-r` rawfile is actually recoverable
against *this* module's specific autorun deck shape end to end — the same
class of "verify empirically, don't assume" work #1686 already did once for
the `.control` case and found broken. Fixing the fail-fast gap should not
require re-litigating the deck's core override mechanism.

**Against option 3 (ngspice server-mode/shared-library).** This is the
"most correct" option in the abstract — real out-of-band progress polling
with zero interference with `.meas` — but the largest lift: a new IPC
surface (the `-s` server protocol or `libngspice`'s C API via a Python
binding), new failure modes (a hung shared-library call cannot be killed by
signaling a subprocess the way `subprocess.run(..., timeout=...)` already
can), and a real architectural shift away from this module's core "process
per corner, killable by a plain OS signal" invocation strategy that
`docs/design/spice-corner-runner-spike.md`'s own survey chose deliberately
(see that document's "Invocation strategy" section: "a hung engine must be
killable without taking down our own process"). The two-pass probe model
gets the same *decision* (can `timeout_s` cover the window?) using only the
subprocess-per-corner primitive already in place, at a small, bounded,
predictable extra wall-clock cost. This document does not find that the
probe model is unable to bound its own estimate (see "What the probe model
gives up" below for the one real limitation, sampling-noise risk, which is
mitigated with a margin rather than requiring server-mode) — so
option 3 is not pursued here; it remains available as future work if a
tighter bound is ever needed.

**Against option 4 (drop AC1/AC2 entirely).** The 45 wall-clock-hour,
zero-measurement incident this issue exists to prevent is exactly what
stays possible under option 4 — the coarse #1686 preflight this repository
already ships is a *static* check (declared step/window vs. a generous,
admittedly-fictional timepoints-per-second floor) with no way to catch a
budget that looks plausible on paper but is wildly wrong for the actual
circuit, which is precisely the sky130-pll#103 shape (a widened window
under a budget that had *some* headroom, just not enough — not an
obviously-absurd request the static floor would catch). Accepting that gap
indefinitely, when a bounded, evidence-based check is implementable with
mechanisms already in this module, is not a good trade against the incident
cost.

**Therefore: option 2, the two-pass probe model**, implemented as follows.

## The design

### What runs, and when

Opt-in via `options.fail_fast_probe` (`bool`, default `false`) or
`--fail-fast-probe` — the same opt-in convention as
`options.resume`/`options.wall_clock_budget_s`, since this changes dispatch
behavior (it can abort a sweep outright) rather than being purely additive
reporting like the #1686 preflight.

When enabled, **once per grid** (not once per corner — the whole point is
to spend a small, bounded amount of extra wall-clock time, not to double
the cost of every corner), before any real corner is dispatched:

1. Take the grid's first corner point (`corner_points[0]`, or the first
   not-yet-checkpointed point under `--resume`).
2. Write a **calibration deck**: the same `_write_corner_deck` shape (same
   `alter` supply-override cards, same `.control` wrapper — a probe on a
   corner whose supply override changes convergence behaviour needs that
   same override active to be representative) with an empty
   `measurements_spec` (the probe's own pass/fail is irrelevant, and
   skipping `.meas` cards sidesteps the exact "out of interval" corruption
   documented above for a truncated window) and a scaled-down `tran`
   window: `probe_window_s = analysis_window_s * 0.02` (2%).
3. Run it with its own bounded timeout, independent of the real
   `options.timeout_s`: `probe_timeout_s = clamp(timeout_s * 0.1, 1.0s,
   60.0s, timeout_s)` — a fraction of the real budget, floored so a tiny
   `timeout_s` still gives the probe a moment to run, capped so a huge
   `timeout_s` never turns the probe itself into an hours-long liability,
   and never exceeding `timeout_s` itself (the probe must never be allowed
   to run longer than the real corner it is standing in for ever could).
4. Measure the probe's own wall-clock time. Two outcomes:
   - **Completed within `probe_timeout_s`:** `rate = probe_window_s /
     probe_wall_s` — a direct measurement.
   - **Killed at `probe_timeout_s`:** the probe did not finish
     `probe_window_s` of simulated time within `probe_timeout_s` of wall
     time. Exactly how far it *did* get cannot be recovered (the same
     `.control`-block recovery gap this document opened with — the probe
     deck is subject to the identical limitation as a real corner deck),
     but `rate <= probe_window_s / probe_timeout_s` is still a valid
     **upper bound**: the true rate is provably no faster than this, which
     is already sufficient to prove infeasibility when even that generous
     upper bound cannot cover the window in time.
   - Either outcome yields a **conclusive** rate; a spawn failure (ngspice
     missing) or the probe's own analysis not completing cleanly (a
     genuine convergence failure at this corner, not the truncated window)
     is **inconclusive** — the probe is silently skipped and the sweep
     proceeds exactly as if `fail_fast_probe` were unset, never aborting a
     grid on untrustworthy evidence.
5. Estimate `estimated_wall_s = analysis_window_s / rate` — the wall-clock
   time the full-window analysis would need per corner at the measured
   rate. If `estimated_wall_s > timeout_s * 1.5` (the abort margin, below),
   **abort the whole grid**: every corner in the dispatch list is reported
   `status: "error"` with a `timeout_budget_unreachable` diagnostic, before
   a single real corner's `ngspice` process is ever spawned. Otherwise the
   sweep proceeds exactly as it would have without the probe.

This is implemented as the natural extension of the existing
`stop_reason`/`_unrun_corner_report` dispatch-loop pattern
(`_run_local`/`_run_local_parallel`, shared by any `hosts > 1` shard built
on them — issue #473's wall-clock-budget/orphan-safety machinery already
established this seam): the probe's abort decision is made once, in
`run_sim`, before backend dispatch, and passed down as a `probe_abort`
parameter that pre-seeds `stop_reason` instead of leaving it `None` — every
corner then takes the same "never started" code path a `budget_exceeded`/
`orphaned` corner already does.

### The abort margin

`estimated_wall_s` must exceed `timeout_s` by more than **1.5x** before the
grid actually aborts. This absorbs the probe's own sampling noise — a short,
possibly-transient-heavy 2%-of-window slice at the *start* of a `tran`
analysis can plausibly under- or over-estimate the deck's steady-state rate
— without masking the kind of gross mismatch the motivating incident
showed: 45 corners that only reached ~54% of the window in their full
budget imply the true run needed roughly `1 / 0.54 ≈ 1.85x timeout_s`,
comfortably past this margin. A margin of exactly `1.0` (abort on any
shortfall at all) would risk false-positive aborts on a sweep that would
actually have finished, just closer to the wire than the probe's necessarily
short slice can precisely predict; `1.5` is chosen as a deliberately
conservative value that still catches the incident's own numbers with
headroom, not a value tuned against any real dataset (there is no
representative one available yet). Revisit if usage surfaces a real sweep
that this margin should have caught but did not, or one it aborted that
would have finished as tuned closer to `1.0`.

### The corner-count-vs-probe-cost tradeoff

Probing once per grid, not once per corner, is a deliberate simplification:
convergence speed genuinely can differ across a PVT matrix (a `ss` process
corner's Newton iteration count is not necessarily `tt`'s), so a single
first-corner probe is not a rate measurement of *every* corner, only of the
one probed. This is accepted for two reasons: (a) probing every corner would
materially increase the very wall-clock cost this feature exists to bound
(defeating the point for a large grid), and (b) the incident this issue
targets is a *budget-shape* problem (the declared window vs. the declared
timeout, independent of which specific corner is probed) rather than a
single-corner outlier — the existing per-corner `options.timeout_s` already
handles one slow corner. A future iteration could probe more than one
corner (e.g. the fastest- and slowest-converging named process corners, if
declared) and take the more conservative rate; not done here, since a
single first-corner probe already prevents the motivating incident and
adding more probes is pure-upside future work, not a correctness gap in
this design.

### `reached_s`/`fraction`: what this design does and does not deliver

This design does **not** recover a real per-corner `reached_s` for a corner
that is actually dispatched and then killed by its own `options.timeout_s`
— that remains blocked by the same `.control`-block finding this document
opened with, unchanged by this issue. **A corner that runs and times out
still reports `reached_s: null`** in its `timeout` diagnostic.

What this design *does* deliver is an **estimated** `reached_s`/`fraction`
on every corner the fail-fast abort itself skips (`timeout_budget_unreachable`
diagnostics): `estimated_reached_s = min(analysis_window_s, rate *
timeout_s)`, `estimated_fraction = estimated_reached_s / analysis_window_s`
— i.e., "given the measured rate, this is roughly how far this corner would
have gotten before its own `options.timeout_s` would have killed it." This
is computed, not recovered, and is clearly labeled as an estimate (both in
the diagnostic's own message and in `docs/cli/sim.md`) — it is not to be
confused with instrumenting a real corner's actual progress. It directly
answers the question the sky130-pll#103 incident needed answered ("roughly
what fraction of the window is each corner actually reaching?") without
requiring the blocked recovery mechanism.

## Out of scope

- ngspice server-mode/shared-library integration (option 3) — not needed;
  see "Why the two-pass probe model" above. Left as a cross-referenced
  future option if a tighter bound is ever needed.
- Redesigning the corner deck to avoid `.control` (option 1) — a
  substantially larger, cross-cutting change; not pursued here.
- Real per-corner `reached_s` recovery for a corner that is actually
  dispatched — unchanged by this issue, remains `null`.
- Probing more than one corner per grid — see "The corner-count-vs-probe-cost
  tradeoff" above; a plausible future refinement, not required for this
  design to prevent the motivating incident.
- Wiring the probe into the `remote` backend — the probe would need to run
  on the *provisioned remote box*, not the dispatching process's own host,
  to be representative of that hardware; `run_sim` only engages the probe
  for `backend in ("local", "local-parallel")` today (see `_run_remote`'s
  docstring). Left as future work if the `remote` backend's own users need
  it.

## Implementation

Shipped in the same PR as this document:
`src/klayout_tools/sim.py` (`_parse_tran_window`, `_probe_window_and_timeout`,
`_run_calibration_probe`, `_run_fail_fast_probe`, and the `probe_abort`
threading through `_run_local`/`_run_local_parallel`),
`src/klayout_tools/sim_remote.py` (`_unrun_corner_report`'s additive
`message`/`reached_s`/`fraction` parameters), `src/klayout_tools/cli/parser.py`
and `src/klayout_tools/cli/sim_cmd.py` (`--fail-fast-probe`), and
`docs/cli/sim.md`'s "Timeout-budget preflight" section (the shipped
`environment.fail_fast_probe` field and `timeout_budget_unreachable`
diagnostic code). See that document for the request/response contract.

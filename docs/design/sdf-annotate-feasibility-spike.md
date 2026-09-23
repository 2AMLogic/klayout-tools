# Spike: Icarus `$sdf_annotate` feasibility for SDF-annotated gate-level re-simulation

**Verdict: GO.** Icarus Verilog 13.0 back-annotates an SDF file onto this
repo's existing gate-level cocotb testbench convention, unmodified, and the
annotated delays are large enough to change the testbench's own pass/fail
outcome — the exact coverage signal
[`post-route-sta-survey.md`](post-route-sta-survey.md) §4.3 exists to
produce. Three flags and one generated wrapper file are all the wiring
`klt functional-verification` needs; §4.3's `write_sdf` half remains a
separate follow-on.

**Status:** spike / feasibility study. This document authorises nothing on
its own — it resolves one flagged `[LIT]`-tier assumption so a follow-on
issue can be scoped honestly. Issue #962, Epic #700 Phase 3.

**What it resolves.** `docs/design/post-route-sta-survey.md` §4.3's own risk
bullet named this the *single largest unverified assumption in that whole
proposal*:

> Icarus's `$sdf_annotate` support and exact invocation convention are
> **[LIT]**-tier, not independently verified in this pass […] the first
> thing a follow-on issue must confirm live before committing to this
> item's own scope. If Icarus's SDF support turns out too limited for this
> repo's own cocotb-driven testbench convention, this item's own "no-go"
> outcome (parking it, documenting why) is a valid, reportable result […]
> not assumed to succeed here.

A No-go was a permitted outcome. It is not the outcome: everything in §2
below was run, not recalled.

---

## Evidence-tier discipline

Following the same convention as the survey this spike serves
(`docs/design-evidence-tiers.md`; the Phase 1/2/3 surveys' own tiering):

- **[RUN]** — captured live in this task, on the host described in §1.
  Every simulator transcript quoted below is verbatim, and every table is
  reproducible via `scripts/research/sdf_annotate_spike.py`.
- **[REPO]** — read directly from this repo's own source/docs, cited by
  file and line against the current tree.
- **[LIT]** — from a tool's published documentation or general EDA
  practice, **not** independently verified here. There are exactly two
  `[LIT]` claims left in this document (§4.1 and §5's note on OpenSTA's
  `write_sdf` output), both explicitly flagged, and both are the follow-on
  issue's own first job.

The survey's methodology note recorded that `openroad/opensta:latest`
publishes no `linux/arm64` manifest and so could not be pulled on this
task's Apple-Silicon host. That is still true, and it is why the `write_sdf`
half of §4.3 is **deliberately not exercised here**: this spike substitutes
a synthetic SDF it generates itself (§2.2) rather than guessing at
OpenSTA's output. That substitution is sound for the question actually
asked — *can Icarus consume an SDF against this repo's testbench
convention?* — and is stated rather than hidden.

---

## 1. Environment

All **[RUN]**:

| Component | Version |
|---|---|
| Icarus Verilog | 13.0 (stable) (v13_0), Homebrew `/opt/homebrew/bin/iverilog` |
| cocotb | 2.0.1 (the pin in `pyproject.toml`'s `functional-verification` extra) |
| Python | 3.12.13 |
| PDK | volare `sky130A`, `open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b` |
| Cell models | `libs.ref/sky130_fd_sc_hd/verilog/{primitives.v,sky130_fd_sc_hd.v}` |
| Host | macOS / Apple Silicon |

Designs exercised — both already in this repo, neither authored for this
spike:

- `gcd` — `examples/functional-verification/gcd.v` (RTL),
  `tests/corpus/statime/gcd_netlist.v` (353 `sky130_fd_sc_hd__*` instances,
  Yosys `write_verilog`), `examples/functional-verification/test_gcd.py`
  (3 cocotb tests, one deliberately failing).
- `modexp` — `examples/functional-verification/modexp.v`,
  `tests/corpus/statime/modexp_netlist.v` (673 instances),
  `examples/functional-verification/test_modexp.py` (2 cocotb tests, both
  expected to pass).

Both testbench modules were used **verbatim**. No testbench, netlist, or
RTL file was edited to make annotation work — that is a load-bearing part
of the result.

---

## 2. The finding

### 2.1 The structural problem, and its solution

`$sdf_annotate` is a Verilog *system task*: it must be called from an
`initial` block inside some elaborated module. A conventional gate-level
flow puts that call in the Verilog testbench that instantiates the DUT.

**This repo has no such module** **[REPO]**. `klt
functional-verification` drives cocotb, whose `hdl_toplevel` *is* the DUT —
`src/klayout_tools/functional_verification.py:595-607` passes
`hdl_toplevel=<design>` straight to `Runner.test()`, and the Python
testbench reaches ports as `dut.clk`, `dut.result`, and so on. There is
nowhere in the shipped convention to put an `initial` block.

Three candidate resolutions were considered; the third is the one that
works and the only one that preserves the existing convention:

| Approach | Outcome |
|---|---|
| Wrap the DUT in a Verilog harness module and make *that* the `hdl_toplevel` | **Rejected.** Breaks every existing testbench: `dut.<port>` would become `dut.u_dut.<port>`. A convention break for every gate-level request. |
| An `iverilog` command-line SDF option, as VCS's `-sdf` provides | **Does not exist.** Icarus has no such flag; `$sdf_annotate` is the only entry point. |
| **A second, otherwise-empty elaboration root carrying the call** | **Works.** `iverilog -s <toplevel> -s klt_sdf_annotate`, with a generated one-module source file. |

The third approach is not an invention here: **cocotb's own Icarus runner
already uses exactly this shape** for waveform dumping. When `waves=True`,
`cocotb_tools/runner.py`'s `Icarus._build_command` generates a
`cocotb_iverilog_dump` module and appends `-s cocotb_iverilog_dump` **[RUN,
read from the installed cocotb 2.0.1]**. The generated shim is three lines:

```verilog
module klt_sdf_annotate();
  initial $sdf_annotate("/abs/path/design.sdf", gcd);
endmodule
```

Because a root module's instance name is its module name, `gcd` resolves to
the DUT root, and the DUT's own hierarchy — the thing the Python testbench
addresses — is untouched.

### 2.2 The three-way comparison §4.3 asks for

§4.3's measurement plan: *"run the same test suite three ways — RTL,
zero-delay gate-level, and SDF-annotated gate-level — and report any status
change per test, not only an aggregate pass/fail."*

The SDF is synthetic — one uniform per-stage delay applied to every cell arc
in the netlist, generated by `scripts/research/sdf_annotate_spike.py`'s
`generate_sdf()`. Uniformity is deliberate: it makes the sweep's threshold
readable as *logic depth × delay vs. clock period* instead of an opaque
number, and it keeps `write_sdf` (out of scope) out of the loop.

`gcd`, testbench clock period 10 ns, cocotb seed 1 **[RUN]**:

| run | `iverilog` build args | `known_pairs` | `random_pairs` | `deliberately_wrong` | SDF diags |
|---|---|---|---|---|---|
| RTL | — | pass | pass | fail (by design) | 0 |
| gate-level | — | pass | pass | fail (by design) | 0 |
| gate-level + `-gspecify -ginterconnect`, **no SDF** | `-gspecify -ginterconnect` | pass | pass | fail (by design) | 0 |
| SDF @ 0.100 ns/stage | + `-s klt_sdf_annotate` | pass | pass | fail (by design) | 0 |
| SDF @ 0.500 ns/stage | " | pass | pass | fail (by design) | 0 |
| SDF @ 0.700 ns/stage | " | pass | pass | fail (by design) | 0 |
| **SDF @ 0.900 ns/stage** | " | **fail** | **fail** | fail | 0 |
| **SDF @ 2.000 ns/stage** | " | **fail** | **fail** | fail | 0 |

`modexp`, same harness **[RUN]**: RTL / gate-level / `gl+specify` / SDF
@0.100 all report both tests passing; SDF @0.900 flips **both** to failing.
Zero SDF diagnostics throughout.

Three things this table establishes, none of which the survey could assert:

1. **Annotation is real, not a no-op.** The verdicts change, at a
   reproducible threshold (between 0.700 and 0.900 ns/stage on `gcd`'s
   10 ns clock), on two independent designs. A silently-skipped annotation
   would leave every row identical to the zero-delay row.
2. **The mechanism is `$sdf_annotate`, not the build flags.** The
   `gl+specify` control row enables `-gspecify -ginterconnect` *without* an
   SDF and is bit-identical in verdict to the plain gate-level row — the
   PDK's own `specify` blocks carry all-zero placeholder delays
   (`( posedge CLK => ( Q : CLK ) ) = ( 0:0:0 , 0:0:0 ) ;`, sky130
   `dfrtp_1` **[REPO, volare install]**), which is precisely why the same
   model files serve both the zero-delay and the annotated run.
3. **The delivered signal is the one §4.3 wants.** A design that is
   functionally correct at zero delay and broken once real delay is
   modelled is caught *by the existing testbench*, with no new assertion
   language and no new engine.

Delay application was independently confirmed at the single-cell level
before the corpus runs, against a hand-written two-gate design **[RUN]**:
with an SDF `(IOPATH (posedge CLK) Q (3.750:…))`, the measured clock-to-Q
was `3750 ps`, against `0 ps` in the unannotated control — the SDF value,
exactly.

---

## 3. What the wiring needs (and what bites)

Every item below is **[RUN]** — an observed Icarus 13.0 behaviour, with its
verbatim diagnostic.

### 3.1 `-gspecify` is mandatory, and its absence fails *silently*

Without it, Icarus discards specify blocks *and the annotation call itself*,
then runs at zero delay and exits 0:

```
sdf_shim.v:4: warning: Omitting $sdf_annotate() since specify blocks and
interconnects are being omitted.
```

That is a compile-time `warning:` on stderr among hundreds of others, and
the run afterwards is indistinguishable from a successful annotation unless
you are measuring delays. This is the single most dangerous behaviour on
this path.

### 3.2 `-ginterconnect` is mandatory for any SDF carrying `INTERCONNECT`

With `-gspecify` alone, an `INTERCONNECT` entry fails at run time,
per entry:

```
Error: Could not find net. Did you run iverilog with '-ginterconnect'?
SDF ERROR: design_ic.sdf:9: Could not find intermodpath!
```

With `-ginterconnect` added, the same entry applies exactly: a
`(INTERCONNECT a u_and.A (4.000:…))` moved a measured input-to-output
transition from `10100 ps` to `14100 ps` — the annotated 4.000 ns, to the
picosecond.

This matters because a static-timing engine's SDF is expected to carry
net delays alongside cell delays (§4.3's whole premise is *post-route
parasitic* delay), so the flag is not optional in practice.

### 3.3 Every SDF failure mode is non-fatal — the transcript is the only signal

Icarus reports SDF problems and **continues**; `vvp` exits `0` in all of
these cases, and cocotb consequently reports `PASS`:

| Condition | Diagnostic | `vvp` exit |
|---|---|---|
| SDF file missing/unopenable | `SDF WARNING: <shim>:2: Unable to open SDF file "does-not-exist.sdf". Skipping this annotation.` | 0 |
| IOPATH names an arc the cell's `specify` block does not declare | `SDF ERROR: <sdf>:2976: Unable to match ModPath D -> Q in gcd._647_` | 0 |
| `INTERCONNECT` without `-ginterconnect` | `SDF ERROR: <sdf>:9: Could not find intermodpath!` | 0 |
| `TIMINGCHECK` section present | `SDF WARNING: <sdf>:7: TIMINGCHECK not supported.` | 0 |

**Any wiring MUST scan the simulator transcript** and fail the run on the
first three. This is not defensive padding: it is the difference between
"the design was re-verified with real delays" and "the design was re-run at
zero delay and nobody noticed." It is also directly analogous to
`functional_verification.py`'s own founding discipline — *never trust a raw
subprocess exit code, derive the verdict from the artifact*
(`src/klayout_tools/functional_verification.py:22-31` **[REPO]**) — applied
one level deeper.

The fourth row is **expected and benign**, and must be classified
separately (see §4.1).

The `Unable to match ModPath` case is not hypothetical. This spike's own
first SDF generator emitted a `D -> Q` IOPATH for every flop; sky130's
`dfrtp_1` declares only `(posedge CLK => Q)` and `(negedge RESET_B => Q)`
arcs, routing `D` through `$setuphold` instead. Result: **50 `SDF ERROR`
lines, 50 flops silently unannotated, and a run that still reported the
expected verdicts.** It was caught only by scanning the transcript. That is
the trap, demonstrated on this repo's own corpus.

### 3.4 Timing checks are not implemented at all

Elaborating the sky130 models with `-gspecify` produces **479** instances of:

```
warning: Timing checks are not supported. Delayed reference and data
signals become copies of the original reference and data signals.
```

Icarus implements SDF **delays**, not SDF **timing checks**. Consequences,
stated plainly:

- **No setup/hold violation *reporting*.** This path will never print
  "setup violation at instance X". OpenSTA remains the only source of slack
  numbers; that is §4.1/§4.2's job and is unaffected.
- **Delays still propagate**, so a genuine timing failure surfaces as a
  *functional* failure the testbench catches — precisely what §2.2's table
  shows. §4.3's stated metric is a coverage metric ("does the testbench's
  own pass/fail outcome change"), not a violation count, so the item's
  value survives this limitation intact.
- `$setuphold`-driven `X` propagation on a violated flop does **not**
  happen, so this path is *less* pessimistic than a commercial gate-level
  sim. Worth stating in whatever the follow-on writes into
  `docs/cli/functional-verification.md`; not worth blocking on.

### 3.5 min/typ/max selection is a *compile-time* flag, not an annotate argument

Icarus ignores every `$sdf_annotate` argument past the second:

```
SDF WARNING: tbm.v:5: $sdf_annotate currently only uses the first two argument.
```

(Icarus's own wording, typo included.) The `mtm_spec`, `scale_factors`,
`scale_type`, config-file and log-file arguments are all inert. Corner
selection is instead `iverilog -T min|typ|max`, verified against an SDF
triplet of `(1.000:2.000:3.000)` **[RUN]**:

| flag | measured clock-to-Q |
|---|---|
| `-T min` | 1.000 ns |
| `-T typ` (default) | 2.000 ns |
| `-T max` | 3.000 ns |

This composes cleanly with §4.2's multi-corner sweep: one `write_sdf` per
corner, or one SDF with real triplets plus a `-T` selection — the follow-on
should pick one and say so.

### 3.6 A top-level-port-attached `INTERCONNECT` entry needs the DUT nested, not rooted

Found live during issue #1056, after this document's original GO verdict had
already shipped (#1002/PR #1007): every purely internal
`INTERCONNECT <inst>.<pin> <inst>.<pin>` entry resolves exactly as §3.2
describes, but an entry touching a **top-level port** —
`INTERCONNECT a u_and.A` (a primary input) or `INTERCONNECT u_and.Y b` (a
primary output) — always failed, even with `-ginterconnect` present and
even though §3.2's own worked example above *looks* like a counter-example
(`(INTERCONNECT a u_and.A (4.000:…))`). The difference: that example's
`$sdf_annotate` call was issued from a **standalone repro harness**, not
through this repo's actual shim generator; §2.1's shim (the one that
shipped) elaborates the DUT as its own `-s` root, and *that* is what breaks
top-level-port resolution.

**Root cause, confirmed live (not the mechanism originally suspected):**
Icarus cannot resolve a bare top-level-port identifier against a module
elaborated as its *own* `-s` root at all — regardless of whether
`$sdf_annotate` is called from a sibling elaboration root (this repo's
shim) or from an `initial` block *inside* the DUT's own scope (tested
directly: identical failure). The previously-plausible "separate
elaboration root" theory is therefore **ruled out** — moving the
`$sdf_annotate` call does not change the outcome. What does: Icarus resolves
the identical bare-port SDF syntax cleanly when the named module is instead
a **nested child instance** of another root — confirmed against Icarus's
own `ivtest` SDF regression fixtures (`ivtest/ivltests/sdf_interconnect1.v`
et al.), which use exactly that shape (a `top` module instantiates the real
design as a named child, and calls `$sdf_annotate` from `top` naming that
child).

**This corrects §2.1's table.** The "wrap the DUT in a Verilog harness
module" row above was rejected there on the assumption that it necessarily
breaks `dut.<port>` testbench access (`dut.u_dut.<port>`) — true only if the
wrapper does *not* re-declare the DUT's own port list. A **transparent**
pass-through wrapper (identical port names/widths, forwarding straight into
a nested DUT instance) does not have this problem: cocotb's `hdl_toplevel`
becomes the wrapper, so `dut.<port>` in Python resolves to the *wrapper's*
identically-named port — completely unchanged from the testbench's point of
view — while `$sdf_annotate`'s own scope argument (Verilog-level, entirely
decoupled from cocotb's `COCOTB_TOPLEVEL` machinery) can now name the nested
DUT instance directly. Verified live end-to-end through real cocotb 2.0.1 +
Icarus 13.0, not just raw `iverilog`/`vvp`: a two-cell design with one
top-level-port `INTERCONNECT` entry and one internal one, both apply
cleanly, and the annotated delay changes the testbench's own verdict exactly
as §2.2's coverage metric expects.

Implemented in `_write_sdf_dut_wrapper`/`_parse_toplevel_ports`
(`src/klayout_tools/functional_verification.py` **[REPO]**): the wrapper's
port list is parsed from the DUT's own module declaration (both the
non-ANSI convention Yosys/OpenROAD `write_verilog` emit and the ANSI
convention this repo's own test fixtures use), so it stays byte-for-byte in
sync with whatever netlist `options.sdf` names — no drift between the
wrapper and the design it wraps.

One known gap, not yet closed: `request.parameters` combined with
`options.sdf` is currently a **request error**, not silently broken —
cocotb's own Icarus parameter-override syntax is always
`-P<hdl_toplevel>.<name>=<value>`, and once `hdl_toplevel` is the generated
wrapper (which declares no parameters), that override would target the
wrong scope. A follow-on could reformat the override to target
`<wrapper>.<nested-instance>.<name>` directly instead of relying on cocotb's
own injection, but no design in this repo's own corpus needs both today.

### 3.7 A bit-selected top-level-port `INTERCONNECT` entry can poison a sibling entry on the same net

Found live during issue #1619, after §3.6's scalar-port fix (#1056/#1069)
shipped. §3.6 fixed `INTERCONNECT a u1.in` / `INTERCONNECT u2.out b` — a
*scalar* bare top-level-port endpoint. Once that fix let a **vector** port
*bit* (`y[0]`, `a[0]` — what any real post-route netlist's bus ports
produce) resolve too, a *new*, narrower failure appeared: a driver net
fanning out to both a bit-selected top-level port and a purely internal
instance-pin load resolves the port-touching entry but not the sibling, on
an otherwise-identical-shaped net with no port touch resolving 100%. Both
reported shapes were reproduced with a minimal fixture, live against Icarus
13.0, via raw `iverilog`/`vvp` directly (§3.2's own methodology) as well as
through this repo's actual wrapper/shim generator and the real cocotb
`Runner`.

**The issue's own hypothesis, refuted.** The issue body speculated Icarus
"loses track of (or double-binds)" a net's identity once the wrapper
resolves *one* `INTERCONNECT` entry against it. That is not what happens.
Two *separate* mechanisms were found instead, depending on which side of the
entry carries the bit-selected port — neither is an identity/double-bind
problem, both are confirmed by evidence a genuine double-bind would not
produce:

**Shape (a) — bit-selected port as a destination** (an output net fanning
out to a vector output-port bit *and* an internal pin, or any entry sharing
a source with one): **order-dependent, and fixed.** Once an entry touching
a bit-selected port is annotated, every entry *later in the same
`$sdf_annotate` call* that shares that entry's physical net fails with
`Could not find intermodpath!` — regardless of its own shape (a purely
internal instance-pin-to-instance-pin entry is equally affected). Reordering
the entries (bit-selected-port entry *last* instead of first) makes every
entry resolve — which a genuine net-identity double-bind would not fix, since
the same physical net is still touched twice either way, just in a different
order. **Fix, verified live end-to-end through real cocotb + Icarus 13.0:**
defer every bit-selected-top-level-port-touching entry to its own, later
`$sdf_annotate` call, after every other entry has already been annotated.
Nothing in the deferred call's own entries runs after a poisoning entry on
that entry's own net (each vector port bit is its own physically distinct
net), so nothing is left to fail. Implemented in
`_split_sdf_bus_port_interconnects` (`src/klayout_tools/functional_
verification.py` **[REPO]**).

**Shape (b) — bit-selected port as a source** (an input net fanning out to
an ordinary cell input *and* a single-pin antenna/diode-style load):
**not order-dependent, and not fixable from this module.** A *single* such
entry, entirely alone with no sibling at all, already fails whenever its
destination pin declares its own `specify`/`IOPATH` block — which an
ordinary standard cell always does; it resolves cleanly only against a
destination with no `specify` block at all (an antenna/diode-style cell).
Reordering or deferring the entry to its own call changes nothing here,
ruling out shape (a)'s workaround. More surprising still: **the outcome
depends on the testbench**, not just the SDF/netlist. The identical fixture,
run through this repo's actual wrapper/shim/split pipeline, resolves cleanly
with a **no-op** cocotb test (a coroutine that returns without ever
`await`ing a trigger) but **deterministically fails** once the testbench
actually drives the DUT and `await`s a `Timer` — reproduced identically
across a clean `.klt/` build directory, a fresh Python interpreter, and a
fresh `klt` CLI subprocess (**[RUN]**, ruling out build-directory or
process-state reuse as the cause). This points at a genuine race inside
Icarus itself between `$sdf_annotate`'s own intermodpath insertion and VPI
callback scheduling once the simulator actually advances past time 0 — not
something a generated wrapper, shim, or SDF split can control from the
outside. **Left unfixed, deliberately**: implementing a workaround for a
mechanism this module cannot observe or control would risk papering over a
still-unresolved entry rather than actually fixing it. Pinned down by
`test_integration_real_icarus_sdf_bus_port_input_fanout_stays_unresolvable`
(`tests/test_functional_verification.py` **[REPO]**), which must keep
raising — a future change making it pass silently would need to *explain*
why, not just observe the new behaviour.

Both this module's own regression (finding 7/§3.6's
`test_integration_real_icarus_sdf_resolves_a_toplevel_port_interconnect`)
and the wrapper/shim mechanism itself are unaffected by this section: no
change to `_write_sdf_dut_wrapper`, `_write_sdf_annotate_shim`'s scope
argument, or `_parse_toplevel_ports` was needed. `_write_sdf_annotate_shim`
was extended to accept a list of SDF paths (one `$sdf_annotate` call per
path, in order) rather than a single path, purely additive — a design with
no vector top-level ports (every existing test/design prior to this issue)
still gets exactly one call, byte-identical to before.

**Deferred, deliberately not investigated here:** issue #1619 also flagged a
"possibly-related second-order effect" — with the same non-`FUNCTIONAL`
sources, a regression with `options.sdf` omitted entirely passes cleanly,
but adding it back (even with only a minority of `INTERCONNECT` entries
unresolved) turns the run into a uniform, constant-zero result on every
case, not merely a subtly different timing outcome. The issue body itself
asked that this not be conflated with the primary `INTERCONNECT`-count bug
above, and this pass leaves it that way: shape (b)'s testbench-sensitivity
(§3.7 above) shows this repo's own minimal fixtures can already reach
Icarus-internal, `$sdf_annotate`-presence-dependent behaviour that is
sensitive to details (a no-op test vs. a driven one) well outside SDF
content — a plausible *adjacent* symptom of the same general fragility
class, but reproducing the specific "constant-zero on any partial-failure
run" shape needs a design closer to the sky130-scale one the issue's own
run used (1927 `INTERCONNECT` entries), not a hand-written unit fixture, to
be confident it is the same mechanism rather than a superficially similar
one. Filed as its own follow-up rather than guessed at here — issue #1854.
**Answered in §3.8 below** (2026-09-15): reproduced at sky130 scale, and it
is *not* the same mechanism — not an annotation failure at all.

### 3.8 The "constant-zero on every test case" shape is a timing outcome, not a broken annotation

> **Scope (2026-09-23, issue #2364):** this attribution was measured on — and
> holds for — the **timing-violating** design class the section's own run was
> built from. On **timing-clean** designs (positive setup/hold slack at the
> SDF's own corner) the same shape is **annotation-class-dependent**: dead
> under any `IOPATH`-bearing SDF at any clock period, correct under
> `INTERCONNECT`-only annotation. See the dated addendum at this section's
> end before applying this section's conclusion to a design whose STA closes.

Found live during issue #1854, closing out the question §3.7 deferred. The
claim under test, from #1619's own report:

> with the same non-`FUNCTIONAL` sources and the same design, a regression
> against these cell models with `options.sdf` **omitted entirely** (no
> `$sdf_annotate` call in the compiled design at all) passes cleanly. Adding
> `options.sdf` back — even with only a minority of `INTERCONNECT` entries
> unresolved — turns the same regression into a uniform, constant-zero result
> on every case, not merely a subtly-different timing outcome.

**What was run** — a real sky130 post-route design generated for this pass,
not a hand-written fixture, per #1854's own acceptance criterion **[RUN]**:

| | |
|---|---|
| Design | `examples/functional-verification/gcd.v` → `klt synthesize` (native Yosys) → `klt place-and-route` (native OpenROAD, `target_stage: "route"`, `post_route_spef: true`, `post_route_sdf: true`, 1.1 ns target clock) |
| Netlist | OpenROAD `write_verilog`, **460** `sky130_fd_sc_hd__*` instances |
| SDF | OpenSTA `write_sdf` from the same post-`read_spef` session: **1049** `INTERCONNECT`, **1068** `IOPATH`, **50** `TIMINGCHECK` entries |
| STA | `spef_sta.worst_slack_ns` −2.0508 ns, 50 setup violations at the 1.1 ns target (`annotation_complete: true`, 494/494 design nets) |
| Models | `sky130_fd_sc_hd.v` + `primitives.v`, **`FUNCTIONAL` undefined** (required — `options.sdf` rejects a `FUNCTIONAL` define) |
| Testbench | `examples/functional-verification/test_gcd.py` verbatim, plus a non-asserting probe variant that *logs* `done`/`result` per case instead of raising |
| Tools | Icarus Verilog 13.0 (stable), cocotb 2.0.1, `klt` 0.5.0 |

Every row below uses `corner: "min"`. That is not a free choice: the default
`corner: "typ"` cannot read an OpenSTA `write_sdf` header at all, a *separate*
defect found on the way here and filed as issue #1880 (`(VOLTAGE 1.800::1.800)`
/ `(TEMPERATURE 25.000::25.000)` are `min::max` triples with an empty typ
member; `iverilog -T typ` answers `SDF ERROR: … Chosen value not defined.`
twice, which the transcript gate turns into exit 1). `"min"`/`"max"` read the
identical file cleanly.

**The isolation table** — every row is the same design, the same netlist, the
same cell models, the same testbench source **[RUN]**:

| # | `options.sdf` | unresolved `INTERCONNECT` | testbench clock | outcome |
|---|---|---|---|---|
| a | omitted | — | 10 ns | 2 pass / 1 deliberate fail; `result` correct on every case |
| b | set, all 1049 applied | **0** | 10 ns | **identical to (a)**: same verdicts, and the probe's logged `result` *and* done-cycle counts match (a) exactly |
| c | set, 20 entries hand-broken | 19 | 10 ns | transcript gate raises (as designed); `results.xml` verdicts and values still identical to (a) |
| d | set, splitter disabled to reproduce the pre-#1857 single `$sdf_annotate` call | **16** genuine §3.7 shape-(a) failures | 10 ns | verdicts and values still identical to (a) |
| e | set, all 1049 applied | **0** | **2 ns** | **`done` never asserts; `result` = `0000000000000000` on every case** |
| f | omitted | — | 2 ns | correct on every case |
| g | set, every delay ×4, all applied | **0** | 10 ns | **constant zero on every case** |

Rows (e)/(f) and (g) *are* the reported effect, reproduced. Rows (b)/(c)/(d)
are what refutes the mechanism it was attributed to.

**The clock-period sweep**, same design, same SDF, probe testbench **[RUN]**:

| testbench clock | annotated `result` per case |
|---|---|
| 10.0 / 3.5 / 3.2 / 3.0 ns | correct (6, 21, 1) |
| 2.8 ns | first case correct, every later case hangs — the narrow "subtly different" band |
| 2.5 / 2.0 ns | `0000000000000000`, `done` never asserts, **every** case |

The threshold (between 2.5 and 3.0 ns) is the design's own annotated critical
path. It sits below OpenSTA's 3.15 ns data-arrival figure for the same design
because Icarus models no setup time at all (§3.4) — the simulated path only
has to settle, not to settle *and* meet a setup window.

**Mechanism.** `gcd`'s `result` is a registered output that loads only when the
iterative subtractor reaches its exit condition; the 16-bit compare/subtract
feedback path is the design's critical path. Once the annotated delay on that
path exceeds the testbench's clock period, the `a`/`b` registers capture stale
values every cycle, the loop never converges, `busy` never clears, `done` never
pulses — and `result` is still holding the `0` its reset put there. Every test
case drives the same FSM down the same broken path, so every case reads exactly
zero. The uniformity that made this look like an engine-level fault is the
*expected* signature: a gate-level design clocked past its critical path fails
catastrophically and identically, not subtly. (Holding `start` high for 10
cycles instead of 1 changes nothing, which rules out inertial rejection of the
one-cycle `start` pulse as the cause on this design — the settling failure is
in the datapath feedback, not the handshake.)

And the other half of the contrast: **a zero-delay run cannot fail for timing
at any clock period at all.** Row (f) passing at 2 ns is not evidence that the
design meets a 2 ns clock; it is evidence that there is nothing there to meet.
"Passes without `options.sdf`, fails uniformly with it" is precisely the
coverage signal §2.2/§4.3 exists to produce, firing — not an engine defect.

**#1619's suspected mechanism is refuted, measured directly.** The hypothesis
was that Icarus's compile-time fallback ("Delayed reference and data signals
become copies of the original reference and data signals", §3.4) is disabled
once any `$sdf_annotate` call exists in the design. It is not **[RUN]**:

- The annotated build emits that warning class **479** times (310 carrying the
  "delayed signals become copies" sentence — the `$setuphold`/`$recrem` checks
  that declare delayed-signal arguments — and 169 without, the `$width` checks
  that declare none).
- A build with the generated wrapper and `-gspecify -ginterconnect` but **no
  `$sdf_annotate` call at all** emits the identical **479 / 310** counts, and
  passes. The fallback is gated on `-gspecify`, not on the presence of an
  annotate call.
- The unannotated baseline emits the warning **0** times only because
  `options.sdf` is also what adds `-gspecify`. "No `$sdf_annotate` ⇒ no
  warning" is a correlation through that flag, not a causal link.
- With the fallback demonstrably active, row (b)'s verdicts and values are
  identical to row (a)'s.

**Not the same mechanism as §3.7 shape (b)** — confirmed distinct on three
independent axes, which is #1854's other acceptance criterion:

1. **Diagnostic signature.** Shape (b) always leaves an `SDF ERROR: … Could
   not find intermodpath!` in the transcript (this module raises on it). The
   constant-zero shape leaves **zero** actionable diagnostics; the only dropped
   class is the benign `TIMINGCHECK` one, and `environment.sdf` reports
   `annotated: true` with every delay applied.
2. **Testbench sensitivity.** Shape (b) flips on whether the cocotb test ever
   `await`s a trigger, against a byte-identical SDF. The constant-zero shape is
   a deterministic function of one number — the testbench's clock period versus
   the annotated critical path — and reproduces on any testbench that drives
   the design.
3. **Unresolved-entry count is not the driver.** Rows (c)/(d) carry 19 and 16
   unresolved entries and produce *correct* results; rows (e)/(g) carry **zero**
   and produce the constant-zero one. The two axes are independent.

**Decision: no fix in `functional_verification.py`, and nothing to raise
upstream with Icarus.** There is no defect in either: the annotation applied
completely, and the simulator reported what the annotated design does. What was
missing was a way to *read* the result — a uniform every-case failure under
`options.sdf` looks identical to a broken engine if you have not separated the
two. That is what this section and the regression test
`test_integration_real_icarus_sdf_constant_zero_is_a_timing_outcome`
(`tests/test_functional_verification.py` **[REPO]**) supply: a PDK-free
three-row fixture — fast clock unannotated (passes), fast clock annotated
(constant zero on every case, zero diagnostics), slow clock annotated (passes
again) — that must keep holding, so a future change cannot quietly turn a real
annotation failure into "oh, that's just the timing shape". The reading
guidance is mirrored for callers in
[`docs/cli/functional-verification.md`](../cli/functional-verification.md)
§"SDF back-annotation".

#### §3.8 addendum (2026-09-23, issue #2364): the attribution above does not generalize to timing-clean designs — the shape is annotation-class-dependent there

The run above was built on a **timing-violating** design (−2.05 ns worst
setup slack at the 1.1 ns target), and on that design class the §3.8
attribution stands: dead at the violating period, alive again at a relaxed
one, which is the signature of a genuine annotated-delay failure. It does
**not** generalize. Issue #2364 isolated the same constant-zero shape on a
design whose SPEF-annotated STA **closes** — and the discriminator is the
SDF's entry class, not the clock period **[RUN]**:

| | |
|---|---|
| Design | `examples/functional-verification/gcd.v` → `klt synthesize` (native Yosys) → `klt place-and-route` (native OpenROAD, `target_stage: "route"`, `post_route_spef: true`, `post_route_sdf: true`, **10 ns** target clock, seed 7) |
| Netlist | OpenROAD `write_verilog`, 402 `sky130_fd_sc_hd__*` instances |
| SDF | OpenSTA `write_sdf` from the same post-`read_spef` session: **1064** `INTERCONNECT`, **1043** `IOPATH`, **50** `TIMINGCHECK` entries |
| STA | `spef_sta.worst_slack_ns` **+5.48 ns**, 0 setup violations, 0 hold violations, TNS 0 — timing **closed** at the testbench's own 10 ns period |
| Models / tools | `sky130_fd_sc_hd.v` + `primitives.v`, `FUNCTIONAL` undefined; Icarus Verilog 13.0, cocotb 2.0.1 |

The isolation table — every row is the same design, netlist, models, and
testbench (the stock `test_gcd.py`, whose one deliberately-failing case makes
"2 pass / 1 deliberate fail, `result` correct" the healthy baseline) **[RUN]**:

| # | SDF handed to `$sdf_annotate` | testbench clock | outcome |
|---|---|---|---|
| a | none (zero-delay models) | 10 ns | 2 pass / 1 deliberate fail; `result` correct on every case |
| b | `INTERCONNECT` entries only (1043 `IOPATH` entries removed from the SDF text) | 10 ns | **identical to (a)** — correct results |
| c | full SDF (all `IOPATH` + `INTERCONNECT` applied, zero actionable diagnostics, `annotated: true`) | 10 ns | **dead** — `result` = 0 on every case, uniform constant-zero failure |
| d | the same full SDF | **100 ns** (10× relaxed) | **still dead** — identical constant-zero failure |

Row (d) is the one a genuine timing failure cannot produce: the design's
annotated critical path is ~4.5 ns (10 ns period − 5.48 ns slack), so a
100 ns period leaves ~95 ns of setup margin, yet the annotated run is
exactly as dead as at 10 ns. Compare §3.8's own
clock-period sweep on the violating design, where relaxation revived it at
3.0 ns. Dead at both 1× and 10× with net-delay-only annotation passing is an
**annotation-mechanism kill**, not a timing outcome — the #1888/§3.8
clock-period-sweep evidence is real but only discriminates on the
timing-violating class it was measured on.

**Suspected mechanism** (consistent with #1854's original hypothesis, not
proven here): the sky130 specify-branch models wire each cell's functional
UDP through `*_delayed` nets driven only by `$setuphold`/`$recrem`
delayed-signal outputs, which Icarus replaces with "copies of the original
signals" (a warning per cell at build). With `IOPATH` module-path delays
annotated on top, the copies and the path-delay scheduling interact so some
cells' outputs settle to stale values — run-to-run nondeterministically in a
driven testbench, matching the §3.7 shape-(b) testbench-sensitivity recorded
by #1619. `INTERCONNECT`-only annotation never touches a module path and is
stable.

**Consequences.**

- The §3.8 reading guidance ("a uniform every-case failure is usually not a
  broken annotation — check `dropped`, the testbench clock, and re-run
  slower") keeps its step 1 and gains a step 0 on timing-clean designs:
  **which entry classes does the SDF carry?** On a design whose STA closes,
  the constant-zero shape is the *expected* outcome of annotating `IOPATH`
  onto the sky130 specify-branch models in Icarus, at any clock period.
- The supported, honestly-reported configuration for net-delay
  back-annotation on this design class is `options.sdf.entries:
  "interconnect"` (issue #2364): every non-`INTERCONNECT` delay entry is
  dropped before annotation and counted as the
  `iopath_interconnect_only` `environment.sdf.dropped` class — a
  first-class mode replacing the hand-filtered SDF row (b) above. See
  [`docs/cli/functional-verification.md`](../cli/functional-verification.md)
  §"SDF back-annotation".
- The PDK-free regression test above keeps holding **and keeps its §3.8
  attribution** — its docstring now states it covers only the
  timing-violating design it was built from (#1854's repro), so it and this
  addendum are not read as contradicting each other. The timing-clean
  isolation table above is reproduced by the `entries` mode's own
  integration coverage, not by a sky130 CI fixture (PDK-scale gate-level
  runs stay out of CI for the same cost reasons §1 recorded).

---

## 4. Follow-on sketch (§4.3, Icarus half)

Sized against what §2/§3 actually established. This is a sketch for a
follow-on issue, not an authorisation.

### 4.1 Request-contract shape

An **additive** optional block on the existing request — no `schema_version`
bump, since `docs/json-contract.md`'s rule makes only field *removal* or
*meaning change* breaking:

```json
{
  "sources": [".klt/synthesize/gcd_synth.v", "<pdk>/sky130_fd_sc_hd.v"],
  "hdl_toplevel": "gcd",
  "testbench": { "module": "test_gcd" },
  "options": {
    "sdf": { "file": "route.sdf", "corner": "typ" }
  }
}
```

- `options.sdf.file` — path, resolved relative to the request like every
  other path field (`_resolve_sources`'s convention, **[REPO]**).
- `options.sdf.corner` — `"min" | "typ" | "max"`, mapping to `-T`
  (§3.5). Default `"typ"`.
- `options.sdf` with `engine: "verilator"` must be a **request error**
  (exit 1), never a silent no-op — the same posture
  `options.coverage` + `icarus` already takes
  (`functional_verification.py:282-286`, **[REPO]**). Verilator has no
  SDF path; §4.3 scopes this to Icarus only.

### 4.2 Implementation, in `functional_verification.py`

Roughly 5 changes, all local:

1. `_resolve_options()` — parse and validate the new block.
2. Generate `<output_dir>/klt_sdf_annotate.v` (§2.1's three-line shim,
   embedding the **absolute** SDF path — `vvp`'s CWD is the run directory,
   and a relative path there is a `SDF WARNING` away from a silent
   zero-delay run).
3. Append that file to `sources`, and `["-gspecify", "-ginterconnect",
   "-s", "klt_sdf_annotate"]` to `build_args` — which the module currently
   hardcodes to `COVERAGE_BUILD_ARGS` or `[]`
   (`functional_verification.py:828`, **[REPO]**), so this is the one place
   an existing constant has to become a computed list.
4. **Transcript gate** (§3.3): after the run, scan `build_<engine>.log` and
   `test_<engine>.log` for `SDF WARNING` / `SDF ERROR`, and raise
   `FunctionalVerificationError` on any line that is *not* the benign
   `TIMINGCHECK not supported` class. Getting this classification right is
   the single highest-value part of the work — **[LIT]**: OpenSTA's
   `write_sdf` is understood to emit `TIMINGCHECK` sections alongside
   `IOPATH`/`INTERCONNECT`, which would make that warning fire once per
   cell on every real SDF; the follow-on must confirm this against real
   `write_sdf` output before choosing between "allowlist the string" and
   "count and report". Do not ship a gate that rejects every real SDF.
5. Echo what was applied in the response's `environment` block (e.g.
   `sdf_file`, `sdf_corner`, `sdf_annotated: true`) so a caller can tell an
   annotated run from an unannotated one *from the JSON*, without reading a
   log. Additive fields only.

Test coverage should extend `tests/test_functional_verification.py`, and
the natural end-to-end fixture already exists: `tests/corpus/statime/
gcd_netlist.v` plus a small checked-in SDF.

### 4.3 Still separate, still upstream

The `write_sdf` half — adding `write_sdf <path>` to `place_and_route.py`'s
`route` stage after #948's `read_spef`, and surfacing an `sdf_path` in that
verb's response — remains its own follow-on, exactly as §4.3 framed it.
Nothing in this spike changes that split. Until it lands, the Icarus half is
usable with any externally-produced SDF, which is a real capability on its
own.

> **Both halves have since shipped (issue #1002, 2026-08-15)**, following
> this sketch: `klt place-and-route`'s `post_route_sdf` request field
> (`spef_sta.sdf_path`) and `klt functional-verification`'s
> `options.sdf: {file, corner}` block. §4.2's item 4 flagged one open
> **[LIT]** question — whether OpenSTA's `write_sdf` emits `TIMINGCHECK`
> sections, which would make Icarus's benign `TIMINGCHECK not supported`
> warning fire once per cell on every real SDF. It was resolved by
> *allowlisting the string* (that class, and only that class, is exempt from
> the transcript gate) rather than by "count and report", precisely so the
> gate cannot reject every real SDF; it remains **[LIT]** whether OpenSTA
> actually emits those sections, since no `openroad`/OpenSTA binary was
> available to the implementing task either. See
> `docs/cli/functional-verification.md` §"SDF back-annotation" for the
> shipped contract.

---

## 5. Reproducing this

```bash
PDK=sky130A uv run --python 3.12 --extra functional-verification python \
    scripts/research/sdf_annotate_spike.py --format text \
    --fail-on-sdf-diagnostics
```

`--python 3.12` is load-bearing on a host whose default interpreter is
3.14: `pyproject.toml` marks cocotb `python_version < "3.14"` (cocotb 2.0.1
caps at 3.13), so a 3.14 resolve silently omits it.

Requires `iverilog` on `$PATH` and a PDK install carrying
`libs.ref/sky130_fd_sc_hd/verilog/` (a volare `sky130A`). `--format json`
emits the full result set; `--delay-ns` sets the sweep;
`--rtl`/`--netlist`/`--hdl-toplevel`/`--testbench-module` point it at
another design (the `modexp` row in §2.2 was produced that way). Like
`tests/corpus/statime/regenerate.sh`, this is a deliberate operator-run
study, not a CI step: it depends on a host PDK install CI does not
provision.

### 5.1 Reproducing §3.8's sky130-scale run

§3.8's table is not produced by the script above — it needs a *real* post-route
SDF, so it runs the shipped verbs end to end. In a scratch directory, with
`iverilog` **13.0** and a native `openroad` on `$PATH` and a volare `sky130A`:

1. `klt synthesize` `examples/functional-verification/gcd.v` against
   `sky130_fd_sc_hd` / `tt_025C_1v80` (the request in
   `tests/corpus/place_and_route/regenerate.sh` verbatim).
2. `klt place-and-route` that netlist with `"target_stage": "route"`,
   `"post_route_spef": true`, `"post_route_sdf": true`, `clock_port: "clk"`,
   `clock_period_ns: 1.1` — yielding `verilog_path` and `spef_sta.sdf_path`.
3. `klt functional-verification` against `[primitives.v,
   sky130_fd_sc_hd.v, <verilog_path>]` with `hdl_toplevel: "gcd"`,
   `testbench.module: "test_gcd"`, **no** `FUNCTIONAL` define, once with
   `options.sdf` omitted and once with
   `{"file": "<sdf_path>", "corner": "min"}` (not `"typ"` — issue #1880).

Rows (e)/(f) need only one further change: a copy of `test_gcd.py` whose
`Clock(dut.clk, 10, unit="ns")` becomes `2`. Row (g) needs a copy of the SDF
with every `(a:b:c)` delay triple multiplied by 4. Row (d) needs
`_split_sdf_bus_port_interconnects` stubbed to return `None` (the pre-#1857
single-call shape). The PDK-free distillation of the finding, which *does* run
anywhere with cocotb + Icarus 13.0, is
`test_integration_real_icarus_sdf_constant_zero_is_a_timing_outcome` in
`tests/test_functional_verification.py`.

---

## References

- [`docs/design/post-route-sta-survey.md`](post-route-sta-survey.md) §3.5,
  §4.3 — the proposal this spike de-risks.
- [`docs/design/cocotb-verification-spike.md`](cocotb-verification-spike.md)
  — the engine survey and invocation discipline
  `functional_verification.py` implements.
- [`docs/design-evidence-tiers.md`](../design-evidence-tiers.md) — T1
  checklist item 7 (digital), the item §4.3 closes.
- [`docs/cli/functional-verification.md`](../cli/functional-verification.md)
  — the verb's contract; line 175 is the `sources` = `netlist_path`
  gate-level convention this spike exercised.
- IEEE 1364-2005 §17 (`$sdf_annotate`), IEEE 1497 (SDF) — the standards
  Icarus implements a delay-only subset of (§3.4).

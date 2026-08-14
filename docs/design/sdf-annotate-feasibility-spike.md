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

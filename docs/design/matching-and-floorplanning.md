# Matching and floorplanning: from a stated spec to `klt gen` parameters

This document answers the gap issue #1016 named (itself filed from
[`analog-resource-survey.md`](analog-resource-survey.md) §2.2, §3, mining
issue #1014): `klt gen`'s `mos_array`/`res_array`/`bjt_array`/`diff_pair`
generators already implement `topology="common_centroid"`
(`src/klayout_tools/gen.py`, documented in [`docs/cli/gen.md`](../cli/gen.md))
— the layout idiom for device matching is built — and the `spec-review`
skill already lists "Pelgrom mismatch coefficients" as a grounding-evidence
category (`.claude/skills/spec-review/SKILL.md` → "Inputs"). What was
missing is the arithmetic step between them: given a matching spec, how big
does a unit device need to be, and how many of them, before calling a
generator at all. This document is that connective step — the bridge an
agent walks when handing off from **S5 sizing**
(`.claude/skills/design-sizing/SKILL.md`) to **S7 layout generation**
(`.claude/skills/design-layout-generation/SKILL.md`) for any block whose
spec includes a matching/offset requirement.

**Scope note**: this is design-time *guidance*, consumed by agent reasoning
— not a new `klt` verb. Sizing judgment stays out of tooling by the
design-sizing skill's own recorded decision (#310); this document is the
same kind of judgment, one stage later in the pipeline. It also feeds, but
does not duplicate, issue #1013's layout-economy review rubric — see
["Relationship to the #1013 rubric"](#relationship-to-the-1013-layout-economy-rubric)
below.

## 1. Pelgrom's law

Random mismatch between two nominally-identical MOS devices scales inversely
with the square root of their gate area, independent of how that area is
split between width and length:

```
σ(ΔVt)    = A_VT / √(W·L)      -- threshold-voltage mismatch
σ(Δβ/β)   = A_β  / √(W·L)      -- current-factor (β = μ·Cox·W/L) mismatch
```

Source: M. J. M. Pelgrom, A. C. J. Duinmaijer, A. P. G. Welbers, "Matching
properties of MOS transistors," *IEEE Journal of Solid-State Circuits*,
vol. 24, no. 5, pp. 1433–1440, Oct. 1989. `A_VT` and `A_β` are **process
constants** (typical units: `mV·µm` for `A_VT`, `%·µm` for `A_β`) — fixed
properties of a given fab process's device physics, not something a design
chooses. Everything else in a specific design (the device's `W`, `L`, and
how many of them are wired together) is what a designer sizes against those
constants to hit a stated matching spec.

For a differential pair specifically, both terms contribute to the pair's
input-referred offset voltage, combined roughly as:

```
σ²(Vos) ≈ σ²(ΔVt) + (VOV / 2)² · σ²(Δβ/β)
```

where `VOV` is the pair's design overdrive voltage (`Vgs - Vt`) — the
standard combined form taught alongside Pelgrom's law in analog IC design
texts (e.g. B. Razavi, *Design of Analog CMOS Integrated Circuits*, the
sizing-approach citation already used by several `kb/entries/*.json`
device-characterization entries such as `five-transistor-ota.json`). At the
low overdrives `klt size`'s gm/Id sizing (S5, `docs/cli/size.md`) typically
targets, the `ΔVt` term dominates — a smaller `VOV` shrinks the `β`-mismatch
contribution's weight, which is part of why gm/Id sizing and matching-driven
area both push toward similar design choices rather than fighting each
other.

### Random mismatch is not the same problem `topology="common_centroid"` solves

Pelgrom's law bounds **random** local mismatch — the irreducible,
area-limited variation between two devices even when they are laid out
identically. `klt gen`'s `topology="common_centroid"` (and `diff_pair`'s
always-on cross-quad interleaving) instead cancels **systematic**
mismatch — the linear-gradient component from process, temperature, or
stress variation across the die, which biases two devices differently
*because of where they physically sit*, not because of area. These are
complementary, not substitutes: a common-centroid layout with too little
unit-device area will still fail a matching spec (systematic error is
cancelled, random error is not shrunk by rearranging the same area), and a
huge non-interleaved unit device can still pick up systematic offset a
smaller interleaved one would have cancelled. Size for Pelgrom's law
first (§3 below), then lay out with `topology="common_centroid"` (or
`diff_pair`'s built-in interleaving) as the separate, essentially-always-
worth-it step for the systematic term.

## 2. Where `A_VT`/`A_β` actually come from

**Do not invent example numbers and present them as if they were verified
for a real process.** `A_VT`/`A_β` are measured, process-specific constants;
a plausible-looking made-up value is indistinguishable from a real one until
someone builds silicon against it. In order of preference, pull the real
constant from:

1. **`kb/entries/*.json`** — this repo's device-characterization knowledge
   base (`kb/SOURCING.md` governs what may land there). As of this writing,
   no `kb/entries/*.json` file carries a numeric `A_VT`/`A_β` field — several
   entries' `layout_idioms` mention matching *qualitatively*
   (e.g. `five-transistor-ota.json`: "common-centroid... to cancel linear
   process gradients and minimize input-referred offset"), but none ship the
   Pelgrom constant itself yet. If you have real characterization data for a
   process this repo targets, that is exactly what a new/updated `kb/entries`
   file should record (per `kb/SOURCING.md`'s sourcing rules) — check there
   again before assuming the gap is still open, and re-derive the "no entry
   has this yet" claim from a fresh `grep -i a_vt kb/entries/*.json` rather
   than trusting this sentence indefinitely.
2. **Device-characterization evidence** the block's own spec-review or
   devchar record already cites. `examples/spec-review/review.md`'s worked
   example (explicitly marked "**Synthetic example**... fictional") shows
   the shape this takes: `"Devchar evidence: amp-pair mismatch (A_VT ≈
   3.5 mV·µm, σ(Vos) ≈ 1.2 mV)"` — that number is illustrative for the demo,
   not a verified sky130 measurement; treat any number sourced this way with
   the same scrutiny you'd give a citation, checking whether it is
   fictional-demo, informally-quoted, or backed by an actual measurement.
3. **PDK documentation**, when a target PDK (sky130, gf180mcu) publishes its
   own device-mismatch model parameters (e.g. in its SPICE model cards or a
   published characterization report). Prefer this over an informally-quoted
   number whenever it's available for the process in question.

If none of the above has a real number for the process at hand, say so
explicitly in the design output rather than filling in a plausible-looking
placeholder — an unresolved matching constant is a legitimate "needs
characterization data" gap, not something to paper over.

## 3. Worked example: spec-review line → generator parameters

Suppose a spec-review line states an amplifier's input-referred offset must
be **σ(Vos) ≤ 2 mV, 3σ over process** (the same shape of line
`examples/spec-review/review.md`'s worked example reviews), and — per §2 —
device-characterization evidence for the process at hand reports
`A_VT ≈ 3.5 mV·µm` (using the same illustrative value
`examples/spec-review/review.md` uses, explicitly **not** sky130-verified;
substitute a real number from §2 for an actual design).

**Step 1 — normalize the spec to 1σ.** Pelgrom's law is stated in terms of
one standard deviation; a spec line stated at `N`σ needs dividing by `N`
first:

```
σ(Vos) = 2 mV / 3 ≈ 0.667 mV   (1σ)
```

**Step 2 — pick the governing term.** At the low overdrive a gm/Id-sized
(S5) differential pair typically runs, `ΔVt` mismatch dominates the combined
offset formula in §1 — take `σ(ΔVt) ≈ σ(Vos)` as a conservative
simplification (a full offset budget would also confirm the `β`-mismatch
term stays subordinate at the pair's actual `VOV`; this worked example
skips that check for brevity).

**Step 3 — invert Pelgrom's law for the minimum unit-device area.**

```
√(W·L) ≥ A_VT / σ(ΔVt) = 3.5 mV·µm / 0.667 mV ≈ 5.25 µm
W·L    ≥ 27.6 µm²
```

**Step 4 — split the area product using S5's sizing output.** `klt size`
(S5) will already have proposed a channel length `L` for gain/output-
resistance reasons independent of matching (see e.g.
`five-transistor-ota.json`'s `sizing_approach`: "channel lengths well above
minimum to raise the mirror's own output resistance"). Suppose S5 proposed
`L = 0.5 µm`; the matching bound then sets a **minimum** `W`:

```
W ≥ 27.6 µm² / 0.5 µm ≈ 55.2 µm
```

This is a *floor*, not a target — S5's own sizing (transconductance,
overdrive, headroom) may already demand a larger `W`, in which case the
matching spec is satisfied for free and does not change the sizing
decision. Only widen `W` beyond what S5 chose if the Pelgrom-derived floor
exceeds it.

**Step 5 — map onto `klt gen mos_array` parameters.** `mos_array`'s
`w_um`/`l_um` (`docs/cli/gen.md`) are a *unit device's* width/length; with
`finger_topology: "parallel"` (the default), `fingers > 1` folds into one
electrical device of width `fingers * w_um` — so the matching-relevant area
for one array cell is `fingers * w_um * l_um`, not `w_um * l_um` alone.
Choosing `fingers` trades layout aspect ratio for the same total area (e.g.
`w_um: 5.52, l_um: 0.5, fingers: 10` and `w_um: 55.2, l_um: 0.5, fingers: 1`
both deliver the same `55.2 µm²`/`0.5 µm` device, satisfying the same
Pelgrom bound) — `fingers` does not let you shrink below the bound, it only
reshapes how the same required area is drawn. Leave `topology` at its
default `"common_centroid"` (per §1's "not the same problem" note, this is
the separate systematic-mismatch step, not a substitute for the area
floor):

```json
{
  "generator": "mos_array",
  "params": {
    "w_um": 5.52,
    "l_um": 0.5,
    "fingers": 10,
    "rows": 2,
    "cols": 1,
    "topology": "common_centroid"
  }
}
```

**Mapping the same bound onto `klt gen diff_pair`.** `diff_pair`'s `w_um`/
`l_um` (`docs/cli/gen.md`) are also per unit sub-instance, and `splits`
(default `2`) sets how many interleaved sub-instances make up *each* side of
the pair in its always-on cross-quad checkerboard (`diff_pair` has no
`topology` parameter to choose — the checkerboard interleaving is
unconditional, unlike `mos_array`/`bjt_array`). `src/klayout_tools/gen.py`'s
`_diff_pair_layout` places each side's `splits` sub-instances as independent
drawn devices (`device_count = 2 * splits`) without electrically strapping
them together itself — combining them into one wider device is a downstream
wiring step (e.g. `klt gen-compose`, or tying same-named ports at the
netlist level), outside this generator call. Two honest ways to satisfy a
Pelgrom bound with `diff_pair`, depending on whether that downstream wiring
is already in place:

- **If each split will be wired together into one device per side**: divide
  the required total area across `splits` sub-instances the same way the
  `mos_array` `fingers` example above does (e.g. `splits: 2` at half the
  per-instance area each).
- **If you have not confirmed that downstream wiring** (the simpler,
  conservative default): size a *single* split's `w_um`/`l_um` to satisfy
  the Pelgrom bound **on its own**, and treat `splits` purely as the
  cross-quad interleaving multiplier for systematic-mismatch cancellation
  (§1), not as an area-pooling knob:

```json
{
  "generator": "diff_pair",
  "params": { "w_um": 55.2, "l_um": 0.5, "splits": 2 }
}
```

**Extending to `res_array`/`bjt_array`.** The same inverse-square-root-of-
area scaling generalizes to other matched-device classes — a unit
resistor's `length_um * width_um` for `res_array`, a unit bipolar device's
emitter area for `bjt_array` — but each device class has its **own**
process-specific matching constant (a resistor's, sometimes written `A_R`;
a BJT's `V_BE` mismatch, its own `A_VBE`), not `A_VT`/`A_β`. Get that
constant from the same §2 sources for the device class in question rather
than reusing a MOS transistor's number, then apply the same
spec → area → generator-parameter steps above via each generator's own
multiplier knob: `num` for `res_array` (its `rows` parameter only folds
that same `num` count into a more-square boustrophedon layout — it has no
`topology` choice, unlike `mos_array`/`bjt_array`), and `rows`/`cols` for
`bjt_array`, which — like `mos_array` — defaults to
`topology: "common_centroid"` and accepts `topology: "array"` when
systematic-mismatch cancellation genuinely isn't needed for that instance.

## Relationship to the #1013 layout-economy rubric

Issue #1013 (layout-economy review skill) needs a rubric criterion that
tells matching-driven area apart from wasted whitespace
(`analog-resource-survey.md` §3): area up to the Pelgrom-derived floor this
document computes is legitimate matching cost; area beyond it is a rubric
finding. **This document produces that numeric relationship as design-time
sizing guidance; #1013 consumes it as a review-time pass/fail threshold.**
Do not duplicate the derivation inside the #1013 rubric — point back here,
the same way this document points to `docs/cli/gen.md` rather than
restating that contract.

## See also

- [`analog-resource-survey.md`](analog-resource-survey.md) §2.2, §3 — the
  survey finding this document closes.
- [`docs/cli/gen.md`](../cli/gen.md) — the full `mos_array`/`res_array`/
  `bjt_array`/`diff_pair` parameter contract this document maps onto.
- [`docs/cli/size.md`](../cli/size.md) — `klt size`'s gm/Id sizing (S5),
  the usual source of the `L` (and `VOV`) this document's §3 Step 4 needs.
- `.claude/skills/design-sizing/SKILL.md` (S5) and
  `.claude/skills/design-layout-generation/SKILL.md` (S7) — the two pipeline
  stages this document bridges.
- `examples/spec-review/review.md` — the worked spec-review example whose
  synthetic `A_VT`/`σ(Vos)` values this document's §3 example reuses for
  continuity (still fictional, not sky130-verified — see §2).

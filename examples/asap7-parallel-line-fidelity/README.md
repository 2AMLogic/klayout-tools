# ASAP7 first-order extraction vs. `klt mom` on parallel lines

Phase 1 of [#5199](https://github.com/2AMLogic/klayout-tools/issues/5199)
(issue [#5201](https://github.com/2AMLogic/klayout-tools/issues/5201)): grades
`klt extract --deck asap7 --parasitics` against `klt mom` on N parallel lines,
mirroring [`../critical-net-mom-fidelity/`](../critical-net-mom-fidelity/).
Capacitance and resistance only -- no circuit quantities, no `klt pex`
warning, no docs table (phases 2-3).

```bash
scripts/fetch-pdks.sh        # optional: lets the script re-verify the cited sha256s
uv run python examples/asap7-parallel-line-fidelity/generate_and_measure.py
```

Needs the built `klt_mom_native` extension (`maturin develop --release` in
`native/mom/`); the full sweep takes about ten minutes. It regenerates
`points/*.gds` (one fixture per point), `asap7-stackup.json` (the explicit
`klt mom` stackup) and `results.json` (per-point numbers, tool versions,
input hashes, idealisations). The docstring in `generate_and_measure.py` is
the full methodology.

## Findings (M2/M4, 0.36 um long, victim = middle line)

- **`--critical-net` is unsupported on the asap7 deck**, reported as
  `"unsupported"` in every point's `b_critical_net` block with the refusal
  text. FinFET decks refuse the flag, and the asap7 parasitics deck curates no
  `metal_sidewalls`.
- **Same-level coupling is not modelled by `--parasitics`**: 0 fF against
  0.07 fF (min pitch) to 0.006 fF (4x pitch) from `klt mom` -- error -100 %.
- **Adjacent-level coupling is about 1000x low** (1e-5 fF vs 0.01-0.03 fF).
- **Resistance is about 5.7x low** (M2: 29.7 vs 168.4 ohm/um). The deck reads
  `typical.rules` as kohm/um x min width; the reference uses the effective
  resistivity of the pinned RC netlist (1.09e-7 ohm.m).
- **Ground capacitance** is within a few percent at min pitch and 2-3x low at
  4x pitch, but this is not independent of the reference's own ground-plate
  placement (below).

Hand check, one M2 gap at min pitch: parallel-plate
`eps0 * 3.9 * t * L / g = 0.0345 fF/um * 2 * 0.36 um = 0.025 fF` per side;
`klt mom` gives 0.034 fF (fringing adds ~38 %).

## What is cited and what is assumed

Cited (lambdapdk v0.2.17, sha256 in `results.json`): widths and pitches
(`asap7_tech.lef`), M1 thickness 36 nm and the effective resistivity
(`asap7sc7p5t_28_R.sp`, median of R*w*t/l over its M1 resistors).

**Assumed, not in the pinned archive**: M2+ thickness = 2x min width,
dielectric height = lower-level thickness, uniform eps_r 3.9, and a finite
ground plate one M1 thickness below M1. These drive the reference's
capacitance, so treat the capacitance errors as indicative, not signoff.
Other idealisations: rectangular full-thickness conductors, no vias (the
orthogonal lines are separate nets), quasi-static C, DC R of one isolated bar.

`stackup_from_pdk` has no ASAP7 route, so the stack is an explicit
`stackup` generated from the table in the script (one way only).

# ASAP7 parallel-line fidelity: circuit consequence (phase 2)

**Status:** measurement record, not a proposal. Phase 2 of epic
[#5199](https://github.com/2AMLogic/klayout-tools/issues/5199), issue
[#5202](https://github.com/2AMLogic/klayout-tools/issues/5202). Phase 1
([#5201](https://github.com/2AMLogic/klayout-tools/issues/5201)) graded
`klt extract --deck asap7 --parasitics` against `klt mom` as capacitance and
resistance; this phase converts each of its 12 points into a circuit quantity.
Modelled on [`critical-net-mom-fidelity-phase2c.md`](critical-net-mom-fidelity-phase2c.md).

Reproduce: `examples/asap7-parallel-line-fidelity/circuit_stage.py`
(reads phase 1's `results.json`, writes `circuit-results.json` and one record
under
[`evidence/sim/asap7-parallel-line-fidelity/circuit-fidelity/`](../../evidence/sim/asap7-parallel-line-fidelity/circuit-fidelity/),
append-only with `supersedes` and `HEAD`, like the sky130 record).

## Method

Per point, three networks, each driven two ways -- 48 ngspice decks in one
`klt sim` deck-list request, run as a single job on the Spot batch fleet
(`--wait-for-capacity 600`); no hand-launched loops.

| Network | Source |
| --- | --- |
| (a) deck | the netlist `klt extract --deck asap7 --parasitics` writes, verbatim |
| (b) critical-net | **unsupported** -- every FinFET deck refuses `--critical-net` (phase 1); reported as `"unsupported"`, never zero-filled |
| (c) reference | R from `klt mom`'s PEEC resistance; C from `klt mom`'s matrix (below) |

Stimulus: each line has a 10 kohm driver (tau of 1-2 ps, so the capacitance
error dominates the line's tens of ohms of R). The victim (middle line) rises
0 -> 1 V over 1 ps. **Quiet**: the two nearest same-level neighbours sit at 0 V
behind their own drivers. **Opposite**: they fall 1 -> 0 V at the same instant.
Every other line is a stiff 0 V. *Delay* = victim source 50% to victim hub 50%.
*Energy* = integral of v*i at the victim source over 0-40 ps.

Reference network: hub-to-ground C is `klt mom` ground + adjacent-level +
non-nearest same-level coupling; nearest-neighbour couplings are explicit
hub-to-hub capacitors; each neighbour copies the victim's R and ground C.

Hand check (one point): M2, min pitch, sparse. Reference hub C to ground
0.0186 fF plus 2 x 0.0343 fF coupling is 0.087 fF, so R*C = 10 kohm x 0.087 fF
= 0.87 ps and a single pole gives 0.69 RC = 0.6 ps. The simulated quiet delay
is 0.514 ps (the 1 ps edge and the neighbours' own drivers trim it). The
Miller case approaches the 2x coupling charge: delay 1.235 ps = 2.4x quiet.
`tests/test_asap7_parallel_line_fidelity.py` brackets this.

## Results (M2/M4, 0.36 um long; ps and fJ; error = 100(deck-ref)/ref)

| Level | Pitch | Lines | Ref delay quiet | Ref delay opp. | Deck delay quiet | Deck delay opp. | Delay err quiet / opp. (%) | Ref E quiet | Ref E opp. | Deck E quiet | Deck E opp. | Energy err quiet / opp. (%) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| M2 | 1x | dense | 0.526 | 1.230 | 0.040 | 0.051 | -92.3 / -95.9 | 0.0759 | 0.1398 | 0.0022 | 0.0028 | -97.1 / -98.0 |
| M2 | 1x | sparse | 0.514 | 1.235 | 0.038 | 0.054 | -92.6 / -95.7 | 0.0755 | 0.1408 | 0.0020 | 0.0029 | -97.3 / -97.9 |
| M2 | 2x | dense | 0.402 | 0.595 | 0.042 | 0.050 | -89.6 / -91.7 | 0.0391 | 0.0594 | 0.0023 | 0.0027 | -94.2 / -95.4 |
| M2 | 2x | sparse | 0.396 | 0.597 | 0.039 | 0.052 | -90.0 / -91.3 | 0.0386 | 0.0593 | 0.0021 | 0.0028 | -94.5 / -95.2 |
| M2 | 4x | dense | 0.368 | 0.415 | 0.043 | 0.048 | -88.3 / -88.4 | 0.0321 | 0.0377 | 0.0023 | 0.0026 | -92.7 / -93.0 |
| M2 | 4x | sparse | 0.361 | 0.410 | 0.041 | 0.050 | -88.6 / -87.8 | 0.0313 | 0.0370 | 0.0022 | 0.0027 | -92.9 / -92.6 |
| M4 | 1x | dense | 0.540 | 1.240 | 0.022 | 0.027 | -95.9 / -97.8 | 0.0776 | 0.1412 | 0.0012 | 0.0014 | -98.5 / -99.0 |
| M4 | 1x | sparse | 0.526 | 1.243 | 0.021 | 0.028 | -95.9 / -97.8 | 0.0769 | 0.1420 | 0.0011 | 0.0015 | -98.6 / -99.0 |
| M4 | 2x | dense | 0.420 | 0.599 | 0.023 | 0.026 | -94.5 / -95.7 | 0.0415 | 0.0609 | 0.0012 | 0.0014 | -97.1 / -97.8 |
| M4 | 2x | sparse | 0.412 | 0.599 | 0.022 | 0.027 | -94.6 / -95.5 | 0.0405 | 0.0605 | 0.0012 | 0.0014 | -97.1 / -97.7 |
| M4 | 4x | dense | 0.389 | 0.428 | 0.024 | 0.025 | -93.9 / -94.1 | 0.0349 | 0.0398 | 0.0012 | 0.0013 | -96.4 / -96.7 |
| M4 | 4x | sparse | 0.379 | 0.421 | 0.023 | 0.026 | -93.9 / -93.8 | 0.0336 | 0.0387 | 0.0012 | 0.0013 | -96.4 / -96.5 |

The critical-net column is `"unsupported"` for all 12 points.

**Covered range:** ASAP7 M2 and M4, 0.36 um long, pitch 1x-4x the LEF pitch,
3 or 5 parallel lines, 10 kohm driver, 1 ps edge. Nothing is claimed outside it.

## What the numbers say

- **On these lines the deck's delay and energy are 88-95% and 93-99% low.** It
  carries no same-level coupling, and its ground C is a small fraction of the
  field-solver total, so it is not merely imprecise on delay: it removes the
  load.
- **The coupling-dominated case is the worst.** At min pitch the reference's
  opposite-switching delay is 2.3-2.4x its quiet delay (the Miller penalty);
  the deck shows 1.1-1.4x. Error shrinks as pitch grows (to -88%
  at 4x pitch on M2) but never approaches the +-10% range.
- **Treat the deck's `--parasitics` delay and energy as a lower bound.** On
  same-level-coupled wiring of this kind they are low by roughly an order of
  magnitude; no tested point is within 10%.
- The deck's quiet and opposite cases differ by 7-41% in delay even though it has no
  same-level coupling: every hub's capacitor returns to a floating `vsubs`
  node (1e12 ohm to ground), which couples the lines weakly. That is the
  netlist as written, and it is not what a designer would expect from it.

## Scope and idealisations

Phase 1's idealisations apply (assumed M2+ thickness, ILD height, eps_r 3.9, a
finite ground plate). Added here, from `CIRCUIT_IDEALISATIONS` in the script:
ideal-step 10 kohm drivers; only the two nearest neighbours switch; symmetric
neighbours in the reference (they copy the victim's R and ground C and their
mutual coupling is dropped); lumped RC with no inductance and no device
nonlinearity; energy windowed to 0-40 ps. The reference is a field-solver
network on **assumed** stack geometry, so the percentages grade the deck
against that reference, not against silicon.

## Follow-ups (not implemented here)

- Phase 3 ([#5203](https://github.com/2AMLogic/klayout-tools/issues/5203)):
  the `klt pex` warning, consuming this table.
- Sweeping driver strength and line length (this grid fixes both).

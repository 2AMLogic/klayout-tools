# `klt mom` full-wave S-parameters: cross-validation against an external FEM solver

Phase 2c of the Method-of-Moments epic
([#701](https://github.com/2AMLogic/klayout-tools/issues/701)), delivered by
[#895](https://github.com/2AMLogic/klayout-tools/issues/895). Where
[`mom-validation.md`](mom-validation.md) checks `klt mom`'s numbers against
*analytic* closed forms (the only oracle available for the canonical
fixtures it uses), this document is the epic's own "external solver as
oracle" cross-check — the discipline Epic #701's Phase 0 reality-grounding
section requires ("Cross-check against an external MoM/FEM ... on a shared
benchmark") and the follow-up
[`mom-validation.md`](mom-validation.md#what-is-not-validated-here) flagged
as still open after Phase 2a/2b landed.

The executable form of this document is
`tests/test_mom_external_fem_crossval.py`; the external solver itself is
`scripts/research/mom_external_fem_crossval.py`. Re-run
`uv run --extra mom-crossval --group mom pytest
tests/test_mom_external_fem_crossval.py -v --capture=tee-sys` to reprint the
numbers below.

## Why an external FEM solver, not openEMS

[`em-field-sim-spike.md`](em-field-sim-spike.md)'s survey names openEMS as
the reference open-source full-wave (FDTD) solver, and the sibling
[geode-fem](https://github.com/rjwalters/geode-fem) repo as an in-house
FEM/DG alternative — but neither is a practical fit for *this* issue's own
acceptance criteria (a benchmark reproducible headlessly in CI, no GUI, no
proprietary/NDA'd data, committed so the check runs on every PR):

- **openEMS** is GPL-3.0 (confirmed in `em-field-sim-spike.md`'s survey) —
  fine to invoke as a subprocess, but it ships no PyPI wheel and needs a
  from-source build (CMake, HDF5, VTK, the companion CSXCAD geometry
  library) with no lightweight CI-installable path; adding that build to
  this repo's CI is a substantially larger undertaking than this phase's
  scope (a single shared-benchmark cross-check on the full-wave/ports
  fixture already in `mom-validation.md`).
- **geode-fem** is a separate repository with no in-repo integration path
  (no published package, no committed vendoring) — wiring it in is exactly
  the kind of new-engine-integration work Epic #701's later phases (and
  Epic #840/#708's own full-field direction) would need to scope
  separately, not something this single cross-check issue should invent.

Instead, this cross-check uses
[scikit-fem](https://github.com/kinnala/scikit-fem) — a real, independently
developed, actively maintained finite-element library (BSD-licensed, so it
composes with this repo's MIT license and can run **in-process**, unlike
openEMS's GPL-3.0), pip-installable with no native toolchain, and fast
enough (~1-4 seconds) to run on every PR. It is a genuine FEM
implementation this repo does not author — `scripts/research/
mom_external_fem_crossval.py` uses it to solve the transverse (cross-
section) electrostatic and magnetostatic problems via bilinear-quad finite
elements; see that module's own docstring for the full method. openEMS/
geode-fem remain candidates for a heavier, later full-3-D cross-check if a
future phase needs it.

## The benchmark

The same canonical two-wire transmission line
[`mom-validation.md`](mom-validation.md#full-wave-frequency-sweep) and its
ports section already validate against closed forms — 500 µm long, 2×2 µm
square bars ("go"/"return"), 40 µm center-to-center separation, vacuum
background, 1 GHz:

```json
{
  "background_permittivity": 1.0,
  "panel_size_um": 2.0,
  "frequencies_hz": [1.0e9],
  "segment_size_um": 5.0,
  "ports": [
    { "position_um": 0.0, "reference_impedance_ohm": 50.0 },
    { "position_um": 500.0, "reference_impedance_ohm": 50.0 }
  ],
  "stackup": [
    { "layer": "1/0", "conductor": "go", "z0_um": 0.0, "z1_um": 2.0 },
    { "layer": "2/0", "conductor": "return", "z0_um": 0.0, "z1_um": 2.0 }
  ]
}
```

Reference impedance is fixed at the standard RF convention (50 ohm) —
deliberately **not** tied to either solver's own derived `Z0` (both
solvers' `Z0` is closer to ~430-450 ohm for this fixture), so a `Z0`
disagreement between the two solvers shows up directly as an `S11`
disagreement, not just an `S21` phase difference.

A second configuration exercises de-embedding: 100 µm + 500 µm (DUT) + 150
µm feed stubs, ports placed at the DUT boundaries — the same fixture
`tests/test_mom_ports_validation.py`'s
`test_de_embedding_matches_the_dut_alone_closed_form` uses.

## Methodology

**Two solvers, one benchmark geometry, same ABCD/de-embedding formalism.**
`native/mom/src/fullwave.rs`'s `line_abcd`/`abcd_to_s`/`de_embedded_s_parameters`
are ported line-for-line into
`scripts/research/mom_external_fem_crossval.py` — so what is actually being
cross-checked is each solver's own path to a per-unit-length `Z0(omega)`/
`gamma(omega)` pair, not the (identical, shared) chain-parameter cascade
math downstream of it:

- **`klt mom`**: retarded-kernel, point-collocation thin-wire MoM for the
  series impedance `Z'` (`fullwave.rs`), plus the existing panelized
  electrostatic BEM for the shunt capacitance `C'` (`solver.rs`) —
  `Z0 = sqrt(Z'/j*omega*C')`.
- **External FEM**: a 2-D finite-element solve of the conductor
  cross-section, both `L'` (magnetostatic, equal/opposite loop current) and
  `C'` (electrostatic, differential-mode drive) independently — see
  `mom_external_fem_crossval.py`'s docstring for the full derivation —
  `Z0 = sqrt(L'/C')`, `gamma = j*omega*sqrt(L'*C')` (the standard lossless
  TEM-line relations, not assumed via the exact `L'*C' = mu0*eps0` vacuum
  identity, so both `L'` and `C'` are genuinely independently computed).

**Metric**: the reported complex `S11`/`S21` (and, by the reciprocal-network
identity `native/mom/src/fullwave.rs` already asserts, `S12 == S21`,
`S22 == S11`), compared as `|S21|` relative error, `S21` phase absolute
difference, and `|S11|` absolute difference (an absolute, not relative,
tolerance for `S11` since a near-matched line has `S11` near zero, where a
relative-error metric is numerically unstable).

**Tolerance**: chosen with generous headroom (5-8x) over the measured
agreement below — loose enough to tolerate the FEM solver's own
open-boundary domain-truncation error and `klt mom`'s own MVP
approximations (both already documented, respectively, in this document and
[`mom-validation.md`](mom-validation.md)), tight enough that a genuine
regression in either solver's full-wave/port math would fail the gate.

## Measured agreement

Measured on `native/mom` + `scripts/research/mom_external_fem_crossval.py`
at the commit that introduced this cross-check (external FEM solver:
`step_um=0.25`, `extent_um=400.0`, `growth=1.15`, 46,961 mesh nodes, ~1.2 s
wall time for the RLGC solve):

### External FEM solver's own credibility (vs the same closed form `klt mom` is checked against)

| quantity | FEM (this module) | closed form (equal-area-circle oracle) | rel. err |
| -------- | ------------------ | --------------------------------------- | -------- |
| `C'`     | 7.921652 pF/m       | 7.797563 pF/m                            | +1.591%  |
| `L'`     | 1516.306830 nH/m    | 1426.920196 nH/m                         | +6.264%  |
| `Z0`     | 437.5077 ohm        | 427.7799 ohm                             | +2.274%  |

The FEM solver meshes the actual square cross-section directly (no
equal-area-circle shape substitution), so its residual against this
shape-substituted oracle is a fair measure of the oracle's own
substitution error plus the FEM solver's open-boundary truncation, not a
FEM implementation bug — consistent with `L'` showing a larger residual
than `C'`, matching the precedent `native/mom/src/peec.rs`'s own validated
5% loop-inductance tolerance already sets for exactly this same shape
substitution (`docs/design/mom-validation.md`'s "Inductance/resistance"
section). `L' * C' = 1.201165e-17` vs the exact vacuum TEM identity
`mu0*eps0 = 1.112650e-17` (+7.955% self-consistency residual) — expected
given `L'`/`C'` are solved independently (not one inferred from the
other via the identity) on a domain with finite (not infinite) truncation.

### `klt mom` vs the external FEM solver (the acceptance-criterion comparison)

**Matched line** (ports at both physical ends, 50 ohm reference):

| quantity                | `klt mom`                | external FEM              | agreement            |
| ------------------------ | ------------------------- | -------------------------- | --------------------- |
| `S11`                    | `0.002617 + 0.050465j`    | `0.002264 + 0.046907j`     | `\|S11\|` diff 0.003572 |
| `S21`                    | `0.997382 - 0.051717j`    | `0.997735 - 0.048151j`     | rel.err 0.3588%       |
| `S21` phase              | -0.051806 rad              | -0.048223 rad               | diff 0.003584 rad     |
| `Z0`                     | 452.3669 ohm               | 437.5077 ohm                | (context, not gated)  |

**De-embedded line** (100 µm + 500 µm DUT + 150 µm feed stubs, ports at the
DUT boundaries):

| quantity | `klt mom` | external FEM | agreement |
| -------- | ---------- | -------------- | ---------- |
| `S21`    | `0.997352 - 0.052011j` | `0.997735 - 0.048151j` | rel.err 0.3884% |
| `S11`    | `0.002648 + 0.050770j` | `0.002263 + 0.046901j` | `\|S11\|` diff 0.003878 |

**Stated tolerances** (measured margin in parentheses): `S21` relative
error < 5% (measured 0.36-0.39%, ~13x margin); `S21` phase absolute
difference < 0.02 rad (measured ~0.0036 rad, ~5.6x margin); `\|S11\|`
absolute difference < 0.03 (measured ~0.0036, ~8x margin).

The two solvers' independent `Z0` estimates (452.4 ohm vs 437.5 ohm, a
~3.3% relative spread) are both consistent with, and roughly bracket, the
closed-form oracle (427.8 ohm) — `klt mom`'s own 5.75% closed-form residual
is already measured and tolerated in
[`mom-validation.md`](mom-validation.md#1-characteristic-impedance-vs-the-two-wire-line-closed-form).
The de-embedded S-parameters land within noise of the matched-line case (as
expected — de-embedding is exact closed-form ABCD algebra, identical on
both solvers, over a uniform-medium fixture where the DUT length is the
same 500 µm in both configurations), confirming the de-embedding path
itself introduces no additional cross-solver disagreement beyond the
underlying `Z0`/`gamma` estimates.

## What this does not cover

- **A genuinely general (non-bar-shaped) layout.** Both solvers restrict to
  the same canonical two-wire-line cross-section this document's benchmark
  uses; a real extracted, irregular-shaped multi-conductor layout is not
  exercised here (or anywhere else in `klt mom`'s current MVP scope — see
  `docs/cli/mom.md`'s "Scope and limitations").
- **A true 3-D full-wave solve on the external side.** The external FEM
  solver reduces to the same uniform-TEM-line abstraction `klt mom` itself
  uses (a 2-D cross-section RLGC extraction, not a 3-D time- or
  frequency-domain field solve) — see
  `mom_external_fem_crossval.py`'s docstring's "Why a 2-D cross-section
  solve, not a full 3-D field solve". A heavier openEMS/geode-fem
  integration remains a candidate follow-up if a future phase needs a
  fully independent 3-D radiative cross-check.
- **Skin effect / frequency-dependent loss.** Neither solver models it —
  both assume uniform current density, matching `klt mom`'s own thin-wire
  MVP.
- **Non-vacuum backgrounds.** `s_parameters_two_wire_line` explicitly
  raises `NotImplementedError` for `background_permittivity != 1.0` rather
  than silently reporting a wrong number.

## See also

- [`docs/cli/mom.md`](../cli/mom.md) — the command, its spec-file schema,
  and its JSON contract.
- [`mom-validation.md`](mom-validation.md) — the analytic-oracle validation
  this document's benchmark is drawn from.
- [`em-field-sim-spike.md`](em-field-sim-spike.md) — the original E&M
  field-sim survey (issue #103) that ranked openEMS/geode-fem as candidate
  external solvers.
- `scripts/research/mom_external_fem_crossval.py` — the external FEM
  solver (importable, and runnable standalone as a CLI).
- `tests/test_mom_external_fem_crossval.py` — the executable form of this
  document.
- [#895](https://github.com/2AMLogic/klayout-tools/issues/895) — this
  cross-check (Phase 2c); [#893](https://github.com/2AMLogic/klayout-tools/issues/893)
  (Phase 2a, full-wave sweep) and
  [#894](https://github.com/2AMLogic/klayout-tools/issues/894) (Phase 2b,
  ports/de-embedding) are its prerequisites.

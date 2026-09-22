# `klt mom` cross-validation against an external MoM solver

Phase 2c of the Method-of-Moments epic
([#701](https://github.com/2AMLogic/klayout-tools/issues/701)), delivered by
[#895](https://github.com/2AMLogic/klayout-tools/issues/895). Where
[`docs/design/mom-validation.md`](mom-validation.md) checks `klt mom`
against **analytic closed forms** (its own re-derivation of the textbook
answer), this document is a different, complementary check the epic's own
Phase 0 reality-grounding section explicitly calls for: "Cross-check against
an external MoM/FEM … on a shared benchmark." A closed-form check can be
fooled by a bug that happens to preserve the closed form's own symmetry (or
by a mistake shared between the code and the oracle formula, if the same
person derived both); an independently-implemented **external** solver has
no such correlated failure mode — it is a genuinely different codebase,
different discretisation, and different implementer.

The executable form of this document is `tests/test_mom_cross_validation.py`
(the gate) plus `scripts/mom_nec_reference.py` (the external solver's own
driver script). Values below were measured at the commit that introduced
this harness; re-run `pytest tests/test_mom_cross_validation.py -v
--capture=tee-sys` (after `uv sync --extra mom-cross-validation`) to reprint
them.

There are now **two** external oracles, one per solver path, each with its own
extra and its own pair of CI steps: **NEC2++** for the full-wave
S-parameter sweep (#895 — the benchmark, methodology, and measured results
that make up the bulk of this document), and **PyPEEC** for the PEEC
inductance path's spiral fixture ([#1886](https://github.com/2AMLogic/klayout-tools/issues/1886),
closing out [#1842](https://github.com/2AMLogic/klayout-tools/issues/1842)'s
acceptance criterion 3 — see "The spiral fixture's oracle" below for the
oracle choice and the FastHenry ruling, and
[`mom-validation.md`](mom-validation.md) for that fixture's measured
numbers).

## Picking the external oracle

Epic #701's own Phase 0 section, and `docs/design/em-field-sim-spike.md`'s
engine survey, name two candidate external oracles: **openEMS** (FDTD) and
**geode-fem** (FEM/DG, the sibling in-house solver `docs/cli/mom.md`'s "Why
Rust" section already commits to eventually cross-checking against once its
own full-wave block-S-parameter integration lands). Neither is a practical
fit for *this* issue's "reproducible in CI" acceptance criterion today:

- **geode-fem** is not yet wired into this repo at all — there is no `klt`
  verb or Python/Rust binding that invokes it (see
  `docs/design/em-field-sim-spike.md` section 4, "proposed", not shipped).
  It remains the intended long-term cross-check partner once that
  integration exists; this issue does not block on it.
- **openEMS** is GPL-3.0, built from C++ source (no prebuilt Python wheel),
  and its own survey entry above flags an unmeasured, plausibly-large
  headless-CI runtime cost (FDTD's grid-cell count scales unfavourably
  against IC-routing-scale sub-micron geometry). Standing up an openEMS
  build step in CI is a real, separate undertaking this issue's scope does
  not need to absorb to satisfy its own acceptance criteria.

Per this issue's own acceptance criteria text, when "a full external solver
isn't practical to install/run in CI," the fallback is "the lightest-weight
real external tool" that still satisfies the "external oracle" spirit. That
tool is **[NEC2++](https://github.com/tmolteno/nec2pp)** (`PyNEC` on PyPI):
a mature, independently-developed **thin-wire Method-of-Moments**
antenna/transmission-line solver — a genuine external MoM tool, not merely a
re-evaluation of a textbook formula — ported to C++ from the classical
Numerical Electromagnetics Code (NEC2, originally Lawrence Livermore
National Laboratory). It ships a **prebuilt manylinux wheel** (verified: `pip
download PyNEC` resolves a `cp312-manylinux` wheel with no source build
required), its only dependency is `numpy`, and it models exactly the
geometry class `klt mom`'s full-wave solve already restricts itself to (see
`native/mom/src/fullwave.rs`'s module docs): thin, bar/wire-shaped
conductors. This is the "lightest-weight real external tool" this issue's
acceptance criteria ask for — a genuine independent MoM implementation,
installable and runnable headlessly in CI with a single `pip install`, no
compiled-from-source engine, no GPU, no mesh generator.

### License: subprocess-only, never embedded

`PyNEC`'s own PyPI metadata declares `License-Expression: GPL-3.0-only` —
the same category `docs/design/em-field-sim-spike.md`'s openEMS survey entry
flags: "fine to invoke as a subprocess, forecloses in-process/library
embedding inside this repo's MIT surface." This repo's own code (`klt mom`,
`klayout_tools`, everything under `src/`) never imports `PyNEC`. Only
`scripts/mom_nec_reference.py` does — a standalone script, not part of the
installed package, always invoked as its **own subprocess**
(`subprocess.run([sys.executable, "scripts/mom_nec_reference.py"], ...)`) by
`tests/test_mom_cross_validation.py`, exchanging a JSON request/response over
stdin/stdout. This is the same "mere aggregation of independent processes"
pattern this repo already uses for every other GPL-adjacent external engine
it touches (`ngspice`, Yosys, Icarus Verilog, Verilator — all invoked as
`PATH` binaries via `subprocess`, never `import`ed). `PyNEC` is declared in
its own `pyproject.toml` extra (`mom-cross-validation`), never pulled in by
`dev`/`mom`/any default install path — a plain `pip install klayout-tools`
or `uv sync --extra dev` never touches it.

### The spiral fixture's oracle: PyPEEC (issue #1886)

The section above picks the oracle for the **full-wave** cross-check (#895).
A second, independent oracle covers the **PEEC inductance** path's spiral
fixture — [#1842](https://github.com/2AMLogic/klayout-tools/issues/1842)'s
generalized filament-pair formula, whose acceptance criterion 3 named
FastHenry and which [#1886](https://github.com/2AMLogic/klayout-tools/issues/1886)
closed out. It is a different tool for a genuinely different reason: NEC2++
is a thin-wire *full-wave* MoM solver and has no partial-inductance matrix to
compare against, while the quantity here is a DC/quasi-static partial
inductance.

**FastHenry is disqualified, permanently.** The operator ruling on #1886
(2026-09-18) resolved the licensing question this document's own
[`em-field-sim-spike.md`](em-field-sim-spike.md) source recorded as
"unresolved, and disqualifying until resolved", on two independent grounds:

1. **Unpackaged.** No `fasthenry` package exists in Debian/Ubuntu (`apt-get
   install fasthenry` → "No such package"), Homebrew, or PyPI, so the
   "install a distro package and invoke it as a subprocess oracle" framing
   #1886 was originally filed around never applied — CI would have had to
   build the sources itself.
2. **Not open source.** Those sources carry MIT RLE's 1990s research notice
   rather than an OSI license, in FastHenry's own core
   (`src/fasthenry/induct.h`, `mulGlobal.h`) and in the FastCap-derived
   `zbuf/` code in every mirror (`ediloren/FastHenry2`, `ediloren/FastCap2`,
   and the `fasthenry-3.0wr` tarball inside `wrcad/xictools`, whose
   Apache-2.0 wrapper explicitly does not override inherited terms):

   > Permission to use, copy and modify for internal, noncommercial purposes
   > is hereby granted. Any distribution of this program or any part thereof
   > is strictly prohibited without prior written consent of M.I.T. […]
   > LICENSEE agrees not to make any copies except for LICENSEE'S internal
   > noncommercial use.

   Unlike `PyNEC`'s GPL-3.0-only (a *known* category with a documented
   subprocess-only policy — see "License: subprocess-only, never embedded"
   above), this is not a copyleft question with a boundary to respect: the
   distribution clause forecloses redistribution of the code or any part of
   it, which rules out vendoring *and* any future clean port. **Never a
   dependency, never an oracle, never ported.**

**[PyPEEC](https://github.com/otvam/pypeec)** is the oracle instead: a 3-D
quasi-magnetostatic **PEEC** solver from Dartmouth College (MPL-2.0, JOSS
[10.21105/joss.06644](https://doi.org/10.21105/joss.06644), `pip install
pypeec` / conda-forge) that extracts terminal R/L from a voxelised conductor
geometry via an FFT-accelerated dense operator. It satisfies the same
"lightest-weight *real* external tool" bar `PyNEC` does for the full-wave
check:

- **Same method class, independent implementation.** PEEC — the same family
  as `native/mom/src/peec.rs` and as FastHenry — but a uniform voxel mesh
  with face currents and an FFT-circulant Green's operator, against this
  repo's per-bar filament bundles with Rosa's closed-form self terms and an
  analytic skew-filament mutual. Different discretisation, different
  codebase, different implementer: no correlated failure mode.
- **Installable and runnable headlessly.** A pure-Python wheel, no compiled
  engine to build, no GPU, no mesh generator; the CI solve takes ~2 seconds.
  Its voxel-grid restriction is harmless for a rectangular-section spiral
  whose every edge lands on a 1 µm grid line.
- **License: subprocess-only, same discipline.** MPL-2.0 is file-scoped weak
  copyleft, materially lighter than GPL-3.0-only, so this is a weaker
  constraint than `PyNEC`'s — but the handling is identical anyway. `klt mom`,
  `klayout_tools`, everything under `src/` never imports `pypeec`; only
  `scripts/mom_pypeec_reference.py` does, always invoked as its own
  subprocess exchanging JSON over stdin/stdout. It is declared in its own
  `mom-pypeec-cross-validation` extra, never pulled in by
  `dev`/`mom`/`mom-cross-validation` or any default install path (its
  declared dependencies drag in a plotting/GUI stack — `vtk`, `pyvista`,
  `pyside6`, with no extras to opt out of — so the install is ~1.6 GB: fine
  for one opt-in CI step, unacceptable anywhere else).

The fixture, the method (terminal impedance of the open spiral at a quasi-DC
frequency), the tolerances, and the measured numbers are documented where the
rest of the PEEC validation lives:
[`mom-validation.md`](mom-validation.md)'s "Generalized filament-pair formula
(issue #1842): a spiral fixture" section. Its executable form is
`tests/test_mom_pypeec_cross_validation.py` plus
`scripts/mom_pypeec_reference.py`, and it is wired into the same
`.github/workflows/ci.yml` mom leg as the NEC2++ check below, with the same
"assert the test actually ran (no silent skip)" companion step.

## The shared benchmark

The canonical two-wire (twin-lead) transmission line
`tests/test_mom_fullwave_validation.py` and
`tests/test_mom_ports_validation.py` already use for their own closed-form
checks: two long, parallel, 2×2 µm bars, 40 µm apart, 500 µm long, in
vacuum (`background_permittivity = 1.0`), swept at 1 GHz, with ports placed
at both physical ends (`position_um = 0.0` / `500.0`) referenced to the
standard 50 ohm — the classical **matched-line** case (no de-embedding
stubs). Reusing the existing fixture rather than inventing a new one is
deliberate: it is the geometry both this repo's own analytic validation
*and* this external cross-check treat as canonical, so a reader comparing
the two documents is comparing apples to apples.

**Scope note**: this benchmark exercises the full-wave solve (#893) and the
ports/S-parameter de-embedding math (#894) *at the identity reduction*
(ports at the physical ends, zero-length feed stubs — see
`native/mom/src/fullwave.rs`'s "no special-cased 'no feed' branch" note).
The de-embedding-with-nonzero-feed-stubs case is already covered by
`tests/test_mom_ports_validation.py`'s closed-form check
(`test_de_embedding_matches_the_dut_alone_closed_form`); reproducing that
scenario in NEC would need a second, more involved NEC model (three
electrically-distinct reference planes rather than two physical wire ends)
that this issue's scope does not require to satisfy its acceptance
criteria — a documented limitation, not an oversight.

## Methodology

### How NEC2++ reports a transmission line's `Z0`/`gamma`

NEC(2++) is a driven-antenna/scattering solver: for a given excitation and
geometry it reports feed-point impedance, not a two-port network's
S-parameters directly. `scripts/mom_nec_reference.py` derives the line's own
characteristic impedance `Z0` and propagation constant `gamma` via the
classical **open-circuit/short-circuit two-measurement technique** (the
NEC-modeling equivalent of open/short-terminated VNA calibration
measurements, standard in transmission-line theory — e.g. Pozar, *Microwave
Engineering*, section 2.2): the same geometry is solved twice, once with the
far end of the two conductors left unconnected (`Z_oc`) and once with a
short connecting wire added between them at the far end (`Z_sc`):

```text
Z0            = sqrt(Z_oc * Z_sc)
tanh(gamma*l) = sqrt(Z_sc / Z_oc)
```

`Z0`/`gamma` are then converted to S-parameters via the same ABCD
(chain-parameter) cascade `native/mom/src/fullwave.rs`'s own
`line_abcd`/`abcd_to_s` use (Pozar, Table 4.2) —
**reimplemented independently** in `scripts/mom_nec_reference.py` (not
imported from the Rust crate or its Python wrapper), so the two solvers'
code paths are disjoint end to end, not just their field solves.

### Geometry mapping

Both solvers model the same **effective thin-wire radius**:
`a_eff = sqrt(area / pi)` — the equal-area-circle substitution
`native/mom/src/fullwave.rs`'s own module docs already use for its
rectangular-bar cross-section, so both solvers see the same nominal
conductor, not merely the same box footprint. Axial discretisation is
matched as closely as each solver's own knob allows:
`segment_size_um = 5.0` (100 segments per conductor) on the `klt mom` side,
`segments_per_wire = 101` on the NEC side — chosen because NEC2++'s
thin-wire kernel becomes numerically ill-conditioned once segment length
approaches ~2x the wire radius (measured: `segments_per_wire >= 201` on this
benchmark drives NEC's own reported impedance to a clearly-nonphysical
result, `Z_oc`/`Z_sc` collapsing toward zero); 101 segments (≈4.95 µm/segment
against a ≈1.13 µm effective radius, a ≈4.4x margin) is the finest resolution
measured stable on this benchmark.

### Tolerance and metric

Two independently-discretised numerical solvers, each with its own MVP-level
approximation (`klt mom`'s thin-wire point-collocation Riemann sum vs.
NEC2++'s own segment-basis MoM), are not expected to agree to the precision
either agrees with the underlying analytic closed form individually — each
carries its own discretisation error budget, and those budgets can partially
cancel or partially add depending on sign. Three metrics, each with its own
tolerance definition (chosen to have ample margin over the measured values
below while still being tight enough to catch a genuinely broken solve):

- **`Z0` relative error, 20%.** `docs/design/mom-validation.md`'s and
  `tests/test_mom_fullwave_validation.py`'s own stated tolerance against the
  *analytic* closed form is 10% for either solver individually; 20% on the
  two solvers' pairwise difference is that same budget doubled (the
  worst-case sum of two independent 10% errors), not a separately
  rationalised number.
- **`|S21|` relative error, 5%.** Unlike `Z0` (which can be dominated by a
  small denominator effect at some geometries), `|S21|` for a near-lossless
  matched line is close to 1 for both solvers by construction — a tighter
  budget is appropriate and, as measured below, met with wide margin.
- **`phase(S21)` absolute error, 0.05 rad.** Compared in absolute radians,
  not relative error: both solvers' `S21` phase is small at this
  frequency/length (a near-zero quantity makes *relative* error an
  ill-defined/misleading metric — the same reasoning
  `tests/test_mom_ports_validation.py`'s own `S11`-near-zero checks use an
  absolute, not relative, bound for).

## Measured results

Two-wire loop, 500 µm × 2×2 µm bars, 40 µm separation, 1 GHz, ports at both
physical ends, 50 ohm reference:

| Quantity | `klt mom` | NEC2++ (external oracle) | Difference |
| -------- | --------- | ------------------------- | ---------- |
| `Z0` | 452.367 − j3.4e-8 ohm | 446.053 − j1.0e-8 ohm | 1.42% rel. |
| `S11` | 2.617e-3 + j5.047e-2 | 2.030e-3 + j4.444e-2 | both < 0.10 abs. |
| `S21` | 0.99738 − j0.05172 | 0.99797 − j0.04558 | |
| `\|S21\|` | 0.998722 | 0.999010 | 0.0288% rel. |
| `phase(S21)` | −0.05182 rad | −0.04565 rad | 0.00617 rad abs. |

All three metrics land well inside their stated tolerances (`Z0`: 1.42% of a
20% budget; `|S21|`: 0.029% of 5%; `phase(S21)`: 0.006 rad of 0.05 rad) — a
comfortable, not marginal, pass. As the "Tolerance and metric" section
above anticipates, `Z0`'s relative error (1.4%) is noticeably tighter than
either solver's own ~5-8% error against the pure analytic closed form
(`docs/design/mom-validation.md`'s companion document) — the two numerical
approximations happen to agree more closely with *each other* here than
either does with the idealised infinite-line formula, itself a useful
reality-grounding data point: two different finite-length, finite-cross-
section models converging toward each other is exactly the kind of
independent-implementation agreement epic #701's Phase 0 section asks this
cross-check to demonstrate.

## Reproducing this cross-check

```bash
# klt mom's own native extension (required for every mom test):
uv sync --extra dev --group mom

# The external oracle (Python >=3.11 only):
uv sync --extra dev --group mom --extra mom-cross-validation

uv run --extra dev --group mom --extra mom-cross-validation \
  pytest tests/test_mom_cross_validation.py -v --capture=tee-sys
```

Wired into CI in the `native` job of `.github/workflows/ci.yml`, so this
cross-check is reproduced on every push/PR, not only locally.

## See also

- [`docs/cli/mom.md`](../cli/mom.md) — the full `klt mom` command reference,
  including the full-wave sweep (#893) and ports/de-embedding (#894)
  sections this cross-check validates.
- [`docs/design/mom-validation.md`](mom-validation.md) — the sibling
  analytic-closed-form validation this document complements.
- [`docs/design/em-field-sim-spike.md`](em-field-sim-spike.md) — the
  original E&M engine survey naming openEMS and geode-fem as candidate
  external oracles, and the source of this document's license-handling
  precedent (GPL-3 tools: subprocess-only, never embedded).
- `tests/test_mom_cross_validation.py` — the executable form of this
  document.
- `scripts/mom_nec_reference.py` — the external NEC2++ solver's own driver
  script (always invoked as a subprocess — see its module docs).
- `tests/test_mom_pypeec_cross_validation.py` /
  `scripts/mom_pypeec_reference.py` — the same pairing for the PEEC spiral
  fixture's external oracle (#1886); the measured comparison lives in
  [`mom-validation.md`](mom-validation.md).

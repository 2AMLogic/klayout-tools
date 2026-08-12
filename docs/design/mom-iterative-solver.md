# `klt mom`'s solve step: preconditioned Conjugate Gradient, not direct LU

Phase 1 of the Method-of-Moments epic
([#701](https://github.com/2AMLogic/klayout-tools/issues/701)), delivered by
[#799](https://github.com/2AMLogic/klayout-tools/issues/799). Where
[#718](https://github.com/2AMLogic/klayout-tools/issues/718) shipped the MVP
solve as a direct (LU-factorised) dense linear solve, and
[#719](https://github.com/2AMLogic/klayout-tools/issues/719) validated its
answers against closed forms, this document covers the solve step's *scale*:
why the direct solve does not scale with conductor/panel count, what replaced
it, and the measured convergence rate and solve-time/memory improvement on a
larger-than-MVP geometry.

The executable form of everything here is `native/mom/src/solver.rs`'s own
test module — this file is the rationale, that module is the gate. Numbers
below were measured at commit `4ac114e` (2026-08-12);
re-run the commands cited under each section to reprint them.

## Why a direct solve does not scale

`klt mom` fills a dense `n x n` potential-coefficient matrix `P` (`n` = panel
count) and, before this issue, solved `V = P q` for `k` right-hand sides (one
per conductor) via an in-place LU factorisation: `O(n^3)` for the
factorisation, `O(n^2)` per right-hand side thereafter. The MVP's own
`MAX_PANELS` guard (`geometry.rs`) caps `n` at 8000 specifically because that
`O(n^3)` term becomes the dominant cost well before then — the guard's own doc
comment already flagged this as the scaling ceiling Phase 1c (#799) needed to
address.

## Why Conjugate Gradient, not GMRES

The acceptance criterion asks for "GMRES/CG as appropriate to the system's
structure." `P` is symmetric by construction (the off-diagonal kernel
`1/(4 pi eps r)` is symmetric in `i`/`j`) and positive definite: `q^T P q` is
(twice) the electrostatic energy of the charge distribution `q`, strictly
positive for any nonzero `q` given non-degenerate panels. A symmetric
positive-definite system is exactly the case Conjugate Gradient is the
standard, provably-optimal Krylov method for — it needs only one
matrix-vector product and a handful of length-`n` vectors per iteration, no
growing orthogonal basis to store the way GMRES needs for a general
(non-symmetric) system. GMRES would still *work* here, but would be doing
more bookkeeping to solve a system that does not need it.

## The preconditioner

Jacobi (diagonal): `M^-1 = diag(1 / P_ii)`. `P`'s diagonal is each panel's
self-term, which — because every off-diagonal coupling `1/(4 pi eps r)` is a
*fraction* of a panel's own self-potential once `r` exceeds roughly a panel
width — is reliably the largest entry in its row. Scaling by it materially
improves the matrix's effective conditioning at `O(n)` setup cost, with none
of the `O(n^2)`-or-worse fill-in a more aggressive preconditioner (e.g.
incomplete Cholesky on this fully dense matrix) would add.

## Fixing a fill bug the direct solve had been hiding

Wiring PCG in immediately broke `tests/test_mom_validation.py`'s square-coax
tests — not because CG was wrong, but because it exposed a latent bug the
direct solve had been silently tolerating. The coax fixture (and
`tests/test_mom.py`'s own coax fixture) approximates a shield ring as four
abutting wall boxes; at each right-angle corner, one wall's outward face and
its neighbour's outward face occupy the *exact same* 3-D rectangle (e.g. one
wall's `y0` face and the perpendicular wall's `y1` face, both spanning the
same `x`/`z` range at their shared `y`). Two panels at that shared location
have `r = 0`, and the point-charge kernel `1/(4 pi eps r)` is `Inf` there.

The old direct LU path never errored on this — the `Inf` entries happened not
to trigger the exact `Inf - Inf` / `0 * Inf` operation that produces `NaN`
during elimination on these particular fixtures, so `klt mom` quietly
returned a (correct, per the closed-form checks) answer built on top of a
matrix containing literal infinities. That was luck, not a property any
solver should rely on: PCG's very first matrix-vector product turns an `Inf`
entry into a non-finite curvature (`d^T A d`) immediately, and fails loudly
instead.

The fix is in `geometry::discretize` (`dedupe_coincident_panels`): after
discretising each conductor's boxes, exact-duplicate panels (identical
center, within the same `EPS_UM` non-degeneracy tolerance the module already
uses for box dimensions) are merged, keeping the first occurrence. This is
the same fix the module already applies to a single box's own opposite faces
(`z0_um == z1_um` emits one face, not six coincident ones, "avoiding... a
singular matrix") — generalised to two *different* boxes of the same
conductor. It only ever compares panels *within* one conductor: genuine
coincidence *between* different conductors is a malformed request
(overlapping/short-circuited surfaces), not a discretisation artefact, and is
still left for the solver to report as singular/ill-conditioned (see
`solver.rs`'s `pcg_solve` error message).

With the duplicate panels removed, every test in `tests/test_mom_validation.py`
and `tests/test_mom.py` passes unchanged — same closed-form agreement, same
convergence orders, same tolerances (see `docs/design/mom-validation.md`).

## Accuracy: iterative vs. the direct solve it replaced

`native/mom/src/solver.rs` keeps the original LU-based solve
(`solve_dense_lu`, `#[cfg(test)]`-only) purely as a cross-check baseline. Three
tests compare the two paths on the same filled matrix:

- `iterative_solve_matches_direct_lu_solve` — a 6-finger/1-ground geometry
  (`n = 344` panels, `k = 7` conductors): every entry of the solved charge
  matrix agrees to `max_relative = 1e-8`.
- `iterative_solve_matches_direct_lu_solve_through_the_public_api` — the same
  cross-check one level up, through `solve_capacitance_matrix_ff` (the
  function `klt mom` actually calls), confirming the assembled/scaled
  capacitance matrix agrees too.
- `iterative_solve_preserves_exact_linearity_in_permittivity` — the tightest
  existing accuracy bar on this solver
  (`test_parallel_plate_is_exactly_linear_in_permittivity`, `rel=1e-12`):
  `C(eps_r) = eps_r * C(1)` to solver round-off, re-checked at the Rust level.

`ITERATIVE_REL_TOL = 1e-12` (the CG stopping tolerance) is what makes the
`1e-12`-relative linearity check achievable — looser tolerances were tried
first and found to leave visible residual error at that check's precision.

The full `tests/test_mom_validation.py` suite (closed-form oracles,
convergence-under-refinement) passes unchanged against the iterative path —
see `docs/design/mom-validation.md` for what each of those checks means;
nothing in this issue changed their tolerances or fixtures.

## Convergence rate and solve-time comparison at scale

Run:

```bash
cd native/mom
cargo test --release iterative_solve_scaling_report -- --ignored --nocapture
```

(`#[ignore]`d because the direct-LU baseline alone takes minutes in an
*unoptimised* debug test build — every ordinary `cargo test` run would
otherwise pay for a one-off scale measurement. Release mode finishes in
seconds per solve.)

Fixture: 8 small "finger" plates over one shared ground plate (an
interdigitated-capacitor-shaped geometry — `k = 9` conductors, well past the
MVP fixtures' 1-2), discretised to `n = 6912` panels (just under the
8000-panel `MAX_PANELS` guard).

| | direct (LU) | iterative (PCG) |
| --- | --- | --- |
| solve time | 112.9 s | 29.5 s |
| **speedup** | | **3.83x** |

CG converged in a mean of **68.8** iterations (max 69) per right-hand side,
against `n = 6912` — roughly **1%** of `n`, which is the whole reason the
`O(k * iterations * n^2)` iterative cost beats the `O(n^3)` direct
factorisation here: `iterations << n`. Maximum relative residual across all 9
right-hand sides: `9.99e-13`, comfortably inside `ITERATIVE_REL_TOL`. The
solved capacitance matrix matches the direct solve's to `max_relative = 1e-7`
(a slightly looser bound than the smaller cross-checks above, chosen only
because this fixture's much larger `n` accumulates more floating-point
round-off across the matrix-vector products — not because CG converged less
tightly).

### Why this is a solve-time win, not (yet) a memory win

Both paths still assemble the same dense `n x n` matrix `P` up front — that
`O(n^2)` allocation is unchanged by this issue, and dominates memory for
either solve method (nalgebra's in-place LU factorises `P` without a second
`O(n^2)` allocation, so the two paths' peak memory is close). The measured
win here is **solve time**, from replacing a one-off `O(n^3)` factorisation
with `O(k * iterations * n^2)` work at `iterations << n`. A genuine *memory*
win (avoiding the `O(n^2)` matrix altogether) would need a matrix-free
formulation — evaluating each `P * v` product directly from panel geometry
inside the CG loop instead of reading a pre-filled dense matrix, most likely
paired with a fast multipole or hierarchical approximation of the Green's
function the way FastCap (Nabors & White 1991, cited in `solver.rs`'s own doc
comment) does. That is a substantially larger undertaking — it changes the
fill step's asymptotic complexity, not just the solve step's — and is a
natural candidate for a later phase of epic #701 if panel counts ever need to
grow past what a dense fill can hold in memory at all, not something this
issue's solver swap needed to reach the scale target it measured.

## What is *not* validated here

- **A matrix-free / memory-scaling win.** See "Why this is a solve-time win,
  not (yet) a memory win" above.
- **A non-diagonal preconditioner.** Jacobi is the simplest preconditioner
  appropriate to this SPD system (see "The preconditioner" above); a
  block-per-conductor or incomplete-Cholesky preconditioner might converge in
  fewer iterations still, but was not needed to clear this issue's
  larger-than-MVP scale target and is a natural follow-up if a future
  geometry's convergence rate degrades.
- **GMRES**, or any non-SPD solve path — not applicable, since `P` is SPD by
  construction (see "Why Conjugate Gradient, not GMRES" above).

## See also

- [`docs/cli/mom.md`](../cli/mom.md) — the command, its spec-file schema, and
  its JSON contract (unchanged by this issue).
- [`docs/design/mom-validation.md`](mom-validation.md) — the closed-form
  oracles and convergence-under-refinement checks this issue's solve-path
  change had to keep passing.
- [#701](https://github.com/2AMLogic/klayout-tools/issues/701) — the parent
  Method-of-Moments epic.

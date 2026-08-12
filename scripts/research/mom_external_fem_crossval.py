"""External FEM cross-check for `klt mom`'s full-wave S-parameters --
2AMLogic/klayout-tools issue #895, Phase 2c of the Method-of-Moments epic
#701.

`klt mom` (`native/mom/src/fullwave.rs`) is a single, purpose-built
implementation -- nothing else in the repo can independently confirm its
full-wave S-parameter numbers are right. This module is a second, genuinely
different solver for the same benchmark geometry (the canonical two-wire
transmission line `docs/cli/mom.md`'s "Full-wave frequency sweep" and "Port
definition and de-embedding" sections already use): a 2-D finite-element
(FEM) solve of the conductor cross-section, via
[scikit-fem](https://github.com/kinnala/scikit-fem) (BSD-licensed,
independently developed, pip-installable, no GUI or heavy native toolchain)
-- an "external solver" in the sense the epic's Phase 0 reality-grounding
section calls for ("Cross-check against an external MoM/FEM ... on a shared
benchmark"), run in-process rather than via subprocess only because its
license (BSD) allows it, unlike openEMS's GPL-3.0 (see
`docs/design/em-field-sim-spike.md`'s survey, which forecloses in-process
embedding for that reason).

## Why a 2-D cross-section solve, not a full 3-D field solve

`klt mom`'s own full-wave derivation (`fullwave.rs` module docs) already
reduces the problem to a **uniform TEM transmission line**: a single
per-unit-length `Z'(omega)`/`C'` pair, from which `Z0` and `gamma` follow via
the standard telegrapher's-equation formulas, and S-parameters follow via
the standard ABCD (chain-parameter) cascade + de-embedding. This module
cross-checks at that same fidelity level -- computing the per-unit-length
line inductance `L'` and capacitance `C'` from a 2-D FEM solve of the
conductor cross-section (the standard technique real "field solver" RLGC
extractors -- e.g. HFSS 2D Extractor, ADS LineCalc -- use for exactly this
class of geometry) -- rather than a full 3-D time- or frequency-domain
solve. This is a fair, apples-to-apples comparison: both solvers ultimately
produce a `Z0(omega)`/`gamma(omega)` pair and feed it through the identical
ABCD/de-embedding formalism (`line_abcd`/`abcd_to_s` below are a direct
Python port of `native/mom/src/fullwave.rs`'s `line_abcd`/`abcd_to_s`); what
differs, and is therefore genuinely being cross-checked, is *how* each
solver arrives at `Z0`/`gamma` -- `klt mom` via a retarded-kernel
point-collocation thin-wire MoM (series impedance) plus a panelized
electrostatic BEM (shunt capacitance); this module via bilinear-quad finite
elements on a graded 2-D mesh, for both quantities independently.

## Method: 2-D bilinear-quad FEM, energy method

Two identical `width_um` x `height_um` square conductors, centre-to-centre
separation `separation_um`, cross section in a `(y, z)` plane transverse to
the shared current-flow axis (matching `klt mom`'s own axis convention) --
the same fixture `tests/test_mom_fullwave_validation.py` and
`tests/test_mom_ports_validation.py` already validate against closed forms.

- **Mesh**: `skfem.MeshQuad` built via `init_tensor` from two independently
  graded 1-D coordinate arrays (`graded_axis`) -- a fine, uniform-spacing
  "core" region tightly covering both conductors (plus a small margin),
  extended geometrically (ratio `growth` per step) out to `extent_um` on
  every side. This resolves the conductor cross-section at full resolution
  while keeping the total node count in the tens of thousands (not the
  hundreds of millions a *uniform* fine mesh over the same domain extent
  would need) -- see `graded_axis`.
- **Conductors as fixed-potential/fixed-source node sets.** Rather than
  meshing the conductor boundary as a geometric hole (which would need a
  triangulating mesh generator, e.g. `gmsh`, as an extra dependency), every
  mesh node whose coordinates fall inside a conductor's cross-section
  rectangle is treated as belonging to that conductor -- a strong Dirichlet
  constraint for the electrostatic solve, a uniform current-density source
  region for the magnetostatic solve. This is a standard, simpler
  alternative to boundary-fitted meshing for a solid (not hollow)
  conductor, at the cost of a "staircase" boundary at the mesh resolution
  -- negligible here since the graded core mesh resolves the conductor
  cross-section at `step_um` (0.25 um by default, an 8x8 element grid across
  a 2x2 um conductor).
- **Electrostatic (`C'`)**: solve Laplace's equation for the potential with
  conductor 1 at `+0.5` V, conductor 2 at `-0.5` V (the standard
  differential-mode drive -- by symmetry `V_1 = -V_2`, matching the same
  differential-mode convention `fullwave.rs`'s own module docs use for its
  `C' = (C_11 - C_12) / 2` derivation), and `V = 0` at the outer mesh
  boundary (the open-region truncation -- see "What this does not model"
  below). Per-unit-length capacitance follows from the stored electrostatic
  energy: `C' = 2*W / V_total^2`, `W = 0.5 * eps0 * integral(|grad V|^2)
  dA`. The Dirichlet energy integral `integral(|grad V|^2) dA` is exactly
  scale-invariant in 2-D (gradient scales as `1/L`, area as `L^2`, product
  as `L^0`) -- physically the standard fact that a 2-D cross-section's
  per-unit-length capacitance depends only on the cross-section's *shape*
  (dimensionless ratios), not its absolute size -- so assembling the
  stiffness matrix directly in the mesh's own micrometer coordinates already
  gives `C'` in F/m with no further unit conversion.
- **Magnetostatic (`L'`)**: solve the vector-potential Poisson equation
  `-(1/mu0) * laplacian(A) = J` with a uniform current density `+J0` over
  conductor 1's cross-section and `-J0` over conductor 2's (equal and
  opposite loop current, `J0` chosen so the total current is exactly 1 A --
  the DC/no-skin-effect approximation, matching `klt mom`'s own thin-wire
  MVP, which likewise does not model skin effect), and `A = 0` at the outer
  mesh boundary. Per-unit-length inductance follows from the stored
  magnetic energy: `L' = 2*W / I^2`, `W = 0.5 * integral(J . A) dA =
  0.5 * A^T b` (`b` the assembled source vector, `A` the solved nodal
  vector-potential values) -- exact for a Galerkin FEM solve, no extra unit
  conversion needed for the same scale-invariance reason as `C'` above
  (verified numerically against the classical two-wire-line closed form,
  see `tests/test_mom_external_fem_crossval.py`).
- **`Z0`/`gamma`**: the standard lossless-TEM-line formulas, `Z0 =
  sqrt(L'/C')`, `gamma = j*omega*sqrt(L'*C')` -- computed independently
  here (not assumed via the exact `L'*C' = mu0*eps0` TEM identity a vacuum
  background satisfies), so both `L'` and `C'` are genuinely
  solver-independent quantities, not just one of them with the other
  inferred from an identity.
- **S-parameters**: `line_abcd`/`abcd_to_s`/`de_embedded_s_parameters`
  below are a line-for-line Python port of
  `native/mom/src/fullwave.rs`'s identically-named Rust functions (the
  standard ABCD chain-parameter cascade + de-embedding technique, Pozar
  *Microwave Engineering* Table 4.2) -- see that module's own docs for the
  derivation.

## What this does not model (scope, matching `klt mom`'s own MVP restrictions)

- **Open-boundary truncation, not a true infinite domain.** The outer mesh
  boundary is a finite distance away (`extent_um` past the conductors'
  bounding box), with a Dirichlet (`V=0`/`A=0`) condition there -- a
  reasonable, standard open-region approximation (the same
  finite-computational-domain compromise any FEM/FDTD field solver makes),
  whose residual error is measured and reported in
  `docs/design/mom-external-crossval.md`, not asserted to be zero.
- **No skin effect / frequency-dependent R.** Uniform current density,
  matching `klt mom`'s own thin-wire MVP (see `docs/cli/mom.md`'s "Scope and
  limitations").
- **Equal-area-circle shape substitution is not used here** (unlike several
  of `klt mom`'s own analytic-oracle comparisons in
  `docs/design/mom-validation.md`) -- this solver meshes the actual square
  cross-section directly, so it is *not* subject to that particular
  approximation; see `docs/design/mom-external-crossval.md` for how its own
  residual error against the (shape-substituted) closed form compares.
- **Vacuum-only background** (`background_permittivity == 1.0`) -- the
  benchmark this module targets; a non-unity background is out of scope
  (raises `NotImplementedError`) rather than silently wrong.

## Reproducing this

Run directly for a standalone JSON report (mirrors invoking any other
external CLI EM tool):

```
python scripts/research/mom_external_fem_crossval.py \\
    --width-um 2.0 --height-um 2.0 --separation-um 40.0 \\
    --axis-lo-um 0.0 --axis-hi-um 500.0 --frequency-hz 1.0e9 \\
    --port-position-um 0.0 500.0 --reference-impedance-ohm 50.0
```

or import `s_parameters_two_wire_line` directly (what
`tests/test_mom_external_fem_crossval.py` does to compare against `klt
mom`'s own `run_mom` output on the identical benchmark).
"""

from __future__ import annotations

import argparse
import json
from typing import NamedTuple

import numpy as np
from skfem import (
    Basis,
    BilinearForm,
    ElementQuad1,
    LinearForm,
    MeshQuad,
    condense,
    solve,
)
from skfem.helpers import dot, grad

MU0_H_PER_M = 4.0 * np.pi * 1e-7
EPS0_F_PER_M = 8.854_187_812_8e-12
ETA0_OHM = np.sqrt(MU0_H_PER_M / EPS0_F_PER_M)
C0_M_PER_S = 299_792_458.0

# Defaults measured (see docs/design/mom-external-crossval.md) to agree with
# `klt mom`'s own two-wire-line benchmark within a few percent, in ~1 second
# of wall time -- fine-grained enough near the conductors, extended far
# enough for the open-boundary truncation not to dominate the error budget.
DEFAULT_STEP_UM = 0.25
DEFAULT_EXTENT_UM = 400.0
DEFAULT_GROWTH = 1.15


def graded_axis(
    lo: float, hi: float, step: float, extent: float, growth: float = DEFAULT_GROWTH
) -> np.ndarray:
    """A 1-D node coordinate array: uniform `step` spacing across `[lo, hi]`,
    extended geometrically (each successive step multiplied by `growth`)
    until at least `extent` past `lo` and past `hi` -- see module docstring
    ("Mesh")."""
    if step <= 0.0:
        raise ValueError(f"step must be positive, got {step}")
    if growth <= 1.0:
        raise ValueError(f"growth must be > 1.0, got {growth}")
    core = np.arange(lo, hi + step / 2.0, step)
    left: list[float] = []
    x, d = lo, step
    while lo - x < extent:
        d *= growth
        x -= d
        left.append(x)
    right: list[float] = []
    x, d = hi, step
    while x - hi < extent:
        d *= growth
        x += d
        right.append(x)
    coords = np.concatenate([np.array(left[::-1]), core, np.array(right)])
    return np.unique(coords)


def _in_box(
    y: np.ndarray, z: np.ndarray, y0: float, y1: float, z0: float, z1: float
) -> np.ndarray:
    eps = 1e-9
    return (y >= y0 - eps) & (y <= y1 + eps) & (z >= z0 - eps) & (z <= z1 + eps)


class CrossSectionMesh(NamedTuple):
    basis: Basis
    laplace: np.ndarray  # assembled dot(grad,grad) stiffness matrix
    cond1: np.ndarray  # bool mask over dofs, conductor 1
    cond2: np.ndarray  # bool mask over dofs, conductor 2
    outer: np.ndarray  # dof indices on the outer mesh boundary


def _build_cross_section(
    width_um: float,
    height_um: float,
    separation_um: float,
    step_um: float,
    extent_um: float,
    growth: float,
) -> CrossSectionMesh:
    if separation_um <= width_um:
        raise ValueError(
            f"separation_um ({separation_um}) must exceed width_um ({width_um}) -- "
            "conductors would overlap"
        )
    y_lo, y_hi = -4.0 * width_um, separation_um + width_um + 4.0 * width_um
    z_lo, z_hi = -4.0 * height_um, height_um + 4.0 * height_um
    y_boundaries = [0.0, width_um, separation_um, separation_um + width_um]
    z_boundaries = [0.0, height_um]
    y = np.union1d(graded_axis(y_lo, y_hi, step_um, extent_um, growth), y_boundaries)
    z = np.union1d(graded_axis(z_lo, z_hi, step_um, extent_um, growth), z_boundaries)

    mesh = MeshQuad.init_tensor(y, z)
    basis = Basis(mesh, ElementQuad1())

    @BilinearForm
    def laplace_form(u, v, w):
        return dot(grad(u), grad(v))

    laplace = laplace_form.assemble(basis)

    yc, zc = basis.doflocs[0], basis.doflocs[1]
    cond1 = _in_box(yc, zc, 0.0, width_um, 0.0, height_um)
    cond2 = _in_box(yc, zc, separation_um, separation_um + width_um, 0.0, height_um)
    outer = mesh.boundary_nodes()

    return CrossSectionMesh(
        basis=basis, laplace=laplace, cond1=cond1, cond2=cond2, outer=outer
    )


def solve_rlgc_per_length(
    width_um: float,
    height_um: float,
    separation_um: float,
    *,
    step_um: float = DEFAULT_STEP_UM,
    extent_um: float = DEFAULT_EXTENT_UM,
    growth: float = DEFAULT_GROWTH,
) -> tuple[float, float, int]:
    """Solve the 2-D electrostatic + magnetostatic cross-section problem for
    two identical `width_um` x `height_um` square conductors, centre-to-
    centre separation `separation_um`, in a vacuum background -- see module
    docstring's "Method" section. Returns `(c_prime_f_per_m, l_prime_h_per_m,
    node_count)`.
    """
    m = _build_cross_section(
        width_um, height_um, separation_um, step_um, extent_um, growth
    )
    ndofs = m.laplace.shape[0]

    # --- electrostatic: differential-mode drive, C' = 2*W/V_total^2 -------
    v = np.zeros(ndofs)
    v[m.cond1] = 0.5
    v[m.cond2] = -0.5
    v[m.outer] = 0.0
    dirichlet = m.cond1 | m.cond2
    dirichlet[m.outer] = True
    phi = solve(*condense(m.laplace, x=v, D=np.nonzero(dirichlet)[0]))
    energy_e = float(phi @ (m.laplace @ phi))
    c_prime_f_per_m = EPS0_F_PER_M * energy_e  # V_total = 1.0 V; see module docs

    # --- magnetostatic: equal/opposite unit loop current, L' = 2*W/I^2 -----
    area1_um2 = width_um * height_um
    area2_um2 = width_um * height_um
    j1, j2 = 1.0 / area1_um2, -1.0 / area2_um2
    width_um_, height_um_, separation_um_ = width_um, height_um, separation_um

    @LinearForm
    def source_form(v_test, w):
        y, z = w.x[0], w.x[1]
        j = np.where(_in_box(y, z, 0.0, width_um_, 0.0, height_um_), j1, 0.0)
        j = j + np.where(
            _in_box(y, z, separation_um_, separation_um_ + width_um_, 0.0, height_um_),
            j2,
            0.0,
        )
        return j * v_test

    b = source_form.assemble(m.basis)
    stiffness = m.laplace / MU0_H_PER_M
    a_vec = solve(*condense(stiffness, b, x=np.zeros(ndofs), D=m.outer))
    energy_m = float(a_vec @ b)  # J/m directly, I = 1 A; see module docs
    l_prime_h_per_m = energy_m  # / I^2, I = 1

    return c_prime_f_per_m, l_prime_h_per_m, ndofs


# --- ABCD / S-parameters -- ports on Python side of `native/mom/src/fullwave.rs` ---


def line_abcd(z0: complex, gamma: complex, length_m: float) -> np.ndarray:
    """The ABCD (chain-parameter) matrix of a uniform transmission-line
    segment, direct Python port of `native/mom/src/fullwave.rs`'s
    `line_abcd`."""
    gl = gamma * length_m
    a = np.cosh(gl)
    b = z0 * np.sinh(gl)
    c = np.sinh(gl) / z0
    return np.array([[a, b], [c, a]], dtype=complex)


def abcd_to_s(
    m: np.ndarray, z01_ohm: float, z02_ohm: float
) -> tuple[complex, complex, complex, complex]:
    """2-port ABCD -> S-parameter conversion (Pozar, Table 4.2), direct
    Python port of `native/mom/src/fullwave.rs`'s `abcd_to_s`. Returns
    `(s11, s12, s21, s22)`."""
    a, b, c, d = m[0, 0], m[0, 1], m[1, 0], m[1, 1]
    z01, z02 = complex(z01_ohm), complex(z02_ohm)
    sqrt_z01z02 = np.sqrt(z01 * z02)
    delta = a * z02 + b + c * z01 * z02 + d * z01
    s11 = (a * z02 + b - c * z01 * z02 - d * z01) / delta
    s12 = (a * d - b * c) * 2.0 * sqrt_z01z02 / delta
    s21 = 2.0 * sqrt_z01z02 / delta
    s22 = (-a * z02 + b - c * z01 * z02 + d * z01) / delta
    return s11, s12, s21, s22


def _abcd_inv(m: np.ndarray) -> np.ndarray:
    det = m[0, 0] * m[1, 1] - m[0, 1] * m[1, 0]
    if abs(det) < 1e-30:
        raise ValueError("de-embedding requires an invertible feed-line ABCD matrix")
    return np.array([[m[1, 1] / det, -m[0, 1] / det], [-m[1, 0] / det, m[0, 0] / det]])


def s_parameters_two_wire_line(
    *,
    width_um: float,
    height_um: float,
    separation_um: float,
    axis_lo_um: float,
    axis_hi_um: float,
    frequency_hz: float,
    port_positions_um: tuple[float, float],
    reference_impedance_ohm: tuple[float, float],
    background_permittivity: float = 1.0,
    step_um: float = DEFAULT_STEP_UM,
    extent_um: float = DEFAULT_EXTENT_UM,
    growth: float = DEFAULT_GROWTH,
) -> dict:
    """Full pipeline: FEM cross-section RLGC -> Z0/gamma -> ABCD cascade +
    de-embedding -> S-parameters, for the same two-wire-line benchmark and
    port convention `docs/cli/mom.md`'s "Port definition and de-embedding"
    section documents (`ports[0]` near, `ports[1]` far, both strictly inside
    `[axis_lo_um, axis_hi_um]`). Returns a dict shaped like `klt mom`'s own
    `full_wave_sweep[i]["s_parameters"]` field, plus the intermediate `c_prime_f_per_m`
    / `l_prime_h_per_m` / `characteristic_impedance_ohm` /
    `propagation_constant_per_m` / `node_count` for diagnostics.
    """
    if background_permittivity != 1.0:
        raise NotImplementedError(
            "s_parameters_two_wire_line only supports a vacuum "
            "(background_permittivity == 1.0) background -- see module docstring's "
            '"What this does not model"'
        )
    if not (axis_lo_um <= port_positions_um[0] < port_positions_um[1] <= axis_hi_um):
        raise ValueError(
            f"ports {port_positions_um} must lie within [axis_lo_um, axis_hi_um] = "
            f"[{axis_lo_um}, {axis_hi_um}] in strictly ascending order"
        )

    c_prime, l_prime, node_count = solve_rlgc_per_length(
        width_um,
        height_um,
        separation_um,
        step_um=step_um,
        extent_um=extent_um,
        growth=growth,
    )
    omega = 2.0 * np.pi * frequency_hz
    z_prime = 1j * omega * l_prime
    y_prime = 1j * omega * c_prime
    gamma = np.sqrt(z_prime * y_prime)
    z0 = np.sqrt(z_prime / y_prime)

    feed1_m = (port_positions_um[0] - axis_lo_um) * 1e-6
    feed2_m = (axis_hi_um - port_positions_um[1]) * 1e-6
    total_m = (axis_hi_um - axis_lo_um) * 1e-6

    abcd_total = line_abcd(z0, gamma, total_m)
    abcd_dut = (
        _abcd_inv(line_abcd(z0, gamma, feed1_m))
        @ abcd_total
        @ _abcd_inv(line_abcd(z0, gamma, feed2_m))
    )
    s11, s12, s21, s22 = abcd_to_s(
        abcd_dut, reference_impedance_ohm[0], reference_impedance_ohm[1]
    )

    return {
        "s_parameters": {
            "s11_real": s11.real,
            "s11_imag": s11.imag,
            "s12_real": s12.real,
            "s12_imag": s12.imag,
            "s21_real": s21.real,
            "s21_imag": s21.imag,
            "s22_real": s22.real,
            "s22_imag": s22.imag,
        },
        "c_prime_f_per_m": c_prime,
        "l_prime_h_per_m": l_prime,
        "characteristic_impedance_ohm": complex(z0),
        "propagation_constant_per_m": complex(gamma),
        "node_count": node_count,
    }


def _main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--width-um", type=float, default=2.0)
    p.add_argument("--height-um", type=float, default=2.0)
    p.add_argument("--separation-um", type=float, required=True)
    p.add_argument("--axis-lo-um", type=float, default=0.0)
    p.add_argument("--axis-hi-um", type=float, required=True)
    p.add_argument("--frequency-hz", type=float, required=True)
    p.add_argument("--port-position-um", type=float, nargs=2, required=True)
    p.add_argument("--reference-impedance-ohm", type=float, nargs="+", default=[50.0])
    p.add_argument("--step-um", type=float, default=DEFAULT_STEP_UM)
    p.add_argument("--extent-um", type=float, default=DEFAULT_EXTENT_UM)
    p.add_argument("--growth", type=float, default=DEFAULT_GROWTH)
    args = p.parse_args()

    ref = args.reference_impedance_ohm
    ref_pair = (ref[0], ref[0]) if len(ref) == 1 else (ref[0], ref[1])

    result = s_parameters_two_wire_line(
        width_um=args.width_um,
        height_um=args.height_um,
        separation_um=args.separation_um,
        axis_lo_um=args.axis_lo_um,
        axis_hi_um=args.axis_hi_um,
        frequency_hz=args.frequency_hz,
        port_positions_um=tuple(args.port_position_um),
        reference_impedance_ohm=ref_pair,
        step_um=args.step_um,
        extent_um=args.extent_um,
        growth=args.growth,
    )
    # complex values aren't JSON-serializable -- report real/imag pairs.
    result["characteristic_impedance_ohm"] = {
        "real": result["characteristic_impedance_ohm"].real,
        "imag": result["characteristic_impedance_ohm"].imag,
    }
    result["propagation_constant_per_m"] = {
        "real": result["propagation_constant_per_m"].real,
        "imag": result["propagation_constant_per_m"].imag,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _main()

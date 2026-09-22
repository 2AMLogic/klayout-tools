#!/usr/bin/env python3
"""Standalone PyPEEC reference solve for `klt mom`'s PEEC **spiral**
inductance cross-validation (2AMLogic/klayout-tools issue #1886, closing out
[#1842](https://github.com/2AMLogic/klayout-tools/issues/1842)'s acceptance
criterion 3).

This script is deliberately **not** importable Python library code, and is
never imported directly by `tests/test_mom_pypeec_cross_validation.py` -- it
is always invoked as a **subprocess** (`subprocess.run([sys.executable,
__file__, ...])`), reading a geometry spec as JSON on stdin and printing a
JSON result to stdout, exactly like its sibling `scripts/mom_nec_reference.py`
does for the NEC2++ full-wave cross-check. PyPEEC is MPL-2.0 (a weak-copyleft,
file-scoped license, materially lighter than the GPL-3.0-only `PyNEC` the
sibling script drives), but the subprocess boundary is kept anyway: it is this
repo's uniform handling for *every* external engine it touches (`ngspice`,
Yosys, Icarus Verilog, Verilator, `PyNEC`), and it keeps the two solvers'
processes -- and therefore their code paths -- genuinely disjoint.

## Why PyPEEC and not FastHenry

[#1842](https://github.com/2AMLogic/klayout-tools/issues/1842)'s stated oracle
was **FastHenry** on a 2-3 turn spiral, and #1886 was originally filed to wire
exactly that binary into CI. The operator ruling on #1886 (2026-09-18) settled
the licensing question that
[`docs/design/em-field-sim-spike.md`](../docs/design/em-field-sim-spike.md)
had left open, and it came out the other way:

- FastHenry is **not packaged** by Debian/Ubuntu, Homebrew, or PyPI, so the
  "install a distro package in CI" framing never applied -- CI would have had
  to build the MIT-RLE sources itself.
- Those sources carry MIT RLE's 1990s research notice, **not** an OSI
  license: *"Permission to use, copy and modify for internal, noncommercial
  purposes is hereby granted. Any distribution of this program or any part
  thereof is strictly prohibited without prior written consent of M.I.T."*
  That is a hard "no" for this repo, permanently.

[PyPEEC](https://github.com/otvam/pypeec) (Dartmouth College; MPL-2.0; JOSS
[10.21105/joss.06644](https://doi.org/10.21105/joss.06644)) is the modern,
permissively-licensed equivalent: a 3-D quasi-magnetostatic **PEEC** solver
with FFT-accelerated dense-operator multiplication that extracts terminal
R/L from a voxelised conductor geometry. It is the *same method class* as
`klt mom`'s own inductance path (partial-element equivalent circuit) and the
same one FastHenry implements, but an entirely independent implementation:
different language surface, different discretisation (a uniform voxel mesh
with face currents and an FFT-circulant Green's operator, vs.
`native/mom/src/peec.rs`'s per-bar filament bundles with Rosa's closed-form
self terms and an analytic skew-filament mutual), different authors.

## Method: terminal impedance of the open spiral

PyPEEC does not report a partial-inductance *matrix*; it reports the terminal
voltages and currents of a driven structure. For a single current source
driving the spiral's inner terminal and a 0 V reference at its outer terminal,
the terminal impedance is

```text
Z = (V_src - V_sink) / I_src = R + j*omega*L
```

so `L = Im(Z) / (2*pi*f)` and `R = Re(Z)`. Because the spiral is an **open**
structure (no return conductor), this `L` is precisely the signed
partial-inductance sum `klt mom`'s own fixture forms from its
`inductance_matrix_nh` -- the self-inductance of the one current path, with
every turn's mutual coupling to every other turn included.

The frequency must be low enough that the current density stays uniform
across the 2x2 um cross-section (so both solvers model the same DC current
distribution, which is all `klt mom`'s filament bundles can represent) while
being high enough that `Im(Z)` is not lost in the solver's residual next to
`Re(Z)`. The default 100 MHz satisfies both by a wide margin: copper's skin
depth there is ~6.6 um, over 3x the conductor's largest cross-sectional
dimension, and `omega*L/R` is ~0.2 rather than ~1e-3.

## Usage

```bash
python3 scripts/mom_pypeec_reference.py <<'EOF'
{
  "turns": 2,
  "start_side_um": 60.0,
  "pitch_growth_um": 15.0,
  "width_um": 2.0,
  "thickness_um": 2.0,
  "voxel_pitch_um": 1.0,
  "frequency_hz": 1.0e8,
  "resistivity_ohm_m": 1.75e-8
}
EOF
```

prints a JSON object with `L_H`/`R_ohm` (the keys #1886's rescope names),
plus `L_nH`, `Z_real_ohm`/`Z_imag_ohm`, the `frequency_hz` they were measured
at, and the voxel counts (`voxel_count_total`/`voxel_count_used`) that fix the
discretisation the numbers belong to. PyPEEC's own progress logging goes to
**stderr**, so stdout carries nothing but the JSON document.
"""

from __future__ import annotations

import json
import sys

UM = 1e-6


def _spiral_vertices(
    turns: int, start_side_um: float, pitch_growth_um: float
) -> list[tuple[float, float]]:
    """The square spiral's centreline vertices, in micrometres.

    Independently re-implemented from the fixture definition in
    `docs/design/mom-validation.md` section 5 (east, north, west, south legs;
    the leg length grows by `pitch_growth_um` after every second leg, so the
    spiral opens out by one pitch per turn) -- not imported from, or shared
    with, `native/mom/src/peec.rs`'s `square_spiral_segments`.
    """
    directions = [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)]
    x, y = 0.0, 0.0
    side = start_side_um
    vertices = [(x, y)]
    for leg in range(turns * 4):
        dx, dy = directions[leg % 4]
        x, y = x + dx * side, y + dy * side
        vertices.append((x, y))
        if leg % 2 == 1:
            side += pitch_growth_um
    return vertices


def _leg_rectangles(
    vertices: list[tuple[float, float]], width_um: float
) -> list[tuple[float, float, float, float]]:
    """Each leg as an axis-aligned `(x0, y0, x1, y1)` rectangle, in
    micrometres -- a `width_um`-wide bar centred on the leg's centreline.

    The eight rectangles **tile** the spiral's footprint: every end that
    meets another leg is pushed forward by `width_um / 2` along its own
    direction, so the square of metal at each corner vertex belongs to
    exactly one leg (the one leaving it) and no two rectangles overlap. The
    two free ends -- the spiral's terminals -- are left on the centreline
    vertex. `tests/test_mom_pypeec_cross_validation.py` states and applies
    the identical rule on the `klt mom` side (independently implemented,
    sharing no code with this script), so both solvers see the same metal,
    not merely the same centreline.
    """
    rectangles = []
    last = len(vertices) - 2
    for index, ((x0, y0), (x1, y1)) in enumerate(
        zip(vertices[:-1], vertices[1:], strict=True)
    ):
        horizontal = abs(x1 - x0) > abs(y1 - y0)
        step = width_um / 2.0
        if horizontal:
            direction = 1.0 if x1 > x0 else -1.0
            a = x0 + (direction * step if index > 0 else 0.0)
            b = x1 + (direction * step if index < last else 0.0)
            rectangles.append((min(a, b), y0 - step, max(a, b), y0 + step))
        else:
            direction = 1.0 if y1 > y0 else -1.0
            a = y0 + (direction * step if index > 0 else 0.0)
            b = y1 + (direction * step if index < last else 0.0)
            rectangles.append((x0 - step, min(a, b), x0 + step, max(a, b)))
    return rectangles


def _terminal_rectangle(
    start: tuple[float, float],
    end: tuple[float, float],
    width_um: float,
    depth_um: float,
) -> tuple[float, float, float, float]:
    """A `depth_um`-deep slab of the leg `start -> end`, taken at its `start`
    end -- the terminal face a source/sink is attached to."""
    (x0, y0), (x1, y1) = start, end
    if abs(x1 - x0) > abs(y1 - y0):
        sign = 1.0 if x1 > x0 else -1.0
        xs = sorted((x0, x0 + sign * depth_um))
        return (xs[0], y0 - width_um / 2.0, xs[1], y0 + width_um / 2.0)
    sign = 1.0 if y1 > y0 else -1.0
    ys = sorted((y0, y0 + sign * depth_um))
    return (x0 - width_um / 2.0, ys[0], x0 + width_um / 2.0, ys[1])


def _polygon(rectangle: tuple[float, float, float, float], layer: str) -> dict:
    """One rectangle as a PyPEEC `shape` mesher polygon, in metres."""
    x0, y0, x1, y1 = rectangle
    return {
        "shape_layer": [layer],
        "shape_operation": "add",
        "shape_type": "polygon",
        "shape_data": {
            "buffer": 0.0,
            "coord_shell": [
                [x0 * UM, y0 * UM],
                [x1 * UM, y0 * UM],
                [x1 * UM, y1 * UM],
                [x0 * UM, y1 * UM],
            ],
            "coord_holes": [],
        },
    }


def _geometry(spec: dict) -> dict:
    """The PyPEEC `shape`-mesher geometry document for the spiral fixture."""
    turns = int(spec["turns"])
    width_um = float(spec["width_um"])
    thickness_um = float(spec["thickness_um"])
    voxel_pitch_um = float(spec["voxel_pitch_um"])

    vertices = _spiral_vertices(
        turns, float(spec["start_side_um"]), float(spec["pitch_growth_um"])
    )
    rectangles = _leg_rectangles(vertices, width_um)

    # The two open ends of the spiral: the inner end starts the first leg,
    # the outer end terminates the last one (walked backwards so the slab is
    # taken at the free end, not at the corner).
    src = _terminal_rectangle(vertices[0], vertices[1], width_um, voxel_pitch_um)
    sink = _terminal_rectangle(vertices[-1], vertices[-2], width_um, voxel_pitch_um)

    n_layer = int(round(thickness_um / voxel_pitch_um))
    if n_layer < 1:
        raise ValueError(
            f"voxel_pitch_um {voxel_pitch_um} does not divide thickness_um "
            f"{thickness_um} into at least one layer"
        )

    return {
        "mesh_type": "shape",
        "data_voxelize": {
            "param": {
                "dx": voxel_pitch_um * UM,
                "dy": voxel_pitch_um * UM,
                "dz": voxel_pitch_um * UM,
                "cz": 0.0,
                # Below the voxel pitch by orders of magnitude: the geometry
                # is exact axis-aligned rectangles, so simplification has
                # nothing legitimate to remove.
                "simplify": 1.0e-12,
                "construct": None,
                "xy_min": None,
                "xy_max": None,
            },
            "layer_stack": [{"n_layer": n_layer, "tag_layer": "metal"}],
            "geometry_shape": {
                "spiral": [_polygon(r, "metal") for r in rectangles],
                "src": [_polygon(src, "metal")],
                "sink": [_polygon(sink, "metal")],
            },
        },
        # No magnetic-field cloud: this cross-check reads terminal
        # quantities only.
        "data_point": {"check_cloud": False, "filter_cloud": False, "pts_cloud": []},
        "data_resampling": {
            "use_reduce": False,
            "use_resample": False,
            "resampling_factor": [1, 1, 1],
        },
        "data_conflict": {
            "resolve_rules": True,
            "resolve_random": False,
            # The terminal slabs overlap the spiral's own first/last leg by
            # construction; the terminals win those voxels.
            "conflict_rules": [
                {"domain_resolve": ["spiral"], "domain_keep": ["src"]},
                {"domain_resolve": ["spiral"], "domain_keep": ["sink"]},
            ],
        },
        "data_integrity": {
            "check_integrity": True,
            # A spiral whose legs failed to meet at the corners, or whose
            # terminals fell off the metal, would otherwise solve happily and
            # report a meaningless inductance.
            "domain_connected": {
                "conductor": {
                    "domain_group": [["spiral"], ["src", "sink"]],
                    "connected": True,
                }
            },
            "domain_adjacent": {
                "terminal": {"domain_group": [["src"], ["sink"]], "connected": False}
            },
        },
    }


def _problem(spec: dict) -> dict:
    """The PyPEEC problem document: one current source into the inner
    terminal, a 0 V reference at the outer one."""
    resistivity = float(spec["resistivity_ohm_m"])
    material_val = {"metal": {"rho_re": resistivity, "rho_im": 0.0}}
    source_val = {
        # A 1 A drive with a negligible internal admittance/impedance, so the
        # extracted terminal quantities are the structure's own, not the
        # sources'.
        "src": {"I_re": 1.0, "I_im": 0.0, "Y_re": 1.0e-12, "Y_im": 0.0},
        "sink": {"V_re": 0.0, "V_im": 0.0, "Z_re": 1.0e-12, "Z_im": 0.0},
    }
    return {
        "material_def": {
            "metal": {
                "domain_list": ["spiral", "src", "sink"],
                "material_type": "electric",
                "orientation_type": "isotropic",
                "var_type": "lumped",
            }
        },
        "source_def": {
            "src": {
                "domain_list": ["src"],
                "source_type": "current",
                "var_type": "lumped",
            },
            "sink": {
                "domain_list": ["sink"],
                "source_type": "voltage",
                "var_type": "lumped",
            },
        },
        "material_val": material_val,
        "source_val": source_val,
        "sweep_solver": {
            "spiral": {
                "init": None,
                "param": {
                    "freq": float(spec["frequency_hz"]),
                    "material_val": material_val,
                    "source_val": source_val,
                },
            }
        },
    }


def _tolerance() -> dict:
    """PyPEEC's numerical options.

    Kept close to the defaults PyPEEC ships in its own `config/tolerance.yaml`
    example, with the iterative-solver budgets tightened: the quantity this
    cross-check reads is `Im(Z)`, which is ~5x smaller than `Re(Z)` on this
    fixture, so a loose residual would show up as inductance noise.
    """
    return {
        "parallel_sweep": {"n_jobs": 0, "n_threads": None},
        "integral_simplify": 20.0,
        "biot_savart": "face",
        "dense_options": {
            "method": "fft",
            "split": True,
            "fft_options": {
                "library": "SciPy",
                "scipy_worker": -1,
                "fftw_thread": -1,
                "fftw_cache": True,
                "fftw_timeout": 100.0,
                "fftw_byte_align": 16,
            },
        },
        "factorization_options": {
            "schur": True,
            "library": "SuperLU",
            "pyamg_options": {"tol": 1.0e-6, "solver": "root", "krylov": None},
            "pardiso_options": {"thread_pardiso": -1, "thread_mkl": -1},
        },
        "solver_options": {
            "coupling": "direct",
            "status_options": {
                "ignore_status": False,
                "ignore_res": True,
                "rel_tol": 1.0e-3,
                "abs_tol": 1.0e-9,
            },
            "power_options": {
                "stop": True,
                "n_min": 4,
                "n_cmp": 3,
                "rel_tol": 1.0e-8,
                "abs_tol": 1.0e-14,
            },
            "direct_options": {
                "solver": "gmres",
                "rel_tol": 1.0e-9,
                "abs_tol": 1.0e-15,
                "n_inner": 50,
                "n_outer": 50,
            },
            "segregated_options": {
                "rel_tol": 1.0e-6,
                "abs_tol": 1.0e-12,
                "relax_electric": 1.0,
                "relax_magnetic": 1.0,
                "n_min": 2,
                "n_max": 20,
                "iter_electric_options": {
                    "solver": "gmres",
                    "rel_tol": 1.0e-6,
                    "abs_tol": 1.0e-12,
                    "n_inner": 20,
                    "n_outer": 20,
                },
                "iter_magnetic_options": {
                    "solver": "gmres",
                    "rel_tol": 1.0e-6,
                    "abs_tol": 1.0e-12,
                    "n_inner": 20,
                    "n_outer": 20,
                },
            },
        },
        "condition_options": {
            "check": True,
            "tolerance_electric": 1.0e15,
            "tolerance_magnetic": 1.0e15,
            "norm_options": {"t_accuracy": 2, "n_iter_max": 25},
        },
    }


def solve(spec: dict) -> dict:
    import math

    import pypeec  # imported here, never at module import -- see module docs

    frequency_hz = float(spec["frequency_hz"])
    if frequency_hz <= 0.0:
        raise ValueError(
            "frequency_hz must be > 0: the inductance is read off Im(Z), which "
            "a DC solve does not carry"
        )

    data_voxel = pypeec.run_mesher_data(_geometry(spec))
    data_solution = pypeec.run_solver_data(data_voxel, _problem(spec), _tolerance())

    if not data_solution["status"]:
        raise RuntimeError("PyPEEC reported an unsuccessful solve")
    sweep = data_solution["data_sweep"]["spiral"]
    if not (sweep["solution_ok"] and sweep["solver_ok"] and sweep["condition_ok"]):
        raise RuntimeError(
            "PyPEEC solve did not converge cleanly: "
            f"solution_ok={sweep['solution_ok']} solver_ok={sweep['solver_ok']} "
            f"condition_ok={sweep['condition_ok']}"
        )

    sources = sweep["source_values"]
    z = (sources["src"]["V"] - sources["sink"]["V"]) / sources["src"]["I"]
    inductance_h = z.imag / (2.0 * math.pi * frequency_hz)

    voxel_status = data_voxel["voxel_status"]
    return {
        "L_H": inductance_h,
        "R_ohm": z.real,
        "L_nH": inductance_h * 1e9,
        "Z_real_ohm": z.real,
        "Z_imag_ohm": z.imag,
        "frequency_hz": frequency_hz,
        "voxel_count_total": int(voxel_status["n_total"]),
        "voxel_count_used": int(voxel_status["n_used"]),
        "pypeec_version": getattr(pypeec, "__version__", "unknown"),
    }


def main() -> int:
    spec = json.loads(sys.stdin.read())
    result = solve(spec)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

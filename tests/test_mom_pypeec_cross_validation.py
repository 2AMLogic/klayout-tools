"""Cross-validation of `klt mom`'s PEEC **spiral** inductance (issue #1842,
the generalized filament-pair formula) against an **external** PEEC solver --
issue #1886, closing out #1842's acceptance criterion 3.

`native/mom/src/peec.rs`'s
`square_spiral_inductance_matches_independent_filament_oracle` already checks
the same fixture against a from-scratch *in-repo* re-implementation of the
same method (single centreline filament per leg, Rosa's closed form, a
brute-force quadrature for the mutual terms). That is a real cross-check of
the new physics, but it is still this repo's own code, written by the same
author, in the same language, against the same understanding of the method --
the correlated-failure mode `docs/design/mom-cross-validation.md` exists to
close. This module is the complementary external check: a genuinely different
codebase, different discretisation, different implementer.

## The external oracle: PyPEEC (not FastHenry)

#1842 named **FastHenry** as its oracle, and #1886 was filed to wire that
binary into CI. The operator ruling on #1886 (2026-09-18) settled the
licensing question `docs/design/em-field-sim-spike.md` had left open, against
FastHenry: it is unpackaged everywhere this repo installs from (Debian/Ubuntu,
Homebrew, PyPI all have no `fasthenry`), and its sources carry MIT RLE's
1990s research notice rather than an OSI license -- *"Permission to use, copy
and modify for internal, noncommercial purposes ... Any distribution of this
program or any part thereof is strictly prohibited without prior written
consent of M.I.T."* -- which forecloses not just vendoring but any future
port. See `docs/design/mom-cross-validation.md`'s "The spiral fixture's
oracle" section for the full ruling.

[PyPEEC](https://github.com/otvam/pypeec) (Dartmouth College, MPL-2.0, JOSS
10.21105/joss.06644) is the permissively-licensed modern equivalent: a 3-D
quasi-magnetostatic **PEEC** solver -- the same method class as `klt mom`'s
inductance path and as FastHenry -- that extracts terminal R/L from a
voxelised geometry via an FFT-accelerated dense operator. It is never
imported by this test module or by any of this repo's own code: only by
`scripts/mom_pypeec_reference.py`, always invoked as its own subprocess (see
that script's module docs).

Requires **both** `klt_mom_native` (this repo's own solver -- see
`docs/cli/mom.md#building-the-native-extension`) and `pypeec` (the external
oracle -- `uv sync --extra mom-pypeec-cross-validation`) to be installed; the
whole module skips with a clear reason when either is missing, exactly like
its sibling `tests/test_mom_cross_validation.py` does for `PyNEC`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.mom import run_mom

pytest.importorskip(
    "klt_mom_native",
    reason=(
        "klt_mom_native is not built -- run `maturin develop --release` in "
        "native/mom/ (see docs/cli/mom.md#building-the-native-extension)"
    ),
)

_PYPEEC_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "mom_pypeec_reference.py"
)

_PYPEEC_PROBE = subprocess.run(
    [sys.executable, "-c", "import pypeec"],
    capture_output=True,
    check=False,
)
if _PYPEEC_PROBE.returncode != 0:
    pytest.skip(
        "pypeec (the external PEEC oracle) is not installed -- run "
        "`uv sync --extra mom-pypeec-cross-validation` (see "
        "docs/design/mom-cross-validation.md)",
        allow_module_level=True,
    )

# The shared benchmark: the same 2-turn square spiral
# `docs/design/mom-validation.md` section 5 and
# `native/mom/src/peec.rs`'s
# `square_spiral_inductance_matches_independent_filament_oracle` already
# treat as canonical for the generalized filament-pair formula (#1842).
# Reusing it rather than inventing a new fixture is deliberate -- it is the
# geometry this repo's own validation and this external cross-check agree to
# compare on.
TURNS = 2
START_SIDE_UM = 60.0
PITCH_GROWTH_UM = 15.0
WIDTH_UM = 2.0
THICKNESS_UM = 2.0
FILAMENT_SIZE_UM = 1.0  # -> 2x2 filaments per leg, matching the Rust fixture
VOXEL_PITCH_UM = 1.0  # PyPEEC's mesh: 2 voxels across the bar, 2 through it
FREQUENCY_HZ = 1.0e8  # quasi-DC (skin depth ~6.6 um >> 2 um) -- see the script
RESISTIVITY_OHM_M = 1.75e-8
CONDUCTIVITY_S_PER_M = 1.0 / RESISTIVITY_OHM_M

# The band the two solvers' spiral inductances must agree inside -- 2%
# (measured: 0.339%), the same tolerance native/mom/src/peec.rs's own spiral
# fixture states against its in-repo oracle. Named rather than inlined so the
# falsifiability control below asserts against *this* number and a future
# retune of it cannot leave the control checking a stale bound. See
# docs/design/mom-cross-validation.md's "Tolerance and metric" section.
AGREEMENT_TOL = 0.02


def _spiral_vertices() -> list[tuple[float, float]]:
    """The square spiral's centreline vertices, in micrometres -- east,
    north, west, south, with the leg length growing by `PITCH_GROWTH_UM`
    after every second leg (see `docs/design/mom-validation.md` section 5)."""
    directions = [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)]
    x, y = 0.0, 0.0
    side = START_SIDE_UM
    vertices = [(x, y)]
    for leg in range(TURNS * 4):
        dx, dy = directions[leg % 4]
        x, y = x + dx * side, y + dy * side
        vertices.append((x, y))
        if leg % 2 == 1:
            side += PITCH_GROWTH_UM
    return vertices


def _leg_boxes_and_signs() -> list[tuple[tuple[float, float, float, float], float]]:
    """Each leg as an axis-aligned box plus the sign correcting for the legs
    whose physical winding direction runs opposite to `klt mom`'s
    box-low-to-high filament convention -- the same external sign correction
    the two-wire "loop" fixtures apply via `L[0][0] + L[1][1] - 2*L[0][1]`,
    generalised past two conductors.

    The eight boxes **tile** the spiral's footprint: every end that meets
    another leg is pushed forward by `WIDTH_UM / 2` along its own direction,
    so each corner's square of metal belongs to exactly one leg (the one
    leaving it) and no two boxes overlap. `scripts/mom_pypeec_reference.py`
    states and applies the identical rule when it builds the PyPEEC voxel
    geometry, so both solvers see the same metal.

    Non-overlap is also a hard requirement of `klt mom`'s own capacitance
    solver, which shares this solve: two conductors whose boxes *intersect*
    present coincident boundary panels and make the potential-coefficient
    matrix singular (`klt mom` detects that and errors out rather than
    returning nonsense). `native/mom/src/peec.rs`'s Rust-only spiral fixture
    can and does overlap its corner boxes because it calls the inductance
    path directly, never the capacitance solve -- hence the small
    corner-metal difference between that fixture's total (0.637718 nH, see
    `docs/design/mom-validation.md` section 5) and this one's.
    """
    vertices = _spiral_vertices()
    out = []
    last = len(vertices) - 2
    step = WIDTH_UM / 2.0
    for index, ((x0, y0), (x1, y1)) in enumerate(
        zip(vertices[:-1], vertices[1:], strict=True)
    ):
        if abs(x1 - x0) > abs(y1 - y0):
            sign = 1.0 if x1 > x0 else -1.0
            a = x0 + (sign * step if index > 0 else 0.0)
            b = x1 + (sign * step if index < last else 0.0)
            box = (min(a, b), y0 - step, max(a, b), y0 + step)
        else:
            sign = 1.0 if y1 > y0 else -1.0
            a = y0 + (sign * step if index > 0 else 0.0)
            b = y1 + (sign * step if index < last else 0.0)
            box = (x0 - step, min(a, b), x0 + step, max(a, b))
        out.append((box, sign))
    return out


def _dbu(value_um: float) -> int:
    return int(round(value_um / 0.001))


def _write_spiral(path: Path, boxes) -> None:
    """One GDS layer per leg: `klt mom` models each leg as its own PEEC
    conductor (multi-box-per-conductor spirals are #1841's separate scope),
    so the solve returns the full leg-by-leg partial-inductance matrix this
    fixture sums."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for index, (x0, y0, x1, y1) in enumerate(boxes):
        top.shapes(layout.layer(index + 1, 0)).insert(
            kdb.Box.new(_dbu(x0), _dbu(y0), _dbu(x1), _dbu(y1))
        )
    layout.write(str(path))


def _write_spec(path: Path, leg_count: int) -> None:
    path.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 5.0,
                "compute_inductance": True,
                "filament_size_um": FILAMENT_SIZE_UM,
                "stackup": [
                    {
                        "layer": f"{index + 1}/0",
                        "conductor": f"leg{index}",
                        "z0_um": 0.0,
                        "z1_um": THICKNESS_UM,
                        "conductivity_S_per_m": CONDUCTIVITY_S_PER_M,
                    }
                    for index in range(leg_count)
                ],
            }
        )
    )


@pytest.fixture(scope="module")
def klt_mom_spiral(tmp_path_factory) -> dict:
    """`klt mom`'s own PEEC solve of the shared spiral benchmark, reduced to
    the one scalar the external oracle also reports: the total inductance of
    the spiral as a single current path, `sum_i sum_j s_i * s_j * L[i][j]`."""
    boxes_and_signs = _leg_boxes_and_signs()
    root = tmp_path_factory.mktemp("mom-pypeec-cross-validation")
    gds = root / "spiral.gds"
    spec = root / "spiral.mom.json"
    _write_spiral(gds, [box for box, _ in boxes_and_signs])
    _write_spec(spec, len(boxes_and_signs))
    report = run_mom(str(gds), str(spec))

    matrix = report["inductance_matrix_nh"]
    signs = [sign for _, sign in boxes_and_signs]
    total_nh = sum(
        signs[i] * signs[j] * matrix[i][j]
        for i in range(len(signs))
        for j in range(len(signs))
    )
    return {
        "total_nh": total_nh,
        "resistance_ohm": sum(report["resistance_ohm"]),
        "filament_count": report["filament_count"],
    }


def _run_pypeec(**overrides: float) -> dict:
    """Solve one spiral with the external PyPEEC oracle, in its own
    subprocess (`pypeec` is never imported by this module -- see the module
    docs). `overrides` perturbs the shared benchmark's geometry, which is how
    the falsifiability control below seeds its defect."""
    request = {
        "turns": TURNS,
        "start_side_um": START_SIDE_UM,
        "pitch_growth_um": PITCH_GROWTH_UM,
        "width_um": WIDTH_UM,
        "thickness_um": THICKNESS_UM,
        "voxel_pitch_um": VOXEL_PITCH_UM,
        "frequency_hz": FREQUENCY_HZ,
        "resistivity_ohm_m": RESISTIVITY_OHM_M,
    }
    request.update(overrides)
    result = subprocess.run(
        [sys.executable, str(_PYPEEC_SCRIPT)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def pypeec_spiral() -> dict:
    """The external PyPEEC oracle's solve of the same benchmark -- see
    `scripts/mom_pypeec_reference.py` for the method (terminal impedance of
    the open spiral at a quasi-DC frequency)."""
    return _run_pypeec()


# --- spiral inductance: klt mom vs the external PyPEEC oracle ----------------


def test_spiral_inductance_matches_pypeec_reference(klt_mom_spiral, pypeec_spiral):
    klt_nh = klt_mom_spiral["total_nh"]
    pypeec_nh = pypeec_spiral["L_nH"]
    rel_err = abs(klt_nh - pypeec_nh) / abs(pypeec_nh)

    print(
        f"\n2-turn square spiral inductance: klt mom PEEC L={klt_nh:.6f} nH "
        f"({klt_mom_spiral['filament_count']} filaments)  PyPEEC (external "
        f"oracle) L={pypeec_nh:.6f} nH "
        f"({pypeec_spiral['voxel_count_used']} conductor voxels of "
        f"{pypeec_spiral['voxel_count_total']}, pypeec "
        f"{pypeec_spiral['pypeec_version']})  rel.err={rel_err * 100:.4f}%"
    )
    assert klt_nh > 0.0, "spiral self-inductance must be positive"
    # AGREEMENT_TOL is 2% (measured: 0.339%) -- the same band
    # native/mom/src/peec.rs's own spiral fixture states against its in-repo
    # oracle, and for the same reason: each solver carries its own
    # discretisation error (klt mom's 2x2 filament bundle per leg vs.
    # PyPEEC's 1 um voxel mesh), and the two budgets can add rather than
    # cancel. See docs/design/mom-cross-validation.md's "Tolerance and
    # metric" section for the derivation.
    assert rel_err < AGREEMENT_TOL, (
        f"klt mom's spiral inductance {klt_nh:.6f} nH should agree with the "
        f"external PyPEEC oracle's {pypeec_nh:.6f} nH (rel.err "
        f"{rel_err * 100:.4f}% exceeds the 2% tolerance)"
    )


def test_spiral_resistance_matches_pypeec_reference(klt_mom_spiral, pypeec_spiral):
    """Secondary metric: the same solve's DC resistance.

    A far looser budget than the inductance check on purpose, because the
    two solvers model the spiral's **corners** differently and are expected
    to disagree slightly. `klt mom` reports the exact 1-D bar resistance of
    each leg summed over the eight legs (`rho * 660 um / 4 um^2` = 2.8875
    ohm, current strictly along each leg's own axis), while PyPEEC solves
    the real 3-D current distribution, in which the current cuts each corner
    diagonally and so travels slightly less than the full centreline length.
    PyPEEC's answer is therefore expected to sit a fraction of a percent
    *below* `klt mom`'s (measured: 0.77% below). This assertion exists to
    catch a gross error -- a wrong conductivity, a dropped leg, a broken
    current path -- not to pin down a tight number.
    """
    klt_ohm = klt_mom_spiral["resistance_ohm"]
    pypeec_ohm = pypeec_spiral["R_ohm"]
    rel_err = abs(klt_ohm - pypeec_ohm) / abs(pypeec_ohm)

    print(
        f"\n2-turn square spiral resistance: klt mom R={klt_ohm:.6f} ohm  "
        f"PyPEEC (external oracle) R={pypeec_ohm:.6f} ohm  "
        f"rel.err={rel_err * 100:.4f}%"
    )
    assert rel_err < 0.10, (
        f"klt mom's spiral resistance {klt_ohm:.6f} ohm should agree with the "
        f"external PyPEEC oracle's {pypeec_ohm:.6f} ohm to within 10% "
        f"(measured {rel_err * 100:.4f}%)"
    )


# --- falsifiability: the comparison can actually fail ------------------------

# The seeded defect: the spiral's innermost side shrinks 60 um -> 45 um (25%),
# every outer turn following it in. A geometry parameter both solvers consume,
# perturbed far enough to move the answer well outside the agreement band --
# the same role `gap_um=1.0` vs `gap_um=1.5` plays in
# tests/test_mom_capacitance_oracle.py's FastCap control. Shrinking rather
# than growing is deliberate: the defective solve meshes to *fewer* voxels
# than the clean one (16744 vs 22684), so the control costs CI less than the
# fixture it guards.
DEFECT_START_SIDE_UM = 45.0


def test_the_comparison_can_actually_fail(klt_mom_spiral):
    """Negative control for the comparison itself: `klt mom`'s answer for the
    *clean* spiral, checked against PyPEEC's answer for a *seeded-defect*
    spiral, must land outside the agreement band.

    Without this, both agreement tests above would pass just as happily if
    the oracle ignored the geometry it was handed and re-solved some cached
    or default spiral -- the two sides would be the same fixture twice and
    "they agree" would be a tautology. This is the evidence that it is a
    falsifiable claim: perturb the geometry on one side only, and the
    comparison notices. Parity with
    tests/test_mom_capacitance_oracle.py::test_the_comparison_can_actually_fail
    (issue #2307).
    """
    pypeec_defect = _run_pypeec(start_side_um=DEFECT_START_SIDE_UM)

    klt_clean_nh = klt_mom_spiral["total_nh"]
    defect_nh = pypeec_defect["L_nH"]
    difference = abs(klt_clean_nh - defect_nh) / abs(defect_nh)

    print(
        f"\nmismatched-geometry control: klt mom (clean, START_SIDE_UM="
        f"{START_SIDE_UM:.1f}) L={klt_clean_nh:.6f} nH vs PyPEEC (defect, "
        f"start_side_um={DEFECT_START_SIDE_UM:.1f}) L={defect_nh:.6f} nH  "
        f"rel.diff={difference * 100:.2f}% (measured: 32.54%; the same "
        f"comparison with the defect removed lands at 0.34%, inside the band)"
    )
    assert difference > AGREEMENT_TOL, (
        f"comparing klt mom's clean spiral answer ({klt_clean_nh:.6f} nH) "
        f"against PyPEEC's seeded-defect answer ({defect_nh:.6f} nH) differed "
        f"by only {difference * 100:.2f}%, inside the "
        f"{AGREEMENT_TOL * 100:.0f}% agreement band -- this comparison cannot "
        "distinguish the two geometries, so the agreement tests above prove "
        "nothing"
    )

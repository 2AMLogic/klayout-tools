"""Cross-validation of `klt mom`'s capacitance matrix against **FastCap
2.0**, an independently implemented Method-of-Moments capacitance solver --
issue #2015, pairing #4 of tracking issue #2007 ("independent
cross-validation oracles for klt verdicts").

`klt mom` is backed by one solver: this repo's own Rust core
(`native/mom/src/solver.rs`). `tests/test_mom.py` pins its *contract* and
`tests/test_mom_validation.py` checks its *numerics* against analytic
closed forms -- but a closed form only exists for a handful of idealised
shapes, so for anything else there has been nothing to ask. FastCap
(Nabors & White, IEEE TCAD 1991) is a second, independent implementation
of exactly this job, and is the very paper `solver.rs` cites as the method
it implements -- with a materially better discretisation (analytic
panel-to-panel integrals plus a multipole-accelerated solve, where
`solver.rs` deliberately uses the bare point-charge kernel between panel
centroids). Agreement between them is therefore evidence about `klt mom`'s
numerics that no amount of self-consistency testing can produce.

`tests/helpers/fastcap_oracle.py` drives FastCap (shell-only, no
PostScript dumps, per this repo's headless-always rule);
`docs/design/fastcap-oracle.md` is the methodology, including the declared
shared surface, the measured agreement, and the tolerance derivations
below. Read that doc before adding or relaxing an assertion here.

## How this tier is gated

Real-binary-gated, exactly like `tests/test_lvs.py`'s netgen tier and
`tests/test_drc_magic_oracle.py`: the whole module skips with a specific
reason when `fastcap` is not on `$PATH` (or the `klt_mom_native` extension
is not built). Absence must never fail CI. ci.yml's `mom` leg is what
provisions both and asserts these tests were *not* skipped there.

## What is compared, and how

Both solvers are handed the **same in-memory conductor geometry** -- the
identical `[{"name", "boxes"}]` request `klt mom`'s own
:func:`~klayout_tools.mom.solve_capacitance_matrix` takes -- and the
exporter reproduces `native/mom/src/geometry.rs`'s face set and panel
subdivision exactly, so both solve the same mesh. What is left to differ
is the kernel and the solve, which is the thing under test.

Entries are compared as **relative differences inside a stated band**,
never for equality: two independently written MoM codes with different
quadrature do not agree bit-for-bit, and asserting that they do would
encode one implementation's discretisation error as the specification.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import klayout.db as kdb
import pytest

from helpers import fastcap_oracle
from helpers.fastcap_oracle import run_fastcap
from klayout_tools.mom import run_mom, solve_capacitance_matrix

pytest.importorskip(
    "klt_mom_native",
    reason=(
        "klt_mom_native is not built -- run `maturin develop --release` in "
        "native/mom/ (see docs/cli/mom.md#building-the-native-extension)"
    ),
)

_ORACLE_SKIP_REASON = fastcap_oracle.oracle_skip_reason()
if _ORACLE_SKIP_REASON:
    pytest.skip(_ORACLE_SKIP_REASON, allow_module_level=True)

#: Vacuum permittivity, F/m (CODATA 2018) -- the same value
#: `native/mom/src/solver.rs` uses, so the analytic anchor below cannot
#: disagree with `klt mom` over a constant.
EPS0_F_PER_M = 8.854_187_812_8e-12

#: Agreement band for a **three-dimensional** fixture whose conductors are
#: separated by at least two panel widths (the coupled-line and shielded
#: fixtures below). Measured on this repo's own geometry at `klt` 0.5.0 /
#: FastCap 2.0: 0.09%-0.17% for the coupled lines, <=1.3% for the shielded
#: triple. 3% leaves room for a different host's arithmetic and for the
#: shielded fixture's flat-ish near field without being loose enough to
#: hide a real kernel regression (the seeded defects below move entries by
#: 19%-39%, an order of magnitude above this band).
AGREEMENT_TOL = 0.03

#: Agreement band for the **flat-lamina** parallel-plate fixture. Two
#: zero-thickness plates one panel-width-and-a-bit apart is the worst case
#: for `solver.rs`'s centroid point-charge off-diagonal kernel -- the panel
#: separation is comparable to the panel size, which is exactly where a
#: point-charge approximation to a panel-to-panel integral is least
#: accurate, and where FastCap's analytic integral is not. Measured 2.8%
#: (self) / 3.3% (coupling); the band is set at 6% rather than at
#: :data:`AGREEMENT_TOL` because this is a *declared* accuracy difference
#: between the two kernels, not a defect -- see
#: docs/design/fastcap-oracle.md's "Where the two solvers disagree most".
FLAT_PLATE_AGREEMENT_TOL = 0.06

#: The panel size every fixture is solved at unless it is refining on
#: purpose. `klt mom`'s own default is 0.5 um (`mom.DEFAULT_PANEL_SIZE_UM`).
PANEL_SIZE_UM = 0.5

#: Relative permittivity of the coupled-line/shielded fixtures: SiO2-like,
#: the value a real interconnect stack would use. The parallel-plate
#: fixture uses 1.0 so its analytic anchor is the textbook vacuum form.
EPS_R = 3.9


# --------------------------------------------------------------------------- #
# Fixture geometry (plain data -- the exact request shape both solvers take)
# --------------------------------------------------------------------------- #


def coupled_lines(gap_um: float = 1.0) -> list[dict[str, Any]]:
    """Two 20 x 2 x 0.6 um coplanar metal bars separated by ``gap_um``.

    The primary known-good fixture: a realistic interconnect cross-section
    (a coupled-line pair), fully three-dimensional, with every conductor
    surface at least two panel widths from every other. ``gap_um`` is the
    seeded-defect knob -- 1.0 um is the correct spacing, 1.5 um is the
    injected spacing error.
    """
    length, width, thickness = 20.0, 2.0, 0.6
    return [
        {
            "name": "a",
            "boxes": [
                {
                    "x0_um": 0.0,
                    "y0_um": 0.0,
                    "x1_um": length,
                    "y1_um": width,
                    "z0_um": 0.0,
                    "z1_um": thickness,
                }
            ],
        },
        {
            "name": "b",
            "boxes": [
                {
                    "x0_um": 0.0,
                    "y0_um": width + gap_um,
                    "x1_um": length,
                    "y1_um": 2 * width + gap_um,
                    "z0_um": 0.0,
                    "z1_um": thickness,
                }
            ],
        },
    ]


def shielded_pair(*, with_shield: bool = True) -> list[dict[str, Any]]:
    """:func:`coupled_lines` over a grounded plane 1 um below it.

    Three conductors, so the comparison covers a full 3x3 matrix rather
    than the 2x2 special case. ``with_shield=False`` is the seeded
    missing-conductor defect: the plane a layout was supposed to have, and
    does not.
    """
    conductors = coupled_lines()
    if with_shield:
        conductors.append(
            {
                "name": "shield",
                "boxes": [
                    {
                        "x0_um": -2.0,
                        "y0_um": -3.0,
                        "x1_um": 22.0,
                        "y1_um": 8.0,
                        "z0_um": -1.6,
                        "z1_um": -1.0,
                    }
                ],
            }
        )
    return conductors


#: Square parallel-plate fixture: two coincident 10 x 10 um zero-thickness
#: laminae 1 um apart, the same idealised-plate geometry
#: `tests/test_mom_validation.py` checks against `eps_0 A / d`.
PLATE_SIDE_UM = 10.0
PLATE_GAP_UM = 1.0


def parallel_plates() -> list[dict[str, Any]]:
    footprint = {
        "x0_um": 0.0,
        "y0_um": 0.0,
        "x1_um": PLATE_SIDE_UM,
        "y1_um": PLATE_SIDE_UM,
    }
    return [
        {
            "name": "top",
            "boxes": [{**footprint, "z0_um": PLATE_GAP_UM, "z1_um": PLATE_GAP_UM}],
        },
        {
            "name": "bottom",
            "boxes": [{**footprint, "z0_um": 0.0, "z1_um": 0.0}],
        },
    ]


def ideal_parallel_plate_ff(area_um2: float, gap_um: float, eps_r: float) -> float:
    """`eps_r eps_0 A / d` in femtofarads -- the textbook infinite-plate
    result, which for *finite* plates is a strict lower bound (the fringing
    field outside the plate edges can only add capacitance). Same oracle,
    same conversion factor, as `tests/test_mom_validation.py`."""
    return EPS0_F_PER_M * eps_r * (area_um2 / gap_um) * 1e9


# --------------------------------------------------------------------------- #
# Comparison helpers
# --------------------------------------------------------------------------- #


def relative_difference(klt_value: float, oracle_value: float) -> float:
    """``|klt - oracle| / |oracle|`` -- FastCap is the denominator because
    it is the reference, not because it is exact."""
    return abs(klt_value - oracle_value) / abs(oracle_value)


def compare_matrices(
    label: str,
    klt_report: dict[str, Any],
    oracle: fastcap_oracle.FastCapResult,
    tolerance: float,
) -> dict[tuple[str, str], float]:
    """Assert every Maxwell-matrix entry agrees within ``tolerance``,
    printing the full side-by-side table so a green run records the
    measured agreement (not only a failing one)."""
    names = list(klt_report["conductors"])
    assert names == list(oracle.conductors), (
        f"{label}: conductor order differs ({names} vs {list(oracle.conductors)})"
    )
    assert klt_report["panel_count"] == oracle.panel_count, (
        f"{label}: klt mom discretised {klt_report['panel_count']} panels, the "
        f"oracle exported {oracle.panel_count} -- the two solved different meshes"
    )

    print(f"\n{label}: {len(names)} conductors, {oracle.panel_count} panels")
    worst = 0.0
    differences: dict[tuple[str, str], float] = {}
    for i, row_name in enumerate(names):
        for j, column_name in enumerate(names):
            klt_value = klt_report["capacitance_matrix_ff"][i][j]
            oracle_value = oracle.entry_ff(row_name, column_name)
            difference = relative_difference(klt_value, oracle_value)
            differences[(row_name, column_name)] = difference
            worst = max(worst, difference)
            print(
                f"    C[{row_name}][{column_name}]  klt mom {klt_value:+.6f} fF  "
                f"FastCap {oracle_value:+.6f} fF  rel.diff {difference * 100:.4f}%"
            )
    print(
        f"    worst-case relative difference: {worst * 100:.4f}% (band {tolerance:.0%})"
    )
    assert worst < tolerance, (
        f"{label}: worst-case klt mom vs FastCap difference {worst * 100:.4f}% "
        f"exceeds the {tolerance:.0%} band -- see docs/design/fastcap-oracle.md"
    )
    return differences


def solve_both(
    conductors: list[dict[str, Any]],
    tmp_path: Path,
    *,
    eps_r: float = EPS_R,
    panel_size_um: float = PANEL_SIZE_UM,
) -> tuple[dict[str, Any], fastcap_oracle.FastCapResult]:
    """`klt mom`'s own solve and FastCap's solve of the *same* geometry."""
    klt_report = solve_capacitance_matrix(
        conductors, eps_r, panel_size_um=panel_size_um
    )
    oracle = run_fastcap(
        conductors,
        background_permittivity=eps_r,
        panel_size_um=panel_size_um,
        work_dir=tmp_path,
    )
    return klt_report, oracle


# --------------------------------------------------------------------------- #
# Known-good cases (#2007 criterion 3, first half)
# --------------------------------------------------------------------------- #


def test_coupled_lines_agree_with_fastcap(tmp_path):
    """The primary known-good case: a realistic coupled-line pair, where
    both solvers must agree on the full Maxwell matrix -- self *and*
    coupling terms, sign convention included."""
    conductors = coupled_lines()
    klt_report, oracle = solve_both(conductors, tmp_path)

    differences = compare_matrices(
        "coupled lines (20 x 2 x 0.6 um, 1.0 um apart, eps_r 3.9)",
        klt_report,
        oracle,
        AGREEMENT_TOL,
    )
    # Both must agree on the *coupling*, not just the easier self terms:
    # a solver could get the diagonal roughly right from panel areas alone.
    assert differences[("a", "b")] < AGREEMENT_TOL

    # Same Maxwell (short-circuit) convention on both sides: positive
    # diagonal, negative off-diagonal. FastCap warns on stdout if its own
    # matrix violates this, and `run_fastcap` keeps the whole log.
    for i, name in enumerate(klt_report["conductors"]):
        assert klt_report["capacitance_matrix_ff"][i][i] > 0
        assert oracle.entry_ff(name, name) > 0
        for other in klt_report["conductors"]:
            if other != name:
                assert oracle.entry_ff(name, other) < 0


def test_parallel_plate_agrees_with_fastcap_and_the_analytic_bound(tmp_path):
    """The historically-known case #2007's criterion 3 asks for: a square
    parallel-plate capacitor, checked against both FastCap *and* the
    textbook `eps_0 A / d`.

    The closed form is a strict lower bound for finite plates, so the
    assertion is three-way: both solvers exceed it (fringing exists), both
    exceed it by a similar margin (the fringing is the same size), and the
    two agree with each other inside the flat-lamina band.
    """
    conductors = parallel_plates()
    klt_report, oracle = solve_both(
        conductors, tmp_path, eps_r=1.0, panel_size_um=PANEL_SIZE_UM
    )

    compare_matrices(
        f"parallel plates ({PLATE_SIDE_UM} um square, {PLATE_GAP_UM} um apart, vacuum)",
        klt_report,
        oracle,
        FLAT_PLATE_AGREEMENT_TOL,
    )

    ideal_ff = ideal_parallel_plate_ff(PLATE_SIDE_UM**2, PLATE_GAP_UM, 1.0)
    klt_coupling = abs(klt_report["capacitance_matrix_ff"][0][1])
    oracle_coupling = abs(oracle.entry_ff("top", "bottom"))
    print(
        f"\n    ideal eps_0 A / d = {ideal_ff:.6f} fF   "
        f"klt mom / ideal = {klt_coupling / ideal_ff:.4f}   "
        f"FastCap / ideal = {oracle_coupling / ideal_ff:.4f}"
    )

    # Fringing is additive: neither solver may land below the closed form.
    assert klt_coupling > ideal_ff
    assert oracle_coupling > ideal_ff
    # ...and neither may be absurdly above it: at d/L = 0.1 the fringing
    # excess is tens of percent, not multiples. Both measured ~15%-19%.
    assert klt_coupling < 1.5 * ideal_ff
    assert oracle_coupling < 1.5 * ideal_ff


def test_shielded_triple_agrees_with_fastcap(tmp_path):
    """Three conductors, so the agreement claim covers a full 3x3 matrix
    (two coupled lines over a grounded plane) and not just the 2x2 case --
    including each line's much larger capacitance to the shield."""
    conductors = shielded_pair()
    klt_report, oracle = solve_both(conductors, tmp_path)

    compare_matrices(
        "coupled lines over a grounded plane (3 conductors)",
        klt_report,
        oracle,
        AGREEMENT_TOL,
    )

    # Physical sanity both solvers must independently reproduce: with a
    # ground plane 1 um below, each line couples far harder to the shield
    # than to its neighbour.
    for report_entry in (
        lambda a, b: abs(
            klt_report["capacitance_matrix_ff"][klt_report["conductors"].index(a)][
                klt_report["conductors"].index(b)
            ]
        ),
        lambda a, b: abs(oracle.entry_ff(a, b)),
    ):
        assert report_entry("a", "shield") > 2 * report_entry("a", "b")


def test_klt_mom_file_path_matches_the_in_memory_path_and_the_oracle(tmp_path):
    """The same comparison through `klt mom`'s *file* entry point: a GDS
    layout plus a stackup spec, read by :func:`klayout_tools.mom.run_mom`.

    This is what ties the oracle to the shipped command rather than to a
    library call. The GDS round trip is proved lossless by requiring
    `run_mom`'s matrix to equal the in-memory solve's *exactly* (same
    solver, same geometry, so anything but equality means the layout or
    the stackup spec did not reproduce the boxes), and the oracle then
    validates that shared answer.
    """
    conductors = coupled_lines()
    gds_path = tmp_path / "coupled_lines.gds"
    spec_path = tmp_path / "coupled_lines.mom.json"
    _write_fixture_gds(conductors, gds_path)
    _write_fixture_spec(conductors, spec_path, eps_r=EPS_R)

    file_report = run_mom(str(gds_path), str(spec_path))
    memory_report = solve_capacitance_matrix(
        conductors, EPS_R, panel_size_um=PANEL_SIZE_UM
    )

    assert file_report["conductors"] == memory_report["conductors"]
    assert file_report["panel_count"] == memory_report["panel_count"]
    assert (
        file_report["capacitance_matrix_ff"] == memory_report["capacitance_matrix_ff"]
    )

    oracle = run_fastcap(
        conductors,
        background_permittivity=EPS_R,
        panel_size_um=PANEL_SIZE_UM,
        work_dir=tmp_path,
    )
    compare_matrices(
        "klt mom (GDS + stackup spec) vs FastCap", file_report, oracle, AGREEMENT_TOL
    )


# --------------------------------------------------------------------------- #
# Refinement: the two solvers converge *together* (not just agree once)
# --------------------------------------------------------------------------- #


def test_agreement_improves_under_refinement(tmp_path):
    """Agreement at one mesh could be a coincidence. This refines the same
    fixture over four meshes and requires the two solvers to behave like
    two discretisations of the same physics:

    - they move in the same direction at every refinement step, and
    - the finest mesh agrees far better than the coarsest.

    Deliberately *not* asserted: that the disagreement shrinks
    monotonically. It does not, and the measured sequence below shows why
    -- the two solvers approach the answer from the same side at different
    rates and cross over between 0.5 um and 0.25 um. Asserting monotonicity
    would have meant picking the mesh sequence that produced it, which is
    the sort of tuning this oracle exists to make unnecessary. See
    docs/design/fastcap-oracle.md's "Behaviour under refinement".
    """
    conductors = coupled_lines()
    panel_sizes = [2.0, 1.0, 0.5, 0.25]

    klt_coupling: list[float] = []
    oracle_coupling: list[float] = []
    differences: list[float] = []
    print("\ncoupled-line coupling capacitance under refinement:")
    for panel_size_um in panel_sizes:
        work = tmp_path / f"panel-{panel_size_um}"
        klt_report, oracle = solve_both(conductors, work, panel_size_um=panel_size_um)
        klt_value = klt_report["capacitance_matrix_ff"][0][1]
        oracle_value = oracle.entry_ff("a", "b")
        klt_coupling.append(klt_value)
        oracle_coupling.append(oracle_value)
        differences.append(relative_difference(klt_value, oracle_value))
        print(
            f"    panel {panel_size_um:5.2f} um  panels {oracle.panel_count:5d}  "
            f"klt mom {klt_value:+.6f} fF  FastCap {oracle_value:+.6f} fF  "
            f"rel.diff {differences[-1] * 100:.4f}%"
        )

    # Same direction at every step: two discretisations of one problem
    # approaching one limit, not two codes drifting apart.
    for index in range(1, len(panel_sizes)):
        klt_step = klt_coupling[index] - klt_coupling[index - 1]
        oracle_step = oracle_coupling[index] - oracle_coupling[index - 1]
        assert klt_step * oracle_step > 0, (
            f"refining from {panel_sizes[index - 1]} to {panel_sizes[index]} um "
            f"moved klt mom by {klt_step:+.6f} fF and FastCap by "
            f"{oracle_step:+.6f} fF -- opposite directions"
        )

    assert differences[-1] < differences[0] / 4.0, (
        f"refining 8x improved agreement only from {differences[0] * 100:.4f}% "
        f"to {differences[-1] * 100:.4f}%"
    )
    # Every mesh from 1.0 um down is inside the band this module's other
    # tests use, so the tolerance is not an artefact of one lucky mesh.
    for panel_size_um, difference in zip(panel_sizes, differences, strict=True):
        if panel_size_um <= 1.0:
            assert difference < AGREEMENT_TOL, (
                f"panel {panel_size_um} um: {difference * 100:.4f}% exceeds the band"
            )


# --------------------------------------------------------------------------- #
# Seeded defects (#2007 criterion 3, load-bearing half)
# --------------------------------------------------------------------------- #


def test_seeded_spacing_error_is_detected_by_both_solvers(tmp_path):
    """A seeded geometry defect: the coupled lines drawn 1.5 um apart when
    they should be 1.0 um apart.

    Both solvers must see it, agree on the defective geometry too, and --
    the load-bearing part -- the defect's signature must be far larger than
    their disagreement with each other. An oracle whose noise floor is the
    size of the error it is meant to catch proves nothing.
    """
    clean = coupled_lines(gap_um=1.0)
    defective = coupled_lines(gap_um=1.5)

    klt_clean, oracle_clean = solve_both(clean, tmp_path / "clean")
    klt_defect, oracle_defect = solve_both(defective, tmp_path / "defect")

    compare_matrices(
        "seeded spacing defect (1.5 um instead of 1.0 um)",
        klt_defect,
        oracle_defect,
        AGREEMENT_TOL,
    )

    klt_shift = relative_difference(
        klt_defect["capacitance_matrix_ff"][0][1],
        klt_clean["capacitance_matrix_ff"][0][1],
    )
    oracle_shift = relative_difference(
        oracle_defect.entry_ff("a", "b"), oracle_clean.entry_ff("a", "b")
    )
    print(
        f"\n    seeded 0.5 um spacing error moves the coupling by "
        f"{klt_shift * 100:.2f}% (klt mom) / {oracle_shift * 100:.2f}% (FastCap)"
    )

    # Both detected it, in the same direction (wider gap => less coupling).
    assert abs(klt_defect["capacitance_matrix_ff"][0][1]) < abs(
        klt_clean["capacitance_matrix_ff"][0][1]
    )
    assert abs(oracle_defect.entry_ff("a", "b")) < abs(oracle_clean.entry_ff("a", "b"))
    # Both by the same amount, and by far more than they disagree.
    assert relative_difference(klt_shift, oracle_shift) < 0.1
    assert klt_shift > 5 * AGREEMENT_TOL
    assert oracle_shift > 5 * AGREEMENT_TOL


def test_seeded_missing_conductor_is_detected_by_both_solvers(tmp_path):
    """The other seeded defect #2007's criterion 3 names: a conductor that
    should be there and is not -- the ground plane under the coupled pair.

    Both solvers must report the large drop in each line's self-capacitance
    and the *rise* in line-to-line coupling that losing the shield causes,
    and must still agree with each other on the defective geometry.
    """
    shielded = shielded_pair(with_shield=True)
    unshielded = shielded_pair(with_shield=False)

    klt_shielded, oracle_shielded = solve_both(shielded, tmp_path / "shielded")
    klt_unshielded, oracle_unshielded = solve_both(unshielded, tmp_path / "unshielded")

    compare_matrices(
        "seeded missing-conductor defect (ground plane deleted)",
        klt_unshielded,
        oracle_unshielded,
        AGREEMENT_TOL,
    )

    klt_self_shift = relative_difference(
        klt_unshielded["capacitance_matrix_ff"][0][0],
        klt_shielded["capacitance_matrix_ff"][0][0],
    )
    oracle_self_shift = relative_difference(
        oracle_unshielded.entry_ff("a", "a"), oracle_shielded.entry_ff("a", "a")
    )
    print(
        f"\n    deleting the ground plane moves C[a][a] by "
        f"{klt_self_shift * 100:.2f}% (klt mom) / "
        f"{oracle_self_shift * 100:.2f}% (FastCap)"
    )

    # Losing the shield removes the dominant capacitance path...
    assert (
        klt_unshielded["capacitance_matrix_ff"][0][0]
        < klt_shielded["capacitance_matrix_ff"][0][0]
    )
    assert oracle_unshielded.entry_ff("a", "a") < oracle_shielded.entry_ff("a", "a")
    # ...and strengthens line-to-line coupling, which both must show.
    assert abs(klt_unshielded["capacitance_matrix_ff"][0][1]) > abs(
        klt_shielded["capacitance_matrix_ff"][0][1]
    )
    assert abs(oracle_unshielded.entry_ff("a", "b")) > abs(
        oracle_shielded.entry_ff("a", "b")
    )
    # Same magnitude on both sides, far above the agreement band.
    assert relative_difference(klt_self_shift, oracle_self_shift) < 0.1
    assert klt_self_shift > 5 * AGREEMENT_TOL


def test_the_comparison_can_actually_fail(tmp_path):
    """Negative control for the comparison itself: `klt mom`'s answer for
    the *clean* geometry, checked against FastCap's answer for the
    *defective* one, must land outside the band.

    Without this, every passing test above would also pass if the exporter
    silently ignored its input and FastCap re-solved some cached geometry.
    It is the evidence that "they agree" is a falsifiable claim on these
    fixtures and not a tautology.
    """
    klt_clean = solve_capacitance_matrix(
        coupled_lines(gap_um=1.0), EPS_R, panel_size_um=PANEL_SIZE_UM
    )
    oracle_defect = run_fastcap(
        coupled_lines(gap_um=1.5),
        background_permittivity=EPS_R,
        panel_size_um=PANEL_SIZE_UM,
        work_dir=tmp_path,
    )
    difference = relative_difference(
        klt_clean["capacitance_matrix_ff"][0][1], oracle_defect.entry_ff("a", "b")
    )
    print(f"\n    mismatched-geometry control: rel.diff {difference * 100:.2f}%")
    assert difference > AGREEMENT_TOL, (
        "comparing klt mom's clean answer against FastCap's defective one "
        f"differed by only {difference * 100:.2f}% -- this comparison cannot "
        "distinguish the two geometries, so the agreement tests above prove "
        "nothing"
    )


# --------------------------------------------------------------------------- #
# Provenance (#2007 criterion 4) and the "did it really run" guards
# --------------------------------------------------------------------------- #


def test_oracle_provenance_records_versions_and_input_hashes(tmp_path):
    """Both stacks' versions, both stacks' settings, and a hash of every
    input -- what makes a future disagreement attributable rather than
    ambiguous."""
    conductors = coupled_lines()
    gds_path = tmp_path / "coupled_lines.gds"
    spec_path = tmp_path / "coupled_lines.mom.json"
    _write_fixture_gds(conductors, gds_path)
    _write_fixture_spec(conductors, spec_path, eps_r=EPS_R)

    klt_report = run_mom(str(gds_path), str(spec_path))
    oracle = run_fastcap(
        conductors,
        background_permittivity=EPS_R,
        panel_size_um=PANEL_SIZE_UM,
        work_dir=tmp_path,
    )
    provenance = fastcap_oracle.oracle_provenance(
        oracle=oracle,
        klt_report=klt_report,
        layout_path=str(gds_path),
        spec_path=str(spec_path),
    )
    print("\noracle provenance:\n" + json.dumps(provenance, indent=2))

    assert provenance["oracle"]["tool"] == "fastcap"
    # A real version string out of the binary's own banner, not a constant.
    assert provenance["oracle"]["version"] == oracle.version
    assert provenance["oracle"]["version"].startswith("2.0")
    assert provenance["oracle"]["input"]["content_hash"].startswith("sha256:")
    assert provenance["oracle"]["input"]["panel_count"] == oracle.panel_count
    assert provenance["oracle"]["settings"]["background_permittivity"] == EPS_R

    for key in ("layout", "spec"):
        assert provenance["input"][key]["content_hash"].startswith("sha256:")

    klt_block = provenance["klt"]
    assert klt_block["klayout_version"]
    assert klt_block["klt_mom_native_source_fingerprint"]
    assert klt_block["schema_version"] == klt_report["schema_version"]
    # The load-bearing line: both solvers meshed the same geometry at the
    # same panel size, which is what makes the two matrices comparable.
    assert klt_block["panel_count"] == provenance["oracle"]["input"]["panel_count"]
    assert klt_block["panel_size_um"] == oracle.panel_size_um
    assert klt_block["background_permittivity"] == oracle.background_permittivity


def test_oracle_reports_evidence_the_solve_actually_ran(tmp_path):
    """Per #2007 criterion 2: the oracle result carries evidence FastCap
    solved *this* problem -- its own panel and conductor counts, and one
    solved column per conductor -- not just an exit status."""
    conductors = shielded_pair()
    oracle = run_fastcap(
        conductors,
        background_permittivity=EPS_R,
        panel_size_um=PANEL_SIZE_UM,
        work_dir=tmp_path,
    )
    assert oracle.reported_panel_count == oracle.panel_count
    assert oracle.reported_conductor_count == len(conductors)
    assert oracle.solved_columns == (1, 2, 3)
    assert "CAPACITANCE MATRIX" in oracle.log
    assert Path(oracle.qui_path).is_file()
    assert Path(oracle.list_path).is_file()


def test_oracle_refuses_geometry_it_cannot_export_faithfully(tmp_path):
    """Fail closed on the cases where the `.qui` export would silently mean
    something different from what `klt mom` solved: boxes that touch (the
    two panel models differ about interior faces) and conductor names that
    collide once exported."""
    touching = [
        {
            "name": "a",
            "boxes": [
                {
                    "x0_um": 0.0,
                    "y0_um": 0.0,
                    "x1_um": 10.0,
                    "y1_um": 2.0,
                    "z0_um": 0.0,
                    "z1_um": 0.6,
                }
            ],
        },
        {
            "name": "b",
            "boxes": [
                {
                    "x0_um": 0.0,
                    "y0_um": 2.0,
                    "x1_um": 10.0,
                    "y1_um": 4.0,
                    "z0_um": 0.0,
                    "z1_um": 0.6,
                }
            ],
        },
    ]
    with pytest.raises(fastcap_oracle.FastCapOracleError, match="overlap or touch"):
        fastcap_oracle.write_qui(tmp_path / "touching.qui", touching, PANEL_SIZE_UM)

    colliding = coupled_lines()
    # A name FastCap cannot carry (whitespace is its field separator) is
    # exported as `cond<i>` -- which collides here with a second conductor
    # literally called `cond2`. Exporting both would silently merge two
    # conductors into one, so it must raise instead.
    colliding[0]["name"] = "cond2"
    colliding[1]["name"] = "net two"
    with pytest.raises(fastcap_oracle.FastCapOracleError, match="names collide"):
        fastcap_oracle.write_qui(tmp_path / "colliding.qui", colliding, PANEL_SIZE_UM)

    # An unsafe name on its own is fine: it is exported under a generated
    # one, and the result still reports (and is addressable by) the name
    # `klt mom` used.
    renamed = coupled_lines()
    renamed[0]["name"] = "net one"
    panel_count, export_names = fastcap_oracle.write_qui(
        tmp_path / "renamed.qui", renamed, PANEL_SIZE_UM
    )
    assert export_names == ["cond1", "b"]
    oracle = run_fastcap(
        renamed,
        background_permittivity=EPS_R,
        panel_size_um=PANEL_SIZE_UM,
        work_dir=tmp_path / "renamed",
    )
    assert oracle.conductors == ("net one", "b")
    assert oracle.panel_count == panel_count
    assert oracle.entry_ff("net one", "b") < 0


def test_exported_mesh_matches_the_rust_discretisation(tmp_path):
    """The exporter reproduces `native/mom/src/geometry.rs`'s panel count
    exactly, for a flat lamina (one face) and a box (six faces), at three
    panel sizes -- the check that keeps "same mesh" true as either side
    changes."""
    for conductors in (parallel_plates(), coupled_lines(), shielded_pair()):
        for panel_size_um in (2.0, 1.0, 0.5):
            klt_report = solve_capacitance_matrix(
                conductors, EPS_R, panel_size_um=panel_size_um
            )
            exported, names = fastcap_oracle.write_qui(
                tmp_path / "mesh.qui", conductors, panel_size_um
            )
            assert names == [c["name"] for c in conductors]
            assert exported == klt_report["panel_count"], (
                f"panel {panel_size_um} um: exporter wrote {exported} panels, "
                f"klt mom discretised {klt_report['panel_count']}"
            )


@pytest.mark.parametrize("thickness_um", [0.0, 1.0])
@pytest.mark.parametrize(
    ("extent_um", "expected_segments"),
    [
        (math.nextafter(2.5, 0.0), 2),
        (2.5, 3),
        (math.nextafter(2.5, math.inf), 3),
        (math.nextafter(3.5, 0.0), 3),
        (3.5, 4),
        (math.nextafter(3.5, math.inf), 4),
        (4.5, 5),
    ],
)
def test_exported_mesh_matches_rust_at_half_panel_boundaries(
    tmp_path, extent_um, expected_segments, thickness_um
):
    """Rust rounds half away from zero; Python's round uses ties-to-even.

    Cross-check actual native panel counts for laminae and six-face boxes,
    including adjacent floats so a shifted rounding threshold is caught.
    """
    conductors = [
        {
            "name": "plate",
            "boxes": [
                {
                    "x0_um": 0.0,
                    "y0_um": 0.0,
                    "x1_um": extent_um,
                    "y1_um": 1.0,
                    "z0_um": 0.0,
                    "z1_um": thickness_um,
                }
            ],
        }
    ]
    assert fastcap_oracle.segment_count(extent_um, 1.0) == expected_segments
    klt_report = solve_capacitance_matrix(conductors, 1.0, panel_size_um=1.0)
    exported, _ = fastcap_oracle.write_qui(tmp_path / "boundary.qui", conductors, 1.0)
    expected_panels = (
        expected_segments if thickness_um == 0.0 else 4 * expected_segments + 2
    )
    assert exported == klt_report["panel_count"] == expected_panels


# --------------------------------------------------------------------------- #
# Fixture serialisation (GDS + stackup spec) for the `run_mom` file path
# --------------------------------------------------------------------------- #


def _write_fixture_gds(conductors: list[dict[str, Any]], path: Path) -> None:
    """Write each conductor's single box onto its own GDS layer, so
    :func:`klayout_tools.mom.run_mom` reconstructs exactly the boxes the
    in-memory request carries (the z-extent comes from the stackup spec)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for index, conductor in enumerate(conductors, start=1):
        for box in conductor["boxes"]:
            top.shapes(layout.layer(index, 0)).insert(
                kdb.Box.new(
                    int(round(box["x0_um"] / layout.dbu)),
                    int(round(box["y0_um"] / layout.dbu)),
                    int(round(box["x1_um"] / layout.dbu)),
                    int(round(box["y1_um"] / layout.dbu)),
                )
            )
    layout.write(str(path))


def _write_fixture_spec(
    conductors: list[dict[str, Any]], path: Path, *, eps_r: float
) -> None:
    stackup = []
    for index, conductor in enumerate(conductors, start=1):
        box = conductor["boxes"][0]
        stackup.append(
            {
                "layer": f"{index}/0",
                "conductor": conductor["name"],
                "z0_um": box["z0_um"],
                "z1_um": box["z1_um"],
            }
        )
    path.write_text(
        json.dumps(
            {
                "background_permittivity": eps_r,
                "panel_size_um": PANEL_SIZE_UM,
                "stackup": stackup,
            }
        ),
        encoding="utf-8",
    )

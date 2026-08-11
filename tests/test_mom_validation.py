"""Closed-form validation and convergence-under-refinement for `klt mom`
(issue #719, Phase 1 of the Method-of-Moments epic #701).

`tests/test_mom.py` (issue #718) covers the command's *contract*: it produces
a numeric capacitance matrix, with the right shape, signs, warnings and error
paths. This module covers its *numerics*: every capacitance the solver
returns here is checked against an analytic oracle, and the discretisation is
refined to show the error actually shrinks.

Every assertion below cites the oracle it is checking against:

- **Parallel plate** -- `C = eps_r eps_0 A / d`, the textbook ideal
  parallel-plate capacitor (no fringing). For finite plates this is a
  *lower* bound: the fringing field outside the plate edges can only add
  capacitance, and the excess vanishes as `d/L -> 0`. Both facts are
  asserted.
- **Parallel plate with fringing** -- Kirchhoff's 1877 asymptotic for a
  circular-disk capacitor of radius `a` and separation `d`,
  `C = eps pi a^2 / d * [1 + (d / (pi a)) (ln(16 pi a / d) - 1)]`,
  evaluated for the disk of the *same area* as the square fixture. A
  shape-substituted oracle (a square has more perimeter than an equal-area
  disk, so the true square value sits slightly above it), so it is checked
  at a deliberately looser tolerance than the ideal formula's bound.
- **Square coaxial line** -- the coaxial closed form
  `C' = 2 pi eps / ln(R_outer / R_inner)` per unit length, with the two
  radii replaced by the exact conformal-mapping equivalent radii of a
  square cross-section (see `SQUARE_LOG_CAPACITY` /
  `SQUARE_CONFORMAL_RADIUS` below). Because `klt mom` is a fully 3-D
  electrostatic solve rather than a 2-D per-unit-length one, the
  per-unit-length capacitance is extracted by differencing two lengths of
  the same cross-section, which cancels the (length-independent) end
  effects.
- **Enclosure** -- for a conductor fully surrounded by a shield, every field
  line from the inner conductor terminates on the shield, so the Maxwell
  matrix satisfies `C_inner,inner = -C_inner,outer`.
- **Permittivity linearity** -- the Laplace problem is linear in `eps`, so
  `C(eps_r) = eps_r * C(1)` exactly.

Requires the `klt_mom_native` Rust extension (see
`docs/cli/mom.md#building-the-native-extension`); like `tests/test_mom.py`,
the whole module skips with a clear reason when it is not built rather than
silently passing. `docs/design/mom-validation.md` records the measured
numbers, the oracles, and the tolerances chosen here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from types import SimpleNamespace

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.mom import run_mom

pytest.importorskip(
    "klt_mom_native",
    reason=(
        "klt_mom_native is not built -- run `maturin develop --release` in "
        "native/mom/ (see docs/cli/mom.md#building-the-native-extension)"
    ),
)

# --- physical constants and analytic oracles --------------------------------

#: Vacuum permittivity, F/m (CODATA 2018) -- the same value
#: `native/mom/src/solver.rs` uses, so an oracle mismatch here can only come
#: from the discretisation/kernel, never from a constant disagreeing across
#: the language boundary.
EPS0_F_PER_M = 8.854_187_812_8e-12

#: Exterior logarithmic capacity (transfinite diameter) of a square of unit
#: side: `Gamma(1/4)^2 / (4 pi^(3/2))` ~= 0.590170. Classical conformal-mapping
#: result -- the radius of the circular cylinder whose *external* field is
#: asymptotically identical to that of a square cylinder of that side.
SQUARE_LOG_CAPACITY = math.gamma(0.25) ** 2 / (4.0 * math.pi**1.5)

#: Interior conformal radius, at its centre, of a square of unit side:
#: `4 sqrt(pi) / Gamma(1/4)^2` ~= 0.539326. From the Schwarz-Christoffel map of
#: the unit disk onto a square (`f(z) = int dw / sqrt(1 - w^4)`, whose
#: prevertices map to the square's *corners*, giving half-diagonal
#: `Gamma(1/4)^2 / (4 sqrt(2 pi))` for `|f'(0)| = 1`). This is the radius of
#: the circular cylinder whose *internal* field is asymptotically identical to
#: that of a square cavity of that side.
SQUARE_CONFORMAL_RADIUS = 4.0 * math.sqrt(math.pi) / math.gamma(0.25) ** 2


def parallel_plate_ff(area_um2: float, gap_um: float, eps_r: float = 1.0) -> float:
    """Ideal parallel-plate capacitance `eps_r eps_0 A / d`, in femtofarads,
    for an area in um^2 and a gap in um.

    Oracle: the textbook infinite-plate result (no fringing). For finite
    plates it is a strict lower bound on the true capacitance.
    """
    # A [um^2] -> m^2 is 1e-12, d [um] -> m is 1e-6, F -> fF is 1e15;
    # 1e-12 / 1e-6 * 1e15 = 1e9.
    return EPS0_F_PER_M * eps_r * (area_um2 / gap_um) * 1e9


def kirchhoff_disk_ff(area_um2: float, gap_um: float, eps_r: float = 1.0) -> float:
    """Kirchhoff's (1877) fringing-corrected capacitance of a circular-disk
    capacitor, in femtofarads, evaluated for the disk with the same area as
    the fixture:

        C = eps pi a^2 / d * [1 + (d / (pi a)) * (ln(16 pi a / d) - 1)]

    Oracle caveat: this is a *shape-substituted* oracle -- a square plate has
    more perimeter than the equal-area disk, so the true square capacitance
    sits slightly above this value. Used only as a cross-check on the
    fringing magnitude, at a correspondingly looser tolerance than the ideal
    formula's bound.
    """
    radius_um = math.sqrt(area_um2 / math.pi)
    correction = 1.0 + (gap_um / (math.pi * radius_um)) * (
        math.log(16.0 * math.pi * radius_um / gap_um) - 1.0
    )
    return parallel_plate_ff(area_um2, gap_um, eps_r) * correction


def square_coax_ff_per_um(
    inner_side_um: float, outer_side_um: float, eps_r: float = 1.0
) -> float:
    """Capacitance per unit length (fF/um) of a square coaxial line, from the
    coaxial closed form `C' = 2 pi eps / ln(R_outer / R_inner)` with the exact
    conformal-mapping equivalent radii of the two square cross-sections.

    The shape factor collapses to a constant:
    `R_outer / R_inner = (16 pi^2 / Gamma(1/4)^4) * (b / a)`, i.e. ~0.9139 b/a
    -- a square coax is within ~9% (inside the logarithm) of a circular coax
    with the same side-to-diameter ratio. Asymptotically exact as `b/a`
    grows; at the `b/a = 3` used here it is the dominant term of the oracle's
    own error budget, which is why the tolerance below is 2% rather than
    0.1%.
    """
    ratio = (SQUARE_CONFORMAL_RADIUS * outer_side_um) / (
        SQUARE_LOG_CAPACITY * inner_side_um
    )
    # 2 pi eps [F/m] -> fF/um: 1e-6 m/um * 1e15 fF/F = 1e9.
    return 2.0 * math.pi * EPS0_F_PER_M * eps_r * 1e9 / math.log(ratio)


# --- convergence analysis ----------------------------------------------------


@dataclass
class ConvergenceReport:
    """Three-mesh Richardson analysis of one quantity solved on successively
    refined meshes."""

    values: list[float]
    differences: list[float]
    observed_order: float
    limit: float
    relative_errors: list[float] = field(default_factory=list)

    @property
    def converged(self) -> bool:
        """True only if every refinement step moved the answer strictly less
        than the step before it *and* the implied order is at least
        first-order. A solver whose answer keeps moving by as much (or more)
        with each refinement has not converged and must not be accepted --
        see issue #719's "a non-converging solver is not accepted"."""
        shrinking = all(
            abs(later) < abs(earlier)
            for earlier, later in zip(
                self.differences, self.differences[1:], strict=False
            )
        )
        # The `- 1e-9` absorbs floating-point round-off in the log ratio: an
        # exactly-first-order sequence lands a few ulp below 1.0.
        return shrinking and self.observed_order >= 1.0 - 1e-9

    def format_table(self, label: str, panel_counts: list[int] | None = None) -> str:
        counts = panel_counts or [0] * len(self.values)
        rows = [
            f"{label}: observed order p = {self.observed_order:.2f}, "
            f"Richardson limit = {self.limit:.8f}"
        ]
        for value, error, count in zip(
            self.values, self.relative_errors, counts, strict=True
        ):
            rows.append(
                f"    panels={count:6d}  value={value:.8f}  "
                f"rel.err vs limit={error * 100:.4f}%"
            )
        return "\n".join(rows)


def richardson_convergence(
    values: list[float], refinement_ratio: float = 2.0
) -> ConvergenceReport:
    """Estimate the observed order of convergence and the extrapolated limit
    of `values`, each computed on a mesh `refinement_ratio` times finer than
    the previous one.

    Standard three-mesh procedure (as used for grid-convergence studies in
    computational E&M/CFD, e.g. Roache's grid-convergence index): with
    `d1 = v1 - v0` and `d2 = v2 - v1`, the observed order is
    `p = ln|d1/d2| / ln(r)` and the extrapolated limit is
    `v2 + d2 / (r^p - 1)`.

    A stagnant (`d2 == d1`) or diverging (`|d2| > |d1|`) sequence yields
    `p <= 0`, which `ConvergenceReport.converged` rejects -- the estimator
    never silently reports "converged" for a sequence that is not.
    """
    if len(values) < 3:
        raise ValueError("need at least three refinement levels")
    if refinement_ratio <= 1.0:
        raise ValueError("refinement_ratio must be > 1")

    differences = [
        later - earlier for earlier, later in zip(values, values[1:], strict=False)
    ]
    d1, d2 = differences[-2], differences[-1]

    if d2 == 0.0:
        # The last refinement changed nothing: exactly converged.
        order = math.inf
        limit = values[-1]
    elif d1 == 0.0:
        # Stagnant then moving again -- not a converging sequence.
        order = -math.inf
        limit = values[-1]
    else:
        order = math.log(abs(d1 / d2)) / math.log(refinement_ratio)
        if order > 0.0 and math.isfinite(order):
            limit = values[-1] + d2 / (refinement_ratio**order - 1.0)
        else:
            limit = values[-1]

    errors = [
        abs(value - limit) / abs(limit) if limit else math.inf for value in values
    ]
    return ConvergenceReport(
        values=list(values),
        differences=differences,
        observed_order=order,
        limit=limit,
        relative_errors=errors,
    )


def order_from_errors(
    errors: list[float], refinement_ratio: float = 2.0
) -> list[float]:
    """Observed order of convergence between consecutive refinement levels
    when the exact answer is *known* (an analytic oracle), rather than
    estimated: `p = ln(e_i / e_i+1) / ln(r)`."""
    return [
        math.log(earlier / later) / math.log(refinement_ratio)
        for earlier, later in zip(errors, errors[1:], strict=False)
    ]


# --- fixtures ----------------------------------------------------------------


def _dbu(value_um: float) -> int:
    return int(round(value_um / 0.001))


def _write_square_plates(path, side_um: float) -> None:
    """Two coincident-footprint square plates on layers 1/0 and 2/0; the
    z-separation comes from the spec's stackup (both are idealised
    zero-thickness laminae)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    box = kdb.Box.new(0, 0, _dbu(side_um), _dbu(side_um))
    top.shapes(layout.layer(1, 0)).insert(box)
    top.shapes(layout.layer(2, 0)).insert(box)
    layout.write(str(path))


def _write_plate_spec(path, gap_um: float, panel_um: float, eps_r: float) -> None:
    path.write_text(
        json.dumps(
            {
                "background_permittivity": eps_r,
                "panel_size_um": panel_um,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "top",
                        "z0_um": gap_um,
                        "z1_um": gap_um,
                    },
                    {"layer": "2/0", "conductor": "bottom", "z0_um": 0.0, "z1_um": 0.0},
                ],
            }
        )
    )


#: Square-coax cross-section used by the coax oracle below: a 1 um square
#: inner conductor centred in a square shield whose *inner* face is 3 um
#: across, with 0.5 um thick walls. The axis runs along x, so the
#: cross-section (and hence the closed form) is independent of the length.
COAX_INNER_UM = 1.0
COAX_OUTER_UM = 3.0
COAX_WALL_UM = 0.5


def _write_square_coax(path, length_um: float) -> None:
    """A square coaxial line of length `length_um` running along x: the inner
    conductor on layer 5/0, and the shield's four walls on layers 10/0-13/0
    (all mapped to one conductor by the spec, per docs/cli/mom.md's coax
    worked example). The y-extent comes from the GDS boxes and the z-extent
    from the stackup, so the walls' z ranges are set in `_write_coax_spec`."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    half_in = COAX_INNER_UM / 2.0
    half_out = COAX_OUTER_UM / 2.0
    outer = half_out + COAX_WALL_UM

    def add(layer_num: int, y0: float, y1: float) -> None:
        top.shapes(layout.layer(layer_num, 0)).insert(
            kdb.Box.new(0, _dbu(y0), _dbu(length_um), _dbu(y1))
        )

    add(5, -half_in, half_in)  # inner conductor
    add(10, -outer, outer)  # shield: +z wall
    add(11, -outer, outer)  # shield: -z wall
    add(12, -outer, -half_out)  # shield: -y wall
    add(13, half_out, outer)  # shield: +y wall
    layout.write(str(path))


def _write_coax_spec(path, panel_um: float, eps_r: float = 1.0) -> None:
    half_in = COAX_INNER_UM / 2.0
    half_out = COAX_OUTER_UM / 2.0
    outer = half_out + COAX_WALL_UM
    path.write_text(
        json.dumps(
            {
                "background_permittivity": eps_r,
                "panel_size_um": panel_um,
                "stackup": [
                    {
                        "layer": "5/0",
                        "conductor": "inner",
                        "z0_um": -half_in,
                        "z1_um": half_in,
                    },
                    {
                        "layer": "10/0",
                        "conductor": "outer",
                        "z0_um": half_out,
                        "z1_um": outer,
                    },
                    {
                        "layer": "11/0",
                        "conductor": "outer",
                        "z0_um": -outer,
                        "z1_um": -half_out,
                    },
                    {
                        "layer": "12/0",
                        "conductor": "outer",
                        "z0_um": -half_out,
                        "z1_um": half_out,
                    },
                    {
                        "layer": "13/0",
                        "conductor": "outer",
                        "z0_um": -half_out,
                        "z1_um": half_out,
                    },
                ],
            }
        )
    )


@pytest.fixture(scope="module")
def solver(tmp_path_factory):
    """Memoised `klt mom` runner shared by the whole module.

    The dense O(n^3) solve at the finest refinement level costs several
    seconds, and several tests want the same operating point (e.g. the
    absolute closed-form check and the finest rung of the aspect-ratio
    sweep), so results are cached by their fixture parameters.
    """
    root = tmp_path_factory.mktemp("mom-validation")
    cache: dict[tuple, dict] = {}

    def plates(side_um: float, gap_um: float, panel_um: float, eps_r: float = 1.0):
        key = ("plates", side_um, gap_um, panel_um, eps_r)
        if key not in cache:
            gds = root / f"plates-{side_um}.gds"
            if not gds.exists():
                _write_square_plates(gds, side_um)
            spec = root / f"plates-{side_um}-{gap_um}-{panel_um}-{eps_r}.json"
            _write_plate_spec(spec, gap_um, panel_um, eps_r)
            cache[key] = run_mom(str(gds), str(spec))
        return cache[key]

    def coax(length_um: float, panel_um: float, eps_r: float = 1.0):
        key = ("coax", length_um, panel_um, eps_r)
        if key not in cache:
            gds = root / f"coax-{length_um}.gds"
            if not gds.exists():
                _write_square_coax(gds, length_um)
            spec = root / f"coax-{panel_um}-{eps_r}.json"
            _write_coax_spec(spec, panel_um, eps_r)
            cache[key] = run_mom(str(gds), str(spec))
        return cache[key]

    return SimpleNamespace(plates=plates, coax=coax)


def _mutual_ff(report: dict) -> float:
    """The two-conductor capacitance in the usual (positive) SPICE sense:
    `-C_01`, per docs/cli/mom.md's "Reading the matrix"."""
    return -report["capacitance_matrix_ff"][0][1]


# --- parallel plate against the closed form ---------------------------------

#: Reference parallel-plate operating point: 10x10 um plates 0.5 um apart
#: (aspect ratio L/d = 20) discretised at half the gap. `panel_size_um <= d`
#: is the documented requirement for the point-collocation fill to hold
#: (docs/cli/mom.md's "Warnings"); d/2 puts it comfortably inside that.
PLATE_SIDE_UM = 10.0
PLATE_GAP_UM = 0.5
PLATE_PANEL_UM = 0.25

#: Tolerances, stated per issue #719's "within a stated tolerance".
#:
#: The ideal formula ignores fringing, which for L/d = 20 adds ~13% (measured;
#: see docs/design/mom-validation.md). The band is therefore one-sided-by-
#: physics (the solver must not fall *below* the fringing-free lower bound)
#: with a 25% ceiling that leaves headroom over the measured excess without
#: admitting a grossly wrong answer.
PLATE_IDEAL_MAX_EXCESS = 0.25
#: Against the fringing-corrected (Kirchhoff) oracle the agreement is much
#: tighter -- 1.8% measured -- but that oracle is itself shape-substituted
#: (equal-area disk, not a square), so 5% is the stated tolerance.
PLATE_FRINGING_TOL = 0.05


def test_parallel_plate_matches_closed_form(solver):
    """Oracle: `C = eps_r eps_0 A / d` (ideal parallel plate), cross-checked
    against Kirchhoff's fringing-corrected disk asymptotic."""
    report = solver.plates(PLATE_SIDE_UM, PLATE_GAP_UM, PLATE_PANEL_UM)
    assert report["warnings"] == [], report["warnings"]

    measured = _mutual_ff(report)
    area = PLATE_SIDE_UM**2
    ideal = parallel_plate_ff(area, PLATE_GAP_UM)
    fringing = kirchhoff_disk_ff(area, PLATE_GAP_UM)

    print(
        f"\nparallel plate L={PLATE_SIDE_UM} d={PLATE_GAP_UM} "
        f"panel={PLATE_PANEL_UM} n={report['panel_count']}\n"
        f"    measured        = {measured:.6f} fF\n"
        f"    ideal eps0*A/d  = {ideal:.6f} fF  "
        f"(measured/ideal = {measured / ideal:.4f})\n"
        f"    Kirchhoff disk  = {fringing:.6f} fF  "
        f"(measured/Kirchhoff = {measured / fringing:.4f})"
    )

    # Fringing can only add capacitance, so the fringing-free closed form is
    # a strict lower bound on any finite-plate solve.
    assert measured >= ideal, f"{measured} fF is below the eps0*A/d lower bound {ideal}"
    assert measured <= ideal * (1.0 + PLATE_IDEAL_MAX_EXCESS), (
        f"{measured} fF exceeds eps0*A/d = {ideal} fF by more than "
        f"{PLATE_IDEAL_MAX_EXCESS:.0%} -- more than fringing explains at L/d = "
        f"{PLATE_SIDE_UM / PLATE_GAP_UM:g}"
    )
    assert measured == pytest.approx(fringing, rel=PLATE_FRINGING_TOL)


def test_parallel_plate_approaches_closed_form_as_the_gap_shrinks(solver):
    """Oracle: `C = eps_r eps_0 A / d` is exact in the limit `d/L -> 0`, so
    the measured excess over it must shrink monotonically as the gap does --
    the closed form's own regime of validity, checked directly."""
    # panel_size = gap/2 at every rung, so the discretisation error stays
    # ~constant and the trend is physics, not mesh.
    rungs = [(2.0, 1.0), (1.0, 0.5), (0.5, 0.25)]
    area = PLATE_SIDE_UM**2

    excesses = []
    lines = ["\nparallel-plate aspect-ratio sweep (L = 10 um):"]
    for gap, panel in rungs:
        report = solver.plates(PLATE_SIDE_UM, gap, panel)
        assert report["warnings"] == [], (gap, report["warnings"])
        measured = _mutual_ff(report)
        ideal = parallel_plate_ff(area, gap)
        excesses.append(measured / ideal - 1.0)
        lines.append(
            f"    d={gap:4.2f} um (L/d={PLATE_SIDE_UM / gap:5.1f})  "
            f"measured={measured:.6f} fF  ideal={ideal:.6f} fF  "
            f"excess={excesses[-1] * 100:6.2f}%"
        )
    print("\n".join(lines))

    assert all(excess > 0.0 for excess in excesses), excesses
    assert excesses == sorted(excesses, reverse=True), (
        f"the excess over eps0*A/d must shrink as d/L shrinks, got {excesses}"
    )


def test_parallel_plate_is_exactly_linear_in_permittivity(solver):
    """Oracle: the electrostatic problem is linear in `eps`, so
    `C(eps_r) = eps_r * C(1)` to solver round-off."""
    vacuum = _mutual_ff(solver.plates(PLATE_SIDE_UM, 1.0, 0.5, 1.0))
    oxide = _mutual_ff(solver.plates(PLATE_SIDE_UM, 1.0, 0.5, 3.9))
    assert oxide == pytest.approx(3.9 * vacuum, rel=1e-12)


def test_parallel_plate_cli_json_matches_closed_form(tmp_path, capsys):
    """The same closed-form check through the shipped CLI/JSON contract, so
    the validated number is the one a consumer actually reads (not just the
    library return value)."""
    gds = tmp_path / "plates.gds"
    spec = tmp_path / "plates.mom.json"
    _write_square_plates(gds, PLATE_SIDE_UM)
    _write_plate_spec(spec, 1.0, 0.5, 3.9)

    assert main(["mom", str(gds), str(spec), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    measured = -data["capacitance_matrix_ff"][0][1]
    ideal = parallel_plate_ff(PLATE_SIDE_UM**2, 1.0, 3.9)
    assert data["warnings"] == []
    assert measured >= ideal
    assert measured <= ideal * (1.0 + 2 * PLATE_IDEAL_MAX_EXCESS)  # L/d = 10
    assert measured == pytest.approx(
        kirchhoff_disk_ff(PLATE_SIDE_UM**2, 1.0, 3.9), rel=2 * PLATE_FRINGING_TOL
    )


# --- convergence under mesh refinement --------------------------------------

#: Convergence-study fixture: 8x8 um plates 1 um apart, refined 2x per level.
#: Deliberately a different operating point from the accuracy checks above
#: (smaller plates, wider gap) so the finest level stays affordable in CI.
CONVERGENCE_SIDE_UM = 8.0
CONVERGENCE_GAP_UM = 1.0
CONVERGENCE_PANELS_UM = [1.0, 0.5, 0.25]

#: The observed order must be at least first order. The measured value for
#: this fixture is ~3.5 (see docs/design/mom-validation.md), so 1.0 is a
#: floor, not a fit -- it fails loudly if the solver ever stops converging.
#: The epsilon absorbs floating-point round-off in the log ratio, matching
#: `ConvergenceReport.converged`.
MIN_OBSERVED_ORDER = 1.0 - 1e-9
#: Relative error of the finest level against the Richardson-extrapolated
#: limit. Measured: 0.008%.
FINEST_LEVEL_TOL = 5e-3


def test_parallel_plate_converges_under_mesh_refinement(solver):
    """Refine the discretisation 2x per level and require the answer to
    converge: successive changes must shrink, the observed order must be at
    least first order, and the finest level must sit within
    `FINEST_LEVEL_TOL` of the Richardson-extrapolated limit.

    Oracle: Richardson extrapolation of the solver's own sequence (a
    self-consistency oracle -- the analytic-oracle version of this check is
    `test_square_coax_error_shrinks_under_refinement` below, which measures
    the error against a closed form directly)."""
    values, counts = [], []
    for panel in CONVERGENCE_PANELS_UM:
        report = solver.plates(CONVERGENCE_SIDE_UM, CONVERGENCE_GAP_UM, panel)
        assert report["warnings"] == [], (panel, report["warnings"])
        values.append(_mutual_ff(report))
        counts.append(report["panel_count"])

    convergence = richardson_convergence(values)
    ladder = "->".join(str(panel) for panel in CONVERGENCE_PANELS_UM)
    print(
        "\n"
        + convergence.format_table(
            f"parallel-plate mesh refinement (L={CONVERGENCE_SIDE_UM} um, "
            f"d={CONVERGENCE_GAP_UM} um, panel {ladder} um)",
            counts,
        )
    )

    assert convergence.converged, (
        f"solver did not converge under refinement: values={values}, "
        f"differences={convergence.differences}, "
        f"observed order={convergence.observed_order}"
    )
    assert convergence.observed_order >= MIN_OBSERVED_ORDER
    assert convergence.relative_errors[-1] <= FINEST_LEVEL_TOL

    # The converged answer must still respect the closed-form lower bound.
    ideal = parallel_plate_ff(CONVERGENCE_SIDE_UM**2, CONVERGENCE_GAP_UM)
    assert convergence.limit >= ideal


# --- square coax against the closed form ------------------------------------

#: Two lengths of the same cross-section; differencing them cancels the
#: length-independent end effects, leaving the per-unit-length capacitance
#: the 2-D coaxial closed form predicts.
COAX_LENGTHS_UM = (4.0, 8.0)
COAX_PANELS_UM = (1.0, 0.5)

#: Measured deviation from the closed form at the finer level is 0.75%; the
#: stated tolerance is 2%, dominated by the oracle's own asymptotic error at
#: b/a = 3 rather than by the solver.
COAX_TOL = 0.02


def _coax_per_um(solver, panel_um: float) -> tuple[float, list[dict]]:
    reports = [solver.coax(length, panel_um) for length in COAX_LENGTHS_UM]
    short, long = (_mutual_ff(r) for r in reports)
    delta_length = COAX_LENGTHS_UM[1] - COAX_LENGTHS_UM[0]
    return (long - short) / delta_length, reports


def test_square_coax_matches_closed_form(solver):
    """Oracle: `C' = 2 pi eps / ln(R_outer / R_inner)` with the exact
    conformal-mapping equivalent radii of the square cross-sections (see
    `square_coax_ff_per_um`)."""
    measured, reports = _coax_per_um(solver, COAX_PANELS_UM[-1])
    for report in reports:
        assert report["conductors"] == ["inner", "outer"]
        assert report["warnings"] == [], report["warnings"]

    oracle = square_coax_ff_per_um(COAX_INNER_UM, COAX_OUTER_UM)
    print(
        f"\nsquare coax a={COAX_INNER_UM} b={COAX_OUTER_UM} "
        f"panel={COAX_PANELS_UM[-1]} (lengths {COAX_LENGTHS_UM} um)\n"
        f"    measured dC/dL = {measured:.8f} fF/um\n"
        f"    closed form    = {oracle:.8f} fF/um  "
        f"(measured/oracle = {measured / oracle:.4f})"
    )
    assert measured == pytest.approx(oracle, rel=COAX_TOL)


def test_square_coax_shield_encloses_the_inner_conductor(solver):
    """Oracle: for a fully enclosed inner conductor every field line
    terminates on the shield, so `C_00 = -C_01` exactly. The fixture's shield
    is an open-ended tube, so the equality holds only up to the end
    leakage -- which shrinks as the line gets longer, asserted here."""
    leakage = []
    for length in COAX_LENGTHS_UM:
        report = solver.coax(length, COAX_PANELS_UM[-1])
        matrix = report["capacitance_matrix_ff"]
        leakage.append(1.0 - (-matrix[0][1]) / matrix[0][0])
    print(f"\ncoax end leakage (1 + C01/C00) by length: {leakage}")
    assert all(0.0 <= value < 0.05 for value in leakage), leakage
    assert leakage[-1] < leakage[0]


def test_square_coax_error_shrinks_under_refinement(solver):
    """Convergence measured against the *analytic* oracle rather than an
    extrapolated limit: halving `panel_size_um` must shrink the deviation
    from the coaxial closed form, at at least first order."""
    errors, lines = [], ["\nsquare-coax mesh refinement vs the closed form:"]
    oracle = square_coax_ff_per_um(COAX_INNER_UM, COAX_OUTER_UM)
    for panel in COAX_PANELS_UM:
        measured, reports = _coax_per_um(solver, panel)
        errors.append(abs(measured / oracle - 1.0))
        panels = sum(report["panel_count"] for report in reports)
        lines.append(
            f"    panel={panel:4.2f} um  panels={panels:6d}  "
            f"dC/dL={measured:.8f} fF/um  rel.err={errors[-1] * 100:5.2f}%"
        )
    orders = order_from_errors(errors)
    lines.append(f"    observed order p = {[round(o, 2) for o in orders]}")
    print("\n".join(lines))

    assert errors == sorted(errors, reverse=True), (
        f"refining the mesh must reduce the error against the closed form, got {errors}"
    )
    assert all(order >= MIN_OBSERVED_ORDER for order in orders), orders
    assert errors[-1] <= COAX_TOL


# --- the convergence gate itself --------------------------------------------
#
# The acceptance criterion is "a non-converging solver is not accepted", so
# the gate has to be shown to actually fire. The first test below runs the
# real harness over the solver's own documented breakdown regime; the rest
# exercise the estimator on synthetic sequences with known behaviour.


def test_convergence_gate_rejects_the_under_resolved_regime(solver):
    """The gate must fire on real solver output, not just synthetic numbers.

    `panel_size_um` far larger than the plate gap is the documented
    breakdown mode of the point-collocation fill (docs/cli/mom.md's
    "Warnings"): the centroid-to-centroid `1/r` kernel wildly overestimates
    the coupling, and refining the mesh in that regime makes the answer move
    *more*, not less. `ConvergenceReport.converged` must say so."""
    values, warned = [], 0
    for panel in (4.0, 2.0, 1.0):
        report = solver.plates(PLATE_SIDE_UM, 0.1, panel)
        warned += bool(report["warnings"])
        values.append(_mutual_ff(report))

    convergence = richardson_convergence(values)
    print(
        f"\nunder-resolved regime (d=0.1 um, panels 4->2->1 um): values={values}, "
        f"differences={convergence.differences}, "
        f"observed order={convergence.observed_order:.2f}"
    )
    assert warned == 3, "every under-resolved solve should carry a physicality warning"
    assert convergence.observed_order < 0.0
    assert not convergence.converged


def test_richardson_convergence_recovers_a_known_first_order_sequence():
    limit = 2.0
    values = [limit + 0.4 / 2**k for k in range(3)]
    report = richardson_convergence(values)
    assert report.observed_order == pytest.approx(1.0, abs=1e-9)
    assert report.limit == pytest.approx(limit, rel=1e-9)
    assert report.converged


def test_richardson_convergence_recovers_a_known_second_order_sequence():
    limit = 2.0
    values = [limit + 0.4 / 4**k for k in range(3)]
    report = richardson_convergence(values)
    assert report.observed_order == pytest.approx(2.0, abs=1e-9)
    assert report.limit == pytest.approx(limit, rel=1e-9)
    assert report.converged


def test_richardson_convergence_rejects_a_stagnant_sequence():
    """Each refinement moves the answer by the same amount: the sequence is
    drifting, not converging. Observed order 0 -> rejected."""
    report = richardson_convergence([1.0, 1.1, 1.2])
    assert report.observed_order == pytest.approx(0.0, abs=1e-9)
    assert not report.converged


def test_richardson_convergence_rejects_a_diverging_sequence():
    """Each refinement moves the answer *more* than the last: rejected with a
    negative observed order."""
    report = richardson_convergence([1.0, 1.2, 1.6])
    assert report.observed_order < 0.0
    assert not report.converged


def test_richardson_convergence_rejects_an_under_ordered_sequence():
    """Convergent but slower than first order (r^0.5 per level): shrinking
    differences are not enough on their own."""
    limit = 2.0
    values = [limit + 0.4 / (2**0.5) ** k for k in range(3)]
    report = richardson_convergence(values)
    assert report.observed_order == pytest.approx(0.5, abs=1e-9)
    assert not report.converged


def test_richardson_convergence_requires_three_levels():
    with pytest.raises(ValueError, match="three refinement levels"):
        richardson_convergence([1.0, 0.5])

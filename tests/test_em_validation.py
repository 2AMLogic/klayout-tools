"""Closed-form validation and convergence-under-refinement for ``klt em``
(issue #5618, brief 1.C of Epic #708 Phase 1).

``tests/test_em.py`` (issue #5616) covers the command's *contract* against a
recorded-report stub, and ``tests/test_em_layout.py`` (issue #5617) covers the
GDS-region geometry path's error handling. This module covers the *numerics*
of the quasi-static capacitance slice on geometry produced by this repo's own
GDS + stackup pipeline -- the epic's reality-grounding discipline: every check
below names its oracle, and none compares the solver only against itself
(``docs/design/em-validation.md`` records the measured numbers behind the
tolerances chosen here):

- **Parallel plate** -- ``C = eps_r eps_0 A / d``, the textbook ideal
  parallel-plate oracle of ``docs/design/mom-validation.md`` section 1,
  evaluated for the sky130A layer pair's own stackup gap and permittivity.
  For finite plates this is a strict *lower* bound (fringing can only add
  capacitance) whose excess shrinks as ``d/L -> 0`` -- both facts asserted.
  The 5 % bar of ``docs/design/em-field-sim-spike.md`` section 5 is asserted
  at ``L/d ~ 148``, the aspect ratio where the closed form's own regime of
  validity reaches that bar through the default mesh.
- **Coaxial line** -- ``C = 2 pi eps_0 L / ln(b/a)``, the exact closed form
  (upstream geode-fem's own oracle 1), on gmsh-built meshes fed through
  ``klt em``'s supplied-mesh request. A three-level mesh-refinement study
  whose observed convergence order must be consistent with the element's
  theoretical order: P1 tets, electrostatic energy, ``O(h^2)``
  (upstream's smooth-fixture study measures 2.0202 on the same oracle
  family; ``benchmarks/electrostatic/results.toml`` in geode-fem).
- **``klt mom`` cross-check** -- the genuinely independent method
  (boundary/integral, open space) against the FEM (volumetric, geode's
  grounded box) on one shared GDS fixture. The committed evidence is
  ``tests/fixtures/em/cross_validation_evidence.json``.

Every live test needs the real engines and skips (never passes silently)
when they are absent: ``geode`` (``$KLT_GEODE_BINARY`` or ``PATH``, see
``docs/cli/em.md``), Gmsh for meshing, and the ``klt_mom_native`` extension
for the cross-check (``docs/cli/mom.md``). The evidence-audit test runs
everywhere and keeps the committed cross-check numbers honest without any
engine installed.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from helpers.em_validation_oracles import (
    COAX_A_M,
    COAX_B_M,
    COAX_LENGTH_M,
    EPS0_F_PER_M,
    build_coax_mesh,
    geode_or_skip,
    gmsh_or_skip,
    layer_pair_geometry,
    mutual_ff,
    parallel_plate_ff,
    plate_pair_gds,
    run_layout_request,
)
from klayout_tools import pdk_stackup
from klayout_tools._em_layout import prepare_layout
from klayout_tools._geode import generate_mesh
from klayout_tools.em import run_em

EVIDENCE = Path(__file__).parent / "fixtures" / "em" / "cross_validation_evidence.json"
EVIDENCE_DOC = json.loads(EVIDENCE.read_text())

#: The tolerance bands the live gates enforce are *read from* the committed
#: evidence, never restated here: ``tolerance_band.max`` bounds the em-vs-mom
#: disagreement, ``each_solver_bound.max`` is mom-validation.md section 1's
#: ceiling on any one solve over the closed form. Relaxing a band therefore
#: means editing the evidence, which the audit test below re-checks.
CROSS_MAX_REL_DIFFERENCE = EVIDENCE_DOC["tolerance_band"]["max"]
EACH_SOLVER_MAX = EVIDENCE_DOC["each_solver_bound"]["max"]


@pytest.fixture(autouse=True)
def _hermetic_pdk(tmp_path, monkeypatch):
    """A fabricated sky130A install so the curated stackup table resolves
    without a real PDK (same pattern as ``tests/test_em_layout.py``); the
    z/eps numbers themselves come from the curated table either way."""
    install = tmp_path / "pdks" / "sky130A"
    (install / "libs.tech").mkdir(parents=True)
    (install / "libs.ref").mkdir()
    monkeypatch.setenv("PDK_ROOT", str(install.parent))


# ---------------------------------------------------------------------------
# Oracle 1: the closed-form parallel plate through the GDS + stackup path
# ---------------------------------------------------------------------------

#: The 5 % bar of docs/design/em-field-sim-spike.md section 5 is asserted at
#: this aspect ratio. At the literal 10x10 um of mom-validation.md section 1
#: the stackup's own gap (0.27 um, L/d = 37) still carries ~7 % of fringing +
#: mesh excess, so the bar is met deeper in the closed form's own regime of
#: validity -- measured at 40x40 um: +3.08 % (docs/design/em-validation.md
#: section 1 tables the whole L/d sweep).
FIVE_PERCENT_SIDE_UM = 40.0
FIVE_PERCENT_MAX = 1.05

#: Measured excess over the oracle at default mesh settings, met1/met2 pair
#: (docs/design/em-validation.md section 1): +12.5 % / +6.1 % / +3.1 % at
#: 10 / 20 / 40 um. The ceiling (``EACH_SOLVER_MAX``, 1.25 in the committed
#: evidence) is section 1's own band at L/d ~ 20-40.
EXCESS_TREND_SIDES_UM = (10.0, 20.0, 40.0)


def test_parallel_plate_meets_five_percent_bar(tmp_path):
    """Oracle: ``C = eps_r eps_0 A / d`` (closed form), sky130A met1/met2
    pair, 40x40 um plates through ``klt em``'s layout path at its default
    mesh settings -- the 5 % bar of docs/design/em-field-sim-spike.md
    section 5, on geometry produced by this repo's own pipeline."""
    gmsh_or_skip()
    geode_or_skip()
    stack = pdk_stackup.stackup("sky130A")
    geom = layer_pair_geometry(stack, "met1", "met2")
    gds = tmp_path / "plates-40.gds"
    plate_pair_gds(gds, FIVE_PERCENT_SIDE_UM, "met1", "met2")

    response = run_layout_request(tmp_path, "req-40", gds)
    measured = mutual_ff(response)
    ideal = parallel_plate_ff(FIVE_PERCENT_SIDE_UM**2, geom["gap_um"], geom["eps_r"])

    print(
        f"\nparallel plate L={FIVE_PERCENT_SIDE_UM} d={geom['gap_um']:.4f} "
        f"eps_r={geom['eps_r']}\n"
        f"    measured = {measured:.4f} fF\n"
        f"    oracle   = {ideal:.4f} fF  (ratio {measured / ideal:.4f})\n"
        f"    nodes={response['environment']['mesh_stats']['nodes']}"
    )

    # Fringing can only add capacitance: the closed form is a strict lower
    # bound on any finite-plate solve.
    assert measured >= ideal, (
        f"{measured} fF is below the eps_r*eps_0*A/d lower bound {ideal} fF"
    )
    assert measured <= ideal * FIVE_PERCENT_MAX, (
        f"{measured} fF exceeds the closed form by more than 5 % "
        f"(ratio {measured / ideal:.4f})"
    )


def test_parallel_plate_excess_shrinks_with_aspect_ratio(tmp_path):
    """Oracle: the same closed form's own regime of validity -- the excess
    over ``eps_r eps_0 A / d`` must shrink monotonically as ``d/L -> 0``
    (mom-validation.md section 1 asserts the same physics for ``klt mom``).
    This is the GDS-path proof that the boxed solve converges to the
    parallel-plate regime, not merely to any number."""
    gmsh_or_skip()
    geode_or_skip()
    stack = pdk_stackup.stackup("sky130A")
    geom = layer_pair_geometry(stack, "met1", "met2")

    excesses = []
    for side in EXCESS_TREND_SIDES_UM:
        gds = tmp_path / f"plates-{side:.0f}.gds"
        plate_pair_gds(gds, side, "met1", "met2")
        response = run_layout_request(tmp_path, f"req-{side:.0f}", gds)
        measured = mutual_ff(response)
        ideal = parallel_plate_ff(side**2, geom["gap_um"], geom["eps_r"])
        excesses.append(measured / ideal - 1.0)
        print(
            f"    L={side:4.0f} um (L/d={side / geom['gap_um']:5.0f})  "
            f"measured={measured:9.4f} fF  oracle={ideal:9.4f} fF  "
            f"excess={excesses[-1] * 100:6.2f}%  "
            f"nodes={response['environment']['mesh_stats']['nodes']}"
        )

    assert all(e > 0 for e in excesses), excesses  # lower bound holds everywhere
    assert all(
        later < earlier for earlier, later in zip(excesses, excesses[1:], strict=False)
    ), f"excess over the closed form does not shrink as d/L -> 0: {excesses}"
    assert 1.0 < 1.0 + excesses[0] < EACH_SOLVER_MAX, excesses[0]


# ---------------------------------------------------------------------------
# Oracle 2: convergence order under mesh refinement (coax, exact closed form)
# ---------------------------------------------------------------------------

#: Characteristic length halved per level. 0.4 is left out of the gate: at
#: ~4 elements across the annulus the pair (0.4 -> 0.2) is still
#: pre-asymptotic (observed order 1.1); the gated levels measure
#: 1.75 / 1.87, converging toward the theoretical 2 from below.
COAX_LEVELS_H_M = (0.2, 0.1, 0.05)
#: P1 energy convergence is O(h^2) (theoretical order 2). The observed order
#: on the gated levels must land in this band: below 1.5 means the solve is
#: not converging at anything like the element's rate, above 2.4 means it is
#: not a P1 discretisation at all. Measured: 1.748, 1.874.
COAX_ORDER_MIN = 1.5
COAX_ORDER_MAX = 2.4
COAX_THEORETICAL_ORDER = 2.0
COAX_FINEST_MAX_REL_ERROR = 1e-3


def test_coax_refinement_converges_at_the_element_order(tmp_path):
    """Oracle: ``C = 2 pi eps_0 L / ln(b/a)`` (exact coax closed form), on
    gmsh meshes fed through ``klt em``'s supplied-mesh request. The observed
    convergence order of the refinement study must be consistent with the
    element's theoretical order (P1 tets, electrostatic energy: ``O(h^2)``)
    or the test fails."""
    geode = geode_or_skip()
    exact = 2.0 * math.pi * EPS0_F_PER_M * COAX_LENGTH_M / math.log(COAX_B_M / COAX_A_M)

    values, errors = [], []
    for h in COAX_LEVELS_H_M:
        mesh = tmp_path / f"coax-{h}.msh"
        build_coax_mesh(mesh, h)
        request = tmp_path / f"coax-req-{h}.json"
        request.write_text(
            json.dumps(
                {
                    "mesh": {"path": mesh.name, "length_unit_m": 1.0},
                    "terminals": [{"name": "inner", "surface": "S_in"}],
                    "ground": [{"name": "gnd", "surface": "S_out"}],
                    "options": {
                        "geode_binary": geode,
                        "keep_artifacts": False,
                        "timeout_s": 1800,
                    },
                }
            )
        )
        response = run_em(str(request))
        c = response["results"]["c"]["matrix"][0][0]
        values.append(c)
        errors.append(abs(c - exact) / exact)

    print(
        f"\ncoax a={COAX_A_M} b={COAX_B_M} L={COAX_LENGTH_M}: exact C = {exact:.6e} F"
    )
    orders = []
    for h, value, error in zip(COAX_LEVELS_H_M, values, errors, strict=True):
        print(
            f"    h={h:<5} C={value:.6e} F  rel.err={error:.3e}  "
            f"({(value - exact) / exact:+.1e})"
        )
    for earlier, later in zip(errors, errors[1:], strict=False):
        orders.append(math.log(earlier / later) / math.log(2.0))
    print(
        f"    observed orders: {[f'{p:.3f}' for p in orders]} "
        f"(theoretical P1 energy order {COAX_THEORETICAL_ORDER})"
    )

    assert all(c > exact for c in values), "coax solve left the from-above regime"
    assert all(
        later < earlier for earlier, later in zip(errors, errors[1:], strict=False)
    ), f"refinement is not reducing the error: {errors}"
    assert all(COAX_ORDER_MIN <= p <= COAX_ORDER_MAX for p in orders), (
        f"observed convergence order {orders} is not consistent with the "
        f"element's theoretical order {COAX_THEORETICAL_ORDER} "
        f"(P1 tets, electrostatic energy)"
    )
    assert errors[-1] <= COAX_FINEST_MAX_REL_ERROR


# ---------------------------------------------------------------------------
# Refinement through the layout path itself (edge-singularity-limited)
# ---------------------------------------------------------------------------

#: Measured (docs/design/em-validation.md section 2): 14.3827 / 14.0365 fF
#: at refinement 1 / 2 (mesh sizes halved), converging from above toward the
#: boxed geometry's value. On plate edges the field has a wedge singularity,
#: so the observable order through the layout path is far below 2 -- the
#: order gate lives on the smooth coax oracle above, exactly as upstream's
#: own layout-path golden (geode-fem tests/mesh_static_golden.rs) gates
#: monotone convergence rather than order on layout geometry.
LAYOUT_REFINE_LEVELS = (1, 2)
LAYOUT_REFINE_SIDE_UM = 10.0


def test_layout_refinement_converges_from_above(tmp_path):
    """Oracle: the closed-form lower bound plus the from-above convergence
    every capacitance-from-energy discretisation of this geometry shows
    (upstream's guard-ring golden asserts the same direction on its own
    layout path). The refinement runs entirely through klt's own geometry
    path: the layout document is ``prepare_layout``'s output and only its
    mesh-size fields are scaled."""
    gmsh_or_skip()
    geode = geode_or_skip()
    stack = pdk_stackup.stackup("sky130A")
    geom = layer_pair_geometry(stack, "met1", "met2")
    ideal = parallel_plate_ff(LAYOUT_REFINE_SIDE_UM**2, geom["gap_um"], geom["eps_r"])

    gds = tmp_path / "plates-refine.gds"
    plate_pair_gds(gds, LAYOUT_REFINE_SIDE_UM, "met1", "met2")
    geometry = prepare_layout(
        {
            "layout": gds.name,
            "region": {"cell": "TOP"},
            "stackup": "sky130A",
        },
        str(tmp_path),
    )

    values = []
    for refine in LAYOUT_REFINE_LEVELS:
        document = json.loads(json.dumps(geometry.document))
        document["mesh"]["size_max"] /= refine
        document["mesh"]["size_conductor"] /= refine
        outdir = tmp_path / f"mesh-r{refine}"
        outdir.mkdir()
        mesh, stats = generate_mesh(geode, document, str(outdir), 1800, True)
        request = outdir / "req.json"
        request.write_text(
            json.dumps(
                {
                    "mesh": {"path": os.path.basename(mesh), "length_unit_m": 1e-6},
                    "terminals": geometry.terminals,
                    "ground": [{"name": "walls", "surface": "outer_boundary"}],
                    "materials": [
                        {"physical_group": d["name"], "eps_r": d["eps_r"]}
                        for d in geometry.document["dielectrics"]
                    ],
                    "options": {
                        "geode_binary": geode,
                        "keep_artifacts": False,
                        "timeout_s": 1800,
                    },
                }
            )
        )
        response = run_em(str(request))
        values.append(mutual_ff(response))
        print(
            f"    refinement x{refine}: |C01|={values[-1]:.4f} fF "
            f"({values[-1] / ideal:.4f} x oracle, nodes={stats['nodes']})"
        )

    assert all(v >= ideal for v in values), values
    assert values[1] < values[0], (
        f"layout-path refinement is not converging from above: {values}"
    )
    assert values[0] <= ideal * EACH_SOLVER_MAX, values[0] / ideal


# ---------------------------------------------------------------------------
# Oracle 3: klt mom -- the independent-method cross-check
# ---------------------------------------------------------------------------

#: The shared benchmark: li1/met1 pair (gap 0.34 um) rather than met1/met2
#: (0.27 um), because klt mom's panel-width guard needs panels at or below
#: the gap while staying under the 8000-panel dense-solve cap -- at 10x10 um
#: this fixture discretises warning-free at 4896 panels.
CROSS_SIDE_UM = 10.0
CROSS_LOWER, CROSS_UPPER = "li1", "met1"
CROSS_PANEL_UM = 0.29
# Measured |em - mom| / mom = 0.0216; the band (CROSS_MAX_REL_DIFFERENCE) and
# its rationale are the committed evidence's own, read at the top of this
# module (tests/fixtures/em/cross_validation_evidence.json -> tolerance_band),
# ~2.3x headroom over the measurement.


def test_em_and_mom_agree_on_the_shared_gds(tmp_path):
    """Oracle: the independent method -- ``klt mom`` (boundary integral, open
    space) on the very same GDS fixture ``klt em`` just solved (volumetric
    FEM in geode's grounded box). Neither solve is checked against itself."""
    pytest.importorskip(
        "klt_mom_native",
        reason=(
            "klt_mom_native is not built -- run `maturin develop --release` in "
            "native/mom/ (see docs/cli/mom.md#building-the-native-extension)"
        ),
    )
    gmsh_or_skip()
    geode_or_skip()
    from klayout_tools.mom import run_mom

    stack = pdk_stackup.stackup("sky130A")
    geom = layer_pair_geometry(stack, CROSS_LOWER, CROSS_UPPER)
    gds = tmp_path / "cross-plates.gds"
    plate_pair_gds(gds, CROSS_SIDE_UM, CROSS_LOWER, CROSS_UPPER)

    em_response = run_layout_request(tmp_path, "cross-em", gds)
    em = mutual_ff(em_response)

    spec = tmp_path / "cross-mom.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": geom["eps_r"],
                "panel_size_um": CROSS_PANEL_UM,
                "stackup": [
                    {
                        "layer": f"{geom['lower']['gds_layer']}",
                        "conductor": "BOT",
                        "z0_um": geom["lower"]["z0_um"],
                        "z1_um": geom["lower"]["z0_um"] + geom["lower"]["thickness_um"],
                    },
                    {
                        "layer": f"{geom['upper']['gds_layer']}",
                        "conductor": "TOPP",
                        "z0_um": geom["upper"]["z0_um"],
                        "z1_um": geom["upper"]["z0_um"] + geom["upper"]["thickness_um"],
                    },
                ],
            }
        )
    )
    mom_report = run_mom(str(gds), str(spec))
    assert mom_report["warnings"] == [], mom_report["warnings"]
    mom = -mom_report["capacitance_matrix_ff"][0][1]

    ideal = parallel_plate_ff(CROSS_SIDE_UM**2, geom["gap_um"], geom["eps_r"])
    print(
        f"\ncross-check li1/met1 {CROSS_SIDE_UM}x{CROSS_SIDE_UM} um "
        f"(d={geom['gap_um']:.4f}, eps_r={geom['eps_r']}):\n"
        f"    klt em  = {em:.4f} fF  ({em / ideal:.4f} x oracle, "
        f"nodes={em_response['environment']['mesh_stats']['nodes']})\n"
        f"    klt mom = {mom:.4f} fF  ({mom / ideal:.4f} x oracle, "
        f"panels={mom_report['panel_count']})\n"
        f"    |em-mom|/mom = {abs(em - mom) / mom:.4f}"
    )

    # Both solves sit above the closed-form lower bound, inside section 1's
    # own band for this aspect ratio ...
    assert ideal <= em <= ideal * EACH_SOLVER_MAX, em / ideal
    assert ideal <= mom <= ideal * EACH_SOLVER_MAX, mom / ideal
    # ... and the two independent methods agree within the committed band.
    assert abs(em - mom) / mom <= CROSS_MAX_REL_DIFFERENCE, (
        f"klt em ({em:.4f} fF) and klt mom ({mom:.4f} fF) disagree by more "
        f"than {CROSS_MAX_REL_DIFFERENCE:.0%}"
    )


# ---------------------------------------------------------------------------
# The committed evidence (runs with no engine installed)
# ---------------------------------------------------------------------------


def test_cross_validation_evidence_is_self_consistent():
    """Audit of ``tests/fixtures/em/cross_validation_evidence.json`` (issue
    #5618's committed cross-check evidence): the oracle arithmetic must
    recompute from physical constants, the difference from the two committed
    values, the band must contain the measurement, and the fixture's own
    gap/eps must still agree with the curated sky130A stackup -- if the
    stackup table moves, this fails and the evidence must be re-measured."""
    evidence = EVIDENCE_DOC
    fixture, oracle = evidence["fixture"], evidence["oracle"]

    # The oracle names itself (closed form) and recomputes exactly.
    assert oracle["kind"] == "closed_form_parallel_plate"
    assert oracle["formula"] == "C = eps_r * eps_0 * A / d"
    ideal = parallel_plate_ff(
        fixture["plate_side_um"] ** 2, fixture["gap_um"], fixture["eps_r"]
    )
    assert oracle["value_ff"] == pytest.approx(ideal, rel=1e-4)
    assert fixture["aspect_ratio_L_over_d"] == pytest.approx(
        fixture["plate_side_um"] / fixture["gap_um"], rel=1e-3
    )

    # The stackup has not drifted out from under the measurement.
    stack = pdk_stackup.stackup("sky130A")
    geom = layer_pair_geometry(stack, CROSS_LOWER, CROSS_UPPER)
    assert fixture["gap_um"] == pytest.approx(geom["gap_um"], abs=1e-9)
    assert fixture["eps_r"] == pytest.approx(geom["eps_r"], abs=1e-9)
    for role in ("lower", "upper"):
        committed = fixture["layers"][role]
        live = geom[role]
        assert committed["gds_layer"] == live["gds_layer"]
        assert committed["z0_um"] == pytest.approx(live["z0_um"], abs=1e-9)

    # The difference arithmetic recomputes from the committed values.
    em = evidence["klt_em"]["mutual_ff"]
    mom = evidence["klt_mom"]["mutual_ff"]
    difference = evidence["difference"]
    assert difference["abs_ff"] == pytest.approx(abs(em - mom), rel=1e-4)
    assert difference["relative_to_mom"] == pytest.approx(abs(em - mom) / mom, rel=1e-3)
    assert difference["relative_to_oracle"] == pytest.approx(
        abs(em - mom) / ideal, rel=1e-3
    )

    # The committed band holds the committed measurement with its stated
    # headroom, and both solves sit above the physics lower bound and
    # inside the per-solver sanity band.
    band = evidence["tolerance_band"]
    assert band["metric"] == "|em - mom| / mom"
    assert difference["relative_to_mom"] < band["max"] < 1.0
    sanity = evidence["each_solver_bound"]
    for value in (em, mom):
        assert value >= oracle["value_ff"] * sanity["min"]
        assert value <= oracle["value_ff"] * sanity["max"]
    assert evidence["klt_mom"]["warnings"] == []

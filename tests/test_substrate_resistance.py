"""Two-terminal closed-form substrate spreading resistance (issue #2561).

Validation strategy mirrors ``docs/design/mom-validation.md``: check the
implementation against answers that are known *independently of it*, rather
than against a second copy of the same expression.

Three independent anchors are used, in increasing strength:

1. A hand-computed reference value for one stated geometry (below), so a
   silent algebra change is caught even if every structural property still
   holds.
2. The ``separation -> infinity`` limit, which must reproduce Holm's
   classic single-contact constriction resistance ``rho/(4a)`` -- a
   textbook result the implementation does not otherwise consult.
3. A **numerical** check of the one approximation the closed form makes:
   the mutual term replaces contact 1's disc current distribution by a point
   source. ``test_point_source_mutual_term_is_within_one_percent_in_far_field``
   integrates the exact half-space Green's function over the real disc
   current density and measures the truncation error at the documented
   far-field threshold. That is what puts a number on
   ``FAR_FIELD_SEPARATION_RATIO`` instead of asserting it.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys

import klayout.db as kdb
import pytest

from klayout_tools.extract import ExtractError, run_extract
from klayout_tools.substrate_resistance import (
    FAR_FIELD_SEPARATION_RATIO,
    HALF_SPACE_DEPTH_RATIO,
    OHM_UM_PER_OHM_CM,
    SUBSTRATE_SPREADING_MODEL,
    SubstrateSpreadingError,
    effective_contact_radius_um,
    pair_regime,
    resolve_substrate_model,
    single_contact_spreading_resistance_ohm,
    substrate_spreading_report,
    tap_contacts_from_region,
    two_contact_spreading_resistance_ohm,
)
from test_extract import _make_inverter_layout, _write_gds

# --------------------------------------------------------------------------- #
# The closed form itself: pure geometry + resistivity, no layout at all
# --------------------------------------------------------------------------- #


def test_matches_hand_computed_reference_value():
    """One stated geometry, one hand-computed answer.

    ``rho = 1 ohm-cm = 1e4 ohm-um``, ``a1 = a2 = 1 um``, ``d = 100 um``::

        R = rho * (1/(4*1) + 1/(4*1) - 1/(pi*100))
          = 1e4 * (0.25 + 0.25 - 0.0031830988...)
          = 1e4 * 0.4968169011...
          = 4968.169011... ohm

    Tolerance is 1e-9 relative: this is a closed form evaluated in double
    precision, not a measurement, so anything looser would hide a real
    algebra change.
    """
    result = two_contact_spreading_resistance_ohm(1.0, 1.0, 100.0, 1.0)
    assert result == pytest.approx(4968.169011381, rel=1e-9)


def test_converges_to_holm_constriction_resistance_at_large_separation():
    """As the contacts move apart the mutual term vanishes and the pair
    becomes two *independent* Holm constriction resistances in series --
    ``rho/(4*a1) + rho/(4*a2)``, the textbook single-contact result this
    implementation does not otherwise use.

    Checked at ``d = 1e6 * a``, where the mutual term is ~1e-7 of the total,
    so the assertion is a genuine convergence statement rather than a
    restatement of the formula.
    """
    rho, a1, a2 = 10.0, 0.5, 2.0
    expected = single_contact_spreading_resistance_ohm(
        a1, rho
    ) + single_contact_spreading_resistance_ohm(a2, rho)
    far = two_contact_spreading_resistance_ohm(a1, a2, 1.0e6 * a2, rho)
    assert far == pytest.approx(expected, rel=1e-6)
    # ...and it approaches that limit from below: the mutual term is a
    # *reduction*, because contact 2's sink lowers contact 1's potential.
    assert far < expected


def _disc_weighted_green_mean(radius_um: float, separation_um: float) -> float:
    """``<1/|r - d|>`` over a disc contact of the given radius, weighted by
    the exact equipotential-disc current density, evaluated at a field point
    ``separation_um`` away in the same plane.

    The closed form replaces this by the point-source value ``1/d``; the
    difference is exactly the approximation
    :data:`FAR_FIELD_SEPARATION_RATIO` bounds.

    An equipotential disc injecting ``I`` carries
    ``J(r) = I / (2*pi*a*sqrt(a**2 - r**2))``, whose ``1/sqrt(a**2 - r**2)``
    edge singularity is integrable but ruinous for a naive quadrature. The
    substitution ``r = a*sin(phi)`` removes it exactly: the Jacobian
    ``a*cos(phi) dphi`` cancels the ``sqrt(a**2 - r**2) = a*cos(phi)``
    denominator, leaving the smooth, bounded integrand below, which a plain
    midpoint rule integrates to well past the precision this test needs.
    """
    n_phi, n_theta = 400, 800
    total = 0.0
    for i in range(n_phi):
        phi = (i + 0.5) * (math.pi / 2.0) / n_phi
        r = radius_um * math.sin(phi)
        inner = 0.0
        for j in range(n_theta):
            theta = (j + 0.5) * (2.0 * math.pi) / n_theta
            dist = math.sqrt(
                r * r
                + separation_um * separation_um
                - 2.0 * r * separation_um * math.cos(theta)
            )
            inner += 1.0 / dist
        inner *= (2.0 * math.pi) / n_theta
        total += math.sin(phi) / (2.0 * math.pi) * inner
    return total * (math.pi / 2.0) / n_phi


def test_point_source_mutual_term_is_within_one_percent_in_far_field():
    """Quantify the closed form's one approximation, don't assume it.

    At exactly the documented far-field threshold
    (``d = FAR_FIELD_SEPARATION_RATIO * (a1 + a2)``), the point-source
    mutual term ``1/d`` is compared against the numerically-integrated exact
    value over the real disc current distribution. The documented claim is
    "better than 1%"; this measures it.

    Also checked one decade closer (``d = (a1 + a2)/2`` inside the
    near-field), where the error is materially worse -- which is why pairs
    below the threshold are reported with ``regime: "near_field"`` instead
    of being silently presented as validated numbers.
    """
    radius = 1.0
    threshold = FAR_FIELD_SEPARATION_RATIO * (radius + radius)
    exact = _disc_weighted_green_mean(radius, threshold)
    approx = 1.0 / threshold
    far_field_error = abs(approx - exact) / exact
    assert far_field_error < 0.01

    near = 1.5 * radius
    near_error = abs(1.0 / near - _disc_weighted_green_mean(radius, near)) / (
        _disc_weighted_green_mean(radius, near)
    )
    assert near_error > far_field_error


def test_symmetric_in_contact_order():
    """``R(a1, a2, d) == R(a2, a1, d)`` -- a two-terminal resistance cannot
    depend on which terminal the report calls ``a``."""
    forward = two_contact_spreading_resistance_ohm(0.3, 2.5, 40.0, 10.0)
    reverse = two_contact_spreading_resistance_ohm(2.5, 0.3, 40.0, 10.0)
    assert forward == pytest.approx(reverse, rel=1e-12)


def test_linear_in_resistivity():
    """Every term carries exactly one factor of ``rho``, so doubling the
    substrate resistivity must double the resistance exactly."""
    single = two_contact_spreading_resistance_ohm(1.0, 1.0, 50.0, 5.0)
    double = two_contact_spreading_resistance_ohm(1.0, 1.0, 50.0, 10.0)
    assert double == pytest.approx(2.0 * single, rel=1e-12)


def test_strictly_positive_for_every_non_overlapping_geometry():
    """The mutual term never overwhelms the two self terms for disjoint
    contacts -- swept over four decades of radius ratio at the tightest
    legal spacing (``d == a1 + a2``, contacts just touching)."""
    for a1 in (0.01, 0.1, 1.0, 10.0, 100.0):
        for a2 in (0.01, 0.1, 1.0, 10.0, 100.0):
            value = two_contact_spreading_resistance_ohm(a1, a2, a1 + a2, 10.0)
            assert value > 0.0


def test_zero_and_negative_separation_are_rejected_not_clamped():
    """Documented boundary behaviour: coincident contacts are not a limit
    this formula has (the mutual term diverges, while two coincident
    contacts are physically one contact with zero resistance to itself), so
    the request is refused rather than answered with an artefact."""
    for separation in (0.0, -1.0):
        with pytest.raises(
            SubstrateSpreadingError, match="separation must be positive"
        ):
            two_contact_spreading_resistance_ohm(1.0, 1.0, separation, 10.0)


def test_overlapping_contacts_are_rejected():
    """Below ``d = a1 + a2`` the two discs intersect, which the two-disc
    derivation does not model."""
    with pytest.raises(SubstrateSpreadingError, match="contacts overlap"):
        two_contact_spreading_resistance_ohm(1.0, 1.0, 1.9, 10.0)


def test_non_positive_radius_and_resistivity_are_rejected():
    with pytest.raises(SubstrateSpreadingError, match="radii must be positive"):
        two_contact_spreading_resistance_ohm(0.0, 1.0, 10.0, 10.0)
    with pytest.raises(SubstrateSpreadingError, match="resistivity must be positive"):
        two_contact_spreading_resistance_ohm(1.0, 1.0, 10.0, 0.0)
    with pytest.raises(SubstrateSpreadingError, match="radius must be positive"):
        single_contact_spreading_resistance_ohm(-1.0, 10.0)


def test_effective_radius_is_the_equal_area_disc():
    """A 2 um x 2 um drawn tap has area 4 um^2, so its equal-area disc has
    radius ``sqrt(4/pi)``."""
    assert effective_contact_radius_um(4.0) == pytest.approx(math.sqrt(4.0 / math.pi))
    with pytest.raises(SubstrateSpreadingError, match="area must be positive"):
        effective_contact_radius_um(0.0)


def test_pair_regime_boundaries():
    assert pair_regime(1.0, 1.0, 1.9) == "overlapping"
    assert pair_regime(1.0, 1.0, 2.0) == "near_field"
    assert pair_regime(1.0, 1.0, 9.99) == "near_field"
    assert pair_regime(1.0, 1.0, 10.0) == "far_field"


def test_ohm_cm_to_ohm_um_conversion_is_applied_exactly_once():
    """A 1 ohm-cm substrate is 1e4 ohm-um; a 1 um-radius contact's Holm
    resistance is therefore ``1e4/4 = 2500`` ohm. Guards the single unit
    conversion in the module against being applied twice or not at all."""
    assert OHM_UM_PER_OHM_CM == 1.0e4
    assert single_contact_spreading_resistance_ohm(1.0, 1.0) == pytest.approx(2500.0)


# --------------------------------------------------------------------------- #
# Provenance: the resistivity is cited, never invented
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("deck_name", ["sky130", "gf180mcu"])
def test_resolved_model_cites_the_curated_stackup_entry(deck_name):
    """Every input the estimate consumes is reported with the curated
    stackup's own ``source`` prose attached (issue #2560), so a reviewer can
    trace the ohms back to the figure rather than trust a constant."""
    model = resolve_substrate_model(deck_name)
    assert model["pdk_family"] == deck_name
    assert model["resistivity_ohm_cm"] == pytest.approx(10.0)
    assert model["substrate_thickness_um"] == pytest.approx(725.0)
    assert model["warnings"] == []
    # The curated source says in as many words that this is a textbook
    # typical figure rather than something the open PDK states -- exactly
    # the qualification a reviewer needs to see, so assert it survives the
    # hop into this report instead of being summarised away.
    assert "resistivity_ohm_cm" in model["source"]
    assert "NOT sky130-specific" in model["source"] or "not " in model["source"]


def test_uncurated_family_substitutes_no_default_resistivity():
    """A PDK family with no curated stackup reports ``null`` and says why --
    inventing a plausible-looking resistivity would produce exactly the
    opaque constant this feature's provenance requirement exists to
    prevent."""
    model = resolve_substrate_model("sg13g2")
    assert model["resistivity_ohm_cm"] is None
    assert model["pdk_family"] is None
    assert any("no curated substrate resistivity" in w for w in model["warnings"])


# --------------------------------------------------------------------------- #
# The per-net report block
# --------------------------------------------------------------------------- #


def _tap(x_um: float, y_um: float, area_um2: float = 0.25) -> dict[str, float]:
    return {
        "x_um": x_um,
        "y_um": y_um,
        "area_um2": area_um2,
        "radius_um": effective_contact_radius_um(area_um2),
    }


def test_two_tap_report_is_a_pure_two_terminal_answer():
    report = substrate_spreading_report(
        "VSUB", [_tap(0.0, 0.0), _tap(30.0, 0.0)], "sky130"
    )
    assert report["model"] == SUBSTRATE_SPREADING_MODEL
    assert report["tap_count"] == 2
    assert report["pair_count"] == 1
    assert report["pairwise_approximation"] is False
    assert report["truncated"] is False
    pair = report["pairs"][0]
    assert pair["separation_um"] == pytest.approx(30.0)
    assert pair["regime"] == "far_field"
    assert pair["exceeds_half_space_depth"] is False
    assert pair["notes"] == []
    # Same number the pure closed form gives for this geometry.
    radius = effective_contact_radius_um(0.25)
    assert pair["resistance_ohm"] == pytest.approx(
        two_contact_spreading_resistance_ohm(radius, radius, 30.0, 10.0)
    )
    assert report["min_resistance_ohm"] == pytest.approx(pair["resistance_ohm"])
    assert report["max_resistance_ohm"] == pytest.approx(pair["resistance_ohm"])
    # Provenance rides along with the number, not in a separate lookup.
    assert report["resistivity_ohm_cm"] == pytest.approx(10.0)
    assert report["substrate_thickness_um"] == pytest.approx(725.0)
    assert report["pdk_family"] == "sky130"
    assert "resistivity_ohm_cm" in report["source"]


def test_more_than_two_taps_reports_every_pair_and_declares_the_caveat():
    """Issue #2561's stated choice: pairwise estimates rather than a
    rejection -- but flagged, because the set of pairs is not a network
    solve (the N-terminal case issue #2515 still tracks)."""
    report = substrate_spreading_report(
        "VSUB", [_tap(0.0, 0.0), _tap(30.0, 0.0), _tap(0.0, 30.0)], "sky130"
    )
    assert report["tap_count"] == 3
    assert report["pair_count"] == 3
    assert report["pairwise_approximation"] is True
    assert {(p["a"], p["b"]) for p in report["pairs"]} == {(0, 1), (0, 2), (1, 2)}
    assert all(p["resistance_ohm"] > 0.0 for p in report["pairs"])


def test_near_field_pair_is_reported_with_its_accuracy_caveat():
    radius = effective_contact_radius_um(0.25)
    separation = 1.5 * (2.0 * radius)  # inside FAR_FIELD_SEPARATION_RATIO
    report = substrate_spreading_report(
        "VSUB", [_tap(0.0, 0.0), _tap(separation, 0.0)], "sky130"
    )
    pair = report["pairs"][0]
    assert pair["regime"] == "near_field"
    assert pair["resistance_ohm"] > 0.0
    assert any("not 1%-accurate" in note for note in pair["notes"])


def test_overlapping_taps_report_no_resistance_rather_than_an_artefact():
    report = substrate_spreading_report(
        "VSUB", [_tap(0.0, 0.0), _tap(0.1, 0.0)], "sky130"
    )
    pair = report["pairs"][0]
    assert pair["regime"] == "overlapping"
    assert pair["resistance_ohm"] is None
    assert report["min_resistance_ohm"] is None


def test_separation_beyond_half_the_wafer_thickness_is_flagged():
    separation = HALF_SPACE_DEPTH_RATIO * 725.0 + 1.0
    report = substrate_spreading_report(
        "VSUB", [_tap(0.0, 0.0), _tap(separation, 0.0)], "sky130"
    )
    pair = report["pairs"][0]
    assert pair["exceeds_half_space_depth"] is True
    assert any("half-space" in note for note in pair["notes"])
    # Still reported: the flag is the caveat, not a suppression.
    assert pair["resistance_ohm"] > 0.0


def test_uncurated_family_reports_geometry_without_a_resistance():
    report = substrate_spreading_report(
        "VSUB", [_tap(0.0, 0.0), _tap(30.0, 0.0)], "sg13g2"
    )
    assert report["resistivity_ohm_cm"] is None
    assert report["pairs"][0]["resistance_ohm"] is None
    assert report["tap_count"] == 2


def test_tap_count_beyond_the_cap_is_truncated_and_says_so():
    taps = [_tap(float(i) * 10.0, 0.0, 0.25 + i * 0.01) for i in range(40)]
    report = substrate_spreading_report("VSUB", taps, "sky130", max_taps=8)
    assert report["taps_total"] == 40
    assert report["tap_count"] == 8
    assert report["truncated"] is True
    assert report["pair_count"] == 8 * 7 // 2
    assert any("only the 8 largest" in w for w in report["warnings"])
    # Deterministic (x, y) ordering survives the largest-area selection.
    assert [t["x_um"] for t in report["taps"]] == sorted(
        t["x_um"] for t in report["taps"]
    )


def test_single_tap_reports_the_contact_with_no_pair():
    report = substrate_spreading_report("VSUB", [_tap(0.0, 0.0)], "sky130")
    assert report["tap_count"] == 1
    assert report["pairs"] == []
    assert any("single substrate-tap contact" in w for w in report["warnings"])


def test_no_taps_at_all_is_an_error_not_an_empty_block():
    with pytest.raises(SubstrateSpreadingError, match="no substrate-tap geometry"):
        substrate_spreading_report("VSUB", [], "sky130")


def test_tap_contacts_from_region_merges_abutting_boxes_and_sorts_by_position():
    """Two abutting drawn boxes are one physical contact; an unmerged read
    would pair them against each other at a separation smaller than either
    one's own radius."""
    region = kdb.Region()
    region.insert(kdb.Box(0, 0, 1000, 1000))
    region.insert(kdb.Box(1000, 0, 2000, 1000))  # abuts the first
    region.insert(kdb.Box(50000, 0, 51000, 1000))  # genuinely separate
    contacts = tap_contacts_from_region(region, 0.001)
    assert len(contacts) == 2
    assert contacts[0]["area_um2"] == pytest.approx(2.0)  # 2 um x 1 um, merged
    assert contacts[0]["x_um"] == pytest.approx(1.0)
    assert contacts[1]["x_um"] == pytest.approx(50.5)
    assert contacts[0]["radius_um"] == pytest.approx(effective_contact_radius_um(2.0))


# --------------------------------------------------------------------------- #
# Integration: klt extract --parasitics --substrate-spreading
# --------------------------------------------------------------------------- #

#: Centre-to-centre separation, in micrometres, between the two substrate
#: taps `_make_two_tap_layout` draws -- the number the integration test's
#: expected resistance is derived from.
_TWO_TAP_SEPARATION_UM = 20.0


def _make_two_tap_layout() -> kdb.Layout:
    """The standard inverter fixture plus a **second** substrate tap, 20 um
    from the first, strapped to it on li1 so both sit on one net ``VSUBRING``.

    This is the geometry `klt extract --parasitics` shorts today: one
    ``substrate_dc_tie`` shunt for the whole net, nothing distance-dependent
    between the two tap locations.
    """
    layout = _make_inverter_layout(substrate_tap_label="VSUBRING")
    top = layout.top_cell()

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    # Second substrate tap: same shape as the fixture's own tap ring
    # (200 x 600 dbu = 0.2 x 0.6 um), 20000 dbu = 20 um to its right.
    draw(65, 44, kdb.Box(19600, -800, 19800, -200))  # tap.drawing
    draw(66, 44, kdb.Box(19620, -650, 19780, -550))  # licon1
    # One li1 strap spanning both taps: the two tap contacts are one
    # electrical net in the extracted netlist, which is precisely the
    # "shorted today" case this estimate quantifies.
    draw(67, 20, kdb.Box(-450, -700, 19850, -500))
    return layout


def test_extract_reports_a_finite_positive_provenance_tagged_estimate(tmp_path):
    """End to end on real tap geometry: two drawn substrate taps on one net
    produce a finite, positive, provenance-tagged spreading resistance where
    the netlist alone says "shorted"."""
    path = _write_gds(_make_two_tap_layout(), tmp_path / "twotap.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "twotap.spice"),
        parasitics=True,
        substrate_spreading_net="VSUBRING",
    )

    block = report["parasitics"]["substrate_spreading"]
    assert block["model"] == SUBSTRATE_SPREADING_MODEL
    assert block["net"] == "VSUBRING"
    assert block["tap_count"] == 2
    assert block["pair_count"] == 1
    assert block["pairwise_approximation"] is False

    pair = block["pairs"][0]
    assert pair["separation_um"] == pytest.approx(_TWO_TAP_SEPARATION_UM, rel=1e-9)
    assert pair["regime"] == "far_field"
    assert pair["exceeds_half_space_depth"] is False
    assert math.isfinite(pair["resistance_ohm"])
    assert pair["resistance_ohm"] > 0.0

    # The drawn taps are 0.2 x 0.6 um, so each has an equal-area radius of
    # sqrt(0.12/pi) um; with the curated 10 ohm-cm substrate the pair is a
    # few hundred kilohm -- the order of magnitude a sub-micron contact in
    # lightly-doped silicon genuinely has, and the reason real designs use
    # tap *arrays* rather than single contacts.
    radius = effective_contact_radius_um(0.2 * 0.6)
    assert pair["resistance_ohm"] == pytest.approx(
        two_contact_spreading_resistance_ohm(
            radius, radius, _TWO_TAP_SEPARATION_UM, 10.0
        ),
        rel=1e-6,
    )
    assert 1.0e5 < pair["resistance_ohm"] < 1.0e6

    # Provenance, not an opaque constant.
    assert block["resistivity_ohm_cm"] == pytest.approx(10.0)
    assert block["substrate_thickness_um"] == pytest.approx(725.0)
    assert block["pdk_family"] == "sky130"
    assert "resistivity_ohm_cm" in block["source"]

    # ...and the pre-existing DC-tie model is untouched alongside it: this
    # block reports the magnitude that model omits, it does not replace it.
    assert report["parasitics"]["substrate_dc_tie"]["node_scope"] == "global"


def test_extract_without_the_flag_reports_null_and_changes_nothing(tmp_path):
    """Additive-capability guarantee: the block is present (schema
    stability) and ``null`` when the flag was never given."""
    path = _write_gds(_make_two_tap_layout(), tmp_path / "twotap.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "twotap.spice"),
        parasitics=True,
    )
    assert report["parasitics"]["substrate_spreading"] is None


def test_substrate_spreading_requires_parasitics(tmp_path):
    path = _write_gds(_make_two_tap_layout(), tmp_path / "twotap.gds")
    with pytest.raises(
        ExtractError, match="--substrate-spreading requires --parasitics"
    ):
        run_extract(
            path,
            "sky130",
            output=str(tmp_path / "twotap.spice"),
            substrate_spreading_net="VSUBRING",
        )


def test_substrate_spreading_on_an_unknown_net_is_an_error(tmp_path):
    path = _write_gds(_make_two_tap_layout(), tmp_path / "twotap.gds")
    with pytest.raises(ExtractError, match="matches no extracted net"):
        run_extract(
            path,
            "sky130",
            output=str(tmp_path / "twotap.spice"),
            parasitics=True,
            substrate_spreading_net="NOSUCHNET",
        )


def test_substrate_spreading_on_a_net_with_no_tap_is_an_error(tmp_path):
    """``Y`` is a real, extracted net -- it just has no substrate tie. An
    empty block would read as "negligible"; an error reads as "nothing was
    measured", which is what actually happened."""
    path = _write_gds(_make_two_tap_layout(), tmp_path / "twotap.gds")
    with pytest.raises(ExtractError, match="no substrate-tap geometry"):
        run_extract(
            path,
            "sky130",
            output=str(tmp_path / "twotap.spice"),
            parasitics=True,
            substrate_spreading_net="Y",
        )


def test_cli_surfaces_the_flag_and_the_json_block(tmp_path):
    """The JSON envelope is the contract: drive the real ``klt extract``
    entry point rather than only the Python API."""
    path = _write_gds(_make_two_tap_layout(), tmp_path / "twotap.gds")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "klayout_tools.cli",
            "extract",
            path,
            "--deck",
            "sky130",
            "--output",
            str(tmp_path / "twotap.spice"),
            "--parasitics",
            "--substrate-spreading",
            "VSUBRING",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    block = payload["parasitics"]["substrate_spreading"]
    assert block["net"] == "VSUBRING"
    assert block["pairs"][0]["resistance_ohm"] > 0.0

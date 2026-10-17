"""Executable design spike for GF180MCU DF.12 / DF.13 / DF.14 (issue #2371).

Test-only: no production rule is enabled and nothing in ``src/`` changes. See
``docs/design/gf180mcu-comp-checks-spike.md`` for the source analysis, the
measured results and the integration plan these tests back.

Two tiers, kept honest about what actually executed:

1. **Hand-derived expectations** (always run, ``klayout.db`` only): expected
   verdicts come from geometric reasoning about each fixture, never from the
   code under test. They exercise the recommended evaluator (``evaluate`` /
   ``evaluate_port_upstream_clip``) and demonstrate the original proposal's
   counterexamples.
2. **Differential vs the upstream Ruby rule deck** (needs a real ``klayout``
   binary; set ``KLT_KLAYOUT_BIN`` or put ``klayout`` on ``PATH``): the
   verbatim upstream expressions are executed under ``klayout -b`` and the
   violation *geometry* is compared (symmetric difference), not counts. When
   the binary is absent these tests are SKIPPED, and a skipped comparison is
   not agreement -- the design note records the executed outcome.
"""

from __future__ import annotations

import pytest

pytest.importorskip("klayout.db")

import klayout.db as kdb  # noqa: E402

from helpers import gf180mcu_comp_spike as sp  # noqa: E402

KLAYOUT = sp.find_klayout_binary()
needs_klayout = pytest.mark.skipif(
    KLAYOUT is None,
    reason="no klayout binary (set KLT_KLAYOUT_BIN); differential not executed",
)


@pytest.fixture(scope="module", params=sp.DBU_SCALES, ids=lambda d: f"dbu{d:g}")
def dbu(request):
    return request.param


@pytest.fixture(scope="module")
def fixtures(dbu):
    return {fx.name: fx for fx in sp.build_fixtures(dbu)}


@pytest.fixture(scope="module")
def upstream(dbu, fixtures):
    """Upstream results for every fixture at this DBU (one klayout launch)."""
    if KLAYOUT is None:
        pytest.skip("no klayout binary")
    return sp.run_upstream_batch({n: f for n, f in fixtures.items()}, KLAYOUT)


def port(rid, fx, dbu):
    return sp.evaluate_port_upstream_clip(sp.RULES[rid], fx.regions(), dbu)


def orig(rid, fx, dbu):
    regs = fx.regions()
    if rid == "DF.12":
        return sp.original_df12(regs)
    return sp.original_proposal(sp.RULES[rid], regs, dbu)


def flagged(r: kdb.Region) -> bool:
    return not r.is_empty()


# --------------------------------------------------------------------------
# Tier 1: hand-derived expectations
# --------------------------------------------------------------------------


class TestDF12:
    def test_absent_implants_flag_whole_comp(self, fixtures, dbu):
        r = port("DF.12", fixtures["df12_absent_implants"], dbu)
        assert sp.area_um2(r, dbu) == pytest.approx(8.0)

    def test_full_coverage_clean(self, fixtures, dbu):
        assert not flagged(port("DF.12", fixtures["df12_full_nplus"], dbu))

    def test_partial_coverage_reports_only_uncovered_part(self, fixtures, dbu):
        r = port("DF.12", fixtures["df12_partial_nplus"], dbu)
        assert sp.area_um2(r, dbu) == pytest.approx(4.0)  # right half only
        assert r.bbox().left * dbu == pytest.approx(2.0)

    def test_joint_n_and_p_coverage_clean(self, fixtures, dbu):
        assert not flagged(port("DF.12", fixtures["df12_joint_n_p"], dbu))

    def test_marker_exempts_whole_polygon_not_just_marker_area(self, fixtures, dbu):
        fx = fixtures["df12_partial_marker"]
        assert not flagged(port("DF.12", fx, dbu))
        # Counterexample to the area-subtraction reading: leaves the 2 um sliver
        # between the implant and the marker.
        assert sp.area_um2(orig("DF.12", fx, dbu), dbu) == pytest.approx(4.0)

    def test_marker_edge_touch_exempts(self, fixtures, dbu):
        fx = fixtures["df12_marker_edge_touch"]
        assert not flagged(port("DF.12", fx, dbu))
        assert flagged(orig("DF.12", fx, dbu))


@pytest.mark.parametrize("tag", ["13", "14"])
class TestTapQualitative:
    def test_zero_taps_flags_active(self, fixtures, dbu, tag):
        r = port(f"DF.{tag}_LV", fixtures[f"df{tag}_zero_taps"], dbu)
        assert sp.area_um2(r, dbu) == pytest.approx(4.0)

    def test_near_tap_clean(self, fixtures, dbu, tag):
        assert not flagged(port(f"DF.{tag}_LV", fixtures[f"df{tag}_near_tap"], dbu))

    def test_remote_tap_flags(self, fixtures, dbu, tag):
        assert flagged(port(f"DF.{tag}_LV", fixtures[f"df{tag}_remote_tap"], dbu))

    def test_no_active_is_inapplicable_not_a_violation(self, fixtures, dbu, tag):
        for v in ("LV", "MV"):
            assert not flagged(
                port(f"DF.{tag}_{v}", fixtures[f"df{tag}_no_active"], dbu)
            )

    def test_long_active_one_end_near_is_selected_whole_not_clipped(
        self, fixtures, dbu, tag
    ):
        fx = fixtures[f"df{tag}_long_active_one_end"]
        # Upstream selects whole polygons: touching the sized tap region is legal.
        assert not flagged(port(f"DF.{tag}_LV", fx, dbu))
        # The original "every point within D" reading rejects the far remainder.
        o = orig(f"DF.{tag}_LV", fx, dbu)
        assert flagged(o)
        assert o.bbox().right * dbu == pytest.approx(45.0)

    def test_resistor_marker_removes_tap(self, fixtures, dbu, tag):
        assert flagged(port(f"DF.{tag}_LV", fixtures[f"df{tag}_res_mk_over_tap"], dbu))


class TestWellIslands:
    def test_tap_across_legal_0p6_gap_does_not_count(self, fixtures, dbu):
        fx = fixtures["df13_island_gap_0p6"]
        assert flagged(port("DF.13_LV", fx, dbu))
        # Original one-shot expansion leaks across the gap: accepts the layout.
        assert not flagged(orig("DF.13_LV", fx, dbu))

    def test_same_geometry_in_one_well_is_clean(self, fixtures, dbu):
        assert not flagged(
            port("DF.13_LV", fixtures["df13_island_control_joined"], dbu)
        )

    def test_u_well_euclid_near_but_path_far(self, fixtures, dbu):
        fx = fixtures["df13_u_well_notch"]
        assert flagged(port("DF.13_LV", fx, dbu))
        assert not flagged(orig("DF.13_LV", fx, dbu))


class TestDeepWellContext:
    def test_dnwell_only_well_is_not_clipped_in_upstream_df13(self, fixtures, dbu):
        """Measured upstream quirk: DF.13 expansion is clipped with raw ``nwell``
        while the active/tap derivations use ``all_nwell`` (dnwell-lvpwell + nwell).
        In a dnwell-only well the tap's expansion is empty, so a tap 5 um away
        does not count. A port that clips with ``all_nwell`` silently disagrees."""
        fx = fixtures["df13_dnwell_only"]
        assert flagged(port("DF.13_LV", fx, dbu))  # upstream-faithful
        assert not flagged(
            sp.evaluate(sp.RULES["DF.13_LV"], fx.regions(), dbu)
        )  # all_nwell clip
        assert not flagged(port("DF.13_LV", fixtures["df13_dnwell_plus_nwell"], dbu))

    def test_dnwell_under_lvpwell_is_not_a_well(self, fixtures, dbu):
        # pcomp in dnwell&lvpwell is a substrate tap; the nplus comp 5 um away is
        # a substrate-domain active within reach of it -> clean for DF.14.
        assert not flagged(port("DF.14_LV", fixtures["df14_dnwell_under_lvpwell"], dbu))


class TestVoltageSelection:
    def test_lv_vs_mv_same_geometry(self, fixtures, dbu):
        assert flagged(port("DF.13_LV", fixtures["df13_v_none"], dbu))
        assert not flagged(port("DF.13_MV", fixtures["df13_v_none"], dbu))
        assert not flagged(port("DF.13_LV", fixtures["df13_v_dualgate"], dbu))
        assert flagged(port("DF.13_MV", fixtures["df13_v_dualgate"], dbu))

    @pytest.mark.parametrize("name", ["df13_v_v5_only", "df13_v_dualgate_edge_touch"])
    def test_unchecked_gaps_in_upstream_partition(self, fixtures, dbu, name):
        """Upstream LV = not_interacting(v5_xtor, dualgate); MV = overlapping(dualgate).
        v5_xtor without dualgate, and dualgate that only touches, fall in neither:
        a 25 um-remote active is accepted. Documented, not fixed, by the spike."""
        for v in ("LV", "MV"):
            assert not flagged(port(f"DF.13_{v}", fixtures[name], dbu))

    def test_dualgate_with_v5_is_mv(self, fixtures, dbu):
        assert not flagged(port("DF.13_LV", fixtures["df13_v_dualgate_and_v5"], dbu))
        assert flagged(port("DF.13_MV", fixtures["df13_v_dualgate_and_v5"], dbu))


@pytest.mark.parametrize("tag", ["13", "14"])
@pytest.mark.parametrize("vt", ["lv", "mv"])
class TestAxialBoundary:
    """Axial gap D-1 / D / D+1 DBU: expected = 'whole polygon interacts with the
    D-sized tap region' (touching is legal), i.e. legal at <= D, flagged > D."""

    def test_axial(self, fixtures, dbu, tag, vt):
        rid = f"DF.{tag}_{vt.upper()}"
        assert not flagged(port(rid, fixtures[f"df{tag}_{vt}_axial_dm1"], dbu))
        assert not flagged(port(rid, fixtures[f"df{tag}_{vt}_axial_d0"], dbu))
        assert flagged(port(rid, fixtures[f"df{tag}_{vt}_axial_dp1"], dbu))

    def test_original_rejects_the_legal_boundary(self, fixtures, dbu, tag, vt):
        rid = f"DF.{tag}_{vt.upper()}"
        # active - tap.sized(D) leaves the whole polygon when it starts at D.
        assert flagged(orig(rid, fixtures[f"df{tag}_{vt}_axial_d0"], dbu))
        assert flagged(orig(rid, fixtures[f"df{tag}_{vt}_axial_dm1"], dbu))


@pytest.mark.parametrize("vt", ["lv", "mv"])
class TestDiagonalBoundary:
    """3-4-5 corner offsets (Euclidean corner distance = pct% of D)."""

    def test_df14_is_euclidean_strict(self, fixtures, dbu, vt):
        rid = f"DF.14_{vt.upper()}"
        assert not flagged(port(rid, fixtures[f"df14_{vt}_diag_dm1"], dbu))
        # exactly D (not < D): not 'good' under the separation refinement
        assert flagged(port(rid, fixtures[f"df14_{vt}_diag_d0"], dbu))
        assert flagged(port(rid, fixtures[f"df14_{vt}_diag_x104"], dbu))

    def test_df13_octagon_is_more_permissive_than_euclid(self, fixtures, dbu, vt):
        rid = f"DF.13_{vt.upper()}"
        # legal at D-1, D and D+1 DBU on this diagonal (octagon over-reach) ...
        for k in ("dm1", "d0", "dp1"):
            assert not flagged(port(rid, fixtures[f"df13_{vt}_diag_{k}"], dbu))
        # ... but still bounded: flagged by 110% of D.
        assert flagged(port(rid, fixtures[f"df13_{vt}_diag_x110"], dbu))


class TestHierarchy:
    def test_hier_equals_flat(self, fixtures, dbu):
        a = port("DF.13_LV", fixtures["df13_hier_flat"], dbu)
        b = port("DF.13_LV", fixtures["df13_hier_hier"], dbu)
        assert sp.same(a, b)
        assert sp.area_um2(a, dbu) == pytest.approx(4.0)  # only the 35 um copy


class TestDescriptorAndMissingLayers:
    def test_empty_taps_never_hide_a_check(self, dbu):
        regs = {n: kdb.Region() for n in sp.LAYER_MAP}
        u = int(round(1 / dbu))
        regs["comp"] = kdb.Region(kdb.Box(0, 0, 2 * u, 2 * u))
        regs["pplus"] = kdb.Region(kdb.Box(-u, -u, 3 * u, 3 * u))
        regs["nwell"] = kdb.Region(kdb.Box(-5 * u, -5 * u, 8 * u, 8 * u))
        assert flagged(sp.evaluate_port_upstream_clip(sp.RULES["DF.13_LV"], regs, dbu))

    def test_absent_checked_active_is_inapplicable(self, dbu):
        regs = {n: kdb.Region() for n in sp.LAYER_MAP}
        for rid in sp.RULES:
            assert not flagged(sp.evaluate_port_upstream_clip(sp.RULES[rid], regs, dbu))

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"well_domain": "bad", "voltage": "lv", "max_distance_um": 20.0},
            {"well_domain": "inside", "voltage": "hv", "max_distance_um": 20.0},
            {"well_domain": "inside", "voltage": "lv", "max_distance_um": 0.0},
            {"well_domain": "inside", "voltage": "lv", "max_distance_um": 20.3},
        ],
    )
    def test_malformed_tap_descriptor_rejected(self, kwargs):
        with pytest.raises(ValueError):
            sp.TapDistanceRule("X", **kwargs)

    def test_malformed_coverage_descriptor_rejected(self):
        with pytest.raises(ValueError):
            sp.CompCoverageRule("X", covered_by_any=())
        with pytest.raises(ValueError):
            sp.CompCoverageRule("X", checked="no_such_layer")

    def test_example_declarations_match_drm_numbers(self):
        assert sp.RULES["DF.13_LV"].max_distance_um == 20.0
        assert sp.RULES["DF.13_MV"].max_distance_um == 15.0
        assert sp.RULES["DF.14_LV"].max_distance_um == 20.0
        assert sp.RULES["DF.14_MV"].max_distance_um == 15.0


# --------------------------------------------------------------------------
# Tier 2: differential against the verbatim upstream Ruby expressions
# --------------------------------------------------------------------------


@needs_klayout
class TestDifferentialAgainstUpstream:
    def test_port_matches_upstream_geometry_on_every_fixture(
        self, fixtures, dbu, upstream
    ):
        mismatches = []
        for name, fx in fixtures.items():
            regs = fx.regions()
            for rid, rule in sp.RULES.items():
                got = sp.evaluate_port_upstream_clip(rule, regs, dbu)
                if not sp.same(got, upstream[name][rid]):
                    mismatches.append((name, rid))
        assert not mismatches, mismatches

    def test_upstream_actually_flags_something(self, upstream):
        # Guard against a vacuous harness (e.g. Ruby script silently emitting nothing).
        assert any(flagged(r) for per in upstream.values() for r in per.values())
        assert flagged(upstream["df13_remote_tap"]["DF.13_LV"])
        assert not flagged(upstream["df13_near_tap"]["DF.13_LV"])

    def test_upstream_hier_equals_flat(self, upstream):
        assert sp.same(
            upstream["df13_hier_flat"]["DF.13_LV"],
            upstream["df13_hier_hier"]["DF.13_LV"],
        )

    def test_original_proposal_is_measurably_rejected(self, fixtures, dbu, upstream):
        """Counterexamples: the original algorithm disagrees with upstream here."""
        cases = [
            ("df13_long_active_one_end", "DF.13_LV"),
            ("df14_long_active_one_end", "DF.14_LV"),
            ("df13_island_gap_0p6", "DF.13_LV"),
            ("df13_u_well_notch", "DF.13_LV"),
            ("df13_lv_axial_d0", "DF.13_LV"),
            ("df14_mv_axial_d0", "DF.14_MV"),
            ("df13_dnwell_only", "DF.13_LV"),
            ("df12_partial_marker", "DF.12"),
            ("df12_marker_edge_touch", "DF.12"),
        ]
        for name, rid in cases:
            o = orig(rid, fixtures[name], dbu)
            assert not sp.same(o, upstream[name][rid]), (name, rid)

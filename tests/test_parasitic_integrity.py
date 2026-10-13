"""Synthetic-graph tests for the per-net parasitic RC network integrity
checker (issue #2764).

``klayout_tools.parasitic_integrity`` is a pure module: every diagnostic is
driven here by a hand-built :class:`ParasiticNetworkRecord` -- no klayout,
no layout fixtures -- so each status value, boundary (the inclusive
short-candidate threshold, the capacitance rounding tolerance) and
degenerate input (zero/one terminal, repeated display names, NaN/Infinity
resistor values) has its own minimal fixture. The end-to-end wiring (a real
``run_extract`` attaching these blocks and ``klt pex`` propagating them) is
covered by ``tests/test_extract.py`` and ``tests/test_pex.py``.
"""

from __future__ import annotations

import json
import math
import time

import pytest

from klayout_tools.parasitic_integrity import (
    CAP_TOLERANCE_ABS_FF,
    CAP_TOLERANCE_REL,
    DEFAULT_SHORT_THRESHOLD_OHM,
    GROUND_OWNER,
    MAX_DETAIL_ITEMS,
    IntegrityRecordBuilder,
    ParasiticNetworkRecord,
    check_parasitic_network,
    empty_integrity_summary,
)

# ---------------------------------------------------------------------------
# Record builders -- one healthy net per topology, corrupted per-test.
# ---------------------------------------------------------------------------


def _distributed_builder() -> IntegrityRecordBuilder:
    """One healthy 2-terminal distributed net (``clk``, net_id 1): two leg
    nodes joined by one segment resistor, each leg with its own ground
    capacitor summing back to the declared 2.0 fF."""
    b = IntegrityRecordBuilder()
    ground = b.add_node("vsubs", GROUND_OWNER)
    n0 = b.add_node("clk__t0", 1)
    n1 = b.add_node("clk__t1", 1)
    b.add_resistor("clk_seg0", n0, n1, 5.0, 1)
    b.add_ground_capacitor("clk_n0", n0, ground, 1.0, 1)
    b.add_ground_capacitor("clk_n1", n1, ground, 1.0, 1)
    b.add_net(1, "clk", "distributed", (n0, n1), 2.0)
    return b


def _lumped_builder() -> IntegrityRecordBuilder:
    """One healthy 2-terminal lumped star net (``a``, net_id 1): two leg
    resistors onto the hub (the net node itself), one hub ground capacitor
    carrying the declared 2.0 fF."""
    b = IntegrityRecordBuilder()
    ground = b.add_node("vsubs", GROUND_OWNER)
    hub = b.add_node("a", 1)
    leg0 = b.add_node("a__t0", 1)
    leg1 = b.add_node("a__t1", 1)
    b.add_resistor("a_t0", leg0, hub, 3.0, 1)
    b.add_resistor("a_t1", leg1, hub, 3.0, 1)
    b.add_ground_capacitor("a", hub, ground, 2.0, 1)
    b.add_net(1, "a", "lumped", (leg0, leg1), 2.0)
    return b


def _build(b: IntegrityRecordBuilder, **overrides: object) -> ParasiticNetworkRecord:
    return b.build(ParasiticNetworkRecord(**overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# open: distributed pass / open, lumped not_applicable, degenerate terminals
# ---------------------------------------------------------------------------


class TestOpen:
    def test_distributed_connected_terminals_pass(self):
        result = check_parasitic_network(_build(_distributed_builder()))
        (net,) = result["nets"]
        assert net["open"] == {
            "status": "pass",
            "reason": None,
            "disconnected_count": 0,
            "disconnected_terminals": [],
            "details_truncated": False,
        }
        assert result["open_nets"] == 0

    def test_dropped_segment_opens_the_ladder(self):
        """The canary: a ladder missing its segment leaves both required
        terminals disconnected -- `open`, with the count complete and the
        terminals named deterministically."""
        b = _distributed_builder()
        b._resistors.clear()  # drop the one segment
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["open"]["status"] == "open"
        assert net["open"]["reason"]
        assert net["open"]["disconnected_count"] == 1
        # The reference terminal is the first required one; the *other*
        # terminal is the one reported disconnected.
        assert net["open"]["disconnected_terminals"] == ["clk__t1"]
        assert net["open"]["details_truncated"] is False
        assert result["open_nets"] == 1
        assert result["nets_with_findings"] == 1

    def test_middle_segment_open_reports_far_half(self):
        """A 3-terminal ladder with its second segment dropped keeps the
        first two terminals connected and isolates the third."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        n0 = b.add_node("w__t0", 1)
        n1 = b.add_node("w__t1", 1)
        n2 = b.add_node("w__t2", 1)
        b.add_resistor("w_seg0", n0, n1, 2.0, 1)
        b.add_ground_capacitor("w_n0", n0, ground, 0.5, 1)
        b.add_ground_capacitor("w_n1", n1, ground, 0.5, 1)
        b.add_ground_capacitor("w_n2", n2, ground, 0.5, 1)
        b.add_net(1, "w", "distributed", (n0, n1, n2), 1.5)
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["open"]["status"] == "open"
        assert net["open"]["disconnected_count"] == 1
        assert net["open"]["disconnected_terminals"] == ["w__t2"]

    def test_lumped_model_is_not_applicable(self):
        """The star/Gamma-shunt model encodes no measurable
        terminal-to-terminal evidence, so the open check reports
        `not_applicable` with its reason -- never a pass, never an open."""
        result = check_parasitic_network(_build(_lumped_builder()))
        (net,) = result["nets"]
        assert net["open"]["status"] == "not_applicable"
        assert "lumped" in net["open"]["reason"]
        assert net["open"]["disconnected_count"] == 0
        assert result["open_nets"] == 0

    def test_zero_required_terminals_is_not_applicable(self):
        """Only a *distributed* net can reach the zero-terminals branch
        (lumped reports not_applicable on model grounds first): with no
        required terminals there is nothing to connect."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        net_node = b.add_node("stub", 1)
        b.add_ground_capacitor("stub", net_node, ground, 1.0, 1)
        b.add_net(1, "stub", "distributed", (), 1.0)
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["open"]["status"] == "not_applicable"
        assert "no required terminals" in net["open"]["reason"]

    def test_one_required_terminal_is_vacuously_connected(self):
        """A single required terminal has no other terminal to be
        disconnected from -- documented vacuous `pass` (the extractor never
        emits this shape for a ladder; the checker still defines it)."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        n0 = b.add_node("solo__t0", 1)
        n1 = b.add_node("solo__t1", 1)
        b.add_resistor("solo_seg0", n0, n1, 1.0, 1)
        b.add_ground_capacitor("solo_n0", n0, ground, 1.0, 1)
        b.add_ground_capacitor("solo_n1", n1, ground, 1.0, 1)
        b.add_net(1, "solo", "distributed", (n0,), 2.0)
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["open"]["status"] == "pass"


# ---------------------------------------------------------------------------
# islands and orphan nodes
# ---------------------------------------------------------------------------


class TestIslandsAndOrphans:
    def test_floating_capacitor_chunk_is_an_island(self):
        """A node cluster with no required terminal (here: a node whose
        only attachment is a ground capacitor) is a floating island."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        n0 = b.add_node("clk__t0", 1)
        n1 = b.add_node("clk__t1", 1)
        float_node = b.add_node("clk__t2", 1)
        b.add_resistor("clk_seg0", n0, n1, 5.0, 1)
        b.add_ground_capacitor("clk_n0", n0, ground, 1.0, 1)
        b.add_ground_capacitor("clk_n1", n1, ground, 1.0, 1)
        b.add_ground_capacitor("clk_n2", float_node, ground, 1.0, 1)
        b.add_net(1, "clk", "distributed", (n0, n1), 2.0)
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["islands"]["count"] == 1
        assert net["islands"]["details"] == [{"nodes": ["clk__t2"]}]
        assert net["islands"]["details_truncated"] is False
        assert result["island_components"] == 1
        # The island's node is attached (the capacitor), so no orphans.
        assert net["orphan_nodes"]["count"] == 0

    def test_zero_required_terminals_makes_islands_not_applicable(self):
        """With no required terminals there is no main body to detach from
        -- the Gamma-shunt stub shape reports `not_applicable`, count 0,
        rather than branding its only component an island."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        net_node = b.add_node("stub", 1)
        hub = b.add_node("stub__par", 1)
        b.add_resistor("stub", net_node, hub, 1.0, 1)
        b.add_ground_capacitor("stub", hub, ground, 1.0, 1)
        b.add_net(1, "stub", "lumped", (), 1.0)
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["islands"]["count"] == 0
        assert "no required terminals" in net["islands"]["reason"]

    def test_inductor_joins_dc_connectivity(self):
        """The --mom-rlc-net splice (hub --L--> l_node --C--> ground) is one
        DC component: the inductor conducts, so the spliced node is neither
        an island nor detached from the terminal-bearing body."""
        b = _lumped_builder()
        hub = 1  # the net node, per _lumped_builder's registration order
        l_node = b.add_node("a__l", 1)
        b.add_inductor("a_l", hub, l_node, 1)
        b.add_ground_capacitor("a_c", l_node, 0, 0.0, 1)  # ground uid 0
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["islands"]["count"] == 0
        assert net["orphan_nodes"]["count"] == 0

    def test_detached_inductor_node_is_an_island(self):
        """Without the inductor, the spliced node plus its capacitor float
        -- an island, exactly the bookkeeping defect the check exists for."""
        b = _lumped_builder()
        l_node = b.add_node("a__l", 1)
        b.add_ground_capacitor("a_c", l_node, 0, 0.0, 1)  # ground uid 0
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["islands"]["count"] == 1
        assert net["islands"]["details"] == [{"nodes": ["a__l"]}]

    def test_fully_detached_node_is_an_orphan(self):
        """A registered node with no element attached at all is an orphan
        node."""
        b = _distributed_builder()
        b.add_node("clk__orphan", 1)
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["orphan_nodes"]["count"] == 1
        assert net["orphan_nodes"]["details"] == [{"node": "clk__orphan"}]
        assert result["orphan_nodes"] == 1

    def test_capacitor_endpoint_is_not_an_orphan(self):
        """A legitimate capacitor endpoint (the coupling-capacitor hub of a
        net with no other attachment) is attached, not an orphan."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        hub_a = b.add_node("a", 1)
        hub_b = b.add_node("b", 2)
        b.add_ground_capacitor("a", hub_a, ground, 1.0, 1)
        b.add_coupling_capacitor("cc_a_b", hub_a, hub_b, 0.5)
        b.add_net(1, "a", "lumped", (), 1.0)
        b.add_net(2, "b", "lumped", (), 0.0)
        result = check_parasitic_network(_build(b))
        net_b = next(n for n in result["nets"] if n["net"] == "b")
        assert net_b["orphan_nodes"]["count"] == 0


# ---------------------------------------------------------------------------
# suspicious resistors: negative, zero, non-finite, self-loop
# ---------------------------------------------------------------------------


class TestSuspiciousResistors:
    @staticmethod
    def _one_resistor(resistance_ohm: float, **kwargs: object) -> dict:
        b = IntegrityRecordBuilder()
        n0 = b.add_node("clk__t0", 1)
        n1 = b.add_node("clk__t1", 1)
        b.add_resistor("clk_seg0", n0, n1, resistance_ohm, 1, **kwargs)
        b.add_net(1, "clk", "distributed", (n0, n1), 0.0)
        result = check_parasitic_network(_build(b))
        return result["nets"][0]["suspicious_resistors"]

    def test_healthy_resistor_produces_nothing(self):
        block = self._one_resistor(5.0)
        assert block == {
            "count": 0,
            "details": [],
            "details_truncated": False,
            "reason": None,
        }

    def test_negative(self):
        block = self._one_resistor(-2.0)
        assert block["count"] == 1
        assert block["details"][0]["issues"] == ["negative"]

    def test_zero(self):
        block = self._one_resistor(0.0)
        assert block["details"][0]["issues"] == ["zero"]

    def test_nan_is_non_finite_and_serializes_as_null(self):
        block = self._one_resistor(float("nan"))
        assert block["details"][0]["issues"] == ["non_finite"]
        assert block["details"][0]["resistance_ohm"] is None

    def test_infinity_is_non_finite(self):
        block = self._one_resistor(float("inf"))
        assert block["details"][0]["issues"] == ["non_finite"]

    def test_self_loop(self):
        b = IntegrityRecordBuilder()
        n0 = b.add_node("clk__t0", 1)
        b.add_resistor("clk_seg0", n0, n0, 5.0, 1)
        b.add_net(1, "clk", "distributed", (n0,), 0.0)
        result = check_parasitic_network(_build(b))
        block = result["nets"][0]["suspicious_resistors"]
        assert block["details"][0]["issues"] == ["self_loop"]

    def test_combined_issues_sort_stably(self):
        b = IntegrityRecordBuilder()
        n0 = b.add_node("clk__t0", 1)
        b.add_resistor("clk_seg0", n0, n0, -1.0, 1)
        b.add_net(1, "clk", "distributed", (n0,), 0.0)
        result = check_parasitic_network(_build(b))
        block = result["nets"][0]["suspicious_resistors"]
        assert block["details"][0]["issues"] == ["negative", "self_loop"]

    def test_details_are_sorted_by_device_name(self):
        b = IntegrityRecordBuilder()
        n0 = b.add_node("clk__t0", 1)
        n1 = b.add_node("clk__t1", 1)
        n2 = b.add_node("clk__t2", 1)
        # Registered out of name order; output must still sort by device.
        b.add_resistor("clk_seg2", n2, n2, 0.0, 1)
        b.add_resistor("clk_seg0", n0, n0, 0.0, 1)
        b.add_resistor("clk_seg1", n1, n1, 0.0, 1)
        b.add_net(1, "clk", "distributed", (n0, n1, n2), 0.0)
        result = check_parasitic_network(_build(b))
        names = [
            d["device"] for d in result["nets"][0]["suspicious_resistors"]["details"]
        ]
        assert names == ["clk_seg0", "clk_seg1", "clk_seg2"]

    def test_substrate_dc_tie_is_excluded(self):
        """The 1e12-ohm intentional tie is never a suspicious resistor --
        even though it is large, cross-net, and ownerless."""
        b = IntegrityRecordBuilder()
        n0 = b.add_node("clk__t0", 1)
        n1 = b.add_node("clk__t1", 1)
        b.add_resistor("clk_seg0", n0, n1, 5.0, 1)
        tied = b.add_node("vsubs", GROUND_OWNER)
        zero = b.add_node("0", GROUND_OWNER)
        b.add_resistor(
            "Rvsubs_dctie", tied, zero, 1e12, GROUND_OWNER, intentional_dc_tie=True
        )
        b.add_net(1, "clk", "distributed", (n0, n1), 0.0)
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["suspicious_resistors"]["count"] == 0
        assert net["short_candidates"]["count"] == 0


# ---------------------------------------------------------------------------
# short candidates: cross-net at/below the inclusive threshold
# ---------------------------------------------------------------------------


class TestShortCandidates:
    @staticmethod
    def _cross_net(threshold: float, resistance_ohm: float) -> dict:
        b = IntegrityRecordBuilder()
        a0 = b.add_node("a__t0", 1)
        b0 = b.add_node("b__t0", 2)
        b.add_resistor("x_r", a0, b0, resistance_ohm, 1)
        b.add_net(1, "a", "distributed", (a0,), 0.0)
        b.add_net(2, "b", "distributed", (b0,), 0.0)
        result = check_parasitic_network(_build(b, short_threshold_ohm=threshold))
        return result["nets"][0]["short_candidates"]

    def test_below_threshold_is_a_candidate(self):
        block = self._cross_net(1.0, 0.5)
        assert block["count"] == 1
        (detail,) = block["details"]
        assert detail["resistance_ohm"] == 0.5
        assert detail["owner_a"] == 1
        assert detail["owner_b"] == 2

    def test_threshold_boundary_is_inclusive(self):
        """A resistor measuring exactly the threshold is a candidate."""
        assert self._cross_net(1.0, 1.0)["count"] == 1

    def test_above_threshold_is_not(self):
        assert self._cross_net(1.0, 1.0 + 1e-9)["count"] == 0

    def test_within_net_low_r_is_never_a_candidate(self):
        """The extractor's own 1e-3-ohm clamp on a *within-net* segment is
        not a short, no matter how low the threshold-relative value."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        n0 = b.add_node("clk__t0", 1)
        n1 = b.add_node("clk__t1", 1)
        b.add_resistor("clk_seg0", n0, n1, 1e-3, 1)
        b.add_ground_capacitor("clk_n0", n0, ground, 1.0, 1)
        b.add_ground_capacitor("clk_n1", n1, ground, 1.0, 1)
        b.add_net(1, "clk", "distributed", (n0, n1), 2.0)
        result = check_parasitic_network(
            _build(b, short_threshold_ohm=DEFAULT_SHORT_THRESHOLD_OHM)
        )
        (net,) = result["nets"]
        assert net["short_candidates"]["count"] == 0

    def test_net_to_ground_resistor_is_a_candidate_with_ground_label(self):
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        n0 = b.add_node("clk__t0", 1)
        b.add_resistor("clk_leak", n0, ground, 0.1, 1)
        b.add_net(1, "clk", "distributed", (n0,), 0.0)
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["short_candidates"]["count"] == 1
        (detail,) = net["short_candidates"]["details"]
        assert detail["owner_a"] == 1
        assert detail["owner_b"] == "ground"

    def test_same_named_nets_stay_distinct(self):
        """Two nets sharing the display name `VGND` (issue #765's island
        case) are distinct identities: a resistor bridging them is
        cross-net, and owners -- not names -- say so."""
        b = IntegrityRecordBuilder()
        a0 = b.add_node("VGND", 7)
        b0 = b.add_node("VGND", 8)
        b.add_resistor("vgnd_bridge", a0, b0, 0.2, 7)
        b.add_net(7, "VGND", "distributed", (a0,), 0.0)
        b.add_net(8, "VGND", "distributed", (b0,), 0.0)
        result = check_parasitic_network(_build(b))
        net7 = next(n for n in result["nets"] if n["net_id"] == 7)
        net8 = next(n for n in result["nets"] if n["net_id"] == 8)
        # Attributed to exactly the net that emitted it -- once.
        assert net7["short_candidates"]["count"] == 1
        assert net8["short_candidates"]["count"] == 0
        (detail,) = net7["short_candidates"]["details"]
        assert (detail["owner_a"], detail["owner_b"]) == (7, 8)
        assert detail["node_a"] == detail["node_b"] == "VGND"


# ---------------------------------------------------------------------------
# capacitance reconciliation
# ---------------------------------------------------------------------------


class TestCapacitance:
    def test_distributed_sum_matches_declared(self):
        result = check_parasitic_network(_build(_distributed_builder()))
        (net,) = result["nets"]
        assert net["capacitance"]["status"] == "pass"
        assert net["capacitance"]["declared_ff"] == pytest.approx(2.0)
        assert net["capacitance"]["computed_ff"] == pytest.approx(2.0)
        assert net["capacitance"]["delta_ff"] == pytest.approx(0.0)

    def test_perturbed_capacitor_is_a_mismatch(self):
        """The canary: one emitted ground capacitor altered while the
        declared total stays fixed trips `mismatch` with the delta."""
        b = _distributed_builder()
        b._capacitors[1] = type(b._capacitors[1])(
            name="clk_n1",
            node_a=b._capacitors[1].node_a,
            node_b=0,
            capacitance_ff=0.5,
            kind="ground",
            owner_net_id=1,
        )
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["capacitance"]["status"] == "mismatch"
        assert net["capacitance"]["computed_ff"] == pytest.approx(1.5)
        assert net["capacitance"]["delta_ff"] == pytest.approx(-0.5)
        assert net["capacitance"]["reason"]
        assert result["capacitance_mismatches"] == 1
        assert result["nets_with_findings"] == 1

    def test_absolute_rounding_tolerance_is_respected(self):
        """Sub-tolerance round-trip noise (the report itself rounds to
        1e-6 fF) passes; anything beyond it mismatches."""
        for computed, expected in ((1.0 + 5e-7, "pass"), (1.0 + 2e-6, "mismatch")):
            b = IntegrityRecordBuilder()
            ground = b.add_node("vsubs", GROUND_OWNER)
            n0 = b.add_node("c__t0", 1)
            b.add_ground_capacitor("c_n0", n0, ground, computed, 1)
            b.add_net(1, "c", "lumped", (n0,), 1.0)
            result = check_parasitic_network(_build(b))
            assert result["nets"][0]["capacitance"]["status"] == expected

    def test_relative_tolerance_scales_with_the_total(self):
        """A 1 nF net tolerates noise scaling with its total: well inside
        the relative term passes, well beyond it mismatches."""
        declared = 1e6
        for offset, expected in (
            (CAP_TOLERANCE_REL * declared / 2.0, "pass"),
            (CAP_TOLERANCE_REL * declared * 2.0, "mismatch"),
        ):
            b = IntegrityRecordBuilder()
            ground = b.add_node("vsubs", GROUND_OWNER)
            n0 = b.add_node("big__t0", 1)
            b.add_ground_capacitor("big_n0", n0, ground, declared + offset, 1)
            b.add_net(1, "big", "lumped", (n0,), declared)
            result = check_parasitic_network(_build(b))
            assert result["nets"][0]["capacitance"]["status"] == expected

    def test_coupling_capacitance_is_counted_exactly_once_and_separately(self):
        """Coupling caps never enter the ground reconciliation: the net's
        declared 1.0 fF is reconciled against only its 1.0 fF ground cap,
        even though a 5.0 fF coupling cap also touches the net."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        hub_a = b.add_node("a", 1)
        hub_b = b.add_node("b", 2)
        b.add_ground_capacitor("a", hub_a, ground, 1.0, 1)
        b.add_ground_capacitor("b", hub_b, ground, 1.0, 2)
        b.add_coupling_capacitor("cc_a_b", hub_a, hub_b, 5.0)
        b.add_net(1, "a", "lumped", (), 1.0)
        b.add_net(2, "b", "lumped", (), 1.0)
        result = check_parasitic_network(_build(b))
        for net in result["nets"]:
            assert net["capacitance"]["status"] == "pass"
            assert net["capacitance"]["computed_ff"] == pytest.approx(1.0)

    def test_mom_replaced_net_stays_checkable(self):
        """The MoM substitution rewrites the declared entry *before*
        injection, so declared and emitted still describe the same
        capacitance and the check runs (and passes) normally."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        n0 = b.add_node("m__t0", 1)
        b.add_ground_capacitor("m", n0, ground, 12.34, 1)
        b.add_net(1, "m", "lumped", (n0,), 12.34)
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["capacitance"]["status"] == "pass"
        assert net["capacitance"]["reason"] is None

    def test_explicit_unsupported_coverage_is_not_applicable(self):
        """A replacement model that cannot be compared fairly declares
        itself uncheckable; the block reports `not_applicable` with the
        model's reason instead of inventing a verdict."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        n0 = b.add_node("x__t0", 1)
        b.add_ground_capacitor("x_n0", n0, ground, 1.0, 1)
        b.add_net(
            1,
            "x",
            "lumped",
            (n0,),
            1.0,
            capacitance_checkable=False,
            capacitance_reason="replacement model emits no comparable ground caps",
        )
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["capacitance"]["status"] == "not_applicable"
        assert "replacement model" in net["capacitance"]["reason"]
        assert net["capacitance"]["delta_ff"] is None
        # Not a finding: not_applicable never counts as a mismatch.
        assert result["capacitance_mismatches"] == 0
        assert result["nets_with_findings"] == 0

    def test_non_finite_declared_total_is_a_mismatch_with_null_value(self):
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        n0 = b.add_node("c__t0", 1)
        b.add_ground_capacitor("c_n0", n0, ground, 1.0, 1)
        b.add_net(1, "c", "lumped", (n0,), float("nan"))
        result = check_parasitic_network(_build(b))
        (net,) = result["nets"]
        assert net["capacitance"]["status"] == "mismatch"
        assert net["capacitance"]["declared_ff"] is None
        assert net["capacitance"]["computed_ff"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# bounded details, deterministic ordering, strict-JSON serialization
# ---------------------------------------------------------------------------


class TestBoundedAndDeterministic:
    def test_counts_survive_truncation(self):
        """25 suspicious resistors: the count is complete (25), the detail
        list is capped at `max_detail_items` (20), and `details_truncated`
        is set."""
        b = IntegrityRecordBuilder()
        nodes = [b.add_node(f"n{i}__t0", 1) for i in range(25)]
        for i, uid in enumerate(nodes):
            b.add_resistor(f"r{i:03d}", uid, uid, 0.0, 1)
        b.add_net(1, "n", "distributed", tuple(nodes), 0.0)
        result = check_parasitic_network(_build(b))
        block = result["nets"][0]["suspicious_resistors"]
        assert block["count"] == 25
        assert len(block["details"]) == MAX_DETAIL_ITEMS
        assert block["details_truncated"] is True

    def test_open_detail_list_truncates_with_complete_count(self):
        disconnected_count = MAX_DETAIL_ITEMS + 5
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        n0 = b.add_node("d__t0", 1)
        rest = [b.add_node(f"d__t{i}", 1) for i in range(1, disconnected_count + 1)]
        b.add_ground_capacitor("d_n0", n0, ground, 1.0, 1)
        for i, uid in enumerate(rest):
            b.add_ground_capacitor(f"d_n{i + 1}", uid, ground, 1.0, 1)
        b.add_net(1, "d", "distributed", (n0, *rest), float(disconnected_count + 1))
        result = check_parasitic_network(_build(b))
        open_block = result["nets"][0]["open"]
        assert open_block["status"] == "open"
        assert open_block["disconnected_count"] == disconnected_count
        assert len(open_block["disconnected_terminals"]) == MAX_DETAIL_ITEMS
        assert open_block["details_truncated"] is True

    def test_same_input_same_output_bytes(self):
        """Ordering is deterministic: two checks of the same record
        serialize identically."""
        first = check_parasitic_network(_build(_distributed_builder()))
        second = check_parasitic_network(_build(_distributed_builder()))
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_net_order_follows_the_record(self):
        b = IntegrityRecordBuilder()
        b.add_net(3, "z", "lumped", (), 0.0)
        b.add_net(1, "a", "lumped", (), 0.0)
        result = check_parasitic_network(_build(b))
        assert [(n["net"], n["net_id"], n["rc_model"]) for n in result["nets"]] == [
            ("z", 3, "lumped"),
            ("a", 1, "lumped"),
        ]

    def test_strict_finite_json_for_non_finite_input(self):
        """A record riddled with NaN/Infinity resistors still serializes as
        strict finite JSON -- `allow_nan=False` is the encoder's own
        contract, and it must hold."""
        b = IntegrityRecordBuilder()
        n0 = b.add_node("c__t0", 1)
        n1 = b.add_node("c__t1", 1)
        b.add_resistor("c_seg0", n0, n1, float("nan"), 1)
        b.add_resistor("c_seg1", n1, n1, float("-inf"), 1)
        b.add_net(1, "c", "distributed", (n0, n1), float("inf"))
        result = check_parasitic_network(_build(b))
        text = json.dumps(result, allow_nan=False)
        assert "NaN" not in text
        assert "Infinity" not in text

    def test_summary_aggregates_all_counters(self):
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        # Net 1: open (no segment joins the legs) *and* a NaN self-loop
        # resistor (non_finite + self_loop -> one suspicious finding).
        n0 = b.add_node("k__t0", 1)
        n1 = b.add_node("k__t1", 1)
        b.add_resistor("k_bad", n0, n0, float("nan"), 1)
        b.add_ground_capacitor("k_n0", n0, ground, 1.0, 1)
        b.add_ground_capacitor("k_n1", n1, ground, 1.0, 1)
        b.add_net(1, "k", "distributed", (n0, n1), 2.0)
        # Net 2: capacitance mismatch (declared 5.0, emitted 1.0).
        m0 = b.add_node("m__t0", 2)
        b.add_ground_capacitor("m_n0", m0, ground, 1.0, 2)
        b.add_net(2, "m", "lumped", (m0,), 5.0)
        # Net 3: clean.
        c0 = b.add_node("c__t0", 3)
        b.add_ground_capacitor("c_n0", c0, ground, 1.0, 3)
        b.add_net(3, "c", "lumped", (c0,), 1.0)
        result = check_parasitic_network(_build(b))
        assert result["nets_checked"] == 3
        assert result["nets_with_findings"] == 2
        assert result["open_nets"] == 1
        assert result["suspicious_resistors"] == 1
        assert result["capacitance_mismatches"] == 1
        assert result["island_components"] == 0
        assert result["orphan_nodes"] == 0
        assert result["short_candidates"] == 0
        assert result["short_threshold_ohm"] == DEFAULT_SHORT_THRESHOLD_OHM
        assert result["cap_tolerance"] == {
            "absolute_ff": CAP_TOLERANCE_ABS_FF,
            "relative": CAP_TOLERANCE_REL,
        }
        assert result["max_detail_items"] == MAX_DETAIL_ITEMS


# ---------------------------------------------------------------------------
# scaling: the checker is linear in the record, not O(nets x elements)
# ---------------------------------------------------------------------------


class _CountingList(list):
    """A ``list`` that counts full iterations -- how many times the checker
    scans a record element list end to end."""

    def __init__(self, items: list) -> None:
        super().__init__(items)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def _ladder_record(n_nets: int) -> ParasiticNetworkRecord:
    """``n_nets`` healthy 5-node distributed ladders (4 R + 5 C each) --
    the shape the Judge's PR #3127 timing used."""
    b = IntegrityRecordBuilder()
    ground = b.add_node("0", GROUND_OWNER)
    for k in range(n_nets):
        uids = [b.add_node(f"n{k}_{i}", k) for i in range(5)]
        for i in range(4):
            b.add_resistor(f"R{k}_{i}", uids[i], uids[i + 1], 1.0, k)
        for i, uid in enumerate(uids):
            b.add_ground_capacitor(f"C{k}_{i}", uid, ground, 0.2, k)
        b.add_net(k, f"n{k}", "distributed", tuple(uids), 1.0)
    return _build(b)


class TestScaling:
    def _scan_counts(self, n_nets: int) -> dict[str, int]:
        record = _ladder_record(n_nets)
        for name in ("nodes", "resistors", "inductors", "capacitors"):
            setattr(record, name, _CountingList(getattr(record, name)))
        result = check_parasitic_network(record)
        assert result["nets_checked"] == n_nets
        assert result["nets_with_findings"] == 0
        return {
            name: getattr(record, name).iterations
            for name in ("nodes", "resistors", "inductors", "capacitors")
        }

    def test_element_lists_are_scanned_a_fixed_number_of_times(self):
        """Deterministic scaling guard: the number of full passes over each
        record element list is independent of the net count (the
        pre-index checker rescanned every list once or more *per net*)."""
        small = self._scan_counts(3)
        large = self._scan_counts(300)
        assert small == large
        assert all(passes <= 1 for passes in large.values()), large

    def test_thousands_of_nets_check_fast(self):
        """Wall-clock backstop with a deliberately generous bound: 5,000
        ladders (45,000 elements) check in well under a second on the
        indexed checker, versus about a minute for the old per-net rescan
        (11.7 s at 2,000 nets, growing quadratically)."""
        record = _ladder_record(5000)
        start = time.perf_counter()
        result = check_parasitic_network(record)
        elapsed = time.perf_counter() - start
        assert result["nets_checked"] == 5000
        assert result["nets_with_findings"] == 0
        assert elapsed < 10.0, f"integrity check took {elapsed:.2f}s"

    def test_foreign_owned_edge_joins_the_endpoint_owners_graph(self):
        """Indexing keeps the connectivity semantics: a DC element whose
        *both* endpoints net 2 owns joins net 2's graph even when net 1
        emitted it (edges bucket by endpoint ownership, not by emitter)."""
        b = IntegrityRecordBuilder()
        ground = b.add_node("vsubs", GROUND_OWNER)
        a0 = b.add_node("b__t0", 2)
        a1 = b.add_node("b__t1", 2)
        b.add_resistor("stray", a0, a1, 5.0, 1)
        b.add_ground_capacitor("b_n0", a0, ground, 1.0, 2)
        b.add_ground_capacitor("b_n1", a1, ground, 1.0, 2)
        b.add_net(2, "b", "distributed", (a0, a1), 2.0)
        (net,) = check_parasitic_network(_build(b))["nets"]
        assert net["open"]["status"] == "pass"
        assert net["islands"]["count"] == 0
        # ...but it is still attributed to its emitter for resistor checks.
        assert net["suspicious_resistors"]["count"] == 0


# ---------------------------------------------------------------------------
# the zero-net summary
# ---------------------------------------------------------------------------


def test_empty_integrity_summary_is_all_zero():
    summary = empty_integrity_summary()
    assert summary["nets_checked"] == 0
    assert summary["nets_with_findings"] == 0
    assert summary["short_threshold_ohm"] == DEFAULT_SHORT_THRESHOLD_OHM
    assert summary["cap_tolerance"]["absolute_ff"] == CAP_TOLERANCE_ABS_FF
    assert summary["max_detail_items"] == MAX_DETAIL_ITEMS
    assert json.dumps(summary, allow_nan=False)


def test_checker_never_raises_on_malformed_ownership():
    """An edge referencing an unregistered uid must not crash the
    union-find -- the checker diagnoses what it can and moves on."""
    b = IntegrityRecordBuilder()
    n0 = b.add_node("c__t0", 1)
    b.add_resistor("c_seg0", n0, 999, 1.0, 1)  # uid 999 does not exist
    b.add_net(1, "c", "distributed", (n0,), 0.0)
    result = check_parasitic_network(_build(b))
    (net,) = result["nets"]
    assert net["open"]["status"] == "pass"
    assert net["short_candidates"]["count"] == 0
    assert not math.isnan(net["capacitance"]["computed_ff"])

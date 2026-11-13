"""Tests for ``monte_carlo.mismatch_scale`` on a scanned PDK family (#5486).

Every test here runs against a **synthetic** sg13g2-shaped library tree built
under ``tmp_path`` -- no PDK install, no ngspice. The shape (a corner file
whose ``.lib mos_<corner>_mismatch`` section ``.include``s a sigma file of
``.param <name>_mm = <number>`` cards, plus a model file whose ``.subckt``
bodies consume them) is the one IHP-Open-PDK's
``libs.tech/ngspice/models/cornerMOSlv.lib`` actually has; the real-library
measurement that confirms the scaling is recorded in the PR, not here.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim, sim_mismatch_scale
from klayout_tools.sim_batch_contract_spec import (
    _MANDATORY_RUNNER_FEATURES,
    _request_features,
)

SIGMA_LIB = """\
* synthetic sigma file
.param sg13g2_lv_nmos_delvto_mm  = 0.0039
.param sg13g2_lv_nmos_dw_mm      = 4e-9
.param sg13g2_lv_pmos_delvto_mm  = 0.0022
"""

MODEL_LIB = """\
* synthetic model file: subcircuit-local params are NOT deck sigmas
.param SWSOA = 0
.subckt sg13_lv_nmos d g s b
.params l=500e-9 w=1e-6 mm_ok=1 other_mm=7
Nsg13_lv_nmos d g s b nmos_psp
+ delvto='agauss(0, sg13g2_lv_nmos_delvto_mm/sqrt(m*l*w*1e12), (mm_ok != 1 ? 0 : 1))'
.ends
"""

CORNER_LIB = """\
.lib mos_tt
  .include model.lib
.endl mos_tt

.lib mos_tt_mismatch
  .include sigma.lib
  .include model.lib
.endl mos_tt_mismatch

.lib res_typ_mismatch
  .include res_sigma.lib
.endl res_typ_mismatch
"""

RES_SIGMA_LIB = ".param sg13g2_rhigh_rsh_mm = 0.01\n"


@pytest.fixture
def pdk_tree(tmp_path):
    """A synthetic sg13g2-shaped model tree; returns its ``corner.lib``."""
    (tmp_path / "sigma.lib").write_text(SIGMA_LIB)
    (tmp_path / "model.lib").write_text(MODEL_LIB)
    (tmp_path / "res_sigma.lib").write_text(RES_SIGMA_LIB)
    corner = tmp_path / "corner.lib"
    corner.write_text(CORNER_LIB)
    return corner


def _resolve(scale, corner, sections=("mos_tt_mismatch",), pdk="ihp-sg13g2"):
    points = sim._expand_corners({"process": list(sections)}, [])
    return sim._resolve_mismatch_scale(
        {"mismatch_scale": scale},
        pdk,
        corner_points=points,
        models={"lib": str(corner)},
        request_dir=str(corner.parent),
    )


# --------------------------------------------------------------------------- #
# Resolution: names and nominals come from the libraries, scaled by k
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "scale,expected",
    [
        (
            0,
            {
                "sg13g2_lv_nmos_delvto_mm": 0.0,
                "sg13g2_lv_nmos_dw_mm": 0.0,
                "sg13g2_lv_pmos_delvto_mm": 0.0,
            },
        ),
        (
            1,
            {
                "sg13g2_lv_nmos_delvto_mm": 0.0039,
                "sg13g2_lv_nmos_dw_mm": 4e-9,
                "sg13g2_lv_pmos_delvto_mm": 0.0022,
            },
        ),
        (
            2.5,
            {
                "sg13g2_lv_nmos_delvto_mm": 0.0039 * 2.5,
                "sg13g2_lv_nmos_dw_mm": 4e-9 * 2.5,
                "sg13g2_lv_pmos_delvto_mm": 0.0022 * 2.5,
            },
        ),
    ],
)
def test_scanned_family_scales_every_library_sigma(pdk_tree, scale, expected):
    resolved = _resolve(scale, pdk_tree)
    assert resolved.param is None
    assert dict(resolved.cards) == expected
    assert resolved.scale == float(scale)


def test_scan_ignores_subcircuit_local_and_non_numeric_params(pdk_tree):
    """``.params`` inside a ``.subckt`` (``mm_ok``, ``other_mm``) is a
    subcircuit-local default, not a deck sigma; an ``agauss(...)`` right-hand
    side is not a scalable nominal."""
    names = {name for name, _ in _resolve(1, pdk_tree).cards}
    assert names == {
        "sg13g2_lv_nmos_delvto_mm",
        "sg13g2_lv_nmos_dw_mm",
        "sg13g2_lv_pmos_delvto_mm",
    }


def test_scan_follows_only_the_selected_section(pdk_tree):
    """``mos_tt`` pulls in no sigma file, so it is a refusal, not a silent
    unscaled run -- the sigmas of a *different* section are never borrowed."""
    with pytest.raises(sim.SimError, match="mos_.*_mismatch process section"):
        _resolve(1, pdk_tree, sections=("mos_tt",))


def test_refuses_when_no_mismatch_section_is_selected(pdk_tree):
    with pytest.raises(sim.SimError, match="none is selected"):
        _resolve(2, pdk_tree, sections=("res_typ_mismatch",))


def test_refuses_when_the_section_defines_no_sigma_param(tmp_path):
    (tmp_path / "model.lib").write_text(MODEL_LIB)
    corner = tmp_path / "corner.lib"
    corner.write_text(".lib mos_tt_mismatch\n  .include model.lib\n.endl\n")
    with pytest.raises(sim.SimError, match="no '.param <name>_mm"):
        _resolve(1, corner)


@pytest.mark.parametrize(
    "value",
    ["2*0.001", "'0.5*2'", "{0.5*2}", "other_mm", "'agauss(1, 0.01, mm_ok)'"],
)
def test_refuses_a_mixed_library_whose_sigma_is_not_a_literal(tmp_path, value):
    """One literal sigma and one the scale cannot read is a **refusal**, not a
    partial scale (#5486).

    Keeping the literal and skipping the other would leave that magnitude at
    nominal while its siblings move -- so ``mismatch_scale: 0`` would no
    longer be a sigma=0 control, and nothing downstream would say so
    (``mismatch_params`` simply omits what it never read).
    """
    (tmp_path / "sigma.lib").write_text(
        ".param sg13g2_lv_nmos_delvto_mm = 0.0039\n"
        f".param sg13g2_lv_nmos_dw_mm = {value}\n"
    )
    corner = tmp_path / "corner.lib"
    corner.write_text(".lib mos_tt_mismatch\n  .include sigma.lib\n.endl\n")
    with pytest.raises(sim.SimError) as excinfo:
        _resolve(1, corner)
    message = str(excinfo.value)
    # Names the parameter and the file, so the fix is actionable.
    assert "sg13g2_lv_nmos_dw_mm" in message
    assert "sigma.lib" in message
    assert "not a plain numeric literal" in message


@pytest.mark.parametrize(
    "written,nominal",
    [("4n", 4e-9), ("2.5meg", 2.5e6), ("4nF", 4e-9), ("3E-9", 3e-9), ("5", 5.0)],
)
def test_scan_reads_a_spice_suffix_nominal(tmp_path, written, nominal):
    """A sigma written the way ngspice accepts it (scale-factor suffix,
    ignored trailing unit) is read and scaled, not refused."""
    (tmp_path / "sigma.lib").write_text(f".param sg13g2_lv_nmos_dw_mm = {written}\n")
    corner = tmp_path / "corner.lib"
    corner.write_text(".lib mos_tt_mismatch\n  .include sigma.lib\n.endl\n")
    assert dict(_resolve(2, corner).cards) == {"sg13g2_lv_nmos_dw_mm": nominal * 2}


def test_scan_ignores_a_commented_out_sigma(tmp_path):
    """An end-of-line comment is neither scaled nor refused."""
    (tmp_path / "sigma.lib").write_text(
        ".param sg13g2_lv_nmos_delvto_mm = 0.0039 $ sg13g2_lv_nmos_dw_mm = 'x*2'\n"
    )
    corner = tmp_path / "corner.lib"
    corner.write_text(".lib mos_tt_mismatch\n  .include sigma.lib\n.endl\n")
    assert dict(_resolve(1, corner).cards) == {"sg13g2_lv_nmos_delvto_mm": 0.0039}


def test_refuses_when_the_library_is_unreadable_on_this_host(pdk_tree):
    pdk_tree.unlink()
    with pytest.raises(sim.SimError, match="not a readable file on this host"):
        _resolve(1, pdk_tree)


def test_refuses_when_an_included_library_is_missing(tmp_path):
    corner = tmp_path / "corner.lib"
    corner.write_text(".lib mos_tt_mismatch\n  .include gone.lib\n.endl\n")
    with pytest.raises(sim.SimError, match="cannot read model library"):
        _resolve(1, corner)


def test_refuses_an_unsupported_family_naming_both_supported_shapes(pdk_tree):
    with pytest.raises(sim.SimError, match="gf180mcu, sg13g2, sky130"):
        _resolve(1, pdk_tree, pdk="bogus")


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "2"])
def test_rejects_bad_values_before_touching_the_filesystem(pdk_tree, value):
    with pytest.raises(sim.SimError, match="finite non-negative"):
        _resolve(value, pdk_tree)


# --------------------------------------------------------------------------- #
# Deck, echo, wire and cache key
# --------------------------------------------------------------------------- #


def _sampled(pdk_tree, scale=2.5, n=1):
    points = sim._expand_corners({"process": ["mos_tt_mismatch"]}, [])
    return sim._expand_monte_carlo(
        points,
        {"n": n, "seed": 1, "vary": "mismatch"},
        mismatch_scale=_resolve(scale, pdk_tree),
    )


def test_echo_is_additive_and_names_every_param_written(pdk_tree):
    _, info = _sampled(pdk_tree)
    assert info["mismatch_scale"] == 2.5
    assert "mismatch_param" not in info
    assert info["mismatch_params"] == {
        "sg13g2_lv_nmos_delvto_mm": 0.0039 * 2.5,
        "sg13g2_lv_nmos_dw_mm": 4e-9 * 2.5,
        "sg13g2_lv_pmos_delvto_mm": 0.0022 * 2.5,
    }


def test_deck_carries_one_card_per_sigma_after_the_lib_and_include(pdk_tree, tmp_path):
    sampled, _ = _sampled(pdk_tree)
    deck = tmp_path / "deck.spice"
    sim._write_corner_deck(
        deck_path=str(deck),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=str(pdk_tree),
        point=sampled[0],
        analysis={"kind": "op", "args": ""},
        measurements_spec=[],
        raw_path=None,
    )
    lines = deck.read_text().splitlines()
    cards = [i for i, ln in enumerate(lines) if ln.startswith(".param sg13g2_")]
    assert [lines[i] for i in cards] == [
        ".param sg13g2_lv_nmos_delvto_mm=0.00975",
        ".param sg13g2_lv_nmos_dw_mm=1e-08",
        ".param sg13g2_lv_pmos_delvto_mm=0.0055000000000000005",
    ]
    # Last definition wins, so every card must follow both the `.lib` that
    # defines the sigma and the netlist `.include` that may too.
    lib = max(i for i, ln in enumerate(lines) if ln.startswith(".lib"))
    body = max(i for i, ln in enumerate(lines) if ln.startswith(".include"))
    assert min(cards) > max(lib, body)


def test_zero_scale_writes_integral_zero_cards(pdk_tree, tmp_path):
    sampled, _ = _sampled(pdk_tree, scale=0)
    deck = tmp_path / "deck.spice"
    sim._write_corner_deck(
        deck_path=str(deck),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=str(pdk_tree),
        point=sampled[0],
        analysis={"kind": "op", "args": ""},
        measurements_spec=[],
        raw_path=None,
    )
    assert ".param sg13g2_lv_nmos_delvto_mm=0" in deck.read_text().splitlines()


def test_cards_ride_the_fleet_wire(pdk_tree):
    sampled, _ = _sampled(pdk_tree)
    back = sim._corner_points_from_wire(sim._corner_points_to_wire(sampled))
    assert back[0].mc_seed["mismatch_params"] == sampled[0].mc_seed["mismatch_params"]
    assert "mismatch_param" not in back[0].mc_seed


def test_checkpoint_fingerprint_distinguishes_two_scales(pdk_tree, tmp_path):
    netlist = tmp_path / "n.cir"
    netlist.write_text("* x\n.end\n")

    def fp(scale):
        sampled, _ = _sampled(pdk_tree, scale=scale, n=2)
        return sim._checkpoint_fingerprint(
            netlist_path=str(netlist),
            models_lib=None,
            analysis={"type": "op"},
            measurements_spec=[],
            corner_points=sampled,
            timeout_s=1.0,
            engine="ngspice",
        )

    assert len({fp(0), fp(1), fp(2.5)}) == 3


# --------------------------------------------------------------------------- #
# The off-host hazard: an older runner must not run the sweep unscaled
# --------------------------------------------------------------------------- #


def test_scanned_cards_are_a_mandatory_runner_feature(pdk_tree):
    sampled, _ = _sampled(pdk_tree)
    wire = {"_explicit_points": sim._corner_points_to_wire(sampled)}
    assert sim_mismatch_scale.wire_uses_scanned_cards(wire)
    assert "mismatch_params" in _request_features(wire)
    assert "mismatch_params" in _MANDATORY_RUNNER_FEATURES


def test_a_switch_family_wire_does_not_claim_the_new_feature():
    points = sim._expand_corners({"process": ["tt_mm"]}, [])
    sampled, _ = sim._expand_monte_carlo(
        points,
        {"n": 1, "seed": 1, "vary": "mismatch"},
        mismatch_scale=sim._resolve_mismatch_scale({"mismatch_scale": 2}, "sky130A"),
    )
    wire = {"_explicit_points": sim._corner_points_to_wire(sampled)}
    assert not sim_mismatch_scale.wire_uses_scanned_cards(wire)
    assert "mismatch_params" not in _request_features(wire)


def test_points_to_push_still_resolves_a_scanned_scale_client_side(pdk_tree):
    from klayout_tools import sim_remote

    points = sim._expand_corners({"process": ["mos_tt_mismatch"]}, [])
    mc = {"n": 1, "seed": 1, "vary": "mismatch", "mismatch_scale": 2}
    assert sim_remote._points_to_push({"monte_carlo": mc}, points, None) is points


# --------------------------------------------------------------------------- #
# The unreached-family diagnostic
# --------------------------------------------------------------------------- #


def _info(families, params=None, sections=("mos_tt_mismatch", "res_typ_mismatch")):
    info = {
        "mismatch_scale": 2.0,
        "mismatch_params": params or {"sg13g2_lv_nmos_delvto_mm": 0.0078},
        "family_mismatch": [{"family": f, "active": True} for f in families],
    }
    return info, sim._expand_corners({"process": list(sections)}, [])


def test_diagnostic_names_a_varying_family_the_scale_did_not_reach():
    info, points = _info(["mosfet", "resistor"])
    diag = sim_mismatch_scale.unreached_families(info, points)
    assert diag["code"] == "mismatch_scale_unreached_family"
    assert diag["severity"] == "warning"
    assert diag["families"] == ["resistor"]
    assert diag["sections"] == ["res_typ_mismatch"]
    assert "res_typ_mismatch" in diag["message"]
    assert diag["code"] in sim.DIAGNOSTIC_CODES


def test_no_diagnostic_when_the_netlist_has_no_such_family():
    info, points = _info(["mosfet"])
    assert sim_mismatch_scale.unreached_families(info, points) is None


def test_no_diagnostic_when_only_mos_mismatch_sections_are_selected():
    info, points = _info(["mosfet", "resistor"], sections=("mos_tt_mismatch",))
    assert sim_mismatch_scale.unreached_families(info, points) is None


def test_no_diagnostic_for_a_switch_family_run():
    """sky130/gf180mcu echo ``mismatch_param``, never ``mismatch_params``, so
    their responses stay byte-for-byte unchanged."""
    info, points = _info(["mosfet", "resistor"])
    del info["mismatch_params"]
    info["mismatch_param"] = "mc_mm_switch"
    assert sim_mismatch_scale.unreached_families(info, points) is None


def test_no_diagnostic_without_a_family_report():
    info, points = _info([])
    assert sim_mismatch_scale.unreached_families(info, points) is None


# --------------------------------------------------------------------------- #
# Section/library selection
# --------------------------------------------------------------------------- #


def test_selected_sections_prefers_a_per_section_library_over_models_lib():
    point = sim.CornerPoint(
        None,
        {},
        27,
        process_sections=["mos_tt_mismatch", "res_typ_mismatch"],
        process_section_libs=["own.lib", None],
    )
    assert sim_mismatch_scale.selected_sections([point], {"lib": "models.lib"}) == [
        ("mos_tt_mismatch", "own.lib"),
        ("res_typ_mismatch", "models.lib"),
    ]


def test_a_bare_string_process_corner_takes_models_lib():
    point = sim.CornerPoint("mos_tt_mismatch", {}, 27)
    assert sim_mismatch_scale.selected_sections([point], {"lib": "models.lib"}) == [
        ("mos_tt_mismatch", "models.lib")
    ]


def test_refuses_when_no_library_resolves_for_the_section():
    points = sim._expand_corners({"process": ["mos_tt_mismatch"]}, [])
    with pytest.raises(sim.SimError, match="resolves none"):
        sim._resolve_mismatch_scale(
            {"mismatch_scale": 1},
            "ihp-sg13g2",
            corner_points=points,
            models={},
            request_dir=".",
        )


def test_scan_follows_a_nested_lib_card_and_a_transitive_include(tmp_path):
    """A section may pull its sigmas in through a nested ``.lib <file>
    <section>`` card, or through a file that itself ``.include``s another."""
    (tmp_path / "deep.lib").write_text(".param sg13g2_deep_mm = 1e-3\n")
    (tmp_path / "mid.lib").write_text(".include deep.lib\n")
    (tmp_path / "nested.lib").write_text(
        ".lib inner\n  .include mid.lib\n.endl inner\n"
    )
    corner = tmp_path / "corner.lib"
    corner.write_text(
        ".lib mos_tt_mismatch\n  .lib nested.lib inner\n.endl mos_tt_mismatch\n"
    )
    assert _resolve(3, corner).cards == (("sg13g2_deep_mm", 3e-3),)


def test_scan_folds_continuation_lines_and_is_cycle_safe(tmp_path):
    """A ``+`` continuation is part of its ``.param`` card, and a library
    that includes itself terminates instead of recursing forever."""
    (tmp_path / "sigma.lib").write_text(
        ".include sigma.lib\n.param sg13g2_a_mm = 1\n+ sg13g2_b_mm = 2\n"
    )
    corner = tmp_path / "corner.lib"
    corner.write_text(".lib mos_tt_mismatch\n  .include sigma.lib\n.endl\n")
    assert _resolve(2, corner).cards == (
        ("sg13g2_a_mm", 2.0),
        ("sg13g2_b_mm", 4.0),
    )


def test_attach_is_a_no_op_without_a_diagnostic():
    report = {}
    info, points = _info(["mosfet"])
    sim_mismatch_scale.attach_unreached_families(report, info, points)
    assert report == {}

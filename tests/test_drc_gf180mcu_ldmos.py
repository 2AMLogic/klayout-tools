"""Tests for the gf180mcu deck's 10V drain-extended LDMOS rules (`MDN.*` from
`ldnmos.rb`, `MDP.*` from `ldpmos.rb`) and the disclosure of the rules not
transcribed (issue #4569).

The gap these close: `klt drc --deck gf180mcu` reported `clean` for a
PDK-drawn `nfet_10v0_asym` with MVSD (210/0), MVPSD (11/39) and LDMOS_XTOR
(226/0) listed in `layers_in_stream_without_rules` and no LDMOS rule run.
"""

from __future__ import annotations

import re
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_deck
from klayout_tools.decks.gf180mcu import (
    LAYER_NAMES,
    OFFGRID_UNCHECKED_LAYERS,
    UNMODELED_RULES,
    UNTRANSCRIBED_RULES,
)
from klayout_tools.drc import run_drc

MVSD = (210, 0)
MVPSD = (11, 39)
LDMOS_XTOR = (226, 0)
DNWELL = (12, 0)

# Every rule id `output(...)`-registered by the two PDK files at the deck's
# pinned commit (`f6eeac7`): 35 in ldnmos.rb, 38 in ldpmos.rb.
LDNMOS_IDS = (
    "MDN.1 MDN.2a MDN.2b MDN.3a MDN.3b MDN.4a MDN.4b MDN.5ai MDN.5aii MDN.5b "
    "MDN.5c MDN.6 MDN.6a MDN.7 MDN.7a MDN.8a MDN.8b MDN.9 MDN.10a MDN.10b "
    "MDN.10c MDN.10d MDN.10ei MDN.10eii MDN.10f MDN.11 MDN.12 MDN.13a MDN.13b "
    "MDN.13c MDN.13d MDN.14 MDN.15a MDN.15b MDN.17"
).split()
LDPMOS_IDS = (
    "MDP.1 MDP.1a MDP.2 MDP.3 MDP.3ai MDP.3aii MDP.3b MDP.3c MDP.3d MDP.4 "
    "MDP.4a MDP.4b MDP.5 MDP.5a MDP.6 MDP.6a MDP.7 MDP.8 MDP.9a MDP.9b MDP.9c "
    "MDP.9d MDP.9ei MDP.9eii MDP.9f MDP.10 MDP.10a MDP.10b MDP.11 MDP.12 "
    "MDP.13a MDP.13b MDP.13c MDP.15 MDP.16a MDP.16b MDP.17a MDP.17c"
).split()


def _write(path, shapes) -> str:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for (layer, datatype), items in shapes.items():
        index = layout.layer(layer, datatype)
        layout.set_info(index, kdb.LayerInfo(layer, datatype))
        for item in items:
            top.shapes(index).insert(item)
    layout.write(str(path))
    return str(path)


def _ldmos_rules():
    return {
        rule.id: rule
        for rule in get_deck("gf180mcu")
        if rule.provenance is not None
        and rule.provenance.source_path.endswith(("/ldnmos.rb", "/ldpmos.rb"))
    }


def test_every_ldmos_rule_id_is_transcribed_or_disclosed():
    transcribed = {r.provenance.rule_id for r in _ldmos_rules().values()}
    assert transcribed == {"MDN.1", "MDN.14", "MDP.6"}
    for rule_id in (*LDNMOS_IDS, *LDPMOS_IDS):
        in_deck = rule_id in transcribed
        disclosed = rule_id in UNTRANSCRIBED_RULES
        assert in_deck != disclosed, f"{rule_id}: must be in exactly one set"
        if disclosed:
            assert rule_id in UNMODELED_RULES
            assert UNTRANSCRIBED_RULES[rule_id], rule_id
    ldmos_untranscribed = {
        k for k in UNTRANSCRIBED_RULES if k.startswith(("MDN.", "MDP."))
    }
    assert ldmos_untranscribed == (set(LDNMOS_IDS) | set(LDPMOS_IDS)) - transcribed


def test_rule_id_lists_match_the_installed_pdk_when_present():
    root = Path.home() / ".ciel/ciel/gf180mcu/versions"
    decks = sorted(
        root.glob("f6eeac7*/gf180mcuA/libs.tech/klayout/tech/drc/rule_decks")
    )
    if not decks:
        pytest.skip("pinned gf180mcu PDK not installed")
    for name, expected in (("ldnmos", LDNMOS_IDS), ("ldpmos", LDPMOS_IDS)):
        text = (decks[0] / f"{name}.rb").read_text()
        assert re.findall(r"output\('([A-Za-z0-9.]+)'", text) == expected


def test_ldmos_layers_are_named_and_no_longer_uncovered():
    assert LAYER_NAMES[MVSD] == "MVSD"
    assert LAYER_NAMES[MVPSD] == "MVPSD"
    assert LAYER_NAMES[LDMOS_XTOR] == "LDMOS_XTOR"
    assert "mvsd" not in OFFGRID_UNCHECKED_LAYERS
    assert "mvpsd" not in OFFGRID_UNCHECKED_LAYERS


def test_pdk_drawn_10v_device_has_no_uncovered_ldmos_layers(tmp_path):
    path = _write(
        tmp_path / "ldmos.gds",
        {
            MVSD: [kdb.Box(0, 0, 2000, 2000)],
            MVPSD: [kdb.Box(5000, 0, 7000, 2000)],
            LDMOS_XTOR: [kdb.Box(-500, -500, 8000, 2500)],
        },
    )
    report = run_drc(path, "gf180mcu")
    assert not report["coverage"]["layers_in_stream_without_rules"]
    assert report["status"] == "clean"


def test_narrow_mvsd_is_flagged_mdn1(tmp_path):
    narrow = _write(tmp_path / "narrow.gds", {MVSD: [kdb.Box(0, 0, 800, 3000)]})
    ok = _write(tmp_path / "ok.gds", {MVSD: [kdb.Box(0, 0, 1000, 3000)]})
    assert run_drc(narrow, "gf180mcu")["rule_counts"]["mvsd.width.1"] >= 1
    assert "mvsd.width.1" not in run_drc(ok, "gf180mcu")["rule_counts"]


def test_mvsd_too_close_to_dnwell_is_flagged_mdn14(tmp_path):
    near = _write(
        tmp_path / "near.gds",
        {MVSD: [kdb.Box(0, 0, 2000, 2000)], DNWELL: [kdb.Box(5000, 0, 20000, 5000)]},
    )  # 3um apart (< 6um)
    far = _write(
        tmp_path / "far.gds",
        {MVSD: [kdb.Box(0, 0, 2000, 2000)], DNWELL: [kdb.Box(9000, 0, 20000, 5000)]},
    )  # 7um apart
    assert run_drc(near, "gf180mcu")["rule_counts"]["mvsd.separation.dnwell.1"] >= 1
    assert "mvsd.separation.dnwell.1" not in run_drc(far, "gf180mcu")["rule_counts"]


def test_mvpsd_outside_ldmos_xtor_is_flagged_mdp6(tmp_path):
    outside = _write(
        tmp_path / "outside.gds",
        {
            MVPSD: [kdb.Box(0, 0, 2000, 2000)],
            LDMOS_XTOR: [kdb.Box(1000, 0, 9000, 2000)],
        },
    )
    inside = _write(
        tmp_path / "inside.gds",
        {
            MVPSD: [kdb.Box(0, 0, 2000, 2000)],
            LDMOS_XTOR: [kdb.Box(-500, -500, 9000, 2500)],
        },
    )
    assert (
        run_drc(outside, "gf180mcu")["rule_counts"]["ldmos_xtor.enclosing.mvpsd.1"] >= 1
    )
    assert (
        "ldmos_xtor.enclosing.mvpsd.1" not in run_drc(inside, "gf180mcu")["rule_counts"]
    )


def test_mvsd_and_mvpsd_ongrid_rules_fire(tmp_path):
    for layer, rule in ((MVSD, "mvsd.ongrid.1"), (MVPSD, "mvpsd.ongrid.1")):
        path = _write(tmp_path / f"{rule}.gds", {layer: [kdb.Box(0, 0, 2003, 2000)]})
        assert run_drc(path, "gf180mcu")["rule_counts"][rule] >= 1

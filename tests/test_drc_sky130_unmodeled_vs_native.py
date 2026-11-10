"""Every native sky130A DRC rule id is implemented, disclosed or untriaged (#5233).

``coverage.unmodeled_rules`` is the deck's disclosure of what a ``clean``
verdict does not cover. This test diffs the native rule-id set (a checked-in
snapshot, so it runs without an installed PDK) against the ids the curated deck
cites in rule ``provenance`` plus ``UNMODELED_RULES``. Ids in neither are
tracked in a frozen "untriaged" list that may only shrink, so a new native id
(or a removed disclosure) fails here instead of passing silently.
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb

from klayout_tools.decks import get_unmodeled_rules
from klayout_tools.decks import sky130 as deck_mod
from klayout_tools.decks.sky130_unmodeled import native_offgrid_aliases
from klayout_tools.drc import run_drc

FIXTURES = Path(__file__).parent / "fixtures"


def _ids(name: str) -> set[str]:
    return {
        ln.strip()
        for ln in (FIXTURES / name).read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    }


NATIVE = _ids("sky130_native_rule_ids.txt")
UNTRIAGED = _ids("sky130_native_rule_ids_untriaged.txt")
_DECK_BY_ID = {r.id: r for r in deck_mod.DECK}

#: Native output ids the deck implements under a different citation (#5252):
#: native id -> deck rule id. ``<layer>_OFFGRID`` / ``<layer>_angle`` are the
#: output names of ``sky130A_mr.drc``'s OFFGRID block, whose rule ids are
#: ``x.1b`` / ``x.3a`` / ``x.2`` (what the deck's provenance cites), and
#: ``m4.4`` is the ``sky130A.lydrc`` name of the 0.24um2 met4 area check the
#: mr deck calls ``m4.4a``. Each target must exist in the deck.
NATIVE_ALIASES: dict[str, str] = {
    "m4.4": "met4.area.1",
    # Enclosure rules cited via the mr deck's ids; kept here (not as deck
    # ``provenance``) so sky130.py stays within its size ratchet.
    "m1.4": "met1.enclosing.mcon.1",
    "m2.4": "met2.enclosing.via.1",
    "m3.4": "met3.enclosing.via2.1",
    "m4.3": "met4.enclosing.via3.1",
    "m5.3": "met5.enclosing.via4.1",
    "capm.3": "met3.enclosing.capm.1",
    "capm.4": "capm.enclosing.via3.1",
    "cap2m.3": "met4.enclosing.capm2.1",
    "cap2m.4": "capm2.enclosing.via4.1",
    **{
        f"{native}_OFFGRID": f"{stem}.ongrid.1"
        for native, stem in (
            ("m1", "met1"),
            ("m2", "met2"),
            ("m3", "met3"),
            ("m4", "met4"),
            ("m5", "met5"),
            ("mf", "mf"),
            ("nsm", "nsm"),
            ("pad", "pad"),
            ("rpm", "rpm"),
        )
    },
}
NATIVE_ALIASES.update(
    {
        k.replace("_OFFGRID", "_angle"): v.replace(".ongrid.1", ".angle.1")
        for k, v in list(NATIVE_ALIASES.items())
        if k.endswith("_OFFGRID")
    }
)
IMPLEMENTED = {r.provenance.rule_id for r in deck_mod.DECK if r.provenance is not None}
IMPLEMENTED |= {n for n, deck_id in NATIVE_ALIASES.items() if deck_id in _DECK_BY_ID}
# <layer>_OFFGRID / <layer>_angle are implemented by the deck's ongrid/angle rules,
# which cite the official x.1b/x.2*/x.3a ids in provenance (#5254). Only the
# stems triaged so far are credited here; the remaining stems stay UNTRIAGED
# until their own slice of #5242 lands (and then widens this set). The metal /
# mf / nsm / pad / rpm stems are credited via NATIVE_ALIASES above (#5252).
_ALIAS_STEMS = (
    "areaid_re",
    "diff",
    "lvtn",
    "ncm",
    "npc",
    "nsdm",
    "poly",
    "psdm",
    "tap",
    "tunm",
)
IMPLEMENTED |= {
    native
    for native, rule_id in native_offgrid_aliases().items()
    if rule_id in _DECK_BY_ID and native.rsplit("_", 1)[0] in _ALIAS_STEMS
}
UNMODELED = set(deck_mod.UNMODELED_RULES)


def test_snapshot_header_records_pdk_version_and_command():
    header = "\n".join(
        ln
        for ln in (FIXTURES / "sky130_native_rule_ids.txt").read_text().splitlines()
        if ln.startswith("#")
    )
    assert "open_pdks" in header and "Generated from" in header


def test_native_aliases_resolve_to_deck_rules():
    missing = {n: d for n, d in NATIVE_ALIASES.items() if d not in _DECK_BY_ID}
    assert not missing, f"alias targets absent from the deck: {missing}"


def test_ct4_and_li5_are_disclosed():
    assert {"ct.4", "li.5"} <= UNMODELED


def test_no_native_rule_is_silently_uncovered():
    missing = sorted(NATIVE - IMPLEMENTED - UNMODELED - UNTRIAGED)
    assert not missing, (
        "native sky130A rule ids neither implemented, in UNMODELED_RULES, nor "
        f"in the untriaged list: {missing}"
    )


def test_untriaged_list_only_shrinks():
    stale = sorted(UNTRIAGED - NATIVE)
    resolved = sorted(UNTRIAGED & (IMPLEMENTED | UNMODELED))
    assert not stale, f"untriaged ids that are not native ids: {stale}"
    assert not resolved, f"already covered, remove from the untriaged list: {resolved}"


def _make_nwell_layout() -> kdb.Layout:
    """One 2um nwell (64/20) square."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    li = layout.layer(64, 20)
    layout.set_info(li, kdb.LayerInfo(64, 20))
    top.shapes(li).insert(kdb.Box(0, 0, 2000, 2000))
    return layout


def test_run_drc_sky130_coverage_lists_unmodeled_rules(tmp_path):
    """A clean sky130 verdict must disclose the enclosure refinements the
    curated deck does not transcribe (issue #3031); non-sky130 decks that
    declare none report an empty list."""
    path = tmp_path / "unmodeled.gds"
    _make_nwell_layout().write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["coverage"]["unmodeled_rules"] == [
        "MR_li.WID.4",
        "MR_licon.SP.1",
        "MR_lvtn.OVL.2",
        "MR_rdl.CON.1",
        "MR_thkox.CON.1",
        "MR_tunm.CON.1",
        "cap2m.11",
        "cap2m.2b",
        "cap2m.2b_a",
        "cap2m.3_a",
        "cap2m.5",
        "capm.10",
        "capm.11",
        "capm.2b",
        "capm.2b_a",
        "capm.5",
        "capm.6",
        "capm.7",
        "capm.8",
        "ct.4",
        "difftap.10",
        "difftap.11",
        "difftap.1_a",
        "difftap.1_b",
        "difftap.1_c",
        "difftap.2",
        "difftap.4",
        "difftap.5",
        "difftap.6",
        "difftap.7",
        "difftap.8",
        "difftap.9",
        "dnwell.2",
        "extd.1",
        "extd.2",
        "hvi.1",
        "hvi.2a",
        "hvntm.1",
        "hvntm.2",
        "hvtp.1",
        "hvtp.2",
        "hvtr.1",
        "hvtr.2",
        "hvtr.2_a",
        "ldntm.1",
        "ldntm.2",
        "li.5",
        "li.7",
        "licon.13",
        "lvtn.1",
        "lvtn.12",
        "lvtn.13",
        "lvtn.1a",
        "lvtn.2",
        "lvtn.3a",
        "lvtn.3b",
        "lvtn.4b",
        "lvtn.9",
        "m1.11",
        "m1.3ab",
        "m1.4a",
        "m1.4a_a",
        "m1.5",
        "m1.x",
        "m2.11",
        "m2.3ab",
        "m2.4_a",
        "m2.4_b",
        "m2.5",
        "m2.via",
        "m2.x",
        "m3.11",
        "m3.3ab",
        "m3.3cd",
        "m3.4_a",
        "m3.via2",
        "m3.x",
        "m4.11",
        "m4.15",
        "m4.3_a",
        "m4.5ab",
        "m4.via3",
        "m4.x",
        "m5.3_a",
        "m5.via4",
        "m5.x",
        "mf.1",
        "mf.2",
        "mf.3",
        "moduleCut.1",
        "n/psdm.10a",
        "n/psdm.10b",
        "n/psdm.5a",
        "n/psdm.5b",
        "n/psdm.6",
        "n/psdm.7",
        "n/psdm.8",
        "ncm.1",
        "ncm.2",
        "ncm.2a",
        "ncm.3",
        "ncm.5",
        "ncm.6",
        "ncm.7",
        "ncm.x.3",
        "npc.4",
        "nsdm.2",
        "nsdm.3",
        "nsdm.4",
        "nsdm.5",
        "nsdm.6",
        "nsdm.6a",
        "nsdm_psdm_overlap",
        "nsm.1",
        "nsm.2",
        "nsm.3",
        "nsm.4",
        "pad.2",
        "photo.10",
        "photo.2",
        "photo.3",
        "photo.4",
        "photo.5.6",
        "photo.7",
        "photo.8",
        "photo.9",
        "poly.10",
        "poly.11",
        "poly.12",
        "poly.15",
        "poly.1b",
        "poly.3",
        "poly.4",
        "poly.5",
        "poly.6",
        "poly.7",
        "poly.9",
        "psdm.2",
        "psdm.3",
        "psdm.4",
        "psdm.5",
        "psdm.6",
        "psdm.6a",
        "pwres.2",
        "pwres.3",
        "pwres.4",
        "pwres.5",
        "pwres.6",
        "pwres.8",
        "rdl.1",
        "rdl.2",
        "rfdiode.1",
        "rfdiode.2",
        "rpm.10",
        "rpm.2",
        "rpm.3",
        "rpm.4",
        "rpm.5",
        "rpm.6",
        "rpm.7",
        "rpm.8",
        "rpm.9",
        "thkox.1",
        "thkox.2",
        "tunm.1",
        "tunm.2",
        "tunm.3",
        "tunm.4",
        "tunm.5",
        "tunm.6a",
        "tunm.7",
        "urpm.2",
        "via.5a",
        "via2.5",
        "via3.5",
    ]
    assert get_unmodeled_rules("no-such-deck") == []

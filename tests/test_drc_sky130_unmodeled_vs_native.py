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

from helpers.sky130_native_aliases import NATIVE_ALIASES
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
    "via",
    "via2",
    "via3",
    "via4",
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
        "ct.3",
        "ct.3_a",
        "ct.3_b",
        "ct.4",
        "ct.irdrop.1",
        "ct.irdrop.2",
        "denmos.1",
        "denmos.10",
        "denmos.11",
        "denmos.12",
        "denmos.13",
        "denmos.2",
        "denmos.3",
        "denmos.4",
        "denmos.5",
        "denmos.6",
        "denmos.7",
        "denmos.8",
        "denmos.9a",
        "denmos.9b",
        "depmos.1",
        "depmos.10",
        "depmos.11",
        "depmos.12",
        "depmos.2",
        "depmos.3",
        "depmos.4",
        "depmos.5",
        "depmos.6",
        "depmos.7",
        "depmos.8",
        "depmos.9a",
        "depmos.9b",
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
        "dnwell.1",
        "dnwell.2",
        "dnwell.3",
        "dnwell.4",
        "dnwell.5",
        "extd.1",
        "extd.2",
        "hv.diff.1",
        "hv.diff.2",
        "hv.diff.3a",
        "hv.nwell.1",
        "hv.poly.2",
        "hv.poly.3",
        "hv.poly.4",
        "hv.poly.6",
        "hvdifftap.14",
        "hvdifftap.14a",
        "hvdifftap.15a",
        "hvdifftap.15b",
        "hvdifftap.16",
        "hvdifftap.17",
        "hvdifftap.18",
        "hvdifftap.19",
        "hvdifftap.20",
        "hvdifftap.21",
        "hvdifftap.22",
        "hvdifftap.23",
        "hvdifftap.24",
        "hvdifftap.25",
        "hvi.1",
        "hvi.2",
        "hvi.2a",
        "hvi.4",
        "hvntm.1",
        "hvntm.2",
        "hvntm.3",
        "hvntm.4",
        "hvntm.5",
        "hvntm.6a",
        "hvntm.9",
        "hvnwel.9",
        "hvnwell.8",
        "hvpoly.13",
        "hvpoly.14",
        "hvtp",
        "hvtp.1",
        "hvtp.2",
        "hvtp.3",
        "hvtp.4",
        "hvtp.5",
        "hvtr.1",
        "hvtr.2",
        "hvtr.2_a",
        "ldntm.1",
        "ldntm.2",
        "li.1a",
        "li.3a",
        "li.5",
        "li.6",
        "li.7",
        "li.8",
        "licon.10",
        "licon.11",
        "licon.11a",
        "licon.11c",
        "licon.12",
        "licon.13",
        "licon.13_a",
        "licon.14",
        "licon.15",
        "licon.16",
        "licon.17",
        "licon.18",
        "licon.1_c",
        "licon.1b/c",
        "licon.2",
        "licon.2b",
        "licon.2c",
        "licon.2d",
        "licon.4",
        "licon.6",
        "licon.7",
        "licon.8a",
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
        "nwell.4",
        "nwell.5",
        "nwell.6",
        "nwell.7",
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
        "pwbm",
        "pwbm.4",
        "pwde.1",
        "pwde.2",
        "pwdem.3",
        "pwdem.4",
        "pwdem.5",
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
        "uhvi.1",
        "uhvi.2",
        "uhvi.3",
        "uhvi.4",
        "uhvi.5",
        "uhvi.6",
        "uhvi.7",
        "urpm.2",
        "varac.1",
        "varac.2",
        "varac.3",
        "varac.4",
        "varac.5",
        "varac.6",
        "varac.7",
        "vhvi.1",
        "vhvi.2",
        "vhvi.3",
        "vhvi.5",
        "vhvi.7",
        "via.11",
        "via.12",
        "via.1b",
        "via.3",
        "via.3_a",
        "via.3_b",
        "via.4a_a",
        "via.4b",
        "via.4c.5c",
        "via.5a",
        "via1.14",
        "via1.5a",
        "via1.5b",
        "via2",
        "via2.11",
        "via2.12",
        "via2.14",
        "via2.1b",
        "via2.3",
        "via2.3_a",
        "via2.3_b",
        "via2.4_a",
        "via2.4a",
        "via2.5",
        "via3",
        "via3.1",
        "via3.11",
        "via3.12",
        "via3.13",
        "via3.4_a",
        "via3.5",
        "via4",
        "via4.1",
        "via4.3",
        "via4.3_a",
        "via4.3_b",
        "via4.4_a",
        "vpp.1",
        "vpp.13",
        "vpp.3",
        "vpp.4",
        "vpp.5",
        "vpp.5a",
        "vpp.5b",
        "vpp.5c",
        "vpp.8",
        "vpp.9",
    ]
    assert get_unmodeled_rules("no-such-deck") == []

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
IMPLEMENTED = {r.provenance.rule_id for r in deck_mod.DECK if r.provenance is not None}
UNMODELED = set(deck_mod.UNMODELED_RULES)


def test_snapshot_header_records_pdk_version_and_command():
    header = "\n".join(
        ln
        for ln in (FIXTURES / "sky130_native_rule_ids.txt").read_text().splitlines()
        if ln.startswith("#")
    )
    assert "open_pdks" in header and "Generated from" in header


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
        "MR_licon.SP.1",
        "cap2m.11",
        "cap2m.2b",
        "cap2m.2b_a",
        "cap2m.5",
        "capm.11",
        "capm.2b",
        "capm.2b_a",
        "capm.5",
        "ct.4",
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
        "difftap.8",
        "difftap.9",
        "dnwell.1",
        "dnwell.2",
        "dnwell.3",
        "dnwell.4",
        "dnwell.5",
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
        "ldntm.2",
        "li.5",
        "li.7",
        "licon.13",
        "lvtn.2",
        "m1.3ab",
        "m1.5",
        "m2.3ab",
        "m2.5",
        "m3.3ab",
        "m4.5ab",
        "n/psdm.10a",
        "n/psdm.10b",
        "n/psdm.5a",
        "n/psdm.5b",
        "n/psdm.6",
        "n/psdm.7",
        "n/psdm.8",
        "ncm.2a",
        "npc.4",
        "nsm.1",
        "nwell.4",
        "nwell.5",
        "nwell.6",
        "nwell.7",
        "pad.2",
        "poly.3",
        "pwbm",
        "pwbm.4",
        "pwde.1",
        "pwde.2",
        "pwdem.3",
        "pwdem.4",
        "pwdem.5",
        "rdl.1",
        "rdl.2",
        "rpm.2",
        "thkox.2",
        "tunm.2",
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
        "via.5a",
        "via2.5",
        "via3.5",
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

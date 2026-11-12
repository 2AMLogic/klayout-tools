"""ERC / `klt extract` substrate-body agreement (issue #5346).

Split out of ``test_erc.py`` to respect the per-file size ratchet.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.erc import run_erc

DBU = 0.001


# --------------------------------------------------------------------------- #
# ERC / `klt extract` agreement on substrate bodies -- issue #5346
#
# Design note section 9, fixtures 1 and 2, on real gf180mcu layers: the body
# identities `klt erc` reports (`provenance.substrate_bodies`, the
# `erc.supply_short` body id) must be the ones the extractor/LVS side names
# for the same layout (`vsubs`, `vsubs_iso<n>`). Synthetic only: the live
# `2AMLogic/sg13g2-ldo` side-by-side run is out-of-band (see the PR).
# --------------------------------------------------------------------------- #


def _gf180_two_nmos_layout(*, isolate_second: bool) -> kdb.Layout:
    """Two NMOS units, each with a labelled p-tap (`VSS` / `VSS2`) on Comp+
    Pplus+Contact+Metal1, with no metal between the rails. Optionally the
    second unit sits in a DNWELL island."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(text, x, y):
        top.shapes(layout.layer(34, 10)).insert(kdb.Text(text, kdb.Trans(x, y)))

    for x0, index, tie in ((0, 0, "VSS"), (20000, 1, "VSS2")):
        draw(22, 0, kdb.Box(x0, 0, x0 + 2000, 1000))
        draw(30, 0, kdb.Box(x0 + 800, -200, x0 + 1200, 1200))
        draw(33, 0, kdb.Box(x0 + 100, 300, x0 + 300, 700))
        draw(33, 0, kdb.Box(x0 + 1700, 300, x0 + 1900, 700))
        draw(34, 0, kdb.Box(x0, 200, x0 + 400, 800))
        draw(34, 0, kdb.Box(x0 + 1600, 200, x0 + 2000, 800))
        label(f"S{index}", x0 + 200, 500)
        label(f"D{index}", x0 + 1800, 500)
        draw(22, 0, kdb.Box(x0 - 700, -700, x0 - 500, -100))
        draw(31, 0, kdb.Box(x0 - 700, -700, x0 - 500, -100))
        draw(33, 0, kdb.Box(x0 - 680, -650, x0 - 520, -150))
        draw(34, 0, kdb.Box(x0 - 750, -600, x0 - 450, -200))
        label(tie, x0 - 600, -400)
    # An unrelated n-well keeps the complement "substrate" a proper subset.
    draw(21, 0, kdb.Box(0, 40000, 20000, 50000))
    if isolate_second:
        draw(12, 0, kdb.Box(19000, -1500, 23000, 2500))  # DNWELL
    return layout


def _gf180_substrate_tie(name, net):
    return {
        "name": name,
        "well_layer": None,
        "well_complement_of": ["21/0"],
        "tap_layer": "22/0",
        "tap_requires": ["31/0"],
        "tap_is_dedicated": True,
        "connect_to": "metal1",
        "net": net,
    }


def _gf180_substrate_spec():
    return {
        "stackup": [
            {"name": "poly", "layer": "30/0", "role": "gate"},
            {"name": "metal1", "layer": "34/0", "label_layer": "34/10"},
        ],
        "vias": [{"name": "contact", "layer": "33/0", "between": ["poly", "metal1"]}],
        "nets": [
            {"name": "VSS", "kind": "supply"},
            {"name": "VSS2", "kind": "supply"},
        ],
        "ties": [
            _gf180_substrate_tie("p1", "VSS"),
            _gf180_substrate_tie("p2", "VSS2"),
        ],
    }


@pytest.mark.parametrize("isolated", [False, True])
def test_substrate_body_identity_agrees_between_erc_and_extract(tmp_path, isolated):
    from klayout_tools.extract import run_extract

    layout = _gf180_two_nmos_layout(isolate_second=isolated)
    gds = tmp_path / "agree.gds"
    layout.write(str(gds))
    spec = tmp_path / "agree.erc.json"
    spec.write_text(json.dumps(_gf180_substrate_spec()))

    erc_report = run_erc(str(gds), str(spec), deck="gf180mcu")
    extract_report = run_extract(
        str(gds), "gf180mcu", output=str(tmp_path / "agree.spice")
    )

    erc_bodies = {b["id"] for b in erc_report["provenance"]["substrate_bodies"]}
    shorts = [f for f in erc_report["erc_findings"] if f["rule"] == "erc.supply_short"]
    nfet_bodies = {
        d["nets"]["b"] for d in extract_report["devices"] if d["class"] == "nfet"
    }
    extract_nets = {n["name"] for n in extract_report["nets"]}

    if isolated:
        # DNWELL island: ERC names the second tap's body `vsubs_iso0` and
        # reports no short; the extractor keeps the two NMOS bodies on their
        # own tie nets (a drawn tie names its island, so `vsubs_iso0` itself
        # appears only for a tie-less island -- same partition, different
        # label) and merges nothing.
        assert erc_bodies == {"vsubs", "vsubs_iso0"}
        assert shorts == []
        assert nfet_bodies == {"VSS", "VSS2"}
        assert not any("|" in name for name in extract_nets)
    else:
        # One shared substrate: ERC reports the short through `vsubs`, and the
        # extractor merges exactly the same two nets into one body
        # (`VSS|VSS2`), the node LVS would report as a short.
        assert erc_bodies == {"vsubs"}
        assert [s["body"]["id"] for s in shorts] == ["vsubs"]
        assert {shorts[0]["net"], shorts[0]["other_net"]} == {"VSS", "VSS2"}
        assert nfet_bodies == {"VSS|VSS2"}
        assert set(nfet_bodies.pop().split("|")) == {"VSS", "VSS2"}

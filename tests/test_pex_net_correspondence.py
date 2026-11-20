"""The geometric net correspondence and the merged ``klt pex --engine
klayout-runset --deck <curated>`` leg (issue #6606).

Same faked-subprocess layer as ``tests/test_extract_runset.py``: the runset
subprocess is stubbed and writes a *real* ``.lvsdb`` built with the pip
``klayout.db`` API over the repo's own sg13cmos5l inverter fixture, while the
curated leg runs for real in-process -- so the join, the merged netlist
writer and the envelope fields are exercised end to end with no PDK install.
The fake builder's ``connect`` table has knobs that deliberately disagree
with the curated deck, which is what the disagreement reporting is asserted
on (issue #6606's Test Plan: "a deliberately-split net").

Real-PDK numbers (the measured outcomes the decision doc records) are pinned
by one skip-gated test at the end, against IHP's own ``pnpMPA`` testcase.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import klayout.db as kdb
import pytest

from klayout_tools.pex_merged_leg import merged_runset_extract
from klayout_tools.pex_net_correspondence import (
    correspondence,
    preferred_renames,
)
from klayout_tools.pex_runset import PexRunset
from test_extract_body_nets_sg13cmos5l import (
    _make_sg13cmos5l_inverter_layout,
)

#: The fixture's own layers (see ``_make_sg13cmos5l_inverter_layout``):
#: activ 1/0, gatpoly 5/0, cont 6/0, metal1 8/0, nwell 31/0, labels 8/2.
ACTIV, GATPOLY, CONT, METAL1 = (1, 0), (5, 0), (6, 0), (8, 0)


def _write_fixture(path: str) -> None:
    _make_sg13cmos5l_inverter_layout().write(path)


def _write_lvsdb(
    gds: str,
    schem: str,
    out: str,
    *,
    isolate_metals: bool = False,
    bridge_terminals: bool = False,
) -> None:
    """Stand in for the PDK runset on the cmos5l inverter fixture.

    ``isolate_metals`` drops every metal1 join (cont no longer bridges
    activ to metal1), so each electrical net the curated deck sees becomes
    two runset clusters -- a **merge** disagreement per labelled net.
    ``bridge_terminals`` additionally connects diffusion to the gate
    (something no engine should do), so one runset cluster spans several
    curated nets -- a **split** disagreement.
    """
    ly = kdb.Layout()
    ly.read(gds)
    l2n = kdb.LayoutVsSchematic(kdb.RecursiveShapeIterator(ly, ly.top_cell(), []))
    activ = l2n.make_layer(ly.layer(*ACTIV), "activ")
    poly = l2n.make_layer(ly.layer(*GATPOLY), "gatpoly")
    cont = l2n.make_layer(ly.layer(*CONT), "cont")
    metal1 = l2n.make_layer(ly.layer(*METAL1), "metal1")
    gate, sd = activ & poly, activ - poly
    l2n.extract_devices(
        kdb.DeviceExtractorMOS4Transistor("NMOS"),
        {"SD": sd, "G": gate, "tG": poly, "W": poly},
    )
    for region in (sd, poly, gate, cont, metal1):
        l2n.connect(region)
    if bridge_terminals:
        l2n.connect(sd, gate)
    if not isolate_metals:
        l2n.connect(metal1, cont)
        l2n.connect(cont, sd)
    l2n.extract_netlist()
    l2n.netlist().make_top_level_pins()
    ref = kdb.Netlist()
    ref.read(schem, kdb.NetlistSpiceReader())
    l2n.reference = ref
    l2n.compare(kdb.NetlistComparer())
    l2n.write(out)


@pytest.fixture
def fake_klayout(monkeypatch, tmp_path):
    """Replace the runset subprocess; ``state`` controls the fake database."""
    state: dict = {"isolate_metals": False, "bridge_terminals": False}
    fake_bin = tmp_path / "klayout"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("KLT_KLAYOUT_BINARY", str(fake_bin))
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[0] != str(fake_bin):
            return real_run(cmd, *a, **kw)
        if cmd[1:] == ["-v"]:
            return subprocess.CompletedProcess(cmd, 0, "KLayout 0.30.12\n", "")
        args = dict(c.split("=", 1) for c in cmd if "=" in c and c != "-rd")
        _write_lvsdb(
            args["input"],
            args["schematic"],
            args["report"],
            isolate_metals=state["isolate_metals"],
            bridge_terminals=state["bridge_terminals"],
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return state


def _merged(tmp_path, fake_klayout):
    gds = tmp_path / "inv.gds"
    _write_fixture(str(gds))
    deck_file = tmp_path / "pdk.lvs"
    deck_file.write_text("# runset\n")
    return merged_runset_extract(
        layout_path=str(gds),
        output=str(tmp_path / "leg.spice"),
        top="TOP",
        runset=PexRunset(deck_file=str(deck_file)),
        deck_name="sg13cmos5l",
        label_layers=None,
        device_models=None,
        deck_options=None,
        declared_pins=None,
        rename_declared_pins=False,
        tie_declared_pins=False,
        critical_nets=None,
        critical_net_patterns=None,
        critical_net_all=False,
        critical_net_excludes=None,
        distributed_rc=False,
        resistance_graph=False,
        resistance_graph_r_cut_ohm=None,
        parasitic_variants=None,
        substrate_tie_ohms=None,
        mom_rlc_net=None,
        mom_rlc_resistance_ohm=None,
        mom_rlc_capacitance_ff=None,
        mom_rlc_inductance_nh=None,
    )


def test_labelled_and_unlabelled_nets_both_match_and_get_parasitics(
    tmp_path, fake_klayout
):
    """Issue #6606's Test Plan: a device on a labelled and an unlabelled
    net -- the correspondence lists both, and parasitics reach the devices.

    The runset leg's nets are all anonymous (the fake reads no labels);
    the curated leg names A/VGND/VPWR/Y from the fixture's 8/2 labels, so
    every match pairs an anonymous runset net with a labelled curated one,
    and the merged netlist adopts the labels -- exactly the case the old
    "independent ``\\$N`` numbering" gap closed off."""
    report = _merged(tmp_path, fake_klayout)
    corr = report["net_correspondence"]

    # The whole labelled routing stack matches -- each pairing an anonymous
    # runset net (the fake reads no labels) with its labelled curated net.
    assert [entry["curated"] for entry in corr["matched"]] == [
        "VGND",
        "Y",
        "VPWR",
        "Y",
        "A",
    ]
    # Every matched runset net is the fixture's own anonymous \$N spelling.
    assert all(entry["runset"].startswith("\\$") for entry in corr["matched"])
    # What the runset saw that the curated leg did not: the well/substrate
    # body clusters, which carry no metal -- listed, never silently dropped.
    assert corr["unmatched_runset_nets"]
    assert corr["disagreements"] == []
    y_matches = [m for m in corr["matched"] if m["curated"] == "Y"]
    assert len({m["cluster_id"] for m in y_matches}) == 2
    assert corr["attached_card_count"] == 11  # 5 parasitic nets x (R + C) + tie
    assert corr["substrate_return"] == {
        "curated": "vsubs",
        "runset": None,
        "node": "vsubs",
        "tie_resistance_ohm": 1e12,
    }

    text = open(str(tmp_path / "leg.spice")).read()
    assert "parasitics attached from curated deck 'sg13cmos5l'" in text
    assert "no parasitics: this engine extracts devices" not in text
    assert ".GLOBAL vsubs" in text
    # The parasitic cards land on the *adopted label* nodes, and the device
    # cards reference the same nodes -- one netlist, not two views.
    assert "RVGND_plr VGND VGND__kltph" in text
    assert "CVGND_plc VGND__kltph vsubs" in text
    for node in ("Y", y_matches[1]["runset"]):
        assert f"R{node}_plr {node} {node}__kltph" in text
        assert f"C{node}_plc {node}__kltph vsubs" in text
        assert any(
            node in line.split()[1:-1]
            for line in text.splitlines()
            if line.startswith("X")
        )
    assert "X$1 " in text  # the runset device card survived the rewrite
    for line in text.splitlines():
        if line.startswith("X$1"):
            # the device card sits on the adopted-label nodes too
            assert "VPWR" in line
    # DC bias path for the substrate the caps land on, exactly as a curated
    # netlist ties it.
    assert "Rvsubs_dctie vsubs 0 1e+12" in text


def test_a_merge_disagreement_is_reported_never_guessed(tmp_path, fake_klayout):
    """A net the runset splits but the curated deck joins: the parasitics
    of the joined curated net cannot attach to either fragment -- the
    disagreement is reported and the fragments stay parasitic-free."""
    fake_klayout["isolate_metals"] = True
    report = _merged(tmp_path, fake_klayout)
    corr = report["net_correspondence"]

    merges = [d for d in corr["disagreements"] if d["kind"] == "merge"]
    assert {m["curated_net"] for m in merges} == {"A", "VGND", "VPWR", "Y"}
    for merge in merges:
        # exactly the two fragments: the metal side and the diffusion side
        assert len(merge["runset_nets"]) == 2
        assert all(net.startswith("\\$") for net in merge["runset_nets"])
    # Nothing matched, so nothing attached: the leg keeps its own honest
    # header and carries no cards.
    assert corr["matched"] == []
    assert corr["attached_card_count"] == 0
    text = open(str(tmp_path / "leg.spice")).read()
    assert "no parasitics: this engine extracts devices" in text
    assert "_plr " not in text


def test_a_split_disagreement_is_reported_not_compared(tmp_path, fake_klayout):
    """A runset cluster spanning several curated nets is refused both ways:
    reported as a split, its curated nets left unmatched, no cards."""
    fake_klayout["bridge_terminals"] = True
    report = _merged(tmp_path, fake_klayout)
    corr = report["net_correspondence"]

    splits = sorted(
        (d for d in corr["disagreements"] if d["kind"] == "split"),
        key=lambda d: d["runset_net"],
    )
    assert [s["curated_nets"] for s in splits] == [["VGND", "Y"], ["VPWR", "Y"]]
    # The one cluster the bridge left whole still matches and attaches.
    assert corr["matched"] == [{"runset": "\\$3", "curated": "A", "cluster_id": 3}]
    assert corr["attached_card_count"] == 3  # A's R + C + the substrate tie


# --------------------------------------------------------------------------- #
# Unit layer: the pure join pieces
# --------------------------------------------------------------------------- #


def _probe(runset_net: str, curated: str, cluster: int, found: bool = True):
    return {
        "name": f"kltcorr|metal0|{runset_net}|0",
        "found": found,
        "net": curated if found else None,
        "cluster_id": cluster if found else None,
    }


def test_correspondence_classifies_every_outcome():
    corr = correspondence(
        ["\\$1", "\\$2", "\\$3", "\\$4"],
        [
            _probe("\\$1", "A", 3),
            _probe("\\$2", "B", 1),
            _probe("\\$2", "C", 2),  # \$2 spans two curated nets: split
            _probe("\\$4", "A", 3),  # A also hit by \$1: merge with \$1
            {"name": "user-probe", "found": True, "net": "X", "cluster_id": 9},
        ],
        [{"net": "D", "net_id": 4}, {"net": "A", "net_id": 3}],
    )
    assert corr["matched"] == []  # A's merge poisons both of its fragments
    assert corr["unmatched_runset_nets"] == ["\\$1", "\\$2", "\\$3", "\\$4"]
    # Only parasitic curated nets are listed unmatched -- B/C were probe
    # hits, not parasitic nets, and D never matched anything.
    assert corr["unmatched_curated_nets"] == ["A", "D"]
    kinds = {
        (d["kind"], d.get("runset_net") or d.get("curated_net"))
        for d in corr["disagreements"]
    }
    assert ("split", "\\$2") in kinds
    assert ("merge", "A") in kinds
    # A caller's own probes never leak into the join.
    assert corr["probe_count"] == 5


def test_unmatched_same_label_and_overlapping_disagreements():
    corr = correspondence(
        ["first", "bridged", "fragment"],
        [
            _probe("first", "Y", 1),
            _probe("bridged", "Y", 2),
            _probe("bridged", "B", 3),
            _probe("fragment", "Y", 2),
        ],
        [{"net": "Y", "net_id": i} for i in [1, 2, 4]],
    )
    assert corr["matched"] == [{"runset": "first", "curated": "Y", "cluster_id": 1}]
    assert corr["unmatched_curated_clusters"] == [
        {"curated": "Y", "cluster_id": 2},
        {"curated": "Y", "cluster_id": 4},
    ]
    assert corr["unmatched_runset_nets"] == ["bridged", "fragment"]
    assert corr["disagreements"][0] == {
        "kind": "merge",
        "curated_net": "Y",
        "cluster_id": 2,
        "runset_nets": ["bridged", "fragment"],
    }


def test_unfound_probes_are_not_matches():
    corr = correspondence(
        ["\\$1"], [_probe("\\$1", None, None, found=False)], [{"net": "A", "net_id": 3}]
    )
    assert corr["matched"] == []
    assert corr["unmatched_runset_nets"] == ["\\$1"]
    assert corr["unmatched_curated_nets"] == ["A"]


def test_preferred_renames_adopt_labels_and_refuse_collisions():
    matched = [
        {"runset": "\\$1", "curated": "A"},
        {"runset": "\\$2", "curated": "B"},
        {"runset": "\\$3", "curated": "A"},  # second A: collision, no rename
    ]
    renames = preferred_renames(matched, ["\\$1", "\\$2", "\\$3", "PAD"])
    assert renames == {"\\$1": "A", "\\$2": "B"}
    # A label already owned by a named runset net is never adopted.
    renames = preferred_renames([{"runset": "\\$4", "curated": "PAD"}], ["\\$4", "PAD"])
    assert renames == {}


def test_probe_results_parse_only_kltcorr_names():
    corr = correspondence(
        ["\\$1"],
        [
            _probe("\\$1", "A", 1),
            {
                "name": "kltcorr|metal0|broken",
                "found": True,
                "net": "X",
                "cluster_id": 2,
            },
            {"name": "kltcorr|a|b|c|d", "found": True, "net": "Y", "cluster_id": 3},
        ],
        [],
    )
    assert corr["matched"] == [{"runset": "\\$1", "curated": "A", "cluster_id": 1}]


# --------------------------------------------------------------------------- #
# Real-PDK layer: the measured pnpMPA outcome, pinned
# --------------------------------------------------------------------------- #


def _pnpmpa_inputs():
    from klayout_tools import pdk

    try:
        root = pdk.find_pdk(variant="ihp-sg13cmos5l")["assets"]["klayout"]
    except Exception:
        pytest.skip("no ihp-sg13cmos5l PDK install")
    lvs_dir = os.path.join(root, "tech", "lvs")
    case = os.path.join(lvs_dir, "testing", "testcases", "unit", "bjt_devices")
    paths = (
        os.path.join(lvs_dir, "sg13cmos5l.lvs"),
        os.path.join(case, "layout", "pnpMPA.gds"),
    )
    if not all(os.path.isfile(p) for p in paths):
        pytest.skip("ihp-sg13cmos5l install lacks the pnpMPA LVS testcase")
    return paths


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_real_pnpmpa_merged_run_reports_the_measured_mismatch(tmp_path):
    """The decision doc's headline measurement, pinned: on IHP's own
    testcase the curated leg is empty (labels on 10/25 and PCell vias the
    pip ``klayout.db`` curated engine cannot see), so the merged run
    reports the total mismatch instead of attaching anything -- the honest
    degeneration that keeps ``no_parasitics`` the item-7 verdict there."""
    deck, gds = _pnpmpa_inputs()
    report = merged_runset_extract(
        layout_path=gds,
        output=str(tmp_path / "pnpMPA.spice"),
        top="pnpMPA_lvs",
        runset=PexRunset(deck_file=deck),
        deck_name="sg13cmos5l",
        label_layers=None,
        device_models=None,
        deck_options=None,
        declared_pins=None,
        rename_declared_pins=False,
        tie_declared_pins=False,
        critical_nets=None,
        critical_net_patterns=None,
        critical_net_all=False,
        critical_net_excludes=None,
        distributed_rc=False,
        resistance_graph=False,
        resistance_graph_r_cut_ohm=None,
        parasitic_variants=None,
        substrate_tie_ohms=None,
        mom_rlc_net=None,
        mom_rlc_resistance_ohm=None,
        mom_rlc_capacitance_ff=None,
        mom_rlc_inductance_nh=None,
    )
    corr = report["net_correspondence"]
    assert report["device_counts"] == {"pnpMPA": 1, "ptap1": 1}
    assert corr["matched"] == []
    assert corr["attached_card_count"] == 0
    # Both measured disagreement shapes: the substrate merge (runset $1 and
    # VSS both reach metal1 the curated leg folded into vsubs) and the
    # pad/power nets with no curated counterpart at all.
    assert {"PAD", "VDD"} <= set(corr["unmatched_runset_nets"])
    merges = [d for d in corr["disagreements"] if d["kind"] == "merge"]
    assert merges and merges[0]["curated_net"] == "vsubs"
    assert {"\\$1", "VSS"} <= set(merges[0]["runset_nets"])
    # The envelope this produces refuses item 7 (no_parasitics), which the
    # signoff tests in test_pex_runset_signoff.py grade separately.
    assert json.dumps(corr)

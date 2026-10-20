"""sky130 implant / npc deck rules (issue #2934, phase C).

`klt gen` draws nsdm (93/44), psdm (94/20) and npc (95/20); the curated deck
checks their plain-layer width/spacing rules and discloses the compound rules
it cannot evaluate through `coverage.unmodeled_rules`.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc


def _layout(boxes: list[tuple[tuple[int, int], kdb.Box]]) -> kdb.Layout:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for (layer, datatype), box in boxes:
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(box)
    return layout


NPC = (95, 20)
NSDM = (93, 44)
PSDM = (94, 20)


@pytest.mark.parametrize(
    ("layer", "rule", "ok_size", "bad_size"),
    [
        (NPC, "npc.width.1", 270, 260),
        (NSDM, "nsdm.width.1", 380, 370),
        (PSDM, "psdm.width.1", 380, 370),
    ],
)
def test_implant_width_rules(tmp_path, layer, rule, ok_size, bad_size):
    for size, expected in ((ok_size, 0), (bad_size, 1)):
        path = tmp_path / f"w{size}.gds"
        _layout([(layer, kdb.Box(0, 0, 2000, size))]).write(str(path))
        report = run_drc(str(path), "sky130")
        assert report["rule_counts"].get(rule, 0) == expected, report["rule_counts"]


@pytest.mark.parametrize(
    ("layer", "rule", "ok_gap", "bad_gap"),
    [
        (NPC, "npc.space.1", 270, 260),
        (NSDM, "nsdm.space.1", 380, 370),
        (PSDM, "psdm.space.1", 380, 370),
    ],
)
def test_implant_spacing_rules(tmp_path, layer, rule, ok_gap, bad_gap):
    for gap, expected in ((ok_gap, 0), (bad_gap, 1)):
        path = tmp_path / f"s{gap}.gds"
        _layout(
            [
                (layer, kdb.Box(0, 0, 1000, 1000)),
                (layer, kdb.Box(1000 + gap, 0, 2000 + gap, 1000)),
            ]
        ).write(str(path))
        report = run_drc(str(path), "sky130")
        assert report["rule_counts"].get(rule, 0) == expected, report["rule_counts"]


def test_unmodeled_implant_rules_are_disclosed(tmp_path):
    path = tmp_path / "x.gds"
    _layout([(NSDM, kdb.Box(0, 0, 1000, 1000))]).write(str(path))
    unmodeled = set(run_drc(str(path), "sky130")["coverage"]["unmodeled_rules"])
    assert {"n/psdm.5a", "n/psdm.5b", "difftap.8", "difftap.10", "npc.4"} <= unmodeled


@pytest.mark.parametrize("flavor", ["nfet", "pfet"])
@pytest.mark.parametrize("gate_contact", [False, True])
def test_generated_diff_pair_has_no_implant_violations(tmp_path, flavor, gate_contact):
    from klayout_tools.cli import main

    gds = tmp_path / "dp.gds"
    params = (
        '{"w_um":3.015,"l_um":1.2,"splits":2,"add_guard_ring":true,'
        f'"gate_contact":{str(gate_contact).lower()},"flavor":"{flavor}"}}'
    )
    assert (
        main(
            [
                "gen",
                "diff_pair",
                "--params",
                params,
                "--pdk",
                "sky130A",
                "-o",
                str(gds),
                "--format",
                "json",
            ]
        )
        == 0
    )
    counts = run_drc(str(gds), "sky130")["rule_counts"]
    implant = {
        k: v for k, v in counts.items() if k.split(".")[0] in {"npc", "nsdm", "psdm"}
    }
    assert implant == {}

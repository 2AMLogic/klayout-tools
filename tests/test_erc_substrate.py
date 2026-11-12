"""Substrate-continuity post-pass for ``erc.supply_short`` (issue #5342).

Design: ``docs/design/erc-substrate-continuity.md`` sections 3 and 9.
Fixtures 1-4 plus the monotonicity pin. gf180mcu's deck declares
``substrate_isolation`` (DNWELL 12/0), so ``deck="gf180mcu"`` enables the pass.
"""

from __future__ import annotations

import json

import klayout.db as kdb

from klayout_tools.erc import run_erc

DBU = 0.001


def _um(v: float) -> int:
    return int(round(v / DBU))


def _box(top, layout, layer, x0, y0, x1, y1):
    top.shapes(layout.layer(*layer)).insert(
        kdb.Box.new(_um(x0), _um(y0), _um(x1), _um(y1))
    )


def _rail(layout, top, y, name, tap_x=2.0):
    """A li1 rail labelled ``name`` carrying one implant-marked tap contact."""
    _box(top, layout, (3, 0), 0, y, 20, y + 1)
    top.shapes(layout.layer(3, 5)).insert(
        kdb.Text(name, kdb.Trans(_um(1), _um(y + 0.5)))
    )
    _box(top, layout, (21, 0), tap_x, y + 0.2, tap_x + 0.6, y + 0.8)
    _box(top, layout, (22, 0), tap_x - 0.2, y, tap_x + 0.8, y + 1)


def _spec(ties, nets=("VSS", "VSS2")):
    return {
        "stackup": [
            {"name": "poly", "layer": "1/0", "role": "gate"},
            {"name": "li1", "layer": "3/0", "label_layer": "3/5"},
        ],
        "vias": [{"name": "licon", "layer": "2/0", "between": ["poly", "li1"]}],
        "nets": [{"name": n, "kind": "supply"} for n in nets],
        "ties": ties,
    }


def _ptie(name, net, **over):
    entry = {
        "name": name,
        "well_layer": None,
        "well_complement_of": ["10/0"],
        "tap_layer": "21/0",
        "tap_requires": ["22/0"],
        "tap_is_dedicated": True,
        "connect_to": "li1",
        "net": net,
    }
    entry.update(over)
    return entry


def _run(tmp_path, layout, spec, stem="t", deck="gf180mcu"):
    gds = tmp_path / f"{stem}.gds"
    layout.write(str(gds))
    spec_path = tmp_path / f"{stem}.erc.json"
    spec_path.write_text(json.dumps(spec))
    return run_erc(str(gds), str(spec_path), deck=deck)


def _two_rails(second_tap_x=2.0):
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    _rail(layout, top, 0, "VSS")
    _rail(layout, top, 5, "VSS2", tap_x=second_tap_x)
    # An unrelated n-well keeps the complement "substrate" a proper subset of
    # the die (a whole-die substrate assertion is graded degenerate).
    _box(top, layout, (10, 0), 0, 20, 20, 30)
    return layout, top


def _shorts(report):
    return [f for f in report["erc_findings"] if f["rule"] == "erc.supply_short"]


def test_fixture1_substrate_only_short_is_reported(tmp_path):
    layout, _ = _two_rails()
    report = _run(tmp_path, layout, _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")]))
    shorts = _shorts(report)
    assert len(shorts) == 1
    assert shorts[0]["via"] == "substrate"
    assert shorts[0]["body"]["kind"] == "substrate"
    assert shorts[0]["body"]["id"] == "vsubs"
    assert shorts[0]["body"]["ties"] == ["p1", "p2"]
    checked = report["erc_coverage"]["checked"]
    assert "erc.supply_short.substrate" in checked
    assert not any(
        e["id"] == "erc.supply_short" for e in report["erc_coverage"]["inapplicable"]
    )
    assert report["status"] != "clean"


def test_fixture2_dnwell_island_isolates_the_second_tap(tmp_path):
    layout, top = _two_rails()
    _box(top, layout, (12, 0), -1, 4, 21, 7)  # DNWELL around the VSS2 rail
    report = _run(tmp_path, layout, _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")]))
    assert _shorts(report) == []
    bodies = {b["id"]: b for b in report["provenance"]["substrate_bodies"]}
    assert set(bodies) == {"vsubs", "vsubs_iso0"}
    assert bodies["vsubs_iso0"]["ties"] == ["p2"]


def test_fixture3_blanket_well_row_has_no_supply_short(tmp_path):
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    _box(top, layout, (10, 0), 0, 8, 20, 12)  # blanket n-well
    _rail(layout, top, 10, "VDD")
    _rail(layout, top, 0, "VSS")
    spec = _spec(
        [
            _ptie("n1", "VDD", well_layer="10/0", well_complement_of=None),
            _ptie("p1", "VSS"),
        ],
        nets=("VDD", "VSS"),
    )
    report = _run(tmp_path, layout, spec)
    assert _shorts(report) == []


def test_fixture4_degenerate_tie_joins_nothing(tmp_path):
    layout, _ = _two_rails()
    # Un-narrowed tap_layer (no tap_requires): degenerate -> no join.
    ties = [
        _ptie("p1", "VSS", tap_requires=[], tap_is_dedicated=False),
        _ptie("p2", "VSS2", tap_requires=[], tap_is_dedicated=False),
    ]
    report = _run(tmp_path, layout, _spec(ties))
    assert _shorts(report) == []


def test_pass_never_alters_gates_or_other_findings(tmp_path):
    layout, _ = _two_rails()
    ties = [_ptie("p1", "VSS"), _ptie("p2", "VSS2")]
    with_pass = _run(tmp_path, layout, _spec(ties), "w")
    without = _run(tmp_path, layout, _spec(ties), "n", deck=None)
    assert with_pass["gates"] == without["gates"]

    def keep(report):
        return [
            f
            for f in report["erc_findings"]
            if f["rule"] != "erc.missing_tie" and f.get("via") != "substrate"
        ]

    assert keep(with_pass) == keep(without)


def test_other_families_keep_the_disclosure(tmp_path):
    layout, _ = _two_rails()
    report = _run(
        tmp_path,
        layout,
        _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")]),
        deck=None,
    )
    assert _shorts(report) == []
    assert {
        "id": "erc.supply_short",
        "reason": "substrate_continuity_not_modelled",
    } in report["erc_coverage"]["inapplicable"]
    assert "substrate_bodies" not in report["provenance"]


import pytest  # noqa: E402


@pytest.mark.parametrize("deck", ["sg13g2", "sg13cmos5l"])
def test_ihp_nbulay_island_isolates_the_second_tap(tmp_path, deck):
    """Issue #5345: the IHP decks declare nBuLay (32/0) as
    ``substrate_isolation``, so a drawn nBuLay island around the VSS2 rail
    is a separate body and there is no substrate-only short; without it the
    two ties share ``vsubs`` and the short is reported."""
    ties = [_ptie("p1", "VSS"), _ptie("p2", "VSS2")]
    layout, top = _two_rails()
    _box(top, layout, (32, 0), -1, 4, 21, 7)
    report = _run(tmp_path, layout, _spec(ties), stem="iso", deck=deck)
    assert _shorts(report) == []
    bodies = {b["id"] for b in report["provenance"]["substrate_bodies"]}
    assert bodies == {"vsubs", "vsubs_iso0"}

    layout, _ = _two_rails()
    report = _run(tmp_path, layout, _spec(ties), stem="noiso", deck=deck)
    assert len(_shorts(report)) == 1

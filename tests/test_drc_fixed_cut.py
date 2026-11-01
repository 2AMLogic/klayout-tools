"""sky130 licon1/mcon fixed-size maximum and exemptions (issue #2835)."""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.drc import DrcError, run_drc

# --- licon1.width.1 / mcon.width.1 maximum size (issue #2835) -----------


def _fixed_cut_report(tmp_path, shapes):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    for (layer, datatype), geometry in shapes:
        top.shapes(layout.layer(layer, datatype)).insert(geometry)
    path = tmp_path / "fixed_cut.gds"
    layout.write(str(path))
    return run_drc(str(path), "sky130")


_CUT_RING = kdb.Region(kdb.Box(0, 0, 1000, 1000)) - kdb.Region(
    kdb.Box(400, 400, 600, 600)
)


@pytest.mark.parametrize(
    ("rule_id", "layer"),
    [("licon1.width.1", (66, 44)), ("mcon.width.1", (67, 44))],
)
def test_sky130_fixed_size_cut_max_ordinary(tmp_path, rule_id, layer):
    """Ordinary 0.17um cuts stay clean; an oversized square or an elongated
    bar violates the rule's maximum half."""
    assert (
        _fixed_cut_report(tmp_path, [(layer, kdb.Box(0, 0, 170, 170))])["status"]
        == "clean"
    )
    for box in (kdb.Box(0, 0, 200, 200), kdb.Box(0, 0, 170, 400)):
        report = _fixed_cut_report(tmp_path, [(layer, box)])
        assert report["rule_counts"] == {rule_id: 1}


@pytest.mark.parametrize("marker", [(86, 20), (79, 20)])
def test_sky130_licon1_precision_resistor_exemption(tmp_path, marker):
    """A 0.19 x 2.0um licon under rpm or urpm is exempt (licon.1b/c)."""
    report = _fixed_cut_report(
        tmp_path,
        [
            ((66, 44), kdb.Box(0, 0, 190, 2000)),
            (marker, kdb.Box(-500, -500, 900, 3000)),
        ],
    )
    assert report["status"] == "clean"
    # Same cut away from the marker is not exempt.
    report = _fixed_cut_report(
        tmp_path,
        [
            ((66, 44), kdb.Box(0, 0, 190, 2000)),
            (marker, kdb.Box(10000, 10000, 20000, 20000)),
        ],
    )
    assert report["rule_counts"] == {"licon1.width.1": 1}


def test_sky130_mcon_areaid_ce_and_ring_exemptions(tmp_path):
    """mcon under areaid:ce and ring-shaped mcon are exempt; a plain
    oversized mcon beside a ring is still flagged."""
    big = kdb.Box(0, 0, 200, 200)
    report = _fixed_cut_report(
        tmp_path, [((67, 44), big), ((81, 2), kdb.Box(-500, -500, 900, 900))]
    )
    assert report["status"] == "clean"
    report = _fixed_cut_report(tmp_path, [((67, 44), _CUT_RING)])
    assert report["status"] == "clean"
    report = _fixed_cut_report(
        tmp_path,
        [((67, 44), _CUT_RING), ((67, 44), kdb.Box(5000, 5000, 5200, 5200))],
    )
    assert report["rule_counts"] == {"mcon.width.1": 1}


def test_derived_layer_exclusions_require_not_interacting():
    from klayout_tools.decks import DerivedLayer, DrcRule

    rule = DrcRule(
        id="x.1",
        description="x",
        layer=(1, 0),
        check="width",
        threshold_dbu=10,
        derived_layer=DerivedLayer(
            base=(1, 0), sized_by_um=0.0, mode="holes", exclude_holed=True
        ),
    )
    from klayout_tools.drc import _validate_derived_layer

    with pytest.raises(DrcError, match="exclude_holed"):
        _validate_derived_layer(rule)

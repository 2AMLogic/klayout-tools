"""Contact-qualified tap connectivity on a two-supply SG13G2 layout."""

import json

import klayout.db as kdb
import pytest

from helpers.erc_substrate_fixtures import _run
from klayout_tools.erc import ErcError, validate_erc_spec


def _spec(through=("cont", "cont")):
    shared = {"tap_layer": "1/0", "tap_is_dedicated": True, "connect_to": "met1"}
    ties = [
        {**shared, "name": "nwell_vdd", "well_layer": "31/0", "net": "VDD"},
        {
            **shared,
            "name": "psub_vss",
            "well_layer": None,
            "net": "VSS",
            "well_complement_of": ["31/0"],
            "tap_requires": ["14/0"],
        },
    ]
    for tie, via in zip(ties, through, strict=True):
        if via is not None:
            tie["through"] = via
    return {
        "stackup": [
            {"name": "poly", "layer": "5/0", "role": "gate"},
            {"name": "met1", "layer": "8/0", "label_layer": "8/25"},
        ],
        "vias": [{"name": "cont", "layer": "6/0", "between": ["poly", "met1"]}],
        "nets": [{"name": net, "kind": "supply"} for net in ("VDD", "VSS")],
        "ties": ties,
    }


def _crossing(*, pad_cuts=("VDD", "VSS"), crossing_cuts=False, metal_short=False):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("fixture")

    def box(layer, *coords):
        top.shapes(layout.layer(layer, 0)).insert(kdb.Box(*coords))

    def label(name, x, y):
        top.shapes(layout.layer(8, 25)).insert(kdb.Text(name, x, y))

    box(31, 0, 0, 3000, 3000)
    box(14, 6000, 200, 6600, 1800)
    for net, x in (("VDD", 500), ("VSS", 6300)):
        box(1, x - 300, 200, x + 300, 1800)
        box(8, x - 300, 200, x + 300, 800)
        label(net, x, 500)
        if net in pad_cuts:
            box(6, x - 50, 450, x + 50, 550)
        if crossing_cuts:
            box(6, x - 50, 1450, x + 50, 1550)
    y = 500 if metal_short else 1500
    box(8, 0, y - 100, 6600, y + 100)
    label("SIG", 3300, y + 20)
    return layout


@pytest.mark.parametrize(
    ("through", "pad_cuts", "crossing_cuts", "expected"),
    [
        (("cont", "cont"), ("VDD", "VSS"), False, []),
        (("cont", "cont"), ("VDD",), False, [("erc.missing_tie", "psub_vss")]),
        (
            ("cont", "cont"),
            (),
            False,
            [("erc.missing_tie", "nwell_vdd"), ("erc.missing_tie", "psub_vss")],
        ),
        (("cont", "cont"), ("VDD", "VSS"), True, [("erc.supply_short", "substrate")]),
        ((None, None), ("VDD", "VSS"), False, [("erc.supply_short", "substrate")]),
        (("cont", None), ("VDD",), False, []),
        ((None, "cont"), ("VDD",), False, [("erc.missing_tie", "psub_vss")]),
    ],
)
def test_tap_connections(tmp_path, through, pad_cuts, crossing_cuts, expected):
    layout = _crossing(pad_cuts=pad_cuts, crossing_cuts=crossing_cuts)
    report = _run(tmp_path, layout, _spec(through), deck="sg13g2")
    assert {
        'erc.missing_tie:["nwell_vdd"]',
        'erc.missing_tie:["psub_vss"]',
        "erc.supply_short.substrate",
    } <= set(report["erc_coverage"]["checked"])
    assert (
        sorted((f["rule"], f.get("via") or f["layer"]) for f in report["erc_findings"])
        == expected
    )


@pytest.mark.parametrize("ties_enabled", [False, True])
def test_short_on_metal_itself(tmp_path, ties_enabled):
    spec = _spec()
    if not ties_enabled:
        spec["ties"] = []
    report = _run(tmp_path, _crossing(metal_short=True), spec, deck="sg13g2")
    assert any(
        f["rule"] == "erc.supply_short" and f["via"] == "metal"
        for f in report["erc_findings"]
    )


def test_crossing_signal_reaches_neither_contacted_tap(tmp_path):
    spec = _spec()
    for tie in spec["ties"]:
        tie["net"] = "SIG"
    report = _run(tmp_path, _crossing(), spec, deck="sg13g2")
    assert {
        f["layer"] for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"
    } == {"nwell_vdd", "psub_vss"}


def test_cut_to_other_endpoint_cannot_replace_local_metal_landing(tmp_path):
    layout = _crossing(pad_cuts=("VDD",))
    top = layout.top_cell()
    # A cut on the VSS tap has poly above it but no Metal1. The poly reaches
    # the VSS pad through a second cut outside the tap.
    for layer, bounds in (
        (6, (6250, 950, 6350, 1050)),
        (5, (6250, 950, 7350, 1050)),
        (5, (7250, 450, 7350, 1050)),
        (6, (7250, 450, 7350, 550)),
        (8, (6300, 300, 7400, 700)),
    ):
        top.shapes(layout.layer(layer, 0)).insert(kdb.Box(*bounds))
    report = _run(tmp_path, layout, _spec(), deck="sg13g2")
    assert [
        f["layer"] for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"
    ] == ["psub_vss"]


@pytest.mark.parametrize("value", [None, False, [], "", "unknown", "upper"])
def test_through_requires_a_via_to_connect_to(tmp_path, value):
    spec = _spec()
    spec["stackup"].extend(
        [{"name": "met2", "layer": "9/0"}, {"name": "met3", "layer": "10/0"}]
    )
    spec["vias"].append({"name": "upper", "layer": "11/0", "between": ["met2", "met3"]})
    spec["ties"][1]["through"] = value
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    with pytest.raises(ErcError, match=r"ties\[1\]\.through"):
        validate_erc_spec(str(path))

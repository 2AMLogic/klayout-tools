"""Native-substrate declarations and measured ASAP7 tap geometry (#6948)."""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from helpers.erc_substrate_fixtures import _box, _ptie, _run, _spec, _two_rails
from klayout_tools.cli import main
from klayout_tools.erc import ErcError, validate_erc_spec
from klayout_tools.erc_substrate import SUBSTRATE_WORK_ID


def _asap7(tmp_path, *, contacts=True, shared=False):
    layout = kdb.Layout()
    layout.dbu = 0.00025
    top = layout.create_cell("TAPS")

    def box(layer, left, bottom, right, upper):
        top.shapes(layout.layer(layer, 0)).insert(kdb.Box(left, bottom, right, upper))

    box(1, 0, 100, 200, 200)
    ties = []
    for y, net, in_well in [
        (30, "VSS", False),
        (65 if shared else 130, "VDD", not shared),
    ]:
        implant, other = (12, 13) if in_well else (13, 12)
        box(11, 20, y, 40, y + 20)
        box(implant, 10, y - 5, 55, y + 25)
        if contacts:
            box(88, 20, y, 40, y + 20)
        box(19, 0, y, 200, y + 20)
        top.shapes(layout.layer(19, 251)).insert(kdb.Text(net, kdb.Trans(30, y + 10)))
        box(11, 100, y, 120, y + 20)
        box(other, 95, y - 5, 125, y + 25)
        ties.append(
            {
                "name": net,
                "well_layer": "1/0" if in_well else None,
                "tap_layer": "11/0",
                "tap_requires": [f"{implant}/0", "88/0"],
                "net": net,
                "connect_to": "M1",
            }
        )
        if not in_well:
            ties[-1]["well_complement_of"] = "1/0"
    layout.write(str(tmp_path / "taps.gds"))
    return {
        "stackup": [
            {"name": "Gate", "role": "gate", "layer": "7/0"},
            {"name": "M1", "layer": "19/0", "label_layer": "19/251"},
        ],
        "nets": [{"name": n, "kind": "supply"} for n in ("VDD", "VSS")],
        "ties": ties,
    }


def _erc(tmp_path, capsys, spec):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    rc = main(
        [
            "erc",
            str(tmp_path / "taps.gds"),
            str(path),
            "--deck",
            "asap7",
            "--findings-only",
            "--omit-antenna-tables",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return rc, json.loads(captured.out)


def _missing(report):
    (entry,) = [
        e
        for e in report["erc_coverage"]["inapplicable"]
        if e["id"] == "erc.supply_short"
    ]
    assert entry["reason"] == "substrate_isolation_undecidable"
    return " ".join(entry["missing"])


@pytest.mark.parametrize("boxes", [None, []])
def test_tapped_asap7_requires_isolation_knowledge(tmp_path, capsys, boxes):
    spec = _asap7(tmp_path)
    spec["ties"][0]["isolation_boxes"] = boxes
    rc, report = _erc(tmp_path, capsys, spec)
    assert rc == 4 and report["erc_findings"] == []
    assert "common_native_substrate" in _missing(report)
    assert "no qualifying" not in _missing(report)


@pytest.mark.parametrize("assert_common", [False, True])
def test_no_contacts_cannot_be_credited_by_an_assertion(
    tmp_path, capsys, assert_common
):
    spec = _asap7(tmp_path, contacts=False)
    spec["common_native_substrate"] = assert_common
    rc, report = _erc(tmp_path, capsys, spec)
    assert rc == 3
    assert [f["rule"] for f in report["erc_findings"]] == ["erc.missing_tie"] * 2
    assert "no qualifying" in _missing(report)
    assert SUBSTRATE_WORK_ID not in report["erc_coverage"]["checked"]


def test_common_substrate_keeps_drawn_well_distinct(tmp_path, capsys):
    spec = _asap7(tmp_path)
    spec["common_native_substrate"] = True
    rc, report = _erc(tmp_path, capsys, spec)
    # A passive top has no antenna ratios to grade, independently of the ERC.
    assert rc == 4 and report["erc_status"] == "clean" and report["erc_findings"] == []
    for key in ("checked", "checked_by_assertion"):
        assert SUBSTRATE_WORK_ID in report["erc_coverage"][key]
    bodies = report["provenance"]["substrate_bodies"]
    assert [(b["kind"], b["ties"]) for b in bodies] == [
        ("substrate", ["VSS"]),
        ("well", ["VDD"]),
    ]
    assert bodies[0]["id"] == "vsubs"
    assert report["provenance"]["common_native_substrate"] is True


def test_common_substrate_shorts_two_tapped_rails(tmp_path, capsys):
    spec = _asap7(tmp_path, shared=True)
    spec["common_native_substrate"] = True
    rc, report = _erc(tmp_path, capsys, spec)
    assert rc == 3
    (short,) = [f for f in report["erc_findings"] if f["rule"] == "erc.supply_short"]
    assert short["via"] == "substrate" and short["body"]["id"] == "vsubs"
    assert {j["net"] for j in short["body"]["joins"]} == {"VDD", "VSS"}


def test_common_substrate_cannot_use_degenerate_taps(tmp_path, capsys):
    spec = _asap7(tmp_path)
    spec["common_native_substrate"] = True
    for tie in spec["ties"]:
        del tie["tap_requires"]
    _, report = _erc(tmp_path, capsys, spec)
    assert SUBSTRATE_WORK_ID not in report["erc_coverage"]["checked"]
    assert "non-degenerate" in _missing(report)


def test_common_substrate_preserves_drawn_deck_island(tmp_path):
    layout, top = _two_rails()
    _box(top, layout, (12, 0), -1, 4, 21, 7)
    spec = _spec([_ptie("p1", "VSS"), _ptie("p2", "VSS2")])
    spec["common_native_substrate"] = True
    report = _run(tmp_path, layout, spec)
    assert {b["id"] for b in report["provenance"]["substrate_bodies"]} == {
        "vsubs",
        "vsubs_iso0",
    }
    assert SUBSTRATE_WORK_ID not in report["erc_coverage"]["checked_by_assertion"]


@pytest.mark.parametrize("value", [None, "true", 1, {}])
def test_common_substrate_requires_a_boolean(tmp_path, value):
    spec = _asap7(tmp_path)
    spec["common_native_substrate"] = value
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(spec))
    with pytest.raises(ErcError, match="common_native_substrate.*boolean"):
        validate_erc_spec(str(path))


def test_conflicting_isolation_assertions_rejected(tmp_path):
    spec = _asap7(tmp_path)
    spec["common_native_substrate"] = True
    spec["ties"][0]["isolation_boxes"] = [[0, 0, 2, 2]]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(spec))
    with pytest.raises(ErcError, match="common_native_substrate.*isolation_boxes"):
        validate_erc_spec(str(path))

"""Tests for `klt erc` and the `run_erc` library function (issue #859,
Phase 1a of the antenna + ERC signoff epic #713).

Fixtures are generated programmatically with `klayout.db` inside the tests,
mirroring `tests/test_power.py`'s convention (`klt power` is the closest
precedent: a sibling connectivity-only Phase 1a verb built on the same
`LayoutToNetlist` wire/via-connectivity API).
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.erc import ErcError, run_erc

DBU = 0.001


def _um(v: float) -> int:
    return int(round(v / DBU))


def _basic_fixture(path) -> None:
    """One gate net threaded through a four-level stack (poly -> li1 ->
    met1 -> met2 via licon/mcon/via1), plus a second, entirely separate
    gate net (a different poly bar, connected only up to li1, with no
    metal at all) -- exercising both "reaches every level" and "stops
    partway" accumulation in the same layout.

    Layer numbers (arbitrary, not sky130's real ones -- kept small and
    distinct for readability, matching `tests/test_mom.py`'s convention):
    poly=1/0, licon=2/0, li1=3/0, mcon=4/0, met1=5/0, via1=6/0, met2=7/0,
    met2 label=7/5.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(1, 0)
    licon = layout.layer(2, 0)
    li1 = layout.layer(3, 0)
    mcon = layout.layer(4, 0)
    met1 = layout.layer(5, 0)
    via1 = layout.layer(6, 0)
    met2 = layout.layer(7, 0)
    met2_label = layout.layer(7, 5)

    # Gate A: poly (1x2 um = 2 um^2) -> li1 (2x1 = 2 um^2) -> met1
    # (3x0.5 = 1.5 um^2) -> met2 (0.4x4 = 1.6 um^2), labelled on met2.
    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(2)))
    top.shapes(licon).insert(kdb.Box.new(_um(0.2), _um(0.2), _um(0.4), _um(0.4)))
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(1)))
    top.shapes(mcon).insert(kdb.Box.new(_um(0.5), _um(0.3), _um(0.7), _um(0.5)))
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(3), _um(0.5)))
    top.shapes(via1).insert(kdb.Box.new(_um(1), _um(0.1), _um(1.2), _um(0.3)))
    top.shapes(met2).insert(kdb.Box.new(_um(0.9), _um(0), _um(1.3), _um(4)))
    top.shapes(met2_label).insert(kdb.Text("GATE_A", kdb.Trans(_um(1.0), _um(1.0))))

    # Gate B: isolated poly bar (0.5x1 = 0.5 um^2) with no contact/metal at
    # all -- an unstrapped gate, whose accumulation should simply stop at
    # the gate level (every level above reports 0).
    top.shapes(poly).insert(kdb.Box.new(_um(10), _um(0), _um(10.5), _um(1)))

    layout.write(str(path))


def _basic_spec(path) -> None:
    path.write_text(
        json.dumps(
            {
                "stackup": [
                    {"name": "poly", "layer": "1/0", "role": "gate"},
                    {"name": "li1", "layer": "3/0"},
                    {"name": "met1", "layer": "5/0"},
                    {"name": "met2", "layer": "7/0", "label_layer": "7/5"},
                ],
                "vias": [
                    {"name": "licon", "layer": "2/0", "between": ["poly", "li1"]},
                    {"name": "mcon", "layer": "4/0", "between": ["li1", "met1"]},
                    {"name": "via1", "layer": "6/0", "between": ["met1", "met2"]},
                ],
            }
        )
    )


def _write_spec(path, spec: dict) -> None:
    path.write_text(json.dumps(spec))


# --- run_erc: connectivity model accumulation -------------------------------


def test_run_erc_reports_two_gates(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))

    assert report["schema_version"] == 1
    assert report["file"] == str(gds)
    assert report["spec"] == str(spec)
    assert report["gate_role"] == "poly"
    assert report["gate_count"] == 2


def test_run_erc_accumulates_connected_area_layer_by_layer(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    gate_a = next(g for g in report["gates"] if g["net"] == "GATE_A")

    assert gate_a["gate_area_um2"] == pytest.approx(2.0)
    assert [level["layer"] for level in gate_a["levels"]] == [
        "poly",
        "li1",
        "met1",
        "met2",
    ]

    step_areas = {level["layer"]: level["step_area_um2"] for level in gate_a["levels"]}
    assert step_areas["poly"] == pytest.approx(2.0)
    assert step_areas["li1"] == pytest.approx(2.0)
    assert step_areas["met1"] == pytest.approx(1.5)
    assert step_areas["met2"] == pytest.approx(1.6)

    # Cumulative is a running sum across fabrication steps, in stackup order.
    cumulative = [level["cumulative_area_um2"] for level in gate_a["levels"]]
    assert cumulative == [
        pytest.approx(2.0),
        pytest.approx(4.0),
        pytest.approx(5.5),
        pytest.approx(7.1),
    ]


def test_run_erc_unstrapped_gate_stops_accumulating_after_gate_level(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    gate_b = next(g for g in report["gates"] if g["net"] is None)

    assert gate_b["gate_area_um2"] == pytest.approx(0.5)
    step_areas = {level["layer"]: level["step_area_um2"] for level in gate_b["levels"]}
    assert step_areas["poly"] == pytest.approx(0.5)
    assert step_areas["li1"] == 0.0
    assert step_areas["met1"] == 0.0
    assert step_areas["met2"] == 0.0
    # Cumulative never grows past the gate level itself.
    cumulative = [level["cumulative_area_um2"] for level in gate_b["levels"]]
    assert cumulative == [pytest.approx(0.5)] * 4


def test_run_erc_gate_ids_are_stable_ascending(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    assert [g["gate_id"] for g in report["gates"]] == ["gate0", "gate1"]


# --- run_erc: no matching geometry ------------------------------------------


def test_run_erc_no_gate_geometry_raises(tmp_path):
    gds = tmp_path / "no_gate.gds"
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    # Metal geometry only -- no poly (gate-role) shapes anywhere.
    met1 = layout.layer(5, 0)
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))
    layout.write(str(gds))

    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)

    with pytest.raises(ErcError, match="no net"):
        run_erc(str(gds), str(spec))


# --- run_erc: spec validation ------------------------------------------------


def test_spec_file_not_found(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    with pytest.raises(ErcError, match="spec file not found"):
        run_erc(str(gds), str(tmp_path / "nope.json"))


def test_spec_requires_stackup_with_at_least_two_entries(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(spec, {"stackup": [{"name": "poly", "layer": "1/0", "role": "gate"}]})
    with pytest.raises(ErcError, match="at least two entries"):
        run_erc(str(gds), str(spec))


def test_spec_requires_stackup_zero_to_be_gate_role(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0"},
                {"name": "li1", "layer": "3/0"},
            ]
        },
    )
    with pytest.raises(ErcError, match="stackup\\[0\\] must set"):
        run_erc(str(gds), str(spec))


def test_spec_rejects_a_second_gate_role(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "li1", "layer": "3/0", "role": "gate"},
            ]
        },
    )
    with pytest.raises(ErcError, match="only stackup\\[0\\] may set"):
        run_erc(str(gds), str(spec))


def test_spec_rejects_duplicate_stackup_names(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "poly", "layer": "3/0"},
            ]
        },
    )
    with pytest.raises(ErcError, match="duplicate stackup name"):
        run_erc(str(gds), str(spec))


def test_spec_rejects_malformed_layer_string(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "not-a-layer", "role": "gate"},
                {"name": "li1", "layer": "3/0"},
            ]
        },
    )
    with pytest.raises(ErcError, match="must be '<layer>/<datatype>'"):
        run_erc(str(gds), str(spec))


def test_spec_via_between_must_name_two_distinct_stackup_entries(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "li1", "layer": "3/0"},
            ],
            "vias": [{"layer": "2/0", "between": ["poly", "nope"]}],
        },
    )
    with pytest.raises(ErcError, match="must name two distinct"):
        run_erc(str(gds), str(spec))


def test_top_required_when_multiple_top_cells(tmp_path):
    gds = tmp_path / "two_tops.gds"
    layout = kdb.Layout()
    layout.dbu = DBU
    layout.create_cell("TOP_A")
    layout.create_cell("TOP_B")
    layout.write(str(gds))

    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)

    with pytest.raises(ErcError, match="needs exactly one top cell"):
        run_erc(str(gds), str(spec))


# --- CLI ----------------------------------------------------------------


def test_cli_json_contract(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "file",
        "spec",
        "gate_role",
        "gate_count",
        "gates",
    }
    assert data["schema_version"] == 1
    assert data["gate_count"] == 2


def test_cli_text_output(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "gates: 2" in out
    assert "GATE_A" in out
    assert "met2: step=" in out


def test_cli_error_exits_one_with_clean_message(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)

    assert main(["erc", str(gds), str(tmp_path / "nope.json")]) == 1
    err = capsys.readouterr().err
    assert "spec file not found" in err
    assert "Traceback" not in err


def test_cli_json_error_shape(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)

    assert main(["erc", str(gds), str(tmp_path / "nope.json"), "--format", "json"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "erc"
    assert "spec file not found" in err["error"]["message"]

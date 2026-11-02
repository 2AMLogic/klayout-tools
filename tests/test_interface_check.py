"""Tests for `klt interface-check` and `run_interface_check` (issue #4024).

Fixtures are built programmatically with `klayout.db`: a "driver strip" A with
N output pads at `y0 + k*pitch` and an "array" B with N landing pads at
`k*pitch`, each pad labelled `OUT[k]` on the pad layer.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.interface_check import InterfaceCheckError, run_interface_check

_PAD = (10, 0)
_PITCH = 5.0
_Y0 = 100.0


def _write(path, pads, *, dbu=0.001, layer=_PAD, cell="TOP"):
    """pads: list of (name, left, bottom, right, top) in um."""
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell(cell)
    index = layout.layer(*layer)
    for name, left, b, r, t in pads:
        top.shapes(index).insert(
            kdb.Box(round(left / dbu), round(b / dbu), round(r / dbu), round(t / dbu))
        )
        top.shapes(index).insert(
            kdb.Text(name, round((left + r) / 2 / dbu), round((b + t) / 2 / dbu))
        )
    layout.write(str(path))
    return str(path)


def _strip(n=4, y0=_Y0, dy=0.0):
    return [
        (f"OUT[{k}]", 0.0, y0 + k * _PITCH + dy, 2.0, y0 + k * _PITCH + dy + 1.0)
        for k in range(n)
    ]


def _array(n=4, dy=0.0, dx_pad=0.0):
    return [
        (
            f"OUT[{k}]",
            10.0 + dx_pad,
            k * _PITCH + dy,
            12.0 + dx_pad,
            k * _PITCH + dy + 1.0,
        )
        for k in range(n)
    ]


# B is the array; placing it at (-10, +100) lands its pad k on A's pad k.
_PLACE = (-10.0, _Y0)
_PINS = r"OUT\[\d+\]"


def test_aligned_pair_passes(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    report = run_interface_check(a, b, _PLACE, _PINS)
    assert report["status"] == "pass"
    assert report["pin_count"] == report["ok_count"] == 4
    assert [p["name"] for p in report["pins"]] == [f"OUT[{k}]" for k in range(4)]
    assert all(p["offset_um"] == [0.0, 0.0] for p in report["pins"])


def test_misaligned_pin_named_with_offset(tmp_path):
    arr = _array()
    arr[2] = ("OUT[2]", 10.0, 2 * _PITCH + 3.0, 12.0, 2 * _PITCH + 4.0)
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", arr)
    report = run_interface_check(a, b, _PLACE, _PINS)
    assert report["status"] == "fail"
    bad = [p for p in report["pins"] if p["status"] != "ok"]
    assert [p["name"] for p in bad] == ["OUT[2]"]
    assert bad[0]["status"] == "misaligned"
    assert bad[0]["offset_um"] == [0.0, 3.0]
    assert bad[0]["gap_um"] == 2.0
    assert report["misaligned_count"] == 1


def test_pitch_drift_flags_only_later_rows(tmp_path):
    arr = [(f"OUT[{k}]", 10.0, k * 5.5, 12.0, k * 5.5 + 1.0) for k in range(4)]
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", arr)
    report = run_interface_check(a, b, _PLACE, _PINS)
    status = {p["name"]: p["status"] for p in report["pins"]}
    assert status["OUT[0]"] == "ok"
    assert status["OUT[3]"] == "misaligned"


def test_tolerance_boundary(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array(dy=1.5))  # 0.5 um gap in y
    assert run_interface_check(a, b, _PLACE, _PINS)["status"] == "fail"
    assert run_interface_check(a, b, _PLACE, _PINS, tol_um=0.4)["status"] == "fail"
    assert run_interface_check(a, b, _PLACE, _PINS, tol_um=0.5)["status"] == "pass"


def test_touching_pads_are_ok(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array(dy=1.0))  # abut at y edge
    report = run_interface_check(a, b, _PLACE, _PINS)
    assert report["status"] == "pass"
    assert report["pins"][0]["gap_um"] == 0.0


def test_pin_only_in_one_stream_is_no_partner(tmp_path):
    a = _write(tmp_path / "a.gds", _strip(5))
    b = _write(tmp_path / "b.gds", _array(4))
    report = run_interface_check(a, b, _PLACE, _PINS)
    assert report["status"] == "fail"
    missing = report["pins"][-1]
    assert missing["name"] == "OUT[4]"
    assert missing["status"] == "no_partner"
    assert missing["b"] is None
    assert "only in stream A" in missing["reason"]


def test_label_without_pad_geometry_is_no_partner(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    report = run_interface_check(a, b, _PLACE, _PINS, pin_layer_b=(99, 0))
    assert report["no_partner_count"] == 4
    assert "no pad geometry" in report["pins"][0]["reason"]


def test_pin_layers_may_differ(tmp_path):
    a = _write(tmp_path / "a.gds", _strip(), layer=(11, 0))
    b = _write(tmp_path / "b.gds", _array(), layer=(12, 0))
    report = run_interface_check(a, b, _PLACE, _PINS)  # each label's own layer
    assert report["status"] == "pass"
    assert report["pins"][0]["a"]["layer"] == "11/0"
    assert report["pins"][0]["b"]["layer"] == "12/0"


def test_different_dbu(tmp_path):
    a = _write(tmp_path / "a.gds", _strip(), dbu=0.005)
    b = _write(tmp_path / "b.gds", _array(), dbu=0.001)
    assert run_interface_check(a, b, _PLACE, _PINS)["status"] == "pass"


def test_no_matching_pins_is_error(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    with pytest.raises(InterfaceCheckError, match="matches no label"):
        run_interface_check(a, b, _PLACE, "NOPE.*")


def test_pattern_is_full_match(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    with pytest.raises(InterfaceCheckError):
        run_interface_check(a, b, _PLACE, "OUT")


def test_label_on_pad_edge_counts_as_contained(tmp_path):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    idx = layout.layer(*_PAD)
    top.shapes(idx).insert(kdb.Box(0, 0, 2000, 1000))
    top.shapes(idx).insert(kdb.Box(5000, 0, 7000, 1000))  # nearer-by-centre decoy
    top.shapes(idx).insert(kdb.Text("P", 2000, 500))  # exactly on the right edge
    a = tmp_path / "a.gds"
    layout.write(str(a))
    b = _write(tmp_path / "b.gds", [("P", 0.0, 0.0, 2.0, 1.0)])
    report = run_interface_check(str(a), b, (0.0, 0.0), "P")
    assert report["pins"][0]["a"]["pad_bbox_um"] == [0.0, 0.0, 2.0, 1.0]
    assert report["status"] == "pass"


def test_label_off_pad_uses_nearest(tmp_path):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    idx = layout.layer(*_PAD)
    top.shapes(idx).insert(kdb.Box(0, 0, 2000, 1000))
    top.shapes(idx).insert(kdb.Box(9000, 0, 11000, 1000))
    top.shapes(idx).insert(kdb.Text("P", 3000, 500))
    a = tmp_path / "a.gds"
    layout.write(str(a))
    b = _write(tmp_path / "b.gds", [("P", 0.0, 0.0, 2.0, 1.0)])
    report = run_interface_check(str(a), b, (0.0, 0.0), "P")
    assert report["pins"][0]["a"]["pad_bbox_um"] == [0.0, 0.0, 2.0, 1.0]


def test_hierarchy_is_flattened(tmp_path):
    layout = kdb.Layout()
    layout.dbu = 0.001
    child = layout.create_cell("PADCELL")
    idx = layout.layer(*_PAD)
    child.shapes(idx).insert(kdb.Box(0, 0, 2000, 1000))
    child.shapes(idx).insert(kdb.Text("P", 1000, 500))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(0, 3000)))
    a = tmp_path / "a.gds"
    layout.write(str(a))
    b = _write(tmp_path / "b.gds", [("P", 0.0, 0.0, 2.0, 1.0)])
    report = run_interface_check(str(a), b, (0.0, 3.0), "P")
    assert report["status"] == "pass"


def test_duplicate_names_pair_by_occurrence(tmp_path):
    pads = [("P", 0.0, 0.0, 2.0, 1.0), ("P", 0.0, 5.0, 2.0, 6.0)]
    a = _write(tmp_path / "a.gds", pads)
    b = _write(tmp_path / "b.gds", pads)
    report = run_interface_check(a, b, (0.0, 0.0), "P")
    assert [p["name"] for p in report["pins"]] == ["P#1", "P#2"]
    assert report["status"] == "pass"


@pytest.mark.parametrize("place", ["1", "a,b", "1,2,3", "nan,0"])
def test_bad_place_is_error(tmp_path, place):
    from klayout_tools.interface_check import parse_place

    with pytest.raises(InterfaceCheckError):
        parse_place(place)


def test_place_accepts_parentheses():
    from klayout_tools.interface_check import parse_place

    assert parse_place("(1.5, -2)") == (1.5, -2.0)


def test_missing_file_is_error(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    with pytest.raises(InterfaceCheckError, match="file not found"):
        run_interface_check(a, str(tmp_path / "nope.gds"), _PLACE, _PINS)


# ---- CLI -----------------------------------------------------------------


def _cli(tmp_path, capsys, arr, *extra):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", arr)
    code = main(
        [
            "interface-check",
            a,
            b,
            "--place=-10,100",
            "--pins",
            _PINS,
            "--format",
            "json",
            *extra,
        ]
    )
    return code, capsys.readouterr()


def test_cli_exit_0_and_envelope(tmp_path, capsys):
    code, out = _cli(tmp_path, capsys, _array())
    assert code == 0
    payload = json.loads(out.out)
    assert payload["schema_version"] == 1
    assert payload["status"] == "pass"


def test_cli_exit_3_on_misaligned(tmp_path, capsys):
    code, out = _cli(tmp_path, capsys, _array(dy=3.0))
    assert code == 3
    assert json.loads(out.out)["misaligned_count"] == 4


def test_cli_exit_1_when_nothing_matches(tmp_path, capsys):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    code = main(
        ["interface-check", a, b, "--place", "0,0", "--pins", "ZZZ", "--format", "json"]
    )
    assert code == 1
    assert "matches no label" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_cli_text_output(tmp_path, capsys):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array(dy=3.0))
    code = main(["interface-check", a, b, "--place=-10,100", "--pins", _PINS])
    assert code == 3
    assert "OUT[0]: misaligned offset=(0.0, 3.0)" in capsys.readouterr().out


# ---- --pair (complementary names, issue #4803) ---------------------------

_CPINS = r"[ns]_(?:in|out)\[\d+\]"
_PAIR = [(r"n_out\[(\d+)\]", r"s_in[\1]")]


def _comp_a(n=3, dy=0.0):
    return [
        (f"n_out[{k}]", 0.0, k * _PITCH + dy, 2.0, k * _PITCH + dy + 1.0)
        for k in range(n)
    ]


def _comp_b(n=3):
    return [(f"s_in[{k}]", 0.0, k * _PITCH, 2.0, k * _PITCH + 1.0) for k in range(n)]


def test_pair_complementary_names_pass(tmp_path):
    a = _write(tmp_path / "a.gds", _comp_a())
    b = _write(tmp_path / "b.gds", _comp_b())
    report = run_interface_check(a, b, (0.0, 0.0), _CPINS, pairs=_PAIR)
    assert report["status"] == "pass"
    assert [p["name"] for p in report["pins"]] == [f"n_out[{k}]" for k in range(3)]
    assert [p["b_name"] for p in report["pins"]] == [f"s_in[{k}]" for k in range(3)]
    assert report["pairs"] == [{"a": _PAIR[0][0], "b": _PAIR[0][1]}]


def test_pair_misaligned(tmp_path):
    a = _write(tmp_path / "a.gds", _comp_a(dy=3.0))
    b = _write(tmp_path / "b.gds", _comp_b())
    report = run_interface_check(a, b, (0.0, 0.0), _CPINS, pairs=_PAIR)
    assert report["misaligned_count"] == 3


def test_pair_missing_partner_and_unclaimed_b(tmp_path):
    a = _write(tmp_path / "a.gds", _comp_a(3))
    b = _write(tmp_path / "b.gds", _comp_b(2) + [("s_in[7]", 0.0, 50.0, 2.0, 51.0)])
    report = run_interface_check(a, b, (0.0, 0.0), _CPINS, pairs=_PAIR)
    by = {p["name"]: p for p in report["pins"]}
    assert by["n_out[2]"]["status"] == "no_partner"
    assert by["n_out[2]"]["b_name"] == "s_in[2]"
    assert by["s_in[7]"]["status"] == "no_partner"
    assert by["s_in[7]"]["a"] is None


def test_no_pair_unchanged_and_b_name_equals_name(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    report = run_interface_check(a, b, _PLACE, _PINS)
    assert report["status"] == "pass"
    assert report["pairs"] == []
    assert all(p["b_name"] == p["name"] for p in report["pins"])


def test_pair_duplicate_names_keep_occurrence(tmp_path):
    a = _write(
        tmp_path / "a.gds",
        [("n_out[0]", 0.0, 0.0, 2.0, 1.0), ("n_out[0]", 0.0, 5.0, 2.0, 6.0)],
    )
    b = _write(
        tmp_path / "b.gds",
        [("s_in[0]", 0.0, 0.0, 2.0, 1.0), ("s_in[0]", 0.0, 5.0, 2.0, 6.0)],
    )
    report = run_interface_check(a, b, (0.0, 0.0), _CPINS, pairs=_PAIR)
    assert [p["name"] for p in report["pins"]] == ["n_out[0]#1", "n_out[0]#2"]
    assert [p["b_name"] for p in report["pins"]] == ["s_in[0]#1", "s_in[0]#2"]
    assert report["status"] == "pass"


@pytest.mark.parametrize(
    "spec", ["no-equals", "=x", "(unclosed=x", r"a(\d)=\2", r"a=\g<nope>"]
)
def test_bad_pair_is_error(spec):
    from klayout_tools.interface_check import parse_pair

    with pytest.raises(InterfaceCheckError):
        parse_pair(spec)


def test_parse_pair_splits_on_last_equals():
    from klayout_tools.interface_check import parse_pair

    assert parse_pair(r"a=b=c\g<0>") == ("a=b", r"c\g<0>")


def test_cli_pair_flag_and_text(tmp_path, capsys):
    a = _write(tmp_path / "a.gds", _comp_a())
    b = _write(tmp_path / "b.gds", _comp_b())
    base = ["interface-check", a, b, "--place", "0,0", "--pins", _CPINS]
    assert main([*base, "--pair", r"n_out\[(\d+)\]=s_in[\1]", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["pairs"][0]["b"] == r"s_in[\1]"
    assert main([*base, "--pair", r"n_out\[(\d+)\]=s_in[\1]"]) == 0
    assert "n_out[0] <-> s_in[0]: ok" in capsys.readouterr().out
    assert main([*base, "--pair", "bad", "--format", "json"]) == 1
    capsys.readouterr()
    assert main([*base, "--format", "json"]) == 3

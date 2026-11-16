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
    assert report["pins_pattern"] == _PINS
    assert report["pins_pattern_a"] is None
    assert report["pins_pattern_b"] is None


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


# ---- per-side pin filters (issue #5778) ------------------------------------
#
# A repeated block mates with its own copy across one seam: A's facing edge
# pairs with B's facing edge via --pair, and the per-side flags scope each
# stream to that edge so the same-name fallback never fires and the far edge
# stays out of the check.

_SEAM_PINS = r"t_(?:in|out)\[\d\]|b_(?:in|out)\[\d\]"
_SEAM_PINS_A = r"t_(?:in|out)\[\d\]"
_SEAM_PINS_B = r"b_(?:in|out)\[\d\]"
_SEAM_PAIRS = [(r"t_out\[(\d+)\]", r"b_in[\1]"), (r"t_in\[(\d+)\]", r"b_out[\1]")]


def _seam_block(b_in1_dy=0.0):
    """A 10x10 block: top edge ``t_out[k]`` / ``t_in[k]``, bottom edge
    ``b_in[k]`` / ``b_out[k]`` (pads + labels only, one layer).

    ``t_out[k]`` mates with a copy's ``b_in[k]`` and ``t_in[k]`` with its
    ``b_out[k]`` when the copy is placed at (0, 10). ``b_in1_dy`` lifts
    ``b_in[1]``'s pad off the seam -- a mating defect."""
    slots = [1.0, 3.5, 6.0, 8.5]
    pads = []
    for k in range(2):
        pads.append((f"t_out[{k}]", slots[k], 9.0, slots[k] + 1.0, 10.0))
        pads.append((f"t_in[{k}]", slots[2 + k], 9.0, slots[2 + k] + 1.0, 10.0))
        dy = b_in1_dy if k == 1 else 0.0
        pads.append((f"b_in[{k}]", slots[k], dy, slots[k] + 1.0, 1.0 + dy))
        pads.append((f"b_out[{k}]", slots[2 + k], 0.0, slots[2 + k] + 1.0, 1.0))
    return pads


def test_same_stream_one_seam_check_passes(tmp_path):
    block = _write(tmp_path / "c.gds", _seam_block())
    report = run_interface_check(
        block,
        block,
        (0.0, 10.0),
        _SEAM_PINS,
        pins_a=_SEAM_PINS_A,
        pins_b=_SEAM_PINS_B,
        pairs=_SEAM_PAIRS,
    )
    assert report["status"] == "pass"
    assert report["pin_count"] == report["ok_count"] == 4
    assert {p["name"] for p in report["pins"]} == {
        "t_out[0]",
        "t_out[1]",
        "t_in[0]",
        "t_in[1]",
    }
    assert {p["b_name"] for p in report["pins"]} == {
        "b_in[0]",
        "b_in[1]",
        "b_out[0]",
        "b_out[1]",
    }
    assert all(p["gap_um"] == 0.0 for p in report["pins"])


def test_same_stream_one_seam_defect_is_misaligned(tmp_path):
    block = _write(tmp_path / "c.gds", _seam_block(b_in1_dy=1.5))
    report = run_interface_check(
        block,
        block,
        (0.0, 10.0),
        _SEAM_PINS,
        pins_a=_SEAM_PINS_A,
        pins_b=_SEAM_PINS_B,
        pairs=_SEAM_PAIRS,
    )
    assert report["status"] == "fail"
    assert report["misaligned_count"] == 1
    bad = [p for p in report["pins"] if p["status"] == "misaligned"]
    assert bad[0]["name"] == "t_out[1]"
    assert bad[0]["b_name"] == "b_in[1]"
    assert bad[0]["gap_um"] == 1.5


def test_pair_collision_error_without_per_side_flags(tmp_path):
    block = _write(tmp_path / "c.gds", _seam_block())
    with pytest.raises(InterfaceCheckError, match="maps both"):
        run_interface_check(block, block, (0.0, 10.0), _SEAM_PINS, pairs=_SEAM_PAIRS)


def test_pins_a_alone_leaves_b_far_edge_in_scope(tmp_path):
    block = _write(tmp_path / "c.gds", _seam_block())
    report = run_interface_check(
        block, block, (0.0, 10.0), _SEAM_PINS, pins_a=_SEAM_PINS_A, pairs=_SEAM_PAIRS
    )
    ok = [p for p in report["pins"] if p["status"] == "ok"]
    orphan = [p for p in report["pins"] if p["status"] == "no_partner"]
    assert len(ok) == 4
    assert {p["b_name"] for p in ok} == {"b_in[0]", "b_in[1]", "b_out[0]", "b_out[1]"}
    assert len(orphan) == 4  # B's own top-edge labels, still in scope
    assert all(p["a"] is None and "only in stream B" in p["reason"] for p in orphan)
    assert report["status"] == "fail"


def test_pins_a_alone_narrows_only_a(tmp_path):
    a = _write(tmp_path / "a.gds", _strip() + [("EXTRA", 30.0, 0.0, 32.0, 1.0)])
    b = _write(tmp_path / "b.gds", _array())
    wide = run_interface_check(a, b, _PLACE, r"OUT\[\d+\]|EXTRA")
    assert wide["status"] == "fail"
    narrow = run_interface_check(a, b, _PLACE, r"OUT\[\d+\]|EXTRA", pins_a=_PINS)
    assert narrow["status"] == "pass"
    assert narrow["pin_count"] == 4


def test_pins_b_alone_narrows_only_b(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array() + [("EXTRA", 30.0, 0.0, 32.0, 1.0)])
    wide = run_interface_check(a, b, _PLACE, r"OUT\[\d+\]|EXTRA")
    assert wide["status"] == "fail"
    narrow = run_interface_check(a, b, _PLACE, r"OUT\[\d+\]|EXTRA", pins_b=_PINS)
    assert narrow["status"] == "pass"
    assert narrow["pin_count"] == 4


def test_per_side_flags_intersect_with_pins_not_override(tmp_path):
    a = _write(tmp_path / "a.gds", _strip() + [("EXTRA", 30.0, 0.0, 32.0, 1.0)])
    b = _write(tmp_path / "b.gds", _array())
    report = run_interface_check(a, b, _PLACE, _PINS, pins_a=".*")
    assert report["status"] == "pass"
    assert [p["name"] for p in report["pins"]] == [f"OUT[{k}]" for k in range(4)]


def test_both_flags_with_identical_patterns(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    report = run_interface_check(a, b, _PLACE, _PINS, pins_a=_PINS, pins_b=_PINS)
    assert report["status"] == "pass"
    assert report["pin_count"] == 4
    assert report["pins_pattern_a"] == report["pins_pattern_b"] == _PINS


def test_per_side_flag_preserves_occurrence_suffix(tmp_path):
    pads = [("P", 0.0, 0.0, 2.0, 1.0), ("P", 0.0, 5.0, 2.0, 6.0)]
    a = _write(tmp_path / "a.gds", pads)
    b = _write(tmp_path / "b.gds", pads)
    report = run_interface_check(a, b, (0.0, 0.0), "P", pins_a="P", pins_b="P")
    assert [p["name"] for p in report["pins"]] == ["P#1", "P#2"]
    assert report["status"] == "pass"


def test_per_side_flag_with_pairs_and_occurrence_suffix(tmp_path):
    a = _write(
        tmp_path / "a.gds",
        [("n_out[0]", 0.0, 0.0, 2.0, 1.0), ("n_out[0]", 0.0, 5.0, 2.0, 6.0)],
    )
    b = _write(
        tmp_path / "b.gds",
        [("s_in[0]", 0.0, 0.0, 2.0, 1.0), ("s_in[0]", 0.0, 5.0, 2.0, 6.0)],
    )
    report = run_interface_check(
        a, b, (0.0, 0.0), _CPINS, pins_a="n_.*", pins_b="s_.*", pairs=_PAIR
    )
    assert [p["name"] for p in report["pins"]] == ["n_out[0]#1", "n_out[0]#2"]
    assert [p["b_name"] for p in report["pins"]] == ["s_in[0]#1", "s_in[0]#2"]
    assert report["status"] == "pass"


def test_per_side_flag_with_pin_layers(tmp_path):
    a = _write(tmp_path / "a.gds", _strip(), layer=(11, 0))
    b = _write(tmp_path / "b.gds", _array(), layer=(12, 0))
    report = run_interface_check(a, b, _PLACE, _PINS, pins_a=_PINS, pins_b=_PINS)
    assert report["status"] == "pass"
    assert report["pins"][0]["a"]["layer"] == "11/0"
    assert report["pins"][0]["b"]["layer"] == "12/0"


def test_report_echoes_per_side_patterns(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    report = run_interface_check(a, b, _PLACE, _PINS, pins_a=_PINS, pins_b=r"OUT\[\d\]")
    assert report["pins_pattern"] == _PINS
    assert report["pins_pattern_a"] == _PINS
    assert report["pins_pattern_b"] == r"OUT\[\d\]"


@pytest.mark.parametrize("flag", ["pins_a", "pins_b"])
def test_bad_per_side_regex_is_error(tmp_path, flag):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    with pytest.raises(InterfaceCheckError, match="not a valid regex"):
        run_interface_check(a, b, _PLACE, _PINS, **{flag: "(unclosed"})


def test_per_side_regex_matching_nothing_is_error(tmp_path):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    with pytest.raises(InterfaceCheckError, match="--pins-a .* matches no label"):
        run_interface_check(a, b, _PLACE, _PINS, pins_a="ZZZ.*")
    with pytest.raises(InterfaceCheckError, match="--pins-b .* matches no label"):
        run_interface_check(a, b, _PLACE, _PINS, pins_b="ZZZ.*")


def test_cli_one_seam_invocation(tmp_path, capsys):
    good = _write(tmp_path / "c.gds", _seam_block())
    base = [
        "interface-check",
        good,
        good,
        "--place",
        "0,10",
        "--pins",
        _SEAM_PINS,
        "--pins-a",
        _SEAM_PINS_A,
        "--pins-b",
        _SEAM_PINS_B,
        "--pair",
        r"t_out\[(\d+)\]=b_in[\1]",
        "--pair",
        r"t_in\[(\d+)\]=b_out[\1]",
        "--format",
        "json",
    ]
    assert main(base) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["pin_count"] == payload["ok_count"] == 4
    assert payload["pins_pattern"] == _SEAM_PINS
    assert payload["pins_pattern_a"] == _SEAM_PINS_A
    assert payload["pins_pattern_b"] == _SEAM_PINS_B

    bad = _write(tmp_path / "bad.gds", _seam_block(b_in1_dy=1.5))
    capsys.readouterr()
    assert main([*base[:2], bad, *base[3:]]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["misaligned_count"] == 1
    bad_row = next(p for p in payload["pins"] if p["status"] == "misaligned")
    assert bad_row["name"] == "t_out[1]"
    assert bad_row["b_name"] == "b_in[1]"


def test_cli_bad_pins_a_regex_exits_1(tmp_path, capsys):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    code = main(
        [
            "interface-check",
            a,
            b,
            "--place=-10,100",
            "--pins",
            _PINS,
            "--pins-a",
            "(unclosed",
            "--format",
            "json",
        ]
    )
    assert code == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "--pins-a" in message
    assert "not a valid regex" in message


def test_cli_pins_a_matching_nothing_exits_1(tmp_path, capsys):
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    code = main(
        [
            "interface-check",
            a,
            b,
            "--place=-10,100",
            "--pins",
            _PINS,
            "--pins-a",
            "ZZZ.*",
            "--format",
            "json",
        ]
    )
    assert code == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "--pins-a" in message
    assert "matches no label" in message

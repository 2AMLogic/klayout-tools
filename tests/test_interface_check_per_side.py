"""Per-side pin filters (`--pins-a` / `--pins-b`) for `klt interface-check`
(issue #5778). Split out of test_interface_check.py for the file-size ratchet.

A repeated block mates with its own copy across one seam: A's facing edge
pairs with B's facing edge via `--pair`, and the per-side flags scope each
stream to that edge so the same-name fallback never fires and the far edge
stays out of the check.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.interface_check import InterfaceCheckError, run_interface_check
from test_interface_check import (
    _CPINS,
    _PAIR,
    _PINS,
    _PLACE,
    _array,
    _strip,
    _write,
)

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


def test_both_per_side_regexes_emptying_their_sides_blame_per_side_flag(tmp_path):
    # --pins itself matches 8 labels here; only the per-side filters are wrong,
    # so the error must name --pins-a, never the regex that was right (#6119).
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    with pytest.raises(InterfaceCheckError, match="--pins-a .* matches no label") as e:
        run_interface_check(a, b, _PLACE, _PINS, pins_a="ZZZ.*", pins_b="ZZZ.*")
    assert "--pins " not in str(e.value)


def test_pins_matching_nothing_still_blames_pins_with_per_side_flags(tmp_path):
    # Wording unchanged when --pins is the narrowest thing that matched nothing,
    # whether or not per-side filters are also supplied.
    a = _write(tmp_path / "a.gds", _strip())
    b = _write(tmp_path / "b.gds", _array())
    for kwargs in ({}, {"pins_a": ".*"}, {"pins_a": ".*", "pins_b": ".*"}):
        with pytest.raises(
            InterfaceCheckError, match="--pins 'ZZZ.*' matches no label"
        ):
            run_interface_check(a, b, _PLACE, "ZZZ.*", **kwargs)


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

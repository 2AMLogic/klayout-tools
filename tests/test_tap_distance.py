"""Tests for `klt tap-distance` and the `run_tap_distance` library function.

Fixtures are synthetic geometry generated with `klayout.db` inside the tests
(no PDK data), mirroring `tests/test_ring_check.py`.
"""

from __future__ import annotations

import json
import math

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.tap_distance import (
    POLYGON_SIDES,
    TapDistanceError,
    parse_layer_expr,
    run_tap_distance,
)

_DBU = 0.005
_SUBJECT = (1, 0)
_TAP = (2, 0)
_WELL = (3, 0)
_M1 = (10, 0)
_LABEL = (10, 1)


def _um(v: float) -> int:
    return round(v / _DBU)


def _box(x0, y0, x1, y1) -> kdb.Box:
    return kdb.Box(_um(x0), _um(y0), _um(x1), _um(y1))


def _write(path, layers: dict[tuple[int, int], list[kdb.Box]], texts=None) -> str:
    layout = kdb.Layout()
    layout.dbu = _DBU
    top = layout.create_cell("TOP")
    for (layer, dt), boxes in layers.items():
        idx = layout.layer(layer, dt)
        for box in boxes:
            top.shapes(idx).insert(box)
    for (layer, dt), items in (texts or {}).items():
        idx = layout.layer(layer, dt)
        for x, y, s in items:
            top.shapes(idx).insert(kdb.Text(s, _um(x), _um(y)))
    layout.write(str(path))
    return str(path)


def _run(path, max_um, **kw):
    return run_tap_distance(path, "1/0", "2/0", max_um, **kw)


def test_parse_layer_expr():
    assert parse_layer_expr("1/0 & 2/3 - 4/5", "--x") == [
        ("+", (1, 0)),
        ("&", (2, 3)),
        ("-", (4, 5)),
    ]
    with pytest.raises(TapDistanceError):
        parse_layer_expr("1/0 ^ 2/0", "--x")
    with pytest.raises(TapDistanceError):
        parse_layer_expr("", "--x")


def test_axis_distance_matches_analytic_within_bound(tmp_path):
    # Subject 0..10 wide; tap is 0..1; farthest subject point is x=10, so the
    # true worst distance is 9 um (y offsets are zero).
    path = _write(
        tmp_path / "a.gds",
        {_SUBJECT: [_box(0, 0, 10, 2)], _TAP: [_box(-1, 0, 1, 2)]},
    )
    report = _run(path, 12.0)
    m = report["measurement"]
    assert report["status"] == "pass"
    assert report["input_hash"].startswith("sha256:")
    # The tap spans the subject's full height, so the true worst distance is
    # the horizontal gap from x=10 to the tap edge x=1.
    true = 9.0
    assert m["lower_bound_um"] <= true <= m["measured_max_distance_um"]
    assert m["error_bound_um"] < 0.01 * true + 4 * _DBU


def test_verdict_flips_at_threshold_within_bound(tmp_path):
    path = _write(
        tmp_path / "b.gds",
        {_SUBJECT: [_box(0, 0, 10, 2)], _TAP: [_box(-1, 0, 1, 2)]},
    )
    true = 9.0
    assert _run(path, true * 1.01 + 0.05)["status"] == "pass"
    failing = _run(path, true * 0.98)
    assert failing["status"] == "fail"
    assert failing["violations"][0]["definite"] is True
    assert failing["violations"][0]["kind"] == "too_far"
    assert failing["islands"][0]["worst_locations"]


def test_euclidean_not_manhattan_diagonal(tmp_path):
    # Tap box 0..10; subject 20..21 square. Farthest subject corner (21,21)
    # to the tap corner (10,10): Euclid 11*sqrt2 = 15.556, Manhattan 22.
    path = _write(
        tmp_path / "c.gds",
        {_SUBJECT: [_box(20, 20, 21, 21)], _TAP: [_box(0, 0, 10, 10)]},
    )
    assert _run(path, 15.7)["status"] == "pass"
    assert _run(path, 15.3)["status"] == "fail"
    # A Manhattan metric would need >= 22 to pass; Euclid passes far below it.
    assert _run(path, 18.0)["status"] == "pass"


def test_bracket_holds_on_failing_diagonal_offset_layout(tmp_path):
    # Small subject, tap far off on the diagonal: the farthest subject corner
    # (0,0) is hypot(100,100) = 141.421 um from the tap corner (100,100). A
    # failing run (max below that) must still bracket the true distance.
    path = _write(
        tmp_path / "diag.gds",
        {_SUBJECT: [_box(0, 0, 1, 1)], _TAP: [_box(100, 100, 101, 101)]},
    )
    true = math.hypot(100, 100)
    report = _run(path, 10.0)
    m = report["measurement"]
    assert report["status"] == "fail"
    assert m["lower_bound_um"] <= true <= m["measured_max_distance_um"]
    island = report["islands"][0]
    assert island["lower_bound_um"] <= true <= island["measured_max_distance_um"]


def test_tap_overlapping_subject_is_zero_distance(tmp_path):
    path = _write(
        tmp_path / "d.gds",
        {_SUBJECT: [_box(0, 0, 4, 4)], _TAP: [_box(-1, -1, 5, 5)]},
    )
    report = _run(path, 0.5)
    assert report["status"] == "pass"
    assert report["measurement"]["measured_max_distance_um"] == 0.0


def test_subject_with_hole(tmp_path):
    # Ring subject around a tap island in its hole: the far side of the ring
    # is the worst point.
    outer = kdb.Region(_box(0, 0, 20, 20)) - kdb.Region(_box(8, 8, 12, 12))
    layout = kdb.Layout()
    layout.dbu = _DBU
    top = layout.create_cell("TOP")
    for poly in outer.each():
        top.shapes(layout.layer(*_SUBJECT)).insert(poly)
    top.shapes(layout.layer(*_TAP)).insert(_box(9, 9, 11, 11))
    path = str(tmp_path / "e.gds")
    layout.write(path)
    far = math.hypot(9.0, 9.0)  # corner (0,0) to the tap corner (9,9)
    assert _run(path, far + 0.2)["status"] == "pass"
    assert _run(path, far - 0.5)["status"] == "fail"


def test_island_partition_tap_in_other_island_does_not_count(tmp_path):
    path = _write(
        tmp_path / "f.gds",
        {
            _WELL: [_box(0, 0, 10, 10), _box(30, 0, 40, 10)],
            _SUBJECT: [_box(1, 1, 9, 9), _box(31, 1, 39, 9)],
            _TAP: [_box(1, 1, 2, 2)],  # only in well A
        },
    )
    report = _run(path, 100.0, island="3/0")
    assert report["status"] == "fail"
    kinds = {v["island"]: v["kind"] for v in report["violations"]}
    assert list(kinds.values()) == ["no_tap"]
    statuses = sorted(e["status"] for e in report["islands"])
    assert statuses == ["fail", "pass"]
    # Without partitioning, the far tap would satisfy a generous bound.
    assert _run(path, 100.0)["status"] == "pass"


def test_island_without_subject_is_not_a_failure(tmp_path):
    path = _write(
        tmp_path / "g.gds",
        {
            _WELL: [_box(0, 0, 10, 10), _box(30, 0, 40, 10)],
            _SUBJECT: [_box(1, 1, 9, 9)],
            _TAP: [_box(1, 1, 2, 2)],
        },
    )
    report = _run(path, 20.0, island="3/0")
    assert report["status"] == "pass"
    assert sorted(e["status"] for e in report["islands"]) == ["no_subject", "pass"]


@pytest.mark.parametrize(
    ("layers", "reason"),
    [
        ({_TAP: [_box(0, 0, 1, 1)]}, "subject_empty"),
        ({_SUBJECT: [_box(0, 0, 1, 1)]}, "taps_empty"),
    ],
)
def test_empty_layers_are_unsupported_never_pass(tmp_path, layers, reason):
    path = _write(tmp_path / "h.gds", layers)
    report = _run(path, 5.0)
    assert report["status"] == "unsupported"
    assert report["reason"] == reason
    assert report["violations"] == []


def test_empty_island_expression_is_unsupported(tmp_path):
    path = _write(
        tmp_path / "i.gds",
        {_SUBJECT: [_box(0, 0, 1, 1)], _TAP: [_box(0, 0, 1, 1)]},
    )
    assert _run(path, 5.0, island="9/9")["reason"] == "island_empty"


def _net_layout(tmp_path):
    # Two taps; the left one is wired to VSS via metal1 (label VSS), the right
    # one to a different label, VDD.
    return _write(
        tmp_path / "n.gds",
        {
            _SUBJECT: [_box(0, 0, 30, 2)],
            _TAP: [_box(0, 0, 2, 2), _box(28, 0, 30, 2)],
            _M1: [_box(0, 0, 2, 2), _box(28, 0, 30, 2)],
        },
        texts={_LABEL: [(1, 1, "VSS"), (29, 1, "VDD")]},
    )


def _spec(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "stackup": [{"name": "m1", "layer": "10/0", "label_layer": "10/1"}],
                "tap_connect_to": "m1",
            }
        )
    )
    return str(spec)


def test_net_filter_counts_only_wired_taps(tmp_path):
    path = _net_layout(tmp_path)
    spec = _spec(tmp_path)
    # Any tap: worst distance is ~14 um (mid-span); with VSS only: ~28.
    assert _run(path, 15.0)["status"] == "pass"
    assert _run(path, 15.0, tap_nets=["VSS"], net_spec=spec)["status"] == "fail"
    assert _run(path, 29.0, tap_nets=["VSS"], net_spec=spec)["status"] == "pass"


def test_net_not_found_is_unsupported(tmp_path):
    path = _net_layout(tmp_path)
    report = _run(path, 99.0, tap_nets=["NOPE"], net_spec=_spec(tmp_path))
    assert report["status"] == "unsupported"
    assert report["reason"] == "net_not_found"


def test_net_filter_without_spec_is_unsupported(tmp_path):
    path = _net_layout(tmp_path)
    report = _run(path, 99.0, tap_nets=["VSS"])
    assert report["status"] == "unsupported"
    assert report["reason"] == "no_extraction"


def test_hierarchical_layout(tmp_path):
    layout = kdb.Layout()
    layout.dbu = _DBU
    child = layout.create_cell("CHILD")
    child.shapes(layout.layer(*_SUBJECT)).insert(_box(0, 0, 4, 4))
    child.shapes(layout.layer(*_TAP)).insert(_box(0, 0, 1, 1))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(_um(100), _um(100))))
    path = str(tmp_path / "hier.gds")
    layout.write(path)
    report = _run(path, math.hypot(3, 3) + 0.2)
    assert report["status"] == "pass"
    assert _run(path, 3.0)["status"] == "fail"


def test_polygon_sides_error_bound_is_documented_constant():
    assert 1 - math.cos(math.pi / POLYGON_SIDES) < 0.002


def test_bad_inputs_raise(tmp_path):
    path = _write(tmp_path / "x.gds", {_SUBJECT: [_box(0, 0, 1, 1)]})
    with pytest.raises(TapDistanceError):
        run_tap_distance(path, "1/0", "2/0", 0)
    with pytest.raises(TapDistanceError):
        run_tap_distance(str(tmp_path / "missing.gds"), "1/0", "2/0", 1.0)
    with pytest.raises(TapDistanceError):
        run_tap_distance(path, "1/0", "2/0", 1.0, top="NOPE")


# --- CLI ---------------------------------------------------------------------


def _cli(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr()


def test_cli_exit_codes_and_envelope(tmp_path, capsys):
    path = _write(
        tmp_path / "cli.gds",
        {_SUBJECT: [_box(0, 0, 10, 2)], _TAP: [_box(-1, 0, 1, 2)]},
    )
    base = [
        "tap-distance",
        path,
        "--subject",
        "1/0",
        "--taps",
        "2/0",
        "--format",
        "json",
    ]
    code, out = _cli([*base, "--max-distance-um", "12"], capsys)
    assert code == 0
    data = json.loads(out.out)
    assert data["schema_version"] == 1 and data["status"] == "pass"

    code, out = _cli([*base, "--max-distance-um", "5"], capsys)
    assert code == 3
    assert json.loads(out.out)["status"] == "fail"

    code, out = _cli(
        [
            "tap-distance",
            path,
            "--subject",
            "1/0",
            "--taps",
            "9/9",
            "--max-distance-um",
            "5",
            "--format",
            "json",
        ],
        capsys,
    )
    assert code == 4
    assert json.loads(out.out)["status"] == "unsupported"

    code, out = _cli([*base, "--max-distance-um", "5", "--subject", "bad"], capsys)
    assert code == 1
    assert json.loads(out.err)["error"]


def test_cli_text_format(tmp_path, capsys):
    path = _write(
        tmp_path / "t.gds",
        {_SUBJECT: [_box(0, 0, 10, 2)], _TAP: [_box(-1, 0, 1, 2)]},
    )
    code, out = _cli(
        [
            "tap-distance",
            path,
            "--subject",
            "1/0",
            "--taps",
            "2/0",
            "--max-distance-um",
            "5",
            "--format",
            "text",
        ],
        capsys,
    )
    assert code == 3
    assert "status: fail" in out.out

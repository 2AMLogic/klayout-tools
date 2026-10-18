"""Tests for `klt keepout-check` and the `run_keepout_check` library function.

Fixtures are generated programmatically with `klayout.db` inside the tests,
mirroring `tests/test_ring_check.py`. Purely geometric: no deck, PDK or
extraction.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.keepout_check import KeepoutCheckError, run_keepout_check

_DBU = 0.001
_SPIRAL = (50, 0)  # the guarded cell's own layer
_FORBID = (30, 0)  # forbidden layer
_OTHER = (31, 0)


def _um(v: float) -> int:
    return round(v / _DBU)


def _box(left, bottom, right, top) -> kdb.Box:
    return kdb.Box(_um(left), _um(bottom), _um(right), _um(top))


def _write(path, foreign=(), place=((0, 0, False, 0),), extra_tops=False):
    """TOP holds ``SPIRAL`` (a 0..100 um square on _SPIRAL, plus its own shape
    on _FORBID) placed per ``place`` = (x_um, y_um, mirror, rot90s), plus
    ``foreign`` = [(layer, box_um)] shapes owned by TOP itself."""
    layout = kdb.Layout()
    layout.dbu = _DBU
    spiral = layout.create_cell("SPIRAL")
    spiral.shapes(layout.layer(*_SPIRAL)).insert(_box(0, 0, 100, 100))
    # The guarded cell's own forbidden-layer shape must never be reported.
    spiral.shapes(layout.layer(*_FORBID)).insert(_box(10, 10, 20, 20))
    top = layout.create_cell("TOP")
    for x, y, mirror, rot in place:
        trans = kdb.CplxTrans(1.0, 90.0 * rot, mirror, _um(x), _um(y))
        top.insert(kdb.CellInstArray(spiral.cell_index(), trans))
    for layer, box in foreign:
        top.shapes(layout.layer(*layer)).insert(_box(*box))
    if extra_tops:
        layout.create_cell("OTHERTOP")
    layout.write(str(path))


def _run(path, **kw):
    kw.setdefault("cells", ["SPIRAL"])
    return run_keepout_check(str(path), [_FORBID], kw.pop("margin", 10.0), **kw)


def test_clean_when_nothing_in_margin(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, foreign=[(_FORBID, (111, 0, 120, 10))])
    report = _run(path)
    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    assert report["schema_version"] == 1
    assert report["cells"] == ["SPIRAL"]


def test_own_subtree_shapes_ignored(tmp_path):
    path = tmp_path / "a.gds"
    _write(path)
    assert _run(path)["status"] == "clean"


def test_margin_band_violation_reported_in_um(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, foreign=[(_FORBID, (105, 20, 130, 30))])
    report = _run(path)
    assert report["status"] == "violated"
    (v,) = report["violations"]
    assert v["rule"] == "keepout.forbidden_shape"
    assert v["kind"] == "margin"
    assert v["layer"] == "30/0"
    assert v["cell"] == "TOP"
    # Only the part inside the zone (x <= 110) is reported.
    assert (v["bbox"]["left"], v["bbox"]["right"]) == (105.0, 110.0)
    assert (v["bbox"]["bottom"], v["bbox"]["top"]) == (20.0, 30.0)


def test_shape_inside_guard_is_inside_kind(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, foreign=[(_FORBID, (40, 40, 60, 60))])
    (v,) = _run(path)["violations"]
    assert v["kind"] == "inside"


def test_margin_boundary_is_clear(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, foreign=[(_FORBID, (110, 20, 120, 30))])  # distance == margin
    assert _run(path)["status"] == "clean"
    path2 = tmp_path / "b.gds"
    _write(path2, foreign=[(_FORBID, (109.999, 20, 120, 30))])
    assert _run(path2)["status"] == "violated"


def test_margin_zero_flags_only_overlap(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, foreign=[(_FORBID, (100, 20, 120, 30))])  # touching only
    assert _run(path, margin=0)["status"] == "clean"
    path2 = tmp_path / "b.gds"
    _write(path2, foreign=[(_FORBID, (99, 20, 120, 30))])
    assert _run(path2, margin=0)["status"] == "violated"


def test_other_layers_not_flagged(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, foreign=[(_OTHER, (105, 20, 130, 30))])
    assert _run(path)["status"] == "clean"


def test_multiple_forbidden_layers(tmp_path):
    path = tmp_path / "a.gds"
    _write(
        path,
        foreign=[(_FORBID, (105, 20, 108, 30)), (_OTHER, (-8, 20, -2, 30))],
    )
    report = run_keepout_check(str(path), [_FORBID, _OTHER], 10.0, cells=["SPIRAL"])
    assert sorted(v["layer"] for v in report["violations"]) == ["30/0", "31/0"]


def test_hierarchical_transforms_every_placement(tmp_path):
    path = tmp_path / "a.gds"
    # Second placement rotated 90 deg about origin lands at x in [-100, 0].
    _write(
        path,
        place=((0, 0, False, 0), (500, 0, False, 1)),
        foreign=[(_FORBID, (395, 10, 405, 20))],
    )
    report = _run(path)
    assert report["violation_count"] == 1
    assert report["violations"][0]["bbox"]["left"] == 395.0


def test_region_guard(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, foreign=[(_FORBID, (300, 300, 310, 310))])
    report = run_keepout_check(
        str(path), [_FORBID], 10.0, region_um=(295, 295, 305, 305)
    )
    assert report["status"] == "violated"
    assert report["region_um"] == [295, 295, 305, 305]
    assert report["cells"] is None


def test_multiple_top_cells_checked(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, extra_tops=True, foreign=[(_FORBID, (105, 20, 130, 30))])
    report = _run(path)  # SPIRAL is placed only in TOP; OTHERTOP is skipped
    assert report["status"] == "violated"


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"cells": ["NOPE"]}, "unknown cell"),
        ({"cells": []}, "empty"),
        ({"cells": [" "]}, "malformed"),
        ({"margin": -1.0}, ">= 0"),
        ({"margin": float("nan")}, "finite"),
        ({"cells": None}, "no guard"),
        ({"top": "NOPE"}, "top cell not found"),
    ],
)
def test_library_errors(tmp_path, kwargs, match):
    path = tmp_path / "a.gds"
    _write(path)
    with pytest.raises(KeepoutCheckError, match=match):
        _run(path, **kwargs)


def test_empty_layers_error(tmp_path):
    path = tmp_path / "a.gds"
    _write(path)
    with pytest.raises(KeepoutCheckError, match="empty"):
        run_keepout_check(str(path), [], 1.0, cells=["SPIRAL"])


def test_cell_without_placement_in_checked_top_errors(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, extra_tops=True)
    with pytest.raises(KeepoutCheckError, match="no placed geometry"):
        _run(path, top="OTHERTOP")


# --- CLI ---------------------------------------------------------------------


def _cli(path, *extra):
    return main(
        [
            "keepout-check",
            str(path),
            "--forbidden",
            "[[30,0]]",
            "--margin",
            "10",
            *extra,
        ]
    )


def test_cli_clean_exit_zero(tmp_path, capsys):
    path = tmp_path / "a.gds"
    _write(path)
    assert _cli(path, "--cells", "SPIRAL") == 0


def test_cli_violated_exit_three_json(tmp_path, capsys):
    path = tmp_path / "a.gds"
    _write(path, foreign=[(_FORBID, (105, 20, 130, 30))])
    code = _cli(path, "--cells", "SPIRAL", "--format", "json")
    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "violated"
    assert payload["violations"][0]["kind"] == "margin"


def test_cli_text_output(tmp_path, capsys):
    path = tmp_path / "a.gds"
    _write(path, foreign=[(_FORBID, (105, 20, 130, 30))])
    assert _cli(path, "--cells", "SPIRAL") == 3
    assert "status: violated" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--forbidden", "[]", "--margin", "1", "--cells", "SPIRAL"],
        ["--forbidden", "not json", "--margin", "1", "--cells", "SPIRAL"],
        ["--forbidden", "[[30,0]]", "--margin", "-1", "--cells", "SPIRAL"],
        ["--forbidden", "[[30,0]]", "--margin", "abc", "--cells", "SPIRAL"],
        ["--forbidden", "[[30,0]]", "--margin", "1"],
        ["--forbidden", "[[30,0]]", "--margin", "1", "--cells", "NOPE"],
        ["--forbidden", "[[30,0]]", "--margin", "1", "--region", "[1,2,3]"],
    ],
)
def test_cli_bad_inputs_exit_one(tmp_path, argv):
    path = tmp_path / "a.gds"
    _write(path)
    assert main(["keepout-check", str(path), *argv]) == 1


def test_cli_missing_file_exit_one():
    assert (
        main(
            [
                "keepout-check",
                "nope.gds",
                "--forbidden",
                "[[1,0]]",
                "--margin",
                "1",
                "--cells",
                "X",
            ]
        )
        == 1
    )

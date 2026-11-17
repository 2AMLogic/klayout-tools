"""Tests for `klt array-check` and the `run_array_check` library function.

Fixtures are generated programmatically with `klayout.db`, mirroring
`tests/test_keepout_check.py`. Purely geometric: no deck, PDK or extraction.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.array_check import ArrayCheckError, run_array_check
from klayout_tools.cli import main

_DBU = 0.001
_L = (68, 20)
_PITCH = 10.0


def _um(v: float) -> int:
    return round(v / _DBU)


def _write(path, *, n=4, step=_PITCH, shift=0.0, mirror_alt=True, nudge=None):
    """TOP holds ``n`` placements of BIT (10 um wide, asymmetric shape), placed
    at ``step`` spacing; odd ones mirrored about the y axis when ``mirror_alt``.
    ``nudge`` = extra box (um) on _L added to TOP."""
    layout = kdb.Layout()
    layout.dbu = _DBU
    bit = layout.create_cell("BIT")
    li = layout.layer(*_L)
    bit.shapes(li).insert(kdb.Box(_um(0), _um(0), _um(10), _um(2)))  # rail
    bit.shapes(li).insert(kdb.Box(_um(1), _um(2), _um(3), _um(8)))  # asymmetric
    top = layout.create_cell("TOP")
    for i in range(n):
        mirror = mirror_alt and i % 2 == 1
        x = i * step + (10.0 if mirror else 0.0) + (shift if i == n - 1 else 0.0)
        trans = kdb.Trans(kdb.Trans.M90 if mirror else kdb.Trans.R0, _um(x), 0)
        top.insert(kdb.CellInstArray(bit.cell_index(), trans))
    if nudge:
        top.shapes(li).insert(kdb.Box(*(_um(v) for v in nudge)))
    layout.write(str(path))


def test_abutment_clean(tmp_path):
    path = tmp_path / "a.gds"
    _write(path)
    r = run_array_check(str(path), cell="BIT", pitch_um=_PITCH, count=4)
    assert r["status"] == "clean" and r["violation_count"] == 0
    assert r["schema_version"] == 2


def test_abutment_gap_and_length(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, shift=1.0)  # last cell displaced by 1 um -> gap + length
    r = run_array_check(str(path), cell="BIT", pitch_um=_PITCH, count=4)
    kinds = {v["kind"] for v in r["violations"]}
    assert r["status"] == "violated"
    assert kinds == {"gap", "length"}
    (gap,) = [v for v in r["violations"] if v["kind"] == "gap"]
    assert gap["measured_um"] == 1.0


def test_abutment_overlap_and_count(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, step=9.0, mirror_alt=False)
    r = run_array_check(str(path), cell="BIT", pitch_um=_PITCH, count=5)
    kinds = {v["kind"] for v in r["violations"]}
    assert {"overlap", "count", "length"} <= kinds


def test_abutment_tolerance_and_single_placement(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, shift=0.004)
    assert (
        run_array_check(str(path), cell="BIT", pitch_um=_PITCH, count=4)["status"]
        == "violated"
    )
    assert (
        run_array_check(
            str(path), cell="BIT", pitch_um=_PITCH, count=4, tolerance_um=0.005
        )["status"]
        == "clean"
    )
    one = tmp_path / "one.gds"
    _write(one, n=1)
    assert (
        run_array_check(str(one), cell="BIT", pitch_um=_PITCH, count=1)["status"]
        == "clean"
    )


def _write_row(path, *, n=4, pitch=2.0, shift_last=0.0):
    """TOP holds ``n`` placements of UNIT (a 2.0 x 1.0 um filled rectangle)
    advancing by exactly ``pitch`` along x; the last one is moved
    +``shift_last`` um. Single lane, no grid."""
    layout = kdb.Layout()
    layout.dbu = _DBU
    unit = layout.create_cell("UNIT")
    li = layout.layer(*_L)
    unit.shapes(li).insert(kdb.Box(_um(0), _um(0), _um(2), _um(1)))
    top = layout.create_cell("TOP")
    for i in range(n):
        x = i * pitch + (shift_last if i == n - 1 else 0.0)
        top.insert(kdb.CellInstArray(unit.cell_index(), kdb.Trans(_um(x), 0)))
    layout.write(str(path))


def _write_grid(
    path,
    *,
    nx=4,
    ny=3,
    px=2.0,
    py=1.0,
    drop=None,
    shift_row=None,
    shift=0.0,
):
    """TOP holds an ``nx x ny`` grid of UNIT (2.0 x 1.0 um rectangle) at exact
    pitches ``px``/``py`` -- perfect abutment on both axes. ``drop`` = one
    ``(ix, iy)`` placement to omit; ``shift_row`` = an ``iy`` whose placements
    with ``ix >= 2`` are moved +``shift`` um along x."""
    layout = kdb.Layout()
    layout.dbu = _DBU
    unit = layout.create_cell("UNIT")
    li = layout.layer(*_L)
    unit.shapes(li).insert(kdb.Box(_um(0), _um(0), _um(2), _um(1)))
    top = layout.create_cell("TOP")
    for iy in range(ny):
        for ix in range(nx):
            if drop is not None and (ix, iy) == drop:
                continue
            x = ix * px + (shift if shift_row == iy and ix >= 2 else 0.0)
            top.insert(
                kdb.CellInstArray(unit.cell_index(), kdb.Trans(_um(x), _um(iy * py)))
            )
    layout.write(str(path))


def test_abutment_grid_is_clean_per_lane(tmp_path):
    """#5698 AC1/AC7: a 4x3 grid is three lanes of four along x (and four
    lanes of three along y) -- zero violations either way, and the record
    carries the lane count."""
    path = tmp_path / "grid.gds"
    _write_grid(path)
    r = run_array_check(str(path), cell="UNIT", pitch_um=2.0, count=4, direction="x")
    assert r["status"] == "clean" and r["violation_count"] == 0
    assert r["lanes"] == 3
    ry = run_array_check(str(path), cell="UNIT", pitch_um=1.0, count=3, direction="y")
    assert ry["status"] == "clean" and ry["violation_count"] == 0
    assert ry["lanes"] == 4


@pytest.mark.parametrize("pitch,expected_gap", [(1.8, -0.2), (2.0, 0.0), (2.5, 0.5)])
def test_abutment_derived_expected_gap(tmp_path, pitch, expected_gap):
    """#5698 AC3: single rows at a declared pitch below or above the cell
    width are clean; a deviating pair reports ``expected_um`` = pitch - width
    (signed), classified by the deviation direction."""
    path = tmp_path / "row.gds"
    _write_row(path, pitch=pitch)
    r = run_array_check(str(path), cell="UNIT", pitch_um=pitch, count=4)
    assert r["status"] == "clean" and r["violation_count"] == 0
    bad = tmp_path / "bad.gds"
    _write_row(bad, pitch=pitch, shift_last=1.0)
    rb = run_array_check(str(bad), cell="UNIT", pitch_um=pitch, count=4)
    seams = [v for v in rb["violations"] if v["kind"] in ("gap", "overlap")]
    assert len(seams) == 1
    assert seams[0]["expected_um"] == pytest.approx(expected_gap)
    assert seams[0]["measured_um"] == pytest.approx(expected_gap + 1.0)
    assert seams[0]["lane"] == 0


@pytest.mark.parametrize("pitch,expected_length", [(1.8, 7.4), (2.0, 8.0), (2.5, 9.5)])
def test_abutment_length_formula(tmp_path, pitch, expected_length):
    """#5698 AC4: the expected lane extent is (count - 1) * pitch + width, so
    the pitch == width case is unchanged and the others expect their measured
    value."""
    path = tmp_path / "row.gds"
    _write_row(path, pitch=pitch, shift_last=1.0)
    r = run_array_check(str(path), cell="UNIT", pitch_um=pitch, count=4)
    (length,) = [v for v in r["violations"] if v["kind"] == "length"]
    assert length["expected_um"] == pytest.approx(expected_length)
    assert length["measured_um"] == pytest.approx(expected_length + 1.0)
    assert length["lane"] == 0


def test_abutment_defects_still_report_per_lane(tmp_path):
    """#5698 AC6: a real defect reports in its lane only -- clean lanes stay
    clean, and a short lane reports ``count`` naming the lane."""
    bad = tmp_path / "shift.gds"
    _write_grid(bad, shift_row=1, shift=0.5)  # middle lane broken
    r = run_array_check(str(bad), cell="UNIT", pitch_um=2.0, count=4, direction="x")
    assert r["status"] == "violated"
    assert {(v["kind"], v["lane"]) for v in r["violations"]} == {
        ("gap", 1),
        ("length", 1),
    }
    short = tmp_path / "short.gds"
    _write_grid(short, drop=(3, 2))  # top lane missing its last placement
    rs = run_array_check(str(short), cell="UNIT", pitch_um=2.0, count=4, direction="x")
    counts = [v for v in rs["violations"] if v["kind"] == "count"]
    assert [v["lane"] for v in counts] == [2]
    assert "lane 2:" in counts[0]["description"]
    assert rs["lanes"] == 3


def test_abutment_unequal_lane_population(tmp_path):
    """#5698 edge: lanes of unequal population each report their own count."""
    layout = kdb.Layout()
    layout.dbu = _DBU
    unit = layout.create_cell("UNIT")
    li = layout.layer(*_L)
    unit.shapes(li).insert(kdb.Box(_um(0), _um(0), _um(2), _um(1)))
    top = layout.create_cell("TOP")
    for iy in range(4):
        n = 4 if iy < 3 else 2  # a fourth, half-populated row
        for ix in range(n):
            top.insert(
                kdb.CellInstArray(unit.cell_index(), kdb.Trans(_um(ix * 2), _um(iy)))
            )
    path = tmp_path / "uneven.gds"
    layout.write(str(path))
    r = run_array_check(str(path), cell="UNIT", pitch_um=2.0, count=4, direction="x")
    assert r["lanes"] == 4
    counts = [v for v in r["violations"] if v["kind"] == "count"]
    assert [v["lane"] for v in counts] == [3]
    assert {v["lane"] for v in r["violations"]} == {3}


def test_abutment_pitch_kind_only_for_differing_extents():
    """#5698 AC5: the ``pitch`` kind survives only for the case the gap test
    cannot express -- two placements whose extents along the axis differ --
    and is never emitted beside ``gap``/``overlap`` for one pair. Same-lane
    differing extents are unreachable with axis-aligned placements of a
    single unit cell (any 90-degree rotation swaps the perpendicular extent,
    hence the lane), so the classifier is exercised directly, in dbu."""
    from klayout_tools.array_check import _classify_pair

    # equal extents: the expected gap is pitch - width, the deviation classifies
    assert _classify_pair(0, 10, 10, 10, 10, 0) is None  # abuts at the pitch
    assert _classify_pair(1, 11, 10, 10, 10, 0) == ("gap", 1, 0)
    assert _classify_pair(-1, 9, 10, 10, 10, 0) == ("overlap", 1, 0)
    assert _classify_pair(1, 11, 9, 9, 10, 0) is None  # matches expected gap 1
    # differing extents (a rotated placement): the advance is checked instead
    assert _classify_pair(0, 5, 10, 5, 10, 0) == ("pitch", 5, 10)
    assert _classify_pair(0, 10, 10, 5, 10, 0) is None  # advance == pitch
    # ...and a heavily overlapping pair still reports pitch once, alone
    assert _classify_pair(-100, 5, 10, 5, 10, 0) == ("pitch", 5, 10)


def test_abutment_tolerance_applies_to_deviation(tmp_path):
    """#5698 AC3: --tolerance bounds the deviation from the expected seam
    gap, not the absolute gap -- a mirror seam's small defect is excusable
    without excusing a whole-cell error elsewhere."""
    path = tmp_path / "row.gds"
    _write_row(path, pitch=1.8, shift_last=0.004)
    assert (
        run_array_check(str(path), cell="UNIT", pitch_um=1.8, count=4)["status"]
        == "violated"
    )
    assert (
        run_array_check(
            str(path), cell="UNIT", pitch_um=1.8, count=4, tolerance_um=0.005
        )["status"]
        == "clean"
    )


def test_abutment_single_placement_lane(tmp_path):
    """#5698 edge: one placement is one lane of one -- no pairs, length
    against the placement's own width."""
    path = tmp_path / "one.gds"
    _write_row(path, n=1)
    r = run_array_check(str(path), cell="UNIT", pitch_um=2.0, count=1)
    assert r["status"] == "clean" and r["violation_count"] == 0
    assert r["lanes"] == 1


def test_mirror_symmetry_clean_and_violated(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, n=2)  # BIT + mirrored BIT: symmetric about x = 10
    kw = dict(mirror_axis="x", at_um=10.0)
    assert run_array_check(str(path), [_L], **kw)["status"] == "clean"
    bad = tmp_path / "b.gds"
    _write(bad, n=2, nudge=(12, 3, 14, 5))
    r = run_array_check(str(bad), [_L], **kw)
    assert r["status"] == "violated"
    # the stray shape and its (missing) mirror image are both residuals
    assert r["violation_count"] == 2
    assert {v["rule"] for v in r["violations"]} == {"array.symmetry_residual"}
    assert {v["layer"] for v in r["violations"]} == {"68/20"}
    spans = {(v["bbox"]["left"], v["bbox"]["right"]) for v in r["violations"]}
    assert spans == {(12.0, 14.0), (6.0, 8.0)}
    # tolerance bigger than half the feature width hides it
    r = run_array_check(str(bad), [_L], tolerance_um=1.5, **kw)
    assert r["status"] == "clean"


def test_mirror_y_axis(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, n=1)
    # BIT is not symmetric about y = 5 (rail only at the bottom)
    r = run_array_check(str(path), [_L], mirror_axis="y", at_um=5.0)
    assert r["status"] == "violated"


def test_translation_symmetry(tmp_path):
    path = tmp_path / "a.gds"
    _write(path, n=4, mirror_alt=False)
    r = run_array_check(str(path), [_L], translate_um=(10.0, 0.0))
    assert r["status"] == "clean"
    bad = tmp_path / "b.gds"
    _write(bad, n=4, mirror_alt=False, shift=0.5)
    assert (
        run_array_check(str(bad), [_L], translate_um=(10.0, 0.0))["status"]
        == "violated"
    )


def test_provenance_in_both_shapes_and_modes(tmp_path):
    """`provenance` (issue #5788) rides every record: clean and violated
    alike, in both the symmetry and the abutment request."""
    clean = tmp_path / "a.gds"
    _write(clean, n=4, mirror_alt=False)
    bad = tmp_path / "b.gds"
    _write(bad, n=2, nudge=(12, 3, 14, 5))
    gapped = tmp_path / "c.gds"
    _write(gapped, shift=1.0)
    records = [
        run_array_check(str(clean), cell="BIT", pitch_um=_PITCH, count=4),
        run_array_check(str(clean), [_L], translate_um=(10.0, 0.0)),
        run_array_check(str(gapped), cell="BIT", pitch_um=_PITCH, count=4),
        run_array_check(str(bad), [_L], mirror_axis="x", at_um=10.0),
    ]
    assert [r["status"] for r in records] == [
        "clean",
        "clean",
        "violated",
        "violated",
    ]
    for r in records:
        prov = r["provenance"]
        assert {
            "klt_version",
            "klayout_version",
            "pdk",
            "deck",
            "input",
        } <= set(prov)
        assert prov["pdk"] is None and prov["deck"] is None
        assert prov["input"]["role"] == "layout"
        assert prov["input"]["content_hash"].startswith("sha256:")


def test_file_anchored_repo_and_external(tmp_path, tmp_path_factory):
    """`file` is the `{path, scope}` envelope (issue #5788): repo-relative
    inside a repo, `{"path": null, "scope": "external"}` outside -- the
    producing host's absolute path never enters the record."""
    (tmp_path / ".git").mkdir()
    design = tmp_path / "design"
    design.mkdir()
    inside = design / "a.gds"
    _write(inside)
    r = run_array_check(str(inside), cell="BIT", pitch_um=_PITCH, count=4)
    assert r["file"] == {"path": "design/a.gds", "scope": "repo"}

    outside = tmp_path_factory.mktemp("external") / "a.gds"
    _write(outside)
    r = run_array_check(str(outside), cell="BIT", pitch_um=_PITCH, count=4)
    assert r["file"] == {"path": None, "scope": "external"}
    assert str(outside) not in json.dumps(r)


def test_record_is_deterministic(tmp_path):
    path = tmp_path / "a.gds"
    _write(path)
    kw = dict(cell="BIT", pitch_um=_PITCH, count=4)
    first = run_array_check(str(path), **kw)
    second = run_array_check(str(path), **kw)
    assert json.dumps(first) == json.dumps(second)


def test_cli_envelope_and_text_path(tmp_path, capsys):
    path = tmp_path / "a.gds"
    _write(path)
    argv = ["array-check", str(path), "--cell", "BIT", "--pitch", "10", "--count", "4"]
    assert main([*argv, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 2
    assert data["file"] == {"path": None, "scope": "external"}
    assert data["provenance"]["input"]["role"] == "layout"
    assert str(path) not in json.dumps(data)

    assert main(argv) == 0
    (file_line,) = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("file:")
    ]
    assert file_line == "file: <outside repo>"


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({}, "no check requested"),
        ({"layers": [], "mirror_axis": "x", "at_um": 1.0}, "layer set is empty"),
        ({"cell": "NOPE", "pitch_um": 10.0, "count": 2}, "unknown cell"),
        ({"cell": "BIT", "pitch_um": 0.0, "count": 2}, "pitch"),
        ({"cell": "BIT", "pitch_um": 10.0, "count": 0}, "count"),
        ({"cell": "BIT", "pitch_um": 10.0}, "both --pitch and --count"),
        ({"layers": [_L], "mirror_axis": "x"}, "requires --at"),
        (
            {"cell": "BIT", "pitch_um": 10.0, "count": 2, "tolerance_um": -1},
            "tolerance",
        ),
    ],
)
def test_bad_inputs(tmp_path, kwargs, match):
    path = tmp_path / "a.gds"
    _write(path)
    layers = kwargs.pop("layers", None)
    with pytest.raises(ArrayCheckError, match=match):
        run_array_check(str(path), layers, **kwargs)


def test_cli_json_and_exit_codes(tmp_path, capsys):
    path = tmp_path / "a.gds"
    _write(path)
    argv = ["array-check", str(path), "--cell", "BIT", "--pitch", "10", "--count", "4"]
    assert main([*argv, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"
    bad = tmp_path / "b.gds"
    _write(bad, shift=1.0)
    argv[1] = str(bad)
    assert main([*argv, "--format", "json"]) == 3
    capsys.readouterr()
    assert main(["array-check", str(bad)]) == 1
    assert main(argv) == 3

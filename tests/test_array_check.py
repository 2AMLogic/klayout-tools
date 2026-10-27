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
    assert r["schema_version"] == 1


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

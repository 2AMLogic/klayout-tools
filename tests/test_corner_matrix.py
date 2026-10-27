"""Tests for `klt corner-matrix check` (issue #4045, survey gap G3)."""

from __future__ import annotations

import json

import pytest

from klayout_tools import sim
from klayout_tools.cli import main
from klayout_tools.corner_matrix import (
    CornerMatrixError,
    check_corner_matrix,
    expected_tuples,
    request_tuples,
)

AXES = {
    "process": ["tt", "ss"],
    "supply_v": {"vdd": [1.62, 1.98]},
    "temperature_c": [-40, 125],
}


def _write(path, data):
    path.write_text(json.dumps(data))
    return str(path)


def _ids(axes, exclude=()):
    return [p.corner_id for p in sim._expand_corners(axes, list(exclude))]


def _report(ids, overrides=None):
    corners = [
        {
            "corner_id": cid,
            "status": "pass",
            "measurements": [{"name": "m", "value": 1.0, "status": "pass"}],
        }
        for cid in ids
    ]
    for i, patch in (overrides or {}).items():
        corners[i].update(patch)
    return {"corners": corners}


@pytest.fixture
def matrix(tmp_path):
    return _write(tmp_path / "matrix.json", AXES)


def test_expected_ids_match_sim_code_path_all_four_axes():
    axes = {**AXES, "params": {"w": [1, 2]}}
    assert expected_tuples(axes) == _ids(axes)
    assert len(expected_tuples(axes)) == 16
    assert "tt/1.620V/-40C/params:w=1" in expected_tuples(axes)


def test_request_ids_equal_matrix_ids():
    assert request_tuples({"corners": AXES}) == expected_tuples(AXES)


def test_exclude_removes_tuples():
    ids = expected_tuples(
        {**AXES, "exclude": [{"process": "ss", "temperature_c": -40}]}
    )
    assert len(ids) == 6 and "ss/1.620V/-40C" not in ids


def test_empty_matrix_is_single_default_tuple():
    assert expected_tuples({}) == ["default/novdd/27C"]


def test_unknown_matrix_key_rejected():
    with pytest.raises(CornerMatrixError, match="unknown matrix key"):
        expected_tuples({**AXES, "temps": [1]})


def test_request_clean(tmp_path, matrix):
    req = _write(tmp_path / "r.json", {"corners": AXES})
    out = check_corner_matrix(matrix, request_path=req)
    assert out["complete"] is True and out["matrix_size"] == 8


def test_request_missing_tuple(tmp_path, matrix):
    req = _write(
        tmp_path / "r.json",
        {
            "corners": AXES,
            "exclude": [
                {"process": "ss", "temperature_c": 125, "supply_v": {"vdd": 1.98}}
            ],
        },
    )
    out = check_corner_matrix(matrix, request_path=req)
    assert out["missing"] == ["ss/1.980V/125C"] and not out["complete"]


def test_request_extra_tuple(tmp_path, matrix):
    req = _write(
        tmp_path / "r.json", {"corners": {**AXES, "process": ["tt", "ss", "ff"]}}
    )
    out = check_corner_matrix(matrix, request_path=req)
    assert len(out["extra"]) == 4 and all(c.startswith("ff/") for c in out["extra"])


def test_request_duplicate_tuple(tmp_path, matrix):
    req = _write(
        tmp_path / "r.json", {"corners": {**AXES, "process": ["tt", "ss", "tt"]}}
    )
    out = check_corner_matrix(matrix, request_path=req)
    assert len(out["duplicate"]) == 4 and not out["complete"]


def test_report_clean(tmp_path, matrix):
    rep = _write(tmp_path / "rep.json", _report(expected_tuples(AXES)))
    out = check_corner_matrix(matrix, report_path=rep)
    assert out["complete"] is True and out["observed_count"] == 8


def test_report_missing_and_duplicate(tmp_path, matrix):
    ids = expected_tuples(AXES)
    out = check_corner_matrix(
        matrix, report_path=_write(tmp_path / "a.json", _report(ids[1:]))
    )
    assert out["missing"] == [ids[0]]
    out = check_corner_matrix(
        matrix, report_path=_write(tmp_path / "b.json", _report(ids + [ids[0]]))
    )
    assert out["duplicate"] == [ids[0]]


def test_report_lost_shard_and_nonfinite_and_fail(tmp_path, matrix):
    ids = expected_tuples(AXES)
    rep = _report(
        ids,
        {
            0: {
                "status": "error",
                "measurements": [{"name": "m", "value": None, "status": "error"}],
            },
            1: {
                "measurements": [{"name": "m", "value": float("nan"), "status": "pass"}]
            },
            2: {"status": "fail"},
        },
    )
    out = check_corner_matrix(matrix, report_path=_write(tmp_path / "r.json", rep))
    assert out["not_simulated"] == [ids[0]]
    assert out["non_finite"] == [ids[1]]
    assert out["out_of_bounds"] == [ids[2]]
    assert not out["complete"]


def test_requires_exactly_one_source(matrix):
    with pytest.raises(CornerMatrixError):
        check_corner_matrix(matrix)


def test_cli_exit_codes(tmp_path, matrix, capsys):
    ids = expected_tuples(AXES)
    good = _write(tmp_path / "g.json", _report(ids))
    bad = _write(tmp_path / "b.json", _report(ids[1:]))
    assert (
        main(["corner-matrix", "check", matrix, "--report", good, "--format", "json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["complete"] is True
    assert main(["corner-matrix", "check", matrix, "--report", bad]) == 3
    capsys.readouterr()
    assert (
        main(
            [
                "corner-matrix",
                "check",
                "/no/such.json",
                "--report",
                good,
                "--format",
                "json",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error"]["command"] == "corner-matrix"

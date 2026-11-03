"""Tests for ``klt ring-check --pdk`` (issue #4000): per-PDK seal-ring rules."""

from __future__ import annotations

import json
import os
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.ring_check import RingCheckError
from klayout_tools.seal_ring_check import run_seal_ring_check
from klayout_tools.seal_ring_rules import PDK_SEAL_RULES

_DBU = 0.001
_UM = 1000
_EDGESEAL = (39, 0)
_PASSIV = (9, 0)
_ACTIV = (1, 0)
_METAL1 = (8, 0)

_KIT_GDS = (
    Path(os.environ.get("IHP_SG13G2_PDK", "/home/ubuntu/share/pdk/ihp-sg13g2"))
    / "libs.tech/klayout/tech/drc/testing/testcases/unit/sealring.gds"
)


def _box(x1, y1, x2, y2) -> kdb.Region:
    return kdb.Region(kdb.Box(x1 * _UM, y1 * _UM, x2 * _UM, y2 * _UM))


def _ring(outer=(0, 0, 400, 400), inner=(20, 20, 380, 380), chamfer=0) -> kdb.Region:
    x1, y1, x2, y2 = (v * _UM for v in outer)
    c = chamfer * _UM
    if c:
        pts = [
            (x1 + c, y1),
            (x2 - c, y1),
            (x2, y1 + c),
            (x2, y2 - c),
            (x2 - c, y2),
            (x1 + c, y2),
            (x1, y2 - c),
            (x1, y1 + c),
        ]
        region = kdb.Region(kdb.Polygon([kdb.Point(*p) for p in pts]))
    else:
        region = kdb.Region(kdb.Box(x1, y1, x2, y2))
    return region - _box(*inner)


def _write(path, shapes: dict[tuple[int, int], kdb.Region]) -> str:
    layout = kdb.Layout()
    layout.dbu = _DBU
    top = layout.create_cell("CHIP_TOP")
    for (layer, dt), region in shapes.items():
        idx = layout.layer(layer, dt)
        for poly in region.each():
            top.shapes(idx).insert(poly)
    layout.write(str(path))
    return str(path)


def _clean_shapes() -> dict:
    return {_EDGESEAL: _ring(chamfer=30), _PASSIV: _box(-10, -10, 410, 410)}


def _status(report, rule_id):
    return next(r for r in report["rules"] if r["id"] == rule_id)["status"]


def test_clean_ring_passes_checked_rules(tmp_path):
    report = run_seal_ring_check(_write(tmp_path / "a.gds", _clean_shapes()), "sg13g2")

    assert report["status"] == "continuous"
    assert report["violations"] == []
    for rid in ("Seal.continuity", "Seal.n", "Seal.b", "Seal.k", "Seal.m"):
        assert _status(report, rid) == "pass"
    assert report["not_checked_rules"] == ["Seal.l"]


def test_gap_in_edgeseal_fails_continuity_with_location(tmp_path):
    shapes = _clean_shapes()
    shapes[_EDGESEAL] = shapes[_EDGESEAL] - _box(190, -5, 210, 25)
    report = run_seal_ring_check(_write(tmp_path / "a.gds", shapes), "sg13g2")

    assert report["status"] == "broken"
    hits = [v for v in report["violations"] if v["rule"] == "Seal.continuity"]
    assert hits and hits[0]["bbox"]["left"] >= 190 * _UM - 1
    assert hits[0]["bbox"]["right"] <= 210 * _UM + 1


def test_missing_edgeseal_layer_fails(tmp_path):
    shapes = {_PASSIV: _box(-10, -10, 410, 410)}
    report = run_seal_ring_check(_write(tmp_path / "a.gds", shapes), "sg13g2")

    assert _status(report, "Seal.continuity") == "fail"
    assert report["status"] == "broken"


def test_missing_passiv_fails_seal_n(tmp_path):
    shapes = {_EDGESEAL: _ring(chamfer=30)}
    report = run_seal_ring_check(_write(tmp_path / "a.gds", shapes), "sg13g2")

    assert _status(report, "Seal.n") == "fail"
    assert report["violations"][0]["rule"] in {"Seal.n", "Seal.continuity"}


def test_broken_passiv_ring_fails_seal_n(tmp_path):
    shapes = _clean_shapes()
    # Passiv ring (not a plate) with a slit through its outer band.
    shapes[_PASSIV] = (
        _box(-10, -10, 410, 410) - _box(-5, -5, 405, 405) - _box(195, -12, 205, -2)
    )
    report = run_seal_ring_check(_write(tmp_path / "a.gds", shapes), "sg13g2")

    assert _status(report, "Seal.n") == "fail"


def test_activ_too_close_to_seal_fails_seal_b(tmp_path):
    shapes = _clean_shapes()
    shapes[_METAL1] = _ring(chamfer=30)
    shapes[_ACTIV] = _box(190, 100, 210, 378)  # 2 um from the ring's inner edge
    report = run_seal_ring_check(_write(tmp_path / "a.gds", shapes), "sg13g2")

    assert _status(report, "Seal.b") == "fail"
    v = next(v for v in report["violations"] if v["rule"] == "Seal.b")
    assert v["bbox"]["top"] <= 382 * _UM


def test_short_corner_fails_seal_k(tmp_path):
    shapes = _clean_shapes()
    shapes[_EDGESEAL] = _ring(chamfer=10)  # diagonal ~14 um < 21 um
    report = run_seal_ring_check(_write(tmp_path / "a.gds", shapes), "sg13g2")

    assert _status(report, "Seal.k") == "fail"


def test_two_rings_fail_seal_m(tmp_path):
    shapes = _clean_shapes()
    shapes[_EDGESEAL] = shapes[_EDGESEAL] + _ring(
        (1000, 0, 1400, 400), (1020, 20, 1380, 380)
    )
    report = run_seal_ring_check(_write(tmp_path / "a.gds", shapes), "sg13g2")

    assert _status(report, "Seal.m") == "fail"


@pytest.mark.parametrize("pdk", ["gf180mcu", "sky130"])
def test_pdk_without_rules_is_not_checked_not_clean(tmp_path, pdk, capsys):
    path = _write(tmp_path / "a.gds", _clean_shapes())
    code = main(["ring-check", path, "--pdk", pdk, "--format", "json"])

    out = json.loads(capsys.readouterr().out)
    assert code == 4
    assert out["status"] == "not_checked"
    assert out["reason"]
    assert out["violations"] == []


def test_cli_pdk_broken_exit_three(tmp_path, capsys):
    shapes = {_PASSIV: _box(-10, -10, 410, 410)}
    code = main(
        [
            "ring-check",
            _write(tmp_path / "a.gds", shapes),
            "--pdk",
            "sg13g2",
            "--format",
            "json",
        ]
    )

    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "broken"


def test_cli_pdk_text_output(tmp_path, capsys):
    path = _write(tmp_path / "a.gds", _clean_shapes())

    assert main(["ring-check", path, "--pdk", "sg13g2"]) == 0
    assert "Seal.l: not_checked" in capsys.readouterr().out


def test_cli_pdk_and_layers_mutually_exclusive(tmp_path):
    path = _write(tmp_path / "a.gds", _clean_shapes())

    assert main(["ring-check", path, "--pdk", "sg13g2", "--layers", "[[39,0]]"]) == 1


def test_cli_requires_layers_or_pdk(tmp_path):
    assert main(["ring-check", _write(tmp_path / "a.gds", _clean_shapes())]) == 1


def test_unknown_pdk_raises(tmp_path):
    with pytest.raises(RingCheckError):
        run_seal_ring_check(_write(tmp_path / "a.gds", _clean_shapes()), "nope")


def test_layers_mode_output_unchanged(tmp_path):
    path = _write(tmp_path / "a.gds", {_EDGESEAL: _ring()})
    assert main(["ring-check", path, "--layers", "[[39,0]]"]) == 0


def test_every_rule_cites_source():
    for table in PDK_SEAL_RULES.values():
        for rule in table.rules:
            assert rule.source_file and rule.source_tag and rule.source_kind
        if not table.rules:
            assert table.not_checked_reason


@pytest.mark.skipif(not _KIT_GDS.exists(), reason="IHP sg13g2 kit not installed")
def test_kit_sealring_testcase_flags_known_defects():
    report = run_seal_ring_check(str(_KIT_GDS), "sg13g2")

    # The kit's unit testcase is a multi-case file (12 rings, some deliberately bad).
    assert _status(report, "Seal.n") == "fail"
    assert _status(report, "Seal.m") == "fail"
    assert report["status"] == "broken"

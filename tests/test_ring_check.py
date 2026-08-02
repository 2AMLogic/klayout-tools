"""Tests for `klt ring-check` and the `run_ring_check` library function.

Fixtures are generated programmatically with `klayout.db` inside the tests --
no dependency on an external corpus, mirroring `tests/test_drc.py`. The
`gf180mcu`-flavoured cases reproduce the issue's own "Measured, not assumed"
table (issue #303): a closed guard ring passes; the same ring with one
deliberate gap in a single segment fails and reports the gap's location.

The check is purely geometric -- every test runs with no extraction, no
netlist, and no PDK/deck resolution.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.ring_check import RingCheckError, run_ring_check

# gf180mcu layer numbers (see decks/gf180mcu.py): a guard ring is drawn as
# COMP (diffusion) + Metal1 tied together through contacts.
_COMP = (22, 0)
_METAL1 = (34, 0)

# A ring on a 0.005 um grid: outer 0..100 um, inner hole 20..80 um (a 20 um
# wide annulus), in database units.
_DBU = 0.005
_OUTER = kdb.Box(0, 0, 20000, 20000)
_INNER = kdb.Box(4000, 4000, 16000, 16000)
# A gap cut into the right segment: removes COMP+Metal1 for 80..100 um x,
# 40..60 um y -- opening the annulus without violating any width/space rule.
_GAP = kdb.Box(16000, 8000, 20000, 12000)


def _write(path, layer_shapes: dict[tuple[int, int], kdb.Region]) -> None:
    """Write a single-top-cell layout with the given per-layer regions."""
    layout = kdb.Layout()
    layout.dbu = _DBU
    top = layout.create_cell("GUARD_RING")
    for (layer, datatype), region in layer_shapes.items():
        index = layout.layer(layer, datatype)
        layout.set_info(index, kdb.LayerInfo(layer, datatype, f"{layer}/{datatype}"))
        for polygon in region.each():
            top.shapes(index).insert(polygon)
    layout.write(str(path))


def _ring_region(gap: kdb.Box | None = None) -> kdb.Region:
    region = kdb.Region(_OUTER) - kdb.Region(_INNER)
    if gap is not None:
        region -= kdb.Region(gap)
    return region


def _write_closed_ring(path) -> None:
    ring = _ring_region()
    _write(path, {_COMP: ring, _METAL1: ring})


def _write_broken_ring(path) -> None:
    ring = _ring_region(gap=_GAP)
    _write(path, {_COMP: ring, _METAL1: ring})


# --- Core annulus assertion (the issue's measured table) ---------------------


def test_closed_ring_is_continuous(tmp_path):
    path = tmp_path / "closed.gds"
    _write_closed_ring(path)

    report = run_ring_check(str(path), [_COMP, _METAL1])

    assert report["schema_version"] == 1
    assert report["file"] == str(path)
    assert report["layers"] == [[22, 0], [34, 0]]
    assert report["region_um"] is None
    assert report["dbu_um"] == _DBU
    assert report["status"] == "continuous"
    assert report["violation_count"] == 0
    assert report["violations"] == []


def test_broken_ring_is_caught_and_gap_located(tmp_path):
    path = tmp_path / "broken.gds"
    _write_broken_ring(path)

    report = run_ring_check(str(path), [_COMP, _METAL1])

    assert report["status"] == "broken"
    assert report["violation_count"] == 1
    (violation,) = report["violations"]
    assert violation["rule"] == "ring.continuity"
    assert violation["check"] == "ring_continuity"
    assert violation["kind"] == "gap"
    assert violation["cell"] == "GUARD_RING"
    assert violation["layer"] == "22/0+34/0"
    assert violation["polygon_count"] == 1
    assert violation["hole_count"] == 0
    # The reported break location is exactly the injected gap.
    assert violation["bbox"] == {
        "left": 16000,
        "bottom": 8000,
        "right": 20000,
        "top": 12000,
    }
    assert violation["polygon"] is not None


def test_check_is_purely_geometric_no_extraction(tmp_path):
    """A broken ring is caught from geometry alone -- no netlist/PDK/deck.

    The single layer set is passed explicitly; nothing resolves a PDK, runs an
    extraction, or reads a rule deck. This is the property no amount of LVS
    improvement can reach (issue #303).
    """
    path = tmp_path / "broken.gds"
    _write_broken_ring(path)

    # Only a layer set and a layout -- no deck/pdk/reference arguments exist on
    # this API at all.
    report = run_ring_check(str(path), [_COMP, _METAL1])

    assert report["status"] == "broken"
    assert "provenance" not in report  # no deck/PDK provenance: nothing was resolved


def test_single_layer_closed_ring(tmp_path):
    path = tmp_path / "closed_single.gds"
    _write(path, {_COMP: _ring_region()})

    report = run_ring_check(str(path), [_COMP])

    assert report["status"] == "continuous"


# --- The other "not one polygon, one hole" failure modes ---------------------


def test_solid_region_is_no_hole_not_a_gap(tmp_path):
    """A filled square (no hole at all) is a non-ring, distinct from a broken
    ring: it must not be reported with a spurious gap location."""
    path = tmp_path / "solid.gds"
    _write(path, {_COMP: kdb.Region(_OUTER)})

    report = run_ring_check(str(path), [_COMP])

    assert report["status"] == "broken"
    (violation,) = report["violations"]
    assert violation["kind"] == "no_hole"
    assert violation["hole_count"] == 0


def test_fragmented_ring_reports_each_piece(tmp_path):
    """Two gaps split the ring into two disjoint arcs -- one violation per
    fragment (a plain connectivity check would still see two groups here, but
    the failure mode is distinct from the single-gap case)."""
    path = tmp_path / "frag.gds"
    ring = _ring_region()
    ring -= kdb.Region(kdb.Box(16000, 8000, 20000, 12000))  # right gap
    ring -= kdb.Region(kdb.Box(0, 8000, 4000, 12000))  # left gap
    _write(path, {_COMP: ring})

    report = run_ring_check(str(path), [_COMP])

    assert report["status"] == "broken"
    assert report["violation_count"] == 2
    assert {v["kind"] for v in report["violations"]} == {"fragmented"}
    assert all(v["polygon_count"] == 2 for v in report["violations"])


def test_extra_holes_is_not_a_simple_annulus(tmp_path):
    """A plate with two separate holes punched through it is one polygon with
    two holes -- not a simple annulus."""
    path = tmp_path / "extra.gds"
    plate = kdb.Region(kdb.Box(0, 0, 30000, 10000))
    plate -= kdb.Region(kdb.Box(2000, 2000, 8000, 8000))
    plate -= kdb.Region(kdb.Box(22000, 2000, 28000, 8000))
    _write(path, {_COMP: plate})

    report = run_ring_check(str(path), [_COMP])

    assert report["status"] == "broken"
    (violation,) = report["violations"]
    assert violation["kind"] == "extra_holes"
    assert violation["hole_count"] == 2


def test_absent_layer_set_is_empty_ring(tmp_path):
    """No geometry on the requested layers -> there is no ring to verify."""
    path = tmp_path / "closed.gds"
    _write_closed_ring(path)

    # Metal2 (36/0) carries nothing in this fixture.
    report = run_ring_check(str(path), [(36, 0)])

    assert report["status"] == "broken"
    (violation,) = report["violations"]
    assert violation["kind"] == "empty"
    assert violation["polygon_count"] == 0
    assert violation["polygon"] is None


# --- Region clipping and multiple top cells ----------------------------------


def test_region_clip_isolates_one_ring(tmp_path):
    """Two rings side by side on the same layer: a clip window isolates one so
    each can be checked independently."""
    path = tmp_path / "two.gds"
    left = kdb.Region(kdb.Box(0, 0, 10000, 10000)) - kdb.Region(
        kdb.Box(2000, 2000, 8000, 8000)
    )
    right = kdb.Region(kdb.Box(20000, 0, 30000, 10000)) - kdb.Region(
        kdb.Box(22000, 2000, 28000, 8000)
    )
    right -= kdb.Region(kdb.Box(28000, 4000, 30000, 6000))  # break the right ring
    _write(path, {_COMP: left + right})

    # Whole stream: two disjoint shapes -> fragmented.
    whole = run_ring_check(str(path), [_COMP])
    assert whole["status"] == "broken"

    # Clip to the good (left) ring, in micrometres.
    left_only = run_ring_check(str(path), [_COMP], region_um=(0.0, 0.0, 50.0, 50.0))
    assert left_only["status"] == "continuous"
    assert left_only["region_um"] == [0.0, 0.0, 50.0, 50.0]

    # Clip to the broken (right) ring.
    right_only = run_ring_check(str(path), [_COMP], region_um=(100.0, 0.0, 150.0, 50.0))
    assert right_only["status"] == "broken"
    (violation,) = right_only["violations"]
    assert violation["kind"] == "gap"
    assert violation["bbox"] == {
        "left": 28000,
        "bottom": 4000,
        "right": 30000,
        "top": 6000,
    }


def test_top_cell_selection(tmp_path):
    """With more than one top cell, --top restricts the check to one."""
    layout = kdb.Layout()
    layout.dbu = _DBU
    index = layout.layer(*_COMP)
    good = layout.create_cell("GOOD")
    for polygon in _ring_region().each():
        good.shapes(index).insert(polygon)
    bad = layout.create_cell("BAD")
    for polygon in _ring_region(gap=_GAP).each():
        bad.shapes(index).insert(polygon)
    path = tmp_path / "multi.gds"
    layout.write(str(path))

    assert run_ring_check(str(path), [_COMP], top="GOOD")["status"] == "continuous"
    assert run_ring_check(str(path), [_COMP], top="BAD")["status"] == "broken"

    # Without --top both top cells are checked; the broken one fails the run.
    both = run_ring_check(str(path), [_COMP])
    assert both["status"] == "broken"
    assert {v["cell"] for v in both["violations"]} == {"BAD"}


def test_unknown_top_cell_raises(tmp_path):
    path = tmp_path / "closed.gds"
    _write_closed_ring(path)

    with pytest.raises(RingCheckError):
        run_ring_check(str(path), [_COMP], top="NOPE")


# --- Determinism and error handling ------------------------------------------


def test_deterministic_across_runs(tmp_path):
    path = tmp_path / "frag.gds"
    ring = _ring_region()
    ring -= kdb.Region(kdb.Box(16000, 8000, 20000, 12000))
    ring -= kdb.Region(kdb.Box(0, 8000, 4000, 12000))
    _write(path, {_COMP: ring})

    assert run_ring_check(str(path), [_COMP]) == run_ring_check(str(path), [_COMP])


def test_empty_layer_set_raises(tmp_path):
    path = tmp_path / "closed.gds"
    _write_closed_ring(path)

    with pytest.raises(RingCheckError):
        run_ring_check(str(path), [])


def test_missing_file_raises():
    with pytest.raises(RingCheckError):
        run_ring_check("/no/such/path/design.gds", [_COMP])


# --- CLI surface: exit codes and JSON envelope -------------------------------


def test_cli_continuous_exit_zero(tmp_path, capsys):
    path = tmp_path / "closed.gds"
    _write_closed_ring(path)

    exit_code = main(["ring-check", str(path), "--layers", "[[22,0],[34,0]]"])

    assert exit_code == 0
    assert "status: continuous" in capsys.readouterr().out


def test_cli_broken_exit_three_json(tmp_path, capsys):
    path = tmp_path / "broken.gds"
    _write_broken_ring(path)

    exit_code = main(
        ["ring-check", str(path), "--layers", "[[22,0],[34,0]]", "--format", "json"]
    )

    assert exit_code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["status"] == "broken"
    assert payload["violations"][0]["kind"] == "gap"


def test_cli_bad_layers_exit_one(tmp_path, capsys):
    path = tmp_path / "closed.gds"
    _write_closed_ring(path)

    exit_code = main(
        ["ring-check", str(path), "--layers", "not-json", "--format", "json"]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # JSON errors go to stderr, stdout stays empty
    error = json.loads(captured.err)
    assert error["error"]["command"] == "ring-check"


def test_cli_missing_file_exit_one():
    exit_code = main(["ring-check", "/no/such/file.gds", "--layers", "[[22,0]]"])

    assert exit_code == 1


def test_cli_bad_region_exit_one(tmp_path):
    path = tmp_path / "closed.gds"
    _write_closed_ring(path)

    exit_code = main(
        ["ring-check", str(path), "--layers", "[[22,0]]", "--region", "[0,0,0,10]"]
    )

    assert exit_code == 1

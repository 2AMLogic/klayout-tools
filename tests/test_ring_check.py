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


# --- --ignore-enclosed: enclosed same-layer geometry (issue #550) -----------

# A same-layer device drawn inside the ring's hole (well within _INNER, which
# spans 4000..16000 in both x and y).
_ENCLOSED_DEVICE = kdb.Box(6000, 6000, 14000, 14000)


def test_enclosed_same_layer_device_is_fragmented_by_default(tmp_path):
    """The reported bug (issue #550): a ring around a device on the same
    layer merges to two disjoint polygons -- the annulus plus the device --
    which trips the strict assertion even though the ring itself is an
    unbroken, continuous annulus. Without --ignore-enclosed this is still
    "broken" (backward compatible default)."""
    path = tmp_path / "enclosed.gds"
    ring = _ring_region()
    _write(path, {_COMP: ring + kdb.Region(_ENCLOSED_DEVICE)})

    report = run_ring_check(str(path), [_COMP])

    assert report["status"] == "broken"
    assert report["violation_count"] == 2
    assert {v["kind"] for v in report["violations"]} == {"fragmented"}


def test_ignore_enclosed_reports_continuous_for_enclosed_device(tmp_path):
    """The fix: with --ignore-enclosed, the same fixture reports "continuous"
    -- the annulus alone is asserted, and the device inside its hole is
    excluded from the pass/fail gate."""
    path = tmp_path / "enclosed.gds"
    ring = _ring_region()
    _write(path, {_COMP: ring + kdb.Region(_ENCLOSED_DEVICE)})

    report = run_ring_check(str(path), [_COMP], ignore_enclosed=True)

    assert report["status"] == "continuous"
    assert report["violation_count"] == 0
    assert report["violations"] == []


def test_ignore_enclosed_still_catches_a_genuine_break(tmp_path):
    """--ignore-enclosed excludes enclosed *content*, not real breaks in the
    ring's own perimeter: a ring with an actual gap, plus an unrelated
    same-layer device in its hole, still reports "broken"."""
    path = tmp_path / "broken_enclosed.gds"
    ring = _ring_region(gap=_GAP)
    _write(path, {_COMP: ring + kdb.Region(_ENCLOSED_DEVICE)})

    report = run_ring_check(str(path), [_COMP], ignore_enclosed=True)

    assert report["status"] == "broken"
    assert report["violation_count"] > 0


def test_ignore_enclosed_is_a_noop_when_nothing_is_enclosed(tmp_path):
    """No regression on the already-passing case: a plain closed ring with no
    enclosed content behaves identically with and without the flag."""
    path = tmp_path / "closed.gds"
    _write_closed_ring(path)

    without_flag = run_ring_check(str(path), [_COMP, _METAL1])
    with_flag = run_ring_check(str(path), [_COMP, _METAL1], ignore_enclosed=True)

    assert without_flag == with_flag
    assert with_flag["status"] == "continuous"


def test_cli_ignore_enclosed_flag_reports_continuous(tmp_path, capsys):
    path = tmp_path / "enclosed.gds"
    ring = _ring_region()
    _write(path, {_COMP: ring + kdb.Region(_ENCLOSED_DEVICE)})

    # Without the flag: broken (the bug).
    exit_code = main(["ring-check", str(path), "--layers", "[[22,0]]"])
    assert exit_code == 3
    assert "status: broken" in capsys.readouterr().out

    # With the flag: continuous (the fix).
    exit_code = main(
        ["ring-check", str(path), "--layers", "[[22,0]]", "--ignore-enclosed"]
    )
    assert exit_code == 0
    assert "status: continuous" in capsys.readouterr().out


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


# --- Cell inclusion scope (--cells, issue #2690) ------------------------------

_L = (8, 0)
# Ring of four bars around a 0..100 um box (dbu 0.005 -> 20000 units); each bar
# is its own cell instance. Bars: bottom/top span full width, left/right fit
# between them, so the union is one annulus with a single hole.
_W = 4000
_S = 20000


def _bar_cells(layout, index):
    horiz = layout.create_cell("HBAR")
    horiz.shapes(index).insert(kdb.Box(0, 0, _S, _W))
    vert = layout.create_cell("VBAR")
    vert.shapes(index).insert(kdb.Box(0, 0, _W, _S - 2 * _W))
    return horiz, vert


def _hierarchical(path, *, drop_right=False, core_bridge=True):
    """TOP -> RING (HBAR bottom, HBAR rotated top, VBAR left, VBAR right) + CORE.

    CORE holds same-layer routing that, together with the ring bars, forms an
    annulus even when the right bar is dropped: a frame bridging the gap.
    """
    layout = kdb.Layout()
    layout.dbu = _DBU
    index = layout.layer(*_L)
    horiz, vert = _bar_cells(layout, index)
    ring = layout.create_cell("RING")
    ring.insert(kdb.CellInstArray(horiz.cell_index(), kdb.Trans(0, 0)))
    # Top bar: reflected about x axis then moved (reflection + translation).
    ring.insert(kdb.CellInstArray(horiz.cell_index(), kdb.Trans(0, True, 0, _S)))
    ring.insert(kdb.CellInstArray(vert.cell_index(), kdb.Trans(0, _W)))
    if not drop_right:
        ring.insert(kdb.CellInstArray(vert.cell_index(), kdb.Trans(_S - _W, _W)))
    core = layout.create_cell("CORE")
    if core_bridge:
        # Fill the missing right bar position from the core side.
        core.shapes(index).insert(kdb.Box(_S - _W, _W, _S, _S - _W))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(ring.cell_index(), kdb.Trans(1000, 1000)))
    top.insert(kdb.CellInstArray(core.cell_index(), kdb.Trans(1000, 1000)))
    top.shapes(index).insert(kdb.Box(-500, -500, -400, -400))  # TOP-owned stray
    layout.write(str(path))


def test_unscoped_check_is_not_discriminating_on_shared_layer(tmp_path):
    intact, broken = tmp_path / "i.gds", tmp_path / "b.gds"
    _hierarchical(intact)
    _hierarchical(broken, drop_right=True)

    # The stray TOP shape fragments both unscoped runs equally; with
    # ignore_enclosed it is still outside the hole.  Clip it away instead.
    clip = (0.0, 0.0, 120.0, 120.0)
    a = run_ring_check(str(intact), [_L], region_um=clip, ignore_enclosed=True)
    b = run_ring_check(str(broken), [_L], region_um=clip, ignore_enclosed=True)
    assert a["status"] == b["status"] == "continuous"
    assert a["cells"] is None


def test_scoped_ring_discriminates(tmp_path):
    intact, broken = tmp_path / "i.gds", tmp_path / "b.gds"
    _hierarchical(intact)
    _hierarchical(broken, drop_right=True)

    good = run_ring_check(str(intact), [_L], cells=["RING"], ignore_enclosed=True)
    bad = run_ring_check(str(broken), [_L], cells=["RING"], ignore_enclosed=True)
    assert good["status"] == "continuous"
    assert good["cells"] == ["RING"]
    assert bad["status"] == "broken"
    # Violation box is in TOP's coordinates (RING placed at 1000,1000): the
    # C-shaped remainder spans 1000..(1000+_S) in x and y.
    assert bad["violations"][0]["bbox"] == {
        "left": 1000,
        "bottom": 1000,
        "right": 1000 + _S,
        "top": 1000 + _S,
    }


def test_scope_with_leaf_cells_gathers_repeated_reflected_instances(tmp_path):
    path = tmp_path / "i.gds"
    _hierarchical(path)
    r = run_ring_check(str(path), [_L], cells=["HBAR", "VBAR"], ignore_enclosed=True)
    assert r["status"] == "continuous"
    # Dropping one leaf cell from the scope opens the ring.
    r = run_ring_check(str(path), [_L], cells=["HBAR"])
    assert r["status"] == "broken"


def test_overlapping_ancestor_and_descendant_do_not_duplicate(tmp_path):
    path = tmp_path / "i.gds"
    _hierarchical(path)
    only = run_ring_check(str(path), [_L], cells=["RING"])
    both = run_ring_check(str(path), [_L], cells=["RING", "HBAR"])
    assert only["status"] == both["status"] == "continuous"
    assert only["violations"] == both["violations"] == []


def test_selected_ancestor_excludes_unselected_ancestor_shapes(tmp_path):
    path = tmp_path / "i.gds"
    _hierarchical(path)
    # TOP's own stray shape must not appear: selecting RING gives exactly one
    # polygon (no 'fragmented'); selecting TOP includes it.
    assert run_ring_check(str(path), [_L], cells=["RING"])["status"] == "continuous"
    r = run_ring_check(str(path), [_L], cells=["TOP"])
    assert r["status"] == "broken"
    assert {v["kind"] for v in r["violations"]} == {"fragmented"}


def test_array_occurrences_are_included(tmp_path):
    layout = kdb.Layout()
    layout.dbu = _DBU
    index = layout.layer(*_L)
    bar = layout.create_cell("BAR")
    bar.shapes(index).insert(kdb.Box(0, 0, 4000, 4000))
    top = layout.create_cell("TOP")
    # 4x4 array of touching squares, minus nothing: solid plate -> no_hole.
    top.insert(
        kdb.CellInstArray(
            bar.cell_index(),
            kdb.Trans(0, 0),
            kdb.Vector(4000, 0),
            kdb.Vector(0, 4000),
            4,
            4,
        )
    )
    path = tmp_path / "arr.gds"
    layout.write(str(path))
    r = run_ring_check(str(path), [_L], cells=["BAR"])
    assert r["violations"][0]["kind"] == "no_hole"
    assert r["violations"][0]["bbox"] == {
        "left": 0,
        "bottom": 0,
        "right": 16000,
        "top": 16000,
    }


def test_rotated_instance_transform(tmp_path):
    layout = kdb.Layout()
    layout.dbu = _DBU
    index = layout.layer(*_L)
    bar = layout.create_cell("BAR")
    bar.shapes(index).insert(kdb.Box(0, 0, 1000, 5000))
    mid = layout.create_cell("MID")
    mid.insert(kdb.CellInstArray(bar.cell_index(), kdb.Trans(1, False, 0, 0)))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(mid.cell_index(), kdb.Trans(100, 200)))
    path = tmp_path / "rot.gds"
    layout.write(str(path))
    r = run_ring_check(str(path), [_L], cells=["BAR"])
    # R90 maps (x,y)->(-y,x): box -5000..0 x 0..1000, then +(100,200).
    assert r["violations"][0]["bbox"] == {
        "left": -4900,
        "bottom": 200,
        "right": 100,
        "top": 1200,
    }


def test_multiple_roots_and_selector_absent_from_one_root(tmp_path):
    layout = kdb.Layout()
    layout.dbu = _DBU
    index = layout.layer(*_L)
    ring = layout.create_cell("RINGC")
    for polygon in _ring_region().each():
        ring.shapes(index).insert(polygon)
    other = layout.create_cell("OTHERC")
    other.shapes(index).insert(kdb.Box(0, 0, 100, 100))
    a = layout.create_cell("A")
    a.insert(kdb.CellInstArray(ring.cell_index(), kdb.Trans()))
    b = layout.create_cell("B")
    b.insert(kdb.CellInstArray(other.cell_index(), kdb.Trans()))
    path = tmp_path / "multi.gds"
    layout.write(str(path))

    r = run_ring_check(str(path), [_L], cells=["RINGC"])
    assert r["status"] == "broken"
    assert {v["cell"] for v in r["violations"]} == {"B"}
    assert r["violations"][0]["kind"] == "empty"  # never a vacuous pass


def test_scope_with_clip_and_absent_layer(tmp_path):
    path = tmp_path / "i.gds"
    _hierarchical(path)
    clipped = run_ring_check(
        str(path), [_L], cells=["RING"], region_um=(0.0, 0.0, 1.0, 1.0)
    )
    assert clipped["violations"][0]["kind"] == "empty"
    absent = run_ring_check(str(path), [(99, 0)], cells=["RING"])
    assert absent["violations"][0]["kind"] == "empty"
    inside = run_ring_check(
        str(path), [_L], cells=["RING"], region_um=(0.0, 0.0, 120.0, 120.0)
    )
    assert inside["status"] == "continuous"


@pytest.mark.parametrize("bad", [[], [""], ["  "], [3], "RING", ["RING", ""]])
def test_malformed_cells_raise(tmp_path, bad):
    path = tmp_path / "i.gds"
    _hierarchical(path)
    with pytest.raises(RingCheckError):
        run_ring_check(str(path), [_L], cells=bad)


def test_unknown_cell_raises(tmp_path):
    path = tmp_path / "i.gds"
    _hierarchical(path)
    with pytest.raises(RingCheckError, match="NOPE"):
        run_ring_check(str(path), [_L], cells=["RING", "NOPE"])


def test_cells_echo_dedups_and_preserves_order(tmp_path):
    path = tmp_path / "i.gds"
    _hierarchical(path)
    r = run_ring_check(str(path), [_L], cells=["VBAR", "HBAR", "VBAR"])
    assert r["cells"] == ["VBAR", "HBAR"]


def test_cli_cells_exit_codes_and_json_echo(tmp_path, capsys):
    intact, broken = tmp_path / "i.gds", tmp_path / "b.gds"
    _hierarchical(intact)
    _hierarchical(broken, drop_right=True)
    base = ["--layers", "[[8,0]]", "--ignore-enclosed", "--format", "json"]

    assert main(["ring-check", str(intact), *base, "--cells", "RING"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cells"] == ["RING"]

    assert main(["ring-check", str(broken), *base, "--cells", "RING"]) == 3
    capsys.readouterr()

    assert main(["ring-check", str(intact), *base, "--cells", "NOPE"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["command"] == "ring-check"
    assert main(["ring-check", str(intact), *base, "--cells", ""]) == 1
    capsys.readouterr()

    # Text output shows the scope; omitted scope echoes null in JSON.
    assert (
        main(
            [
                "ring-check",
                str(intact),
                "--layers",
                "[[8,0]]",
                "--cells",
                "HBAR",
                "--cells",
                "VBAR",
            ]
        )
        == 0
    )
    assert "cells: HBAR, VBAR" in capsys.readouterr().out
    main(
        [
            "ring-check",
            str(intact),
            "--layers",
            "[[8,0]]",
            "--format",
            "json",
            "--region",
            "[0,0,120,120]",
        ]
    )
    assert json.loads(capsys.readouterr().out)["cells"] is None

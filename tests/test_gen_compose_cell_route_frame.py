"""Composed-frame regressions for routes between ``blocks[].cell`` ports
(issue #6543).

The report behind #6543 said a no-waypoint two-pin leg between two
``blocks[].cell`` ports written at non-zero ``origins_um`` came back
``status: "routed"`` while the GDS held its via-drop landing pads at the
ports' *cell-local* coordinates and no backbone at all. Waypointed legs drew
correctly. The reporter's own cells/request were never supplied, and a
reduced fixture (plus a ~40-variant sweep over DBU, port layer, direction,
local position, origin grid alignment, ``blocks[].orientation``, bbox-target
placement and nested cell hierarchy) did **not** reproduce it -- see the PR
for #6543 for the evidence.

These tests therefore pin the behaviour that *was* measured, so a real frame
regression in the shared route pipeline (``route_two_pin`` ->
``_resolve_via_drops`` -> ``_leg_conflict`` -> ``_draw_routed_entry``) fails
here rather than only in a downstream flow:

* the automatic (no ``waypoints_um``) leg and the equivalent waypointed leg
  both land their via cuts and pads on the *translated* pins, with one
  connected Metal2 polygon (backbone **and** both pads) between them;
* no route-only shape is drawn at the untranslated local port coordinates;
* the route-vs-route conflict check compares the same composed-frame
  footprints that are written: two nets that are disjoint where drawn both
  route (even though their footprints would coincide if compared in the
  blocks' local frame), and two nets that really do overlap are still
  rejected (negative control -- conflict detection is not disabled).

Everything is asserted on the **read-back GDS** (``klayout.db.Region``),
counting ``Path`` shapes as metal -- a backbone is written as a ``PATH`` and
is invisible to a "boxes only" scan, which is the likeliest way to read
"no backbone" out of a correctly written file. The blocks live in
sub-cells, so ``top.shapes(...)`` holds *only* the route geometry this verb
drew; that is what separates "newly drawn routing" from the block's own
metal when asserting absence.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.gen_compose import compose

# gf180mcuD layer numbers (the curated deck compose() resolves `metal2` on).
M1 = (34, 0)
V1 = (35, 0)
M2 = (36, 0)

# Half-side of the Metal1 pin square each fixture cell draws at its port.
_PIN_HALF_UM = 0.21


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Scrub PDK env vars and empty the host search space -- see test_pdk.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    """A fabricated open_pdks-layout gf180mcuD install (just the probe dir)."""
    root = tmp_path / "pdk_install"
    (root / "gf180mcuD" / "libs.tech").mkdir(parents=True)
    return root


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def _write_pin_cell(path, cell_name, pins, dbu=0.001):
    """Write a tiny cell with one Metal1 pin square at each cell-local
    ``(x, y)`` in ``pins``."""
    layout = kdb.Layout()
    layout.dbu = dbu
    cell = layout.create_cell(cell_name)
    layer = layout.layer(*M1)
    for x, y in pins:
        cell.shapes(layer).insert(
            kdb.DBox(
                x - _PIN_HALF_UM, y - _PIN_HALF_UM, x + _PIN_HALF_UM, y + _PIN_HALF_UM
            )
        )
    layout.write(str(path))


def _port(name, x, y, direction=90):
    return {
        "name": name,
        "x_um": x,
        "y_um": y,
        "width_um": 0.42,
        "direction_deg": direction,
        "layer": {"layer": M1[0], "datatype": M1[1]},
    }


def _compose_cells(tmp_path, pdk_root, *, blocks, nets, dbu=0.001):
    """Compose ``blocks`` (``{id: {"origin": (x, y), "pins": [(x, y, name,
    direction), ...]}}``) wired by ``nets`` (``[{"net", "pins": [(block,
    port), ...], "waypoints_um"?}, ...]``) with the shared routing config
    #6543 reported. Returns ``(report, layout, top)`` -- the response and the
    composed GDS read back from disk."""
    request_blocks = []
    origins = {}
    for block_id, spec in blocks.items():
        gds = tmp_path / f"{block_id}.gds"
        _write_pin_cell(gds, "PINS", [(x, y) for x, y, *_ in spec["pins"]], dbu=dbu)
        request_blocks.append(
            {
                "id": block_id,
                "cell": {
                    "gds_path": str(gds),
                    "cell_name": "PINS",
                    "ports": [_port(n, x, y, d) for x, y, n, d in spec["pins"]],
                },
            }
        )
        origins[block_id] = {"x": spec["origin"][0], "y": spec["origin"][1]}

    connectivity = []
    for net in nets:
        entry = {
            "net": net["net"],
            "pins": [{"block": b, "port": p} for b, p in net["pins"]],
            "layer_role": "metal2",
            "width_um": 0.7,
        }
        if net.get("waypoints_um"):
            entry["waypoints_um"] = net["waypoints_um"]
        connectivity.append(entry)

    output = tmp_path / "composed.gds"
    request = {
        "pdk": {"variant": "gf180mcuD", "root": str(pdk_root)},
        "blocks": request_blocks,
        "placement": {
            "strategy": "explicit",
            "order": list(blocks),
            "origins_um": origins,
        },
        "connectivity": connectivity,
        "routing": {"layer_role": "metal2", "width_um": 0.7},
        "options": {"cell_name": "TOP", "output": str(output)},
    }
    report = compose(request)
    layout = kdb.Layout()
    layout.read(str(output))
    return report, layout, layout.cell("TOP")


# --------------------------------------------------------------------------- #
# Read-back helpers
# --------------------------------------------------------------------------- #


def _route_only(layout, top, layer):
    """Merged region of the shapes drawn *directly in the top cell* on
    ``layer`` -- i.e. only the route geometry (backbone ``Path`` s, via cuts,
    landing pads); every block's own metal lives in its sub-cell. Paths are
    converted to polygons, so a backbone counts as metal."""
    index = layout.find_layer(*layer)
    if index is None:
        return kdb.Region()
    return kdb.Region(top.shapes(index)).merged()


def _block_geometry(layout, top, layer):
    """The blocks' own placed metal on ``layer`` (sub-cell contents only,
    transformed into the composed frame)."""
    index = layout.find_layer(*layer)
    region = kdb.Region()
    if index is None:
        return region
    for inst in top.each_inst():
        sub = kdb.Region(layout.cell(inst.cell_index).begin_shapes_rec(index))
        region += sub.transformed(inst.trans)
    return region.merged()


def _touches(layout, region, point_um):
    """True if ``region`` covers ``point_um`` (a ~2-dbu probe square)."""
    cx = round(point_um[0] / layout.dbu)
    cy = round(point_um[1] / layout.dbu)
    return not (region & kdb.Region(kdb.Box(cx - 1, cy - 1, cx + 1, cy + 1))).is_empty()


def _via_cut_centers_dbu(layout, top):
    """Sorted dbu centres of every via cut the router drew in the top cell."""
    index = layout.find_layer(*V1)
    if index is None:
        return []
    return sorted(
        (shape.bbox().center().x, shape.bbox().center().y)
        for shape in top.shapes(index).each()
    )


def _expected_dbu(layout, points_um):
    return sorted((round(x / layout.dbu), round(y / layout.dbu)) for x, y in points_um)


def _assert_centers_match(actual, expected, tol_dbu=1):
    assert len(actual) == len(expected)
    for (ax, ay), (ex, ey) in zip(actual, expected, strict=True):
        assert abs(ax - ex) <= tol_dbu and abs(ay - ey) <= tol_dbu, (actual, expected)


# --------------------------------------------------------------------------- #
# Two-pin leg between two blocks[].cell ports: auto vs waypointed, composed frame
# --------------------------------------------------------------------------- #

# (origin_a, origin_b, cell-local pin, gds dbu). Pins of both blocks share one
# latitude, so the default backbone is one lifted horizontal between them --
# the shape #6543 reported. The zero-origin control keeps block `a` untranslated.
_FRAME_CASES = [
    pytest.param((10.0, 20.0), (30.0, 20.0), (1.0, 1.0), 0.001, id="origin-10-20"),
    pytest.param(
        (-12.5, 7.25), (41.75, 7.25), (3.3, 2.2), 0.001, id="negative-fractional"
    ),
    pytest.param((10.0, 20.0), (30.0, 20.0), (1.0, 1.0), 0.0005, id="half-nm-dbu"),
    pytest.param((0.0, 0.0), (25.0, 0.0), (1.0, 1.0), 0.001, id="zero-origin-control"),
]


def _two_pin_case(tmp_path, pdk_root, origin_a, origin_b, local, dbu, waypointed):
    pa = (origin_a[0] + local[0], origin_a[1] + local[1])
    pb = (origin_b[0] + local[0], origin_b[1] + local[1])
    net = {"net": "N", "pins": [("a", "P"), ("b", "P")]}
    if waypointed:
        # Same lifted latitude the automatic backbone picks (pin + 0.7um).
        net["waypoints_um"] = [[pa[0], pa[1] + 0.7], [pb[0], pb[1] + 0.7]]
    pin = (local[0], local[1], "P", 90)
    report, layout, top = _compose_cells(
        tmp_path,
        pdk_root,
        blocks={
            "a": {"origin": origin_a, "pins": [pin]},
            "b": {"origin": origin_b, "pins": [pin]},
        },
        nets=[net],
        dbu=dbu,
    )
    return pa, pb, report, layout, top


@pytest.mark.parametrize("waypointed", [False, True], ids=["auto", "waypointed"])
@pytest.mark.parametrize(("origin_a", "origin_b", "local", "dbu"), _FRAME_CASES)
def test_cell_port_leg_is_drawn_in_the_composed_frame(
    tmp_path, pdk_root, origin_a, origin_b, local, dbu, waypointed
):
    """Backbone, via cuts and pads sit on the *translated* pins, in one
    connected Metal2 polygon, with nothing route-drawn at the local coords."""
    pa, pb, report, layout, top = _two_pin_case(
        tmp_path, pdk_root, origin_a, origin_b, local, dbu, waypointed
    )

    (net,) = report["nets"]
    assert net["status"] == "routed"
    assert net["routed"] is True
    assert net["landed_on_block"] is True
    assert net["route_length_um"] >= pb[0] - pa[0]

    # One connected Metal2 polygon (Path backbone + both landing pads) that
    # reaches both translated pins and spans the gap between them.
    metal2 = _route_only(layout, top, M2)
    assert metal2.count() == 1
    assert _touches(layout, metal2, pa)
    assert _touches(layout, metal2, pb)
    assert metal2.bbox().width() * layout.dbu >= (pb[0] - pa[0])

    # Via cuts land exactly on the translated pins, and the Metal1 pads meet
    # the blocks' own *placed* Metal1 there.
    _assert_centers_match(
        _via_cut_centers_dbu(layout, top), _expected_dbu(layout, [pa, pb])
    )
    route_m1 = _route_only(layout, top, M1)
    block_m1 = _block_geometry(layout, top, M1)
    for pin in (pa, pb):
        assert _touches(layout, route_m1, pin)
        assert _touches(layout, block_m1, pin)

    # Every route-drawn shape (any layer) stays inside the composed frame of
    # the two pins -- nowhere near the layout origin / untranslated local frame.
    margin = round(2.0 / layout.dbu)
    frame = kdb.Box(
        round(min(pa[0], pb[0]) / layout.dbu) - margin,
        round(min(pa[1], pb[1]) / layout.dbu) - margin,
        round(max(pa[0], pb[0]) / layout.dbu) + margin,
        round(max(pa[1], pb[1]) / layout.dbu) + margin,
    )
    for layer in (M1, V1, M2):
        drawn = _route_only(layout, top, layer)
        if not drawn.is_empty():
            assert frame.contains(drawn.bbox().p1)
            assert frame.contains(drawn.bbox().p2)

    # Absence at the *untranslated* local coordinates (skipped where the
    # local point coincides with a genuine pin, i.e. a zero-origin block).
    for offset in (origin_a, origin_b):
        local_point = local
        if offset == (0.0, 0.0) or local_point in (pa, pb):
            continue
        for layer in (M1, V1, M2):
            assert not _touches(layout, _route_only(layout, top, layer), local_point)


@pytest.mark.parametrize(("origin_a", "origin_b", "local", "dbu"), _FRAME_CASES)
def test_auto_and_waypointed_legs_share_endpoints(
    tmp_path, pdk_root, origin_a, origin_b, local, dbu
):
    """The automatic and the explicit path need not share intermediate
    vertices, but they must land the same via cuts on the same pins and join
    them with one connected polygon."""
    summaries = []
    for waypointed, sub in ((False, "auto"), (True, "wp")):
        out_dir = tmp_path / sub
        out_dir.mkdir()
        pa, pb, _report, layout, top = _two_pin_case(
            out_dir, pdk_root, origin_a, origin_b, local, dbu, waypointed
        )
        metal2 = _route_only(layout, top, M2)
        summaries.append(
            (
                _via_cut_centers_dbu(layout, top),
                metal2.count(),
                _touches(layout, metal2, pa) and _touches(layout, metal2, pb),
            )
        )
    assert summaries[0] == summaries[1]
    assert summaries[0][1:] == (1, True)


# --------------------------------------------------------------------------- #
# Adjacent nets: conflict detection uses the footprints that are written
# --------------------------------------------------------------------------- #


def test_disjoint_adjacent_nets_both_route_in_the_composed_frame(tmp_path, pdk_root):
    """Two adjacent nets between the same two blocks whose legal routes are
    deliberately disjoint (one leaves upward and runs above the blocks, the
    other leaves downward and runs below) both route and are drawn at the
    translated pins."""
    cell_pins = [(1.0, 1.0, "UP", 90), (3.0, 1.0, "DN", 270)]
    report, layout, top = _compose_cells(
        tmp_path,
        pdk_root,
        blocks={
            "a": {"origin": (10.0, 20.0), "pins": cell_pins},
            "b": {"origin": (30.0, 20.0), "pins": cell_pins},
        },
        nets=[
            {"net": "NUP", "pins": [("a", "UP"), ("b", "UP")]},
            {"net": "NDN", "pins": [("a", "DN"), ("b", "DN")]},
        ],
    )

    assert [(n["net"], n["status"]) for n in report["nets"]] == [
        ("NUP", "routed"),
        ("NDN", "routed"),
    ]
    assert all(n["landed_on_block"] is True for n in report["nets"])

    metal2 = _route_only(layout, top, M2)
    polygons = list(metal2.each())
    assert len(polygons) == 2  # two electrically separate nets

    pins = {
        "NUP": [(11.0, 21.0), (31.0, 21.0)],
        "NDN": [(13.0, 21.0), (33.0, 21.0)],
    }
    for net_pins in pins.values():
        owners = [
            poly
            for poly in polygons
            if all(_touches(layout, kdb.Region(poly), pin) for pin in net_pins)
        ]
        assert len(owners) == 1, net_pins
    # The two nets share no metal: their route polygons are spaced apart.
    assert (kdb.Region(polygons[0]) & kdb.Region(polygons[1])).is_empty()

    # Their backbones run on opposite sides of the placed blocks (above the
    # Metal1 pin row for NUP, below it for NDN) -- composed-frame latitudes.
    centers_y = sorted(poly.bbox().center().y * layout.dbu for poly in polygons)
    assert centers_y[0] < 21.0 < centers_y[1]

    _assert_centers_match(
        _via_cut_centers_dbu(layout, top),
        _expected_dbu(layout, [p for pins_ in pins.values() for p in pins_]),
    )


def test_conflict_check_uses_composed_frame_footprints(tmp_path, pdk_root):
    """Two nets on *different* block pairs have identical footprints in the
    blocks' own (local) frame but are disjoint where they are drawn. Both
    must route: a conflict check comparing untranslated footprints would
    wrongly reject the second one."""
    pin = (1.0, 1.0, "P", 90)
    report, layout, top = _compose_cells(
        tmp_path,
        pdk_root,
        blocks={
            "a": {"origin": (10.0, 20.0), "pins": [pin]},
            "b": {"origin": (30.0, 20.0), "pins": [pin]},
            "c": {"origin": (10.0, 60.0), "pins": [pin]},
            "d": {"origin": (30.0, 60.0), "pins": [pin]},
        },
        nets=[
            {"net": "N1", "pins": [("a", "P"), ("b", "P")]},
            {"net": "N2", "pins": [("c", "P"), ("d", "P")]},
        ],
    )

    assert [(n["net"], n["status"]) for n in report["nets"]] == [
        ("N1", "routed"),
        ("N2", "routed"),
    ]
    metal2 = _route_only(layout, top, M2)
    assert metal2.count() == 2
    for pins_ in ([(11.0, 21.0), (31.0, 21.0)], [(11.0, 61.0), (31.0, 61.0)]):
        assert all(_touches(layout, metal2, pin_) for pin_ in pins_)


def test_overlapping_nets_are_still_rejected_and_not_drawn(tmp_path, pdk_root):
    """Negative control: two same-direction nets between the same two blocks
    share one channel, so their natural routes genuinely overlap in the
    composed frame. The second is rejected (conflict detection is live) and
    leaves no geometry behind; the first is untouched."""
    cell_pins = [(1.0, 1.0, "P0", 90), (3.0, 1.0, "P1", 90)]
    report, layout, top = _compose_cells(
        tmp_path,
        pdk_root,
        blocks={
            "a": {"origin": (10.0, 20.0), "pins": cell_pins},
            "b": {"origin": (30.0, 20.0), "pins": cell_pins},
        },
        nets=[
            {"net": "N0", "pins": [("a", "P0"), ("b", "P0")]},
            {"net": "N1", "pins": [("a", "P1"), ("b", "P1")]},
        ],
    )

    n0, n1 = report["nets"]
    assert (n0["net"], n0["status"]) == ("N0", "routed")
    assert n1["net"] == "N1"
    assert n1["status"] == "unrouted"
    assert n1["routed"] is False
    assert "crosses already-routed net 'N0'" in n1["legs"][0]["reason"]

    # Independently show the rejection is for a *real* overlap: N1's natural
    # route (same lifted latitude, its own two pins) intersects N0's drawn
    # geometry in the composed frame.
    metal2 = _route_only(layout, top, M2)
    assert metal2.count() == 1  # only N0 was drawn
    assert _touches(layout, metal2, (11.0, 21.0))
    assert _touches(layout, metal2, (31.0, 21.0))
    assert not _touches(layout, metal2, (13.0, 21.0))
    assert not _touches(layout, metal2, (33.0, 21.0))

    lift_y = metal2.bbox().top * layout.dbu - 0.35  # backbone centreline
    natural_n1 = kdb.Path(
        [
            kdb.Point(round(x / layout.dbu), round(y / layout.dbu))
            for x, y in [(13.0, 21.0), (13.0, lift_y), (33.0, lift_y), (33.0, 21.0)]
        ],
        round(0.7 / layout.dbu),
    )
    assert not (kdb.Region(natural_n1.polygon()) & metal2).is_empty()

"""Tests for the routed-DEF ``SHAPE DRCFILL`` scan (issue #5740).

Three tiers, mirroring ``tests/test_place_and_route_via_tip.py``'s split:

- unit checks on the captured real fixture
  (``tests/fixtures/place_and_route_drcfill/routed.def``): the scan finds
  every patch OpenROAD wrote, at exactly the rectangles KLayout itself
  draws, and behaves on the edge cases (no DRCFILL, missing file, missing
  UNITS, ``NETS``- vs ``SPECIALNETS``-section items, wildcards,
  extensions, non-Manhattan points);
- a **non-circular geometry oracle** gated on the pinned lambdapdk tree
  (``scripts/fetch-pdks.sh``): the fixture DEF is read through KLayout's
  own LEF/DEF reader twice -- once verbatim, once with the DRCFILL items
  stripped -- and the scan's reconstruction must cover the difference
  region while staying inside the full drawing. Both endpoints of that
  comparison come from KLayout, not from this scan's own parser, so the
  test cannot pass by the parser merely agreeing with a fixture its own
  author wrote;
- an end-to-end ``_merge_def_to_gds`` run on a synthetic LEF/DEF fixture
  (real KLayout readers, fabricated content -- the same technique
  ``tests/test_place_and_route_min_area.py`` uses): a DRCFILL patch beside
  a routed wire reaches the response's ``via_pad_trim.tip_check`` as a
  ``router_fill`` attribution through the production call path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools.place_and_route_def_drcfill import scan_def_drcfill

kdb = pytest.importorskip("klayout.db")

_FIXTURE = Path(__file__).parent / "fixtures/place_and_route_drcfill/routed.def"
_APR = Path(__file__).parent.parent / "pdks/lambdapdk/lambdapdk/asap7/base/apr"
_TECH_LEF = _APR / "asap7_tech.lef"
_LAYERMAP = _APR / "asap7.layermap"
#: Skip-gated exactly like ``tests/test_place_and_route_asap7_spef_sdf.py``:
#: the pinned lambdapdk tree is a local, gitignored fetch.
_needs_lambdapdk = pytest.mark.skipif(
    not (_TECH_LEF.is_file() and _LAYERMAP.is_file()),
    reason="pinned lambdapdk ASAP7 tree not found -- run scripts/fetch-pdks.sh",
)

DBU = 0.001

#: The fixture's first DRCFILL line:
#: ``NEW M3 28 + SHAPE DRCFILL ( 1872 5116 ) ( 1872 5165 )`` -- a vertical
#: 28 nm-wide segment, butt ends, centered box.
_FIRST_BOX = (1858, 5116, 1886, 5165)


def _scan(def_path: str = str(_FIXTURE), dbu_um: float = DBU, **kwargs):
    return scan_def_drcfill(kdb, def_path=def_path, dbu_um=dbu_um, **kwargs)


def _boxes(region) -> list[tuple[int, int, int, int]]:
    return [
        (
            polygon.bbox().left,
            polygon.bbox().bottom,
            polygon.bbox().right,
            polygon.bbox().top,
        )
        for polygon in region.each()
    ]


# ---------------------------------------------------------------------------
# The captured real fixture
# ---------------------------------------------------------------------------


def test_real_routed_fixture_finds_every_drcfill_patch():
    report = _scan()
    assert report["status"] == "scanned" and report["reason"] is None
    assert report["segments"] == 15
    assert report["specialnets_segments"] == 15 and report["nets_segments"] == 0
    assert report["unresolved"] == 0
    assert list(report["regions"]) == ["M3"]
    assert _FIRST_BOX in _boxes(report["regions"]["M3"])
    assert len(_boxes(report["regions"]["M3"])) == 15


def test_real_routed_fixture_needs_a_drcfill_free_def_to_stay_silent():
    """A DEF without DRCFILL wiring must scan clean: empty regions, no
    segments -- the no-op that keeps ``router_fill`` at 0 on every flow
    whose router never wrote patch metal."""
    text = "\n".join(
        line
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
        if "+ SHAPE DRCFILL" not in line
    )
    stripped = _FIXTURE.with_name("stripped.def")
    stripped.write_text(text, encoding="utf-8")
    try:
        report = _scan(str(stripped))
        assert report["status"] == "scanned"
        assert report["segments"] == 0 and report["regions"] == {}
    finally:
        stripped.unlink()


def test_missing_file_is_a_named_skip():
    report = _scan("/nonexistent/design.def")
    assert report["status"] == "skipped"
    assert "/nonexistent/design.def" in report["reason"]
    assert report["segments"] == 0 and report["regions"] == {}


def test_missing_units_statement_is_a_named_skip(tmp_path):
    path = tmp_path / "nounits.def"
    path.write_text("VERSION 5.8 ;\nDESIGN top ;\nEND DESIGN\n", encoding="utf-8")
    report = _scan(str(path))
    assert report["status"] == "skipped"
    assert "UNITS DISTANCE MICRONS" in report["reason"]


#: The fixture's ``NETS`` section carries no DRCFILL, so the ``NETS``-section
#: branch has no real payload to test against. This hand-written excerpt is a
#: *supplementary* edge case on top of the real fixture (the body's circularity
#: warning is about hand-synthesised payloads as the only input), exercising
#: the width-source rules a regular net item follows: no width of its own, so
#: it needs the tech-LEF width the caller supplies -- or counts as unresolved
#: rather than guessed.
_NETS_EXCERPT = """\
VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN top ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 4000 4000 ) ;
NETS 1 ;
  - n1 ( inst1 A )
    + ROUTED M1 ( 1000 900 ) ( 1000 1900 )
    NEW M3 ( 0 -200 ) ( 0 0 ) + SHAPE DRCFILL ;
END NETS
END DESIGN
"""


def test_nets_section_item_borrows_the_lef_width(tmp_path):
    path = tmp_path / "nets.def"
    path.write_text(_NETS_EXCERPT, encoding="utf-8")
    report = _scan(str(path), lef_layer_widths_dbu={"M3": 28})
    assert report["status"] == "scanned"
    assert report["nets_segments"] == 1 and report["specialnets_segments"] == 0
    assert _boxes(report["regions"]["M3"]) == [(-14, -200, 14, 0)]


def test_nets_section_item_without_a_width_source_is_unresolved(tmp_path):
    path = tmp_path / "nets.def"
    path.write_text(_NETS_EXCERPT, encoding="utf-8")
    report = _scan(str(path))
    assert report["status"] == "scanned"
    assert report["segments"] == 0 and report["unresolved"] == 1
    assert report["regions"] == {}


def test_wildcard_extension_and_multipoint_items(tmp_path):
    """DEF point syntax beyond the fixture's two-point form: ``( * y )``
    wildcards, explicit end extensions, and a three-point item that
    becomes two segments (corner-joined, so the merged region is one
    polygon -- its bbox pins both segments' extents)."""
    path = tmp_path / "syntax.def"
    path.write_text(
        "VERSION 5.8 ;\n"
        "DESIGN top ;\n"
        "UNITS DISTANCE MICRONS 1000 ;\n"
        "SPECIALNETS 1 ;\n"
        "- VPWR ( PIN VPWR ) + USE POWER\n"
        "  + ROUTED M2 20 + SHAPE STRIPE ( 0 0 ) ( 1000 0 )\n"
        "  NEW M2 20 + SHAPE DRCFILL ( 100 1000 50 ) ( * 2000 ) ( 200 * ) ;\n"
        "END SPECIALNETS\n"
        "END DESIGN\n",
        encoding="utf-8",
    )
    report = _scan(str(path))
    assert report["specialnets_segments"] == 2
    region = report["regions"]["M2"]
    assert region.count() == 1
    box = region.bbox()
    # Vertical segment (100,1000)->(100,2000) at width 20, its bottom end
    # (the point carrying the explicit 50) extended down to 950; then the
    # horizontal corner segment (100,2000)->(200,2000) reaching x=200.
    assert (box.left, box.bottom, box.right, box.top) == (90, 950, 200, 2010)


def test_non_manhattan_pair_is_counted_unresolved(tmp_path):
    path = tmp_path / "diag.def"
    path.write_text(
        "VERSION 5.8 ;\n"
        "DESIGN top ;\n"
        "UNITS DISTANCE MICRONS 1000 ;\n"
        "SPECIALNETS 1 ;\n"
        "- VPWR ( PIN VPWR ) + USE POWER\n"
        "  + ROUTED M2 20 + SHAPE DRCFILL ( 0 0 ) ( 100 100 ) ;\n"
        "END SPECIALNETS\n"
        "END DESIGN\n",
        encoding="utf-8",
    )
    report = _scan(str(path))
    assert report["segments"] == 0 and report["unresolved"] == 1
    assert report["regions"] == {}


def test_coordinates_scale_to_the_requested_dbu(tmp_path):
    path = tmp_path / "scale.def"
    path.write_text(_NETS_EXCERPT, encoding="utf-8")
    report = _scan(str(path), dbu_um=0.0005, lef_layer_widths_dbu={"M3": 28})
    assert _boxes(report["regions"]["M3"]) == [(-28, -400, 28, 0)]


# ---------------------------------------------------------------------------
# Non-circular geometry oracle: KLayout's own reader as both endpoints
# ---------------------------------------------------------------------------


@_needs_lambdapdk
def test_reconstruction_matches_klayout_s_own_drawing():
    """The scan's patch region must cover exactly what KLayout draws *because
    of* the DRCFILL items, and nothing outside what it draws at all: read the
    fixture DEF verbatim (A) and with the DRCFILL items stripped (B), both
    through KLayout's own LEF/DEF reader, and require ``A-B <= scan <= A``.
    The difference ``A-B`` is the patch area that no other wiring covers --
    patches overlap their via stack's own metal, so it is a subset of the
    patch rectangles, which is exactly what the two inclusions express."""

    def _read(path: str):
        layout = kdb.Layout()
        opts = kdb.LoadLayoutOptions()
        opts.lefdef_config.lef_files = [str(_TECH_LEF)]
        opts.lefdef_config.map_file = str(_LAYERMAP)
        opts.lefdef_config.dbu = DBU
        layout.read(path, opts)
        return layout

    full = _read(str(_FIXTURE))
    text = "\n".join(
        line
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
        if "+ SHAPE DRCFILL" not in line
    )
    stripped = _FIXTURE.with_name("stripped-oracle.def")
    stripped.write_text(text + "\n", encoding="utf-8")
    try:
        minus = _read(str(stripped))
    finally:
        stripped.unlink()

    scan = _scan()
    assert list(scan["regions"]) == ["M3"]
    m3 = full.find_layer(30, 0)
    assert m3 is not None
    region_a = kdb.Region(full.cell("top").begin_shapes_rec(m3)).merged()
    region_b = kdb.Region(minus.cell("top").begin_shapes_rec(m3)).merged()
    region_scan = scan["regions"]["M3"]

    only_drcfill = region_a - region_b
    assert not only_drcfill.is_empty(), "fixture's DRCFILL items drew nothing"
    assert (only_drcfill - region_scan).is_empty()
    assert (region_scan - region_a).is_empty()


# ---------------------------------------------------------------------------
# End-to-end through the production merge path
# ---------------------------------------------------------------------------

_M1 = (19, 0)


def _write_tech_lef(path: Path) -> None:
    path.write_text(
        "VERSION 5.7 ;\n"
        'BUSBITCHARS "[]" ;\n'
        'DIVIDERCHAR "/" ;\n'
        "UNITS\n"
        "  DATABASE MICRONS 1000 ;\n"
        "END UNITS\n"
        "MANUFACTURINGGRID 0.005 ;\n"
        "SITE asap7sc7p5t\n"
        "  SYMMETRY Y ;\n"
        "  CLASS CORE ;\n"
        "  SIZE 0.54 BY 2.7 ;\n"
        "END asap7sc7p5t\n"
        "LAYER M1\n"
        "  TYPE ROUTING ;\n"
        "  DIRECTION HORIZONTAL ;\n"
        "  WIDTH 0.018 ;\n"
        "END M1\n"
        "LAYER M2\n"
        "  TYPE ROUTING ;\n"
        "  DIRECTION VERTICAL ;\n"
        "  WIDTH 0.018 ;\n"
        "END M2\n"
        "LAYER M3\n"
        "  TYPE ROUTING ;\n"
        "  DIRECTION HORIZONTAL ;\n"
        "  WIDTH 0.018 ;\n"
        "END M3\n"
        "LAYER V1\n"
        "  TYPE CUT ;\n"
        "END V1\n"
        "END LIBRARY\n",
        encoding="utf-8",
    )


def _write_cell_lef(path: Path, cell_name: str) -> None:
    path.write_text(
        "VERSION 5.7 ;\n"
        'BUSBITCHARS "[]" ;\n'
        'DIVIDERCHAR "/" ;\n'
        "UNITS\n"
        "  DATABASE MICRONS 1000 ;\n"
        "END UNITS\n"
        f"MACRO {cell_name}\n"
        "  CLASS CORE ;\n"
        "  SITE asap7sc7p5t ;\n"
        "  SIZE 0.54 BY 2.7 ;\n"
        "  PIN A\n"
        "    DIRECTION INPUT ;\n"
        "    PORT\n"
        "      LAYER M1 ; RECT 0 0 0.1 0.1 ;\n"
        "    END\n"
        "  END A\n"
        f"END {cell_name}\n"
        "END LIBRARY\n",
        encoding="utf-8",
    )


def _write_cell_gds(path: Path, cell_name: str) -> None:
    layout = kdb.Layout()
    layout.dbu = DBU
    cell = layout.create_cell(cell_name)
    cell.shapes(layout.layer(*_M1)).insert(kdb.Box(0, 0, 540, 2700))
    layout.write(str(path))


def _write_layer_map(path: Path) -> None:
    path.write_text(
        "M1 NET,SPNET,VIA 19 0\n"
        "M2 NET,SPNET,VIA 20 0\n"
        "M3 NET,SPNET,VIA 30 0\n"
        "V1 NET,SPNET,VIA 21 0\n",
        encoding="utf-8",
    )


def _write_def(path: Path, cell_name: str) -> None:
    """A routed DEF whose M3 patch tip faces a routed wire's side 11 nm
    away -- the S.2 case issue #5740's follow-up report describes, with the
    patch as the short-edged geometry and the routed wire as the long one."""
    path.write_text(
        "VERSION 5.8 ;\n"
        'DIVIDERCHAR "/" ;\n'
        'BUSBITCHARS "[]" ;\n'
        "DESIGN top ;\n"
        "UNITS DISTANCE MICRONS 1000 ;\n"
        "DIEAREA ( 0 0 ) ( 4000 4000 ) ;\n"
        "ROW ROW_0 asap7sc7p5t 0 0 N DO 10 BY 1 STEP 540 0 ;\n"
        "COMPONENTS 1 ;\n"
        f"- inst1 {cell_name} + PLACED ( 500 500 ) N ;\n"
        "END COMPONENTS\n"
        "NETS 1 ;\n"
        "- n1\n"
        "  ( inst1 A )\n"
        "  + ROUTED M3 ( 200 1220 ) ( 800 1220 ) ;\n"
        "END NETS\n"
        "SPECIALNETS 1 ;\n"
        "- VPWR\n"
        "  ( PIN VPWR )\n"
        "  + ROUTED M3 28 + SHAPE DRCFILL ( 300 1000 ) ( 300 1200 )\n"
        "  + USE POWER ;\n"
        "END SPECIALNETS\n"
        "END DESIGN\n",
        encoding="utf-8",
    )


def _build_merge_fixture(tmp_path: Path) -> dict[str, object]:
    """Write the whole fixture and return ``_merge_def_to_gds``'s kwargs --
    an asap7-family variant assembled in ``tmp_path``, so the merge's own
    tip-check call path runs end to end without OpenROAD or the binary."""
    cell_name = "testcell"
    root = tmp_path / "install"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tech_lef(tech_lef)
    _write_cell_lef(cell_lef, cell_name)

    gds_dir = root / "libs.ref" / cell_name / "gds"
    gds_dir.mkdir(parents=True)
    _write_cell_gds(gds_dir / f"{cell_name}.gds", cell_name)

    klayout_dir = root / "libs.tech" / "klayout"
    (klayout_dir / "tech").mkdir(parents=True)
    _write_layer_map(klayout_dir / "tech" / "asap7.map")

    def_path = tmp_path / "design.def"
    _write_def(def_path, cell_name)

    return {
        "def_path": str(def_path),
        "tech_lef": str(tech_lef),
        "cell_lef": str(cell_lef),
        "pdk_info": {
            "schema_version": 1,
            "root": str(root),
            "variant": "asap7",
            "version": None,
            "resolved_via": "test",
            "assets": {
                "libs_ref": str(root / "libs.ref"),
                "klayout": str(klayout_dir),
            },
        },
        "cell_library": cell_name,
        "hdl_toplevel": "top",
        "macros": [],
        "out_path": str(tmp_path / "out.gds"),
    }


def test_merge_attributes_drcfill_patch_tip_in_tip_check(tmp_path):
    from klayout_tools import place_and_route

    info = place_and_route._merge_def_to_gds(**_build_merge_fixture(tmp_path))
    tip_check = info["via_pad_trim"]["tip_check"]
    assert tip_check["status"] == "violations"
    assert tip_check["router_fill_violations"] == 1
    assert tip_check["via_pad_violations"] == 0
    by_rule = {entry["rule"]: entry for entry in tip_check["rules"]}
    assert set(by_rule) == {"M3.S.2"}
    entry = by_rule["M3.S.2"]
    assert entry["violations"] == 1
    assert entry["router_fill"] == 1 and entry["via_pads"] == 0

"""Synthetic "OpenROAD-produced" macro-scale GDS fixture (issue #436).

Epic #391 Phase 5's whole point is the first exercise of `klt drc`/
`klt precheck`/`klt layout-metrics` against machine-generated, macro-scale,
standard-cell GDS -- as opposed to the small, hand-drawn-analog fixtures
those three verbs' own test suites have used exclusively until now (see
`tests/test_drc.py`'s own module docstring: "fixtures are generated
programmatically ... no dependency on an external corpus" for the *rule*
tests, plus a handful of real single-cell corpus files).

Real OpenROAD is not on `$PATH` in this environment (and CI installs neither
`openroad` nor a real sky130 standard-cell PDK -- the same documented gap
`klt synthesize`/`klt place-and-route`'s own test suites note). So this
module cannot literally run `klt place-and-route`. Instead it drives the
exact **production** DEF->GDS merge code path place-and-route itself uses
(`klayout_tools.place_and_route._merge_def_to_gds`) against a hand-authored
DEF that places hundreds of real `sky130_fd_sc_hd` standard-cell instances,
reusing the real corpus GDS views already checked in at
`tests/corpus/sky130/` (`buf_4`, `inv_1`, `nand2_2`, `dfxtp_2` -- see
`tests/corpus/README.md`) -- never `pya`-drawn placeholder shapes. This is
the closest available proxy for "OpenROAD's actual DEF/GDS output shape"
without a real OpenROAD binary or a full real sky130 PDK install.

Known simplifications (documented, not fixed -- explicitly out of scope for
this fixture, see issue #436's own acceptance criteria: identify-or-fix
verb-side assumptions, not build a full synthetic P&R engine):

- **Placement only, no routing.** No `NETS`/`SPECIALNETS` section -- no
  routing metal (met1-met5 wires/vias), no VPWR/VGND power straps, no tap
  cells. Fabricating plausible-looking-but-fake routing risks manufacturing
  DRC findings that are artifacts of this fixture's own synthetic data
  rather than genuine `klt drc`/`klt precheck` behaviour worth exercising.
- **Uniform row orientation.** Every row is placed `N` (never the
  alternating `N`/`FS` mirroring a real power-rail-sharing floorplan uses) --
  avoids a transform-math error in this fixture silently producing
  overlapping geometry that would look like a spurious DRC violation.
- Cells tile edge-to-edge with zero gaps (real DEF site-pitch placement,
  each cell width is a whole multiple of the `unithd` site width, 0.46um) so
  the fixture introduces no *new* spacing violations at cell/cell
  boundaries beyond whatever each individual standard cell already draws.

The result is a genuinely hierarchical, multi-master, real-sky130-layer GDS
at a scale (hundreds of standard-cell instances across multiple rows) this
repo's existing DRC/precheck/layout-metrics test fixtures never exercised.
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb

from klayout_tools import place_and_route

#: Real corpus cells (tests/corpus/sky130/), cycled in this fixed order to
#: build each row. Widths are in micrometres, verified against each cell's
#: own `236/0` (PR boundary) layer -- see the issue's own investigation.
_CORPUS_DIR = Path(__file__).parent.parent / "corpus" / "sky130"
_CELL_LIBRARY = "sky130_fd_sc_hd"
_SITE = "unithd"
_SITE_WIDTH_UM = 0.46
_SITE_HEIGHT_UM = 2.72

_CELLS: tuple[tuple[str, float], ...] = (
    ("sky130_fd_sc_hd__inv_1", 1.38),
    ("sky130_fd_sc_hd__nand2_2", 2.3),
    ("sky130_fd_sc_hd__buf_4", 2.76),
    ("sky130_fd_sc_hd__dfxtp_2", 7.82),
)


def _write_tech_lef(path: Path) -> None:
    path.write_text(
        "VERSION 5.7 ;\n"
        'BUSBITCHARS "[]" ;\n'
        'DIVIDERCHAR "/" ;\n'
        "UNITS\n"
        "  DATABASE MICRONS 1000 ;\n"
        "END UNITS\n"
        "MANUFACTURINGGRID 0.005 ;\n"
        f"SITE {_SITE}\n"
        "  SYMMETRY Y ;\n"
        "  CLASS CORE ;\n"
        f"  SIZE {_SITE_WIDTH_UM} BY {_SITE_HEIGHT_UM} ;\n"
        f"END {_SITE}\n"
        "LAYER met1\n"
        "  TYPE ROUTING ;\n"
        "  DIRECTION HORIZONTAL ;\n"
        "  WIDTH 0.14 ;\n"
        "  PITCH 0.34 ;\n"
        "END met1\n",
        encoding="utf-8",
    )


def _write_cell_lef(path: Path) -> None:
    lines = [
        "VERSION 5.7 ;",
        'BUSBITCHARS "[]" ;',
        'DIVIDERCHAR "/" ;',
        "UNITS",
        "  DATABASE MICRONS 1000 ;",
        "END UNITS",
    ]
    for name, width_um in _CELLS:
        lines += [
            f"MACRO {name}",
            "  CLASS CORE ;",
            f"  SITE {_SITE} ;",
            f"  SIZE {width_um} BY {_SITE_HEIGHT_UM} ;",
            "  PIN A",
            "    DIRECTION INPUT ;",
            "    PORT",
            "      LAYER met1 ; RECT 0 0 0.1 0.1 ;",
            "    END",
            "  END A",
            f"END {name}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_combined_cell_gds(path: Path) -> None:
    """Merge the four real per-cell corpus GDS files into one combined
    library GDS -- the shape `_resolve_gds_view` expects
    (``<libs_ref>/<cell_library>/gds/<cell_library>.gds``, a single stream
    holding every standard cell as its own top cell, matching a real
    open_pdks install's own layout)."""
    combined = kdb.Layout()
    for name, _width_um in _CELLS:
        src = kdb.Layout()
        src.read(str(_CORPUS_DIR / f"{name}.gds"))
        combined.dbu = src.dbu
        dest_cell = combined.create_cell(name)
        dest_cell.copy_tree(src.cell(name))
    combined.write(str(path))


def _write_def(
    path: Path,
    *,
    design_name: str,
    num_rows: int,
    cycles_per_row: int,
    overlap_shift_um: float = 0.0,
) -> int:
    """Write a placement-only DEF tiling ``_CELLS`` across ``num_rows`` rows
    of ``cycles_per_row`` repetitions of the 4-cell cycle each. Returns the
    total instance count written.

    ``overlap_shift_um`` (default ``0.0``, no shift) pulls the row's *second*
    instance left by that many micrometres, deliberately overlapping the
    first instance -- used by the regression test that confirms `klt drc`
    still detects a genuine violation at macro scale/hierarchy (as opposed
    to every other fixture in this module, which is placement-only and
    tiles cells edge to edge with zero gaps, i.e. is always DRC-clean by
    construction). A real, physical spacing/overlap violation, not
    fabricated geometry on a layer no rule checks.
    """
    cycle_width_um = sum(width for _name, width in _CELLS)
    row_width_um = cycle_width_um * cycles_per_row
    row_width_dbu = round(row_width_um * 1000)
    row_height_dbu = round(_SITE_HEIGHT_UM * 1000)
    site_width_dbu = round(_SITE_WIDTH_UM * 1000)

    lines = [
        "VERSION 5.8 ;",
        'DIVIDERCHAR "/" ;',
        'BUSBITCHARS "[]" ;',
        f"DESIGN {design_name} ;",
        "UNITS DISTANCE MICRONS 1000 ;",
        f"DIEAREA ( 0 0 ) ( {row_width_dbu} {row_height_dbu * num_rows} ) ;",
    ]

    sites_per_row = round(row_width_um / _SITE_WIDTH_UM)
    for row in range(num_rows):
        y = row * row_height_dbu
        lines.append(
            f"ROW ROW_{row} {_SITE} 0 {y} N "
            f"DO {sites_per_row} BY 1 STEP {site_width_dbu} 0 ;"
        )

    shift_dbu = round(overlap_shift_um * 1000)
    instance_index = 0
    instances: list[str] = []
    for row in range(num_rows):
        x_dbu = 0
        y_dbu = row * row_height_dbu
        for cycle in range(cycles_per_row):
            for name, width_um in _CELLS:
                placed_x_dbu = x_dbu
                if instance_index == 1 and shift_dbu:
                    placed_x_dbu = x_dbu - shift_dbu
                inst_name = f"inst_{row}_{cycle}_{name.split('__')[-1]}"
                instances.append(
                    f"- {inst_name} {name} + PLACED ( {placed_x_dbu} {y_dbu} ) N ;"
                )
                x_dbu += round(width_um * 1000)
                instance_index += 1

    lines.append(f"COMPONENTS {len(instances)} ;")
    lines.extend(instances)
    lines.append("END COMPONENTS")
    lines.append("END DESIGN")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(instances)


def build_macro_gds(
    build_dir: Path,
    *,
    design_name: str = "openroad_macro_fixture",
    num_rows: int = 10,
    cycles_per_row: int = 8,
    overlap_shift_um: float = 0.0,
) -> Path:
    """Build the synthetic OpenROAD-shaped macro GDS under ``build_dir``.

    Returns the path to the merged, hierarchical, multi-master GDS (real
    sky130 layers, real cell footprints, ``design_name`` as the single top
    cell). ``num_rows`` * ``cycles_per_row`` * 4 is the resulting instance
    count (default: 10 * 8 * 4 = 320 standard-cell instances).

    ``overlap_shift_um`` forwards to :func:`_write_def` -- see its own
    docstring; ``0.0`` (the default) produces a DRC-clean, edge-to-edge
    placement.
    """
    build_dir.mkdir(parents=True, exist_ok=True)

    tech_lef = build_dir / "tech.tlef"
    cell_lef = build_dir / "cells.lef"
    _write_tech_lef(tech_lef)
    _write_cell_lef(cell_lef)

    root = build_dir / "pdk_root"
    gds_dir = root / "sky130A" / "libs.ref" / _CELL_LIBRARY / "gds"
    gds_dir.mkdir(parents=True, exist_ok=True)
    _write_combined_cell_gds(gds_dir / f"{_CELL_LIBRARY}.gds")

    def_path = build_dir / f"{design_name}.def"
    _write_def(
        def_path,
        design_name=design_name,
        num_rows=num_rows,
        cycles_per_row=cycles_per_row,
        overlap_shift_um=overlap_shift_um,
    )

    pdk_info = {
        "schema_version": 1,
        "root": str(root),
        "variant": "sky130A",
        "version": None,
        "resolved_via": "test",
        "assets": {
            "libs_ref": str(root / "sky130A" / "libs.ref"),
            "klayout": None,
        },
    }

    out_path = build_dir / f"{design_name}.gds"
    place_and_route._merge_def_to_gds(
        def_path=str(def_path),
        tech_lef=str(tech_lef),
        cell_lef=str(cell_lef),
        pdk_info=pdk_info,
        cell_library=_CELL_LIBRARY,
        hdl_toplevel=design_name,
        out_path=str(out_path),
    )
    return out_path

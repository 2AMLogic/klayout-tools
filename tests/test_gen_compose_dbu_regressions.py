"""Regressions for the #5721 dbu-reconciliation machinery's two measured
holes (PR #6062 review), split out of `tests/test_gen_compose_dbu.py` for
that file's file-size ratchet (issue #2766):

- the composed-dbu target's fallback: a family with no declared stream
  requirement keeps #1514's finest-across-blocks target even when its
  install ships a readable tech LEF -- `klt gen-compose` resolves the
  target with `pdk_output_dbu.required_stream_dbu` (declared entries
  only, no fall-through), not `resolve_output_dbu`;
- the refuse-don't-round gate measures each reachable cell's *own*
  coordinates: a magnified (``mag != 1``) instance hides cell-local
  off-grid geometry from a flattened top-region scan, while
  ``kdb.Layout.transform`` still divides the child's own coordinates.
"""

import pytest

from klayout_tools import gen
from klayout_tools.gen_compose import GenComposeError, compose


def _make_install(root, variant):
    """Fabricate a minimal open_pdks-layout variant (just the layout probe)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    return variant_dir


@pytest.fixture()
def both_pdk_root(tmp_path):
    """Both PDK families the phase-2 generators support, under one install
    root -- the same shape as `tests/test_gen.py`'s fixture of the same
    name, kept local here for the moved tests."""
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    _make_install(root, "gf180mcuD")
    return root


def _gen_block_variant(tmp_path, pdk_root, variant, generator, cell_name, **params):
    """A real `klt gen` generator response for an arbitrary ``variant`` --
    the same shape as `tests/test_gen_compose.py`'s helper of the same
    name, kept local here for the moved tests."""
    output = tmp_path / f"{cell_name}.gds"
    request = {
        "schema": gen.REQUEST_SCHEMA,
        "generator": generator,
        "pdk": {"variant": variant, "root": str(pdk_root)},
        "params": params,
        "options": {"cell_name": cell_name, "output": str(output)},
    }
    return gen.generate(request)


def _write_place_and_route_style_gds(path, cell_name, width_um, height_um, dbu):
    """A single-cell stream at an explicit ``dbu`` -- the same shape
    `tests/test_gen_compose_dbu.py`'s helper of the same name builds, kept
    local here for the moved tests."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = dbu
    li1 = layout.layer(67, 20)
    cell = layout.create_cell(cell_name)
    cell.shapes(li1).insert(
        kdb.Box(0, 0, int(round(width_um / dbu)), int(round(height_um / dbu)))
    )
    layout.write(str(path))
    return str(path)


def _write_magnified_instance_gds(path, dbu):
    """A two-cell stream whose top cell instantiates a sub-cell through a
    ``mag != 1`` instance: the sub-cell's *own* coordinates sit off the
    ratio-2 grid, but the magnified flattened region lands on it -- exactly
    the shape the flattened vertex pass measured instead of the cell-local
    one ``Layout.transform`` actually divides."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = dbu
    li1 = layout.layer(67, 20)
    sub = layout.create_cell("macro_sub")
    sub.shapes(li1).insert(kdb.Box(1, 1, 3, 3))
    top = layout.create_cell("macro_top")
    top.insert(
        kdb.CellInstArray(sub.cell_index(), kdb.ICplxTrans(2.0, 0.0, False, 0, 0))
    )
    layout.write(str(path))
    return str(path)


def test_compose_no_requirement_family_with_tech_lef_writes_at_finest_block(
    tmp_path, both_pdk_root
):
    """The composed-dbu target's fallback regression: a family with no
    declared stream requirement (sky130) whose install *does* ship a
    readable tech LEF must keep #1514's finest-across-blocks target, not
    inherit the LEF's dbu through `resolve_output_dbu`'s fall-through (the
    fall-through is `klt gen`'s byte-for-byte #1496 answer, not compose's).
    Here the LEF declares `DATABASE MICRONS 1000` (0.001) while a block
    sits at 0.0005 with a right edge at dbu 10001 -- on its own grid, off
    the LEF's: the fall-through version resolved a required dbu of 0.001
    and refused this composition outright; the declared-requirement-only
    target writes at the blocks' finest 0.0005."""
    variant_dir = both_pdk_root / "sky130A"
    techlef_dir = variant_dir / "libs.ref" / "sky130_fd_sc_hd" / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / "sky130_fd_sc_hd__tt.tlef").write_text(
        "UNITS\n  DATABASE MICRONS 1000 ;\nEND UNITS\n", encoding="utf-8"
    )

    sky_report = _gen_block_variant(
        tmp_path, both_pdk_root, "sky130A", "guard_ring", "ring0"
    )
    macro_gds = _write_place_and_route_style_gds(
        tmp_path / "macro.gds", "macro_top", 5.0005, 5.0, dbu=0.0005
    )
    output = tmp_path / "composed.gds"

    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(both_pdk_root)},
            "blocks": [
                {"id": "ring", "generator_report": sky_report},
                {
                    "id": "macro",
                    "cell": {"gds_path": macro_gds, "cell_name": "macro_top"},
                },
            ],
            "placement": {
                "strategy": "row",
                "order": ["ring", "macro"],
                "spacing_um": 1.0,
            },
            "options": {"cell_name": "composed_0", "output": str(output)},
        }
    )

    assert report["dbu_um"] == pytest.approx(0.0005)
    assert output.exists(), (
        "a no-requirement family keeps the #1514 refinement target even "
        "when its install ships a readable tech LEF"
    )


def test_compose_coarsening_refuses_off_grid_geometry_behind_magnified_instance(
    tmp_path, both_pdk_root
):
    """The #5721 coarsening gate must measure each reachable cell's *own*
    coordinates: a magnified (``mag != 1``) instance makes the flattened
    top-region view on-grid while ``Layout.transform`` still divides the
    child's cell-local coordinates by the ratio -- a sub-cell box at odd
    dbu ``(1, 1; 3, 3)`` seen through a mag-2 instance spans
    ``(2, 2; 6, 6)`` flattened (every vertex a multiple of 2, so the
    flattened pass passed the gate) but rounds to ``(1, 1; 2, 2)`` when the
    layout is halved, silently moving *and* shrinking the shape. Measured
    cell-locally, the gate refuses by name and writes no output."""
    variant_dir = both_pdk_root / "gf180mcuD"
    techlef_dir = variant_dir / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / "gf180mcu_fd_sc_mcu9t5v0__nom.tlef").write_text(
        "UNITS\n  DATABASE MICRONS 2000 ;\nEND UNITS\n", encoding="utf-8"
    )

    ring_report = _gen_block_variant(
        tmp_path, both_pdk_root, "gf180mcuD", "guard_ring", "ring0"
    )
    macro_gds = _write_magnified_instance_gds(tmp_path / "macro.gds", dbu=0.0005)
    output = tmp_path / "composed.gds"
    assert not output.exists()

    with pytest.raises(GenComposeError, match=r"'macro'.*macro_sub.*\(1, 1\)"):
        compose(
            {
                "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
                "blocks": [
                    {"id": "ring", "generator_report": ring_report},
                    {
                        "id": "macro",
                        "cell": {"gds_path": macro_gds, "cell_name": "macro_top"},
                    },
                ],
                "placement": {
                    "strategy": "row",
                    "order": ["ring", "macro"],
                    "spacing_um": 1.0,
                },
                "options": {"cell_name": "composed_0", "output": str(output)},
            }
        )

    assert not output.exists(), (
        "a refused composition must not leave an output stream behind"
    )

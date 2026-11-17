"""Compose-side dbu reconciliation (issues #5721/#1514/#1496) -- the
composed cell's dbu target (the PDK's required stream dbu when its family
declares one, else the finest block's own), the exact-rescale mechanics in
both directions, and the measured refuse-don't-round gate for the coarsening
direction. Split out of `tests/test_gen_compose.py` for that file's file-size
ratchet (issue #2766), alongside the implementation in
`klayout_tools/gen_compose_dbu.py`."""

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


# required **stream** dbu (0.001, #5721), with any block still sitting at the
# LEF's finer 0.0005 grid coarsened onto it -- exactly, never silently
# rounded. A block generated against a genuinely *different* PDK variant (a
# real cross-PDK composition mistake) still refuses on the non-integer ratio.
# --------------------------------------------------------------------------- #


def _write_place_and_route_style_gds(path, cell_name, width_um, height_um, dbu):
    """Fabricate a single-cell stream at an explicit ``dbu`` -- a stand-in for
    a `klt place_and_route`-produced GDS, whose dbu is resolved from the
    target PDK's own tech LEF (#1032) rather than always 0.001, the same
    shape `_write_library_gds` uses but with the dbu made a parameter so it
    can mimic a non-1000-DATABASE-MICRONS PDK (e.g. gf180mcu's 0.0005)."""
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


def test_compose_gen_block_with_place_and_route_style_gds_same_gf180mcu_pdk(
    tmp_path, both_pdk_root
):
    """The core #5721 regression: a `klt gen guard_ring` block and a
    `place_and_route`-style GDS, both resolved against the same gf180mcuD
    PDK (tech LEF declaring `DATABASE MICRONS 2000`), compose cleanly at the
    family's required **stream** dbu 0.001 (#5721) -- the ring is written
    there directly, and the finer 0.0005 macro block is coarsened onto it
    (its geometry is on the 0.001 grid, so the rescale is exact) with a
    warning naming the block and the ratio."""
    variant_dir = both_pdk_root / "gf180mcuD"
    techlef_dir = variant_dir / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / "gf180mcu_fd_sc_mcu9t5v0__nom.tlef").write_text(
        "UNITS\n  DATABASE MICRONS 2000 ;\nEND UNITS\n", encoding="utf-8"
    )

    ring_report = _gen_block_variant(
        tmp_path, both_pdk_root, "gf180mcuD", "guard_ring", "ring0"
    )
    assert ring_report["gds_path"]
    import klayout.db as kdb

    ring_layout = kdb.Layout()
    ring_layout.read(ring_report["gds_path"])
    assert ring_layout.dbu == pytest.approx(0.001)

    macro_gds = _write_place_and_route_style_gds(
        tmp_path / "macro.gds", "macro_top", 5.0, 5.0, dbu=0.0005
    )
    output = tmp_path / "composed.gds"

    report = compose(
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

    assert output.is_file()
    assert [b["id"] for b in report["blocks"]] == ["ring", "macro"]
    # The composed cell is written at the family's required stream dbu --
    # and says so, so a caller can nest this response as a block in another
    # compose (#1189) without re-reading the stream (#1496).
    assert report["dbu_um"] == pytest.approx(0.001)
    assert any(
        "macro" in w and "rescaled" in w and "ratio 2" in w for w in report["warnings"]
    ), report["warnings"]
    composed_layout = kdb.Layout()
    composed_layout.read(str(output))
    assert composed_layout.dbu == pytest.approx(0.001)


# --------------------------------------------------------------------------- #
# `klt draw`-style (always dbu=0.001, #230) block composed against a
# gf180mcu-family `klt gen` block -- now (issue #5721) the two agree by
# construction: the gen block writes at the family's required stream dbu
# 0.001, so the composition needs no rescale at all (ratio 1 -- the edge
# case that must add no warning and move no coordinate).
# --------------------------------------------------------------------------- #


def test_compose_drawn_style_and_gf180mcu_gen_block_agree_at_ratio_1(
    tmp_path, both_pdk_root
):
    """The #1514 repro, resolved by #5721: `klt draw` always writes
    `dbu=0.001` (no PDK awareness, #230), and a `klt gen guard_ring --pdk
    gf180mcuD` block now writes at the family's required stream dbu 0.001
    too (#5721) -- so composing them needs no reconciliation: ratio 1, no
    rescale, no warning, and the drawn block's literal coordinates reach the
    composed stream unchanged."""
    variant_dir = both_pdk_root / "gf180mcuD"
    techlef_dir = variant_dir / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / "gf180mcu_fd_sc_mcu9t5v0__nom.tlef").write_text(
        "UNITS\n  DATABASE MICRONS 2000 ;\nEND UNITS\n", encoding="utf-8"
    )

    ring_report = _gen_block_variant(
        tmp_path, both_pdk_root, "gf180mcuD", "guard_ring", "ring0"
    )
    import klayout.db as kdb

    ring_layout = kdb.Layout()
    ring_layout.read(ring_report["gds_path"])
    assert ring_layout.dbu == pytest.approx(0.001)

    drawn_gds = _write_place_and_route_style_gds(
        tmp_path / "drawn.gds", "drawn_top", 1.0, 0.15, dbu=0.001
    )
    output = tmp_path / "composed.gds"

    report = compose(
        {
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "blocks": [
                {"id": "ring", "generator_report": ring_report},
                {
                    "id": "drawn",
                    "cell": {"gds_path": drawn_gds, "cell_name": "drawn_top"},
                },
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["ring", "drawn"],
                "origins_um": {
                    "ring": {"x": 0.0, "y": 0.0},
                    "drawn": {"x": 2.0, "y": 2.0},
                },
            },
            "options": {"cell_name": "composed_0", "output": str(output)},
        }
    )

    assert output.is_file()
    # Both blocks were already at the composed dbu -- no rescale, no warning.
    assert report["dbu_um"] == pytest.approx(0.001)
    assert not any("rescaled" in w for w in report["warnings"]), report["warnings"]

    drawn_block = next(b for b in report["blocks"] if b["id"] == "drawn")
    assert drawn_block["offset_um"] == {"x": 2.0, "y": 2.0}

    composed_layout = kdb.Layout()
    composed_layout.read(str(output))
    assert composed_layout.dbu == pytest.approx(0.001)

    # The drawn block's own sub-cell, named "<block_id>__<cell_name>" by
    # `_write_composed_gds` -- read its geometry directly to confirm it was
    # copied through untouched: a box drawn (0,0)-(1000,150) at dbu=0.001 is
    # exactly 1.0um x 0.15um in the composed stream too.
    drawn_cell = composed_layout.cell("drawn__drawn_top")
    assert drawn_cell is not None
    li1 = composed_layout.find_layer(67, 20)
    assert li1 is not None
    shapes = list(drawn_cell.shapes(li1).each())
    assert len(shapes) == 1
    box = shapes[0].box
    assert box.width() * composed_layout.dbu == pytest.approx(1.0)
    assert box.height() * composed_layout.dbu == pytest.approx(0.15)


def test_compose_dbu_coarsening_preserves_block_hierarchy_and_array_pitch(
    tmp_path, both_pdk_root
):
    """The #5721 coarsening rescale must remap a finer block's *whole* layout
    -- its internal cell hierarchy and any instance-array step vectors -- not
    just the shapes sitting directly in its top cell. A block whose own top
    cell instances a leaf as a 3x2 array is the case that would silently
    break if only shapes were scaled: the array pitch would stay at its raw
    integer value and double in microns on the coarser grid, tearing the
    array apart. This is the direction #1514 refused outright; #5721 opened
    it (for a PDK's required stream dbu) behind the exactness gate."""
    import klayout.db as kdb

    variant_dir = both_pdk_root / "gf180mcuD"
    techlef_dir = variant_dir / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / "gf180mcu_fd_sc_mcu9t5v0__nom.tlef").write_text(
        "UNITS\n  DATABASE MICRONS 2000 ;\nEND UNITS\n", encoding="utf-8"
    )

    ring_report = _gen_block_variant(
        tmp_path, both_pdk_root, "gf180mcuD", "guard_ring", "ring0"
    )

    # A `place_and_route`-style hierarchical block at the finer 0.0005 grid
    # -- every coordinate an even multiple, i.e. on the required 0.001 grid.
    hier_gds = tmp_path / "hier.gds"
    src = kdb.Layout()
    src.dbu = 0.0005
    src_li1 = src.layer(67, 20)
    leaf = src.create_cell("hier_leaf")
    leaf.shapes(src_li1).insert(kdb.Box(0, 0, 2000, 300))  # 1.0um x 0.15um
    hier_top = src.create_cell("hier_top")
    hier_top.insert(
        kdb.CellInstArray(
            leaf.cell_index(),
            kdb.Trans(0, False, 10000, 14000),  # 5.0um, 7.0um
            kdb.Vector(6000, 0),  # 3.0um column pitch
            kdb.Vector(0, 4000),  # 2.0um row pitch
            3,
            2,
        )
    )
    expected_bbox_um = hier_top.dbbox()
    src.write(str(hier_gds))

    output = tmp_path / "composed.gds"
    report = compose(
        {
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "blocks": [
                {"id": "ring", "generator_report": ring_report},
                {
                    "id": "hier",
                    "cell": {
                        "gds_path": str(hier_gds),
                        "cell_name": "hier_top",
                    },
                },
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["ring", "hier"],
                "origins_um": {
                    "ring": {"x": 0.0, "y": 0.0},
                    "hier": {"x": 40.0, "y": 40.0},
                },
            },
            "options": {"cell_name": "composed_0", "output": str(output)},
        }
    )
    assert report["dbu_um"] == pytest.approx(0.001)
    assert any(
        "hier" in w and "rescaled" in w and "ratio 2" in w for w in report["warnings"]
    ), report["warnings"]

    composed_layout = kdb.Layout()
    composed_layout.read(str(output))
    assert composed_layout.dbu == pytest.approx(0.001)

    hier_cell = composed_layout.cell("hier__hier_top")
    assert hier_cell is not None
    # The block's own hierarchy survived (the leaf is still a separate cell,
    # instanced as an array -- not flattened away by the rescale).
    insts = list(hier_cell.each_inst())
    assert len(insts) == 1
    inst = insts[0]
    # ...and every micron-space dimension is unchanged: array pitch, counts,
    # the instance's own displacement, and the leaf's own drawn box. (The
    # a/b axes are compared as an unordered pair: a GDS AREF round-trip
    # reports them rows-first, independent of this rescale.)
    axes = {
        (
            round(inst.a.x * composed_layout.dbu, 6),
            round(inst.a.y * composed_layout.dbu, 6),
            inst.na,
        ),
        (
            round(inst.b.x * composed_layout.dbu, 6),
            round(inst.b.y * composed_layout.dbu, 6),
            inst.nb,
        ),
    }
    assert axes == {(3.0, 0.0, 3), (0.0, 2.0, 2)}
    assert inst.trans.disp.x * composed_layout.dbu == pytest.approx(5.0)
    assert inst.trans.disp.y * composed_layout.dbu == pytest.approx(7.0)

    composed_bbox = hier_cell.dbbox()
    assert composed_bbox.left == pytest.approx(expected_bbox_um.left)
    assert composed_bbox.bottom == pytest.approx(expected_bbox_um.bottom)
    assert composed_bbox.right == pytest.approx(expected_bbox_um.right)
    assert composed_bbox.top == pytest.approx(expected_bbox_um.top)


def test_compose_still_rejects_non_integer_ratio_dbu_mismatch(tmp_path, both_pdk_root):
    """A block generated against sky130A (dbu 0.001, no tech LEF resolver
    override in this fixture) composed alongside a GDS at a dbu whose ratio
    to 0.001 is *not* an exact integer (0.001 / 0.0004 == 2.5) is a genuine,
    irreconcilable composition mistake -- the mismatch error must still
    fire, not be silently (and lossily) rescaled (#1514, narrowing #1496's
    original blanket refusal to only the non-integer-ratio case)."""
    sky_report = _gen_block_variant(
        tmp_path, both_pdk_root, "sky130A", "guard_ring", "ring0"
    )
    macro_gds = _write_place_and_route_style_gds(
        tmp_path / "macro.gds", "macro_top", 5.0, 5.0, dbu=0.0004
    )
    output = tmp_path / "composed.gds"

    with pytest.raises(GenComposeError, match="does not evenly divide"):
        compose(
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


def test_compose_coarsening_refuses_off_grid_geometry_and_writes_no_output(
    tmp_path, both_pdk_root
):
    """The #5721 coarsening gate: a gf180mcu composition's required stream
    dbu (0.001) is *coarser* than a 0.0005 block whose geometry carries a
    coordinate that is an odd multiple of 0.0005 -- dividing it onto the
    target grid would silently round (verified: ``Layout.transform`` rounds
    such a coordinate to the nearest surviving dbu), so the request is
    refused **by name** -- block, layer, and one offending coordinate -- and
    no output stream is written. A silently-rounded composition would fail
    this criterion."""
    variant_dir = both_pdk_root / "gf180mcuD"
    techlef_dir = variant_dir / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / "gf180mcu_fd_sc_mcu9t5v0__nom.tlef").write_text(
        "UNITS\n  DATABASE MICRONS 2000 ;\nEND UNITS\n", encoding="utf-8"
    )

    ring_report = _gen_block_variant(
        tmp_path, both_pdk_root, "gf180mcuD", "guard_ring", "ring0"
    )

    # 5.0005um wide at dbu 0.0005 -> right edge at dbu 10001, an odd
    # multiple: off the 0.001 grid the composition targets.
    macro_gds = _write_place_and_route_style_gds(
        tmp_path / "macro.gds", "macro_top", 5.0005, 5.0, dbu=0.0005
    )
    output = tmp_path / "composed.gds"
    assert not output.exists()

    with pytest.raises(
        GenComposeError, match=r"'macro'.*layer 67/20.*\(10001, (?:0|10000)\)"
    ):
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


def test_compose_refinement_rescale_still_applies_without_family_requirement(
    tmp_path, both_pdk_root
):
    """The #1514 refinement direction (coarser block rescaled *up* onto the
    finest block's own dbu) is untouched by #5721 for PDKs with no declared
    stream requirement: a sky130A gen block (0.001, no tech LEF in this
    fixture -> no required dbu) composed with a 0.0005 block writes at
    0.0005 with the coarser block rescaled x2."""
    sky_report = _gen_block_variant(
        tmp_path, both_pdk_root, "sky130A", "guard_ring", "ring0"
    )
    macro_gds = _write_place_and_route_style_gds(
        tmp_path / "macro.gds", "macro_top", 5.0, 5.0, dbu=0.0005
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
    assert any(
        "ring" in w and "rescaled" in w and "ratio 2" in w for w in report["warnings"]
    ), report["warnings"]


# --------------------------------------------------------------------------- #


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

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


def _compose_fine_macro(tmp_path, pdk_root, shapes):
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = 0.0005
    macro = layout.create_cell("macro_top")
    layer = layout.layer(34, 0)
    for shape in shapes:
        macro.shapes(layer).insert(shape)
    source = tmp_path / "macro.gds"
    layout.write(str(source))
    ring = _gen_block_variant(tmp_path, pdk_root, "gf180mcuD", "guard_ring", "ring0")
    return compose(
        {
            "pdk": {"variant": "gf180mcuD", "root": str(pdk_root)},
            "blocks": [
                {"id": "ring", "generator_report": ring},
                {
                    "id": "macro",
                    "cell": {"gds_path": str(source), "cell_name": "macro_top"},
                },
            ],
            "placement": {
                "strategy": "row",
                "order": ["ring", "macro"],
                "spacing_um": 1,
            },
            "options": {"cell_name": "composed", "output": str(tmp_path / "out.gds")},
        }
    )


@pytest.mark.parametrize(
    ("x", "width", "start", "end", "detail"),
    [
        (1001, 1002, 0, 0, r"path point.*\(1001, 0\)"),
        (1000, 1001, 0, 0, "path width.*1001"),
        (1000, 1000, 3, 0, "path begin extension.*3"),
        (1000, 1000, 0, 3, "path end extension.*3"),
    ],
)
def test_coarsening_validates_stored_path_fields(
    tmp_path, both_pdk_root, x, width, start, end, detail
):
    import klayout.db as kdb

    path = kdb.Path([kdb.Point(x, 0), kdb.Point(x, 4000)], width, start, end)
    with pytest.raises(GenComposeError, match=rf"'macro'.*layer 34/0.*{detail}"):
        _compose_fine_macro(tmp_path, both_pdk_root, [path])
    assert not (tmp_path / "out.gds").exists()


def test_coarsening_validates_covered_shape_coordinates(tmp_path, both_pdk_root):
    import klayout.db as kdb

    output = tmp_path / "out.gds"
    output.write_bytes(b"existing output")
    with pytest.raises(
        GenComposeError, match=r"'macro'.*layer 34/0.*\(15, (?:15|45)\)"
    ):
        _compose_fine_macro(
            tmp_path, both_pdk_root, [kdb.Box(0, 0, 200, 200), kdb.Box(15, 15, 45, 45)]
        )
    assert output.read_bytes() == b"existing output"


def test_exact_path_coarsening_preserves_drawn_region_and_extensions(
    tmp_path, both_pdk_root
):
    import klayout.db as kdb

    path = kdb.Path([kdb.Point(1000, 0), kdb.Point(1000, 4000)], 1000, 12, 20)
    _compose_fine_macro(tmp_path, both_pdk_root, [path])
    output = kdb.Layout()
    output.read(str(tmp_path / "out.gds"))
    assert output.dbu == pytest.approx(0.001)
    [shape] = list(output.cell("macro__macro_top").shapes(output.layer(34, 0)).each())
    assert shape.is_path()
    rescaled = shape.path.transformed(kdb.ICplxTrans(2))
    assert (kdb.Region(path.polygon()) ^ kdb.Region(rescaled.polygon())).is_empty()
    assert (rescaled.bgn_ext, rescaled.end_ext) == (12, 20)


def _compose_single_external_cell(tmp_path, pdk_root, source_gds, output_gds):
    return compose(
        {
            "pdk": {"variant": "gf180mcuD", "root": str(pdk_root)},
            "blocks": [
                {
                    "id": "macro",
                    "cell": {"gds_path": str(source_gds), "cell_name": "macro_top"},
                }
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["macro"],
                "origins_um": {"macro": {"x": 0.0, "y": 0.0}},
            },
            "options": {"cell_name": "composed", "output": str(output_gds)},
        }
    )


def test_near_integer_dbu_ratio_is_refused_while_exact_reciprocal_round_trips(
    tmp_path, both_pdk_root
):
    """A near-integer ratio such as ``0.001 / 0.0003333333 == 3.0000003`` would
    turn a 30,000,000-dbu (9,999.999 um) box into 10,000,000 dbu at 0.001 um
    (10,000.000 um); only true stream/float noise around ``1 / 3000`` is
    accepted as exact integer ratio 3."""
    import klayout.db as kdb

    near_gds = tmp_path / "near_third.gds"
    near = kdb.Layout()
    near.dbu = 0.0003333333
    near.create_cell("macro_top").shapes(near.layer(34, 0)).insert(
        kdb.Box(0, 0, 30_000_000, 3_000)
    )
    near.write(str(near_gds))

    refused_out = tmp_path / "refused.gds"
    refused_out.write_bytes(b"untouched")
    with pytest.raises(GenComposeError, match=r"'macro'.*does not evenly divide"):
        _compose_single_external_cell(tmp_path, both_pdk_root, near_gds, refused_out)
    assert refused_out.read_bytes() == b"untouched"

    exact_gds = tmp_path / "exact_third.gds"
    exact = kdb.Layout()
    exact.dbu = 1.0 / 3000.0
    exact.create_cell("macro_top").shapes(exact.layer(34, 0)).insert(
        kdb.Box(0, 0, 30_000_000, 3_000)
    )
    exact.write(str(exact_gds))

    ok_out = tmp_path / "ok.gds"
    report = _compose_single_external_cell(tmp_path, both_pdk_root, exact_gds, ok_out)
    assert report["dbu_um"] == pytest.approx(0.001)
    assert any("exact integer ratio 3" in w for w in report["warnings"])

    written = kdb.Layout()
    written.read(str(ok_out))
    assert written.dbu == pytest.approx(0.001)
    [shape] = list(written.cell("macro__macro_top").shapes(written.layer(34, 0)).each())
    assert shape.box.width() == 10_000_000
    assert shape.box.width() * written.dbu == pytest.approx(10_000.0, abs=1e-9)


@pytest.mark.parametrize("near_dbu", [0.0009999999998, 0.0010000000002])
def test_near_equal_dbu_outside_float_noise_is_refused(
    tmp_path, both_pdk_root, near_dbu
):
    """The ratio-1 early return must use the same stream/float-noise bound as
    the coarsening ratio check rather than accepting a distinct dbu such as
    ``0.0009999999998`` silently."""
    import klayout.db as kdb

    src_gds = tmp_path / "near_one.gds"
    lay = kdb.Layout()
    lay.dbu = near_dbu
    lay.create_cell("macro_top").shapes(lay.layer(34, 0)).insert(
        kdb.Box(0, 0, 1000, 1000)
    )
    lay.write(str(src_gds))

    output = tmp_path / "out.gds"
    output.write_bytes(b"untouched")
    with pytest.raises(GenComposeError, match=r"'macro'.*does not evenly divide"):
        _compose_single_external_cell(tmp_path, both_pdk_root, src_gds, output)
    assert output.read_bytes() == b"untouched"


def test_stream_encoding_noise_at_ratio_1_composes_without_warning(
    tmp_path, both_pdk_root
):
    """GDSII hex-float vs OASIS IEEE-754 encoding of ``1 / 9000`` differs by
    ~1 ULP (~1.2e-16 relative); composing the two at ratio 1 succeeds with no
    rescale warning."""
    import klayout.db as kdb

    gds_path = tmp_path / "a.gds"
    oas_path = tmp_path / "b.oas"
    for path, cell_name in ((gds_path, "cell_a"), (oas_path, "cell_b")):
        lay = kdb.Layout()
        lay.dbu = 1.0 / 9000.0
        lay.create_cell(cell_name).shapes(lay.layer(67, 20)).insert(
            kdb.Box(0, 0, 9000, 9000)
        )
        lay.write(str(path))

    out = tmp_path / "composed.oas"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(both_pdk_root)},
            "blocks": [
                {"id": "a", "cell": {"gds_path": str(gds_path), "cell_name": "cell_a"}},
                {"id": "b", "cell": {"gds_path": str(oas_path), "cell_name": "cell_b"}},
            ],
            "placement": {"strategy": "row", "order": ["a", "b"], "spacing_um": 1.0},
            "options": {"cell_name": "composed", "output": str(out)},
        }
    )
    assert not any("rescaled" in w for w in report["warnings"]), report["warnings"]


def test_coarsening_validates_stored_text_size_and_preserves_on_grid_height(
    tmp_path, both_pdk_root
):
    """An on-grid text anchor with an odd ``Text.size`` (101 dbu = 50.5 nm at
    0.0005 um) would round to 51 dbu (51 nm) at 0.001 um; the gate refuses
    naming block, layer, and size while leaving destination untouched, and
    preserves an on-grid size (100 dbu = 50 nm -> 50 dbu at 0.001 um)."""
    import klayout.db as kdb

    output = tmp_path / "out.gds"
    output.write_bytes(b"existing output")
    off_grid_label = kdb.Text("NET", kdb.Trans(0, False, 0, 0), 101, 0)
    with pytest.raises(GenComposeError, match=r"'macro'.*layer 34/0.*text size 101"):
        _compose_fine_macro(tmp_path, both_pdk_root, [off_grid_label])
    assert output.read_bytes() == b"existing output"

    output.unlink()
    on_grid_label = kdb.Text("NET", kdb.Trans(0, False, 0, 0), 100, 0)
    _compose_fine_macro(tmp_path, both_pdk_root, [on_grid_label])
    written = kdb.Layout()
    written.read(str(output))
    assert written.dbu == pytest.approx(0.001)
    [shape] = list(written.cell("macro__macro_top").shapes(written.layer(34, 0)).each())
    assert shape.is_text()
    assert shape.text.size == 50
    assert shape.text.size * written.dbu == pytest.approx(0.050)

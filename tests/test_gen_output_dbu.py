"""`klt gen`'s output stream dbu (issues #5721/#1496) -- the family's
required **stream** dbu when it declares one (gf180mcu's native runset
rejects any dbu but 0.001), else the tech LEF `DATABASE MICRONS` declaration
the same way `klt place_and_route`'s DEF/GDS merge already does (#1032) --
rather than always hard-coding 0.001. Falls back to the historical 0.001
default when neither resolves. Split out of `tests/test_gen.py` for that
file's file-size ratchet (issue #2766), alongside the implementation in
`klayout_tools/pdk_output_dbu.py`."""

import pytest

from klayout_tools import pdk
from klayout_tools.drc import run_drc
from klayout_tools.gen import generate
from klayout_tools.pdk_output_dbu import resolve_output_dbu


def _make_install(root, variant):
    """Fabricate a minimal open_pdks-layout variant (just the layout probe)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    return variant_dir


def _write_gen_dbu_tech_lef(variant_dir, cell_library, corner, database_microns):
    """Write `libs.ref/<cell_library>/techlef/<cell_library>__<corner>.tlef`
    declaring only `DATABASE MICRONS <database_microns>` -- the dbu
    resolution needs no layer content at all, just the `UNITS` block."""
    techlef_dir = variant_dir / "libs.ref" / cell_library / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / f"{cell_library}__{corner}.tlef").write_text(
        f"UNITS\n  DATABASE MICRONS {database_microns} ;\nEND UNITS\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# `generate()` resolves its output dbu from the target PDK -- the family's
# required **stream** dbu when it declares one (issue #5721: gf180mcu's
# native runset rejects any dbu but 0.001), else the tech LEF `DATABASE
# MICRONS` declaration (issue #1496) the same way `klt place_and_route`'s
# DEF/GDS merge already does (#1032) -- rather than always hard-coding 0.001.
# Falls back to the historical 0.001 default when neither resolves -- see
# `pdk_output_dbu.resolve_output_dbu`'s own docstring for the resolution order.
# --------------------------------------------------------------------------- #


def _merged_geometry_um(gds_path):
    """Every drawn layer's merged polygon set, expressed in *micrometres* --
    `{(layer, datatype): (x0, y0, x1, y1, area_um2, polygon_count)}`, with the
    micrometre values rounded to 1e-9um so two streams written at different
    dbu (0.001 vs 0.0005) can be compared for drawn-*geometry* equality
    rather than integer-coordinate equality (the whole point of #1496: a
    finer grid changes the integers, never the shapes)."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    dbu = layout.dbu
    geometry = {}
    for index in layout.layer_indexes():
        info = layout.get_info(index)
        region = kdb.Region(layout.top_cell().begin_shapes_rec(index)).merged()
        bbox = region.bbox()
        geometry[(info.layer, info.datatype)] = (
            round(bbox.left * dbu, 9),
            round(bbox.bottom * dbu, 9),
            round(bbox.right * dbu, 9),
            round(bbox.top * dbu, 9),
            round(region.area() * dbu * dbu, 9),
            region.count(),
        )
    return geometry


def _write_gen_dbu_tech_lef(variant_dir, cell_library, corner, database_microns):
    """Write `libs.ref/<cell_library>/techlef/<cell_library>__<corner>.tlef`
    declaring only `DATABASE MICRONS <database_microns>` -- mirrors
    `test_pdk.py`'s `_write_tech_lef` fixture shape, kept local to this file
    since `generate()`'s dbu resolution needs no layer content at all, just
    the `UNITS` block `pdk.resolve_pdk_dbu` reads."""
    techlef_dir = variant_dir / "libs.ref" / cell_library / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / f"{cell_library}__{corner}.tlef").write_text(
        f"UNITS\n  DATABASE MICRONS {database_microns} ;\nEND UNITS\n",
        encoding="utf-8",
    )


def test_generate_writes_pdk_required_stream_dbu_despite_disagreeing_tech_lef(
    tmp_path,
):
    """The core #5721 contract: a resolved gf180mcu PDK whose tech LEF
    declares `DATABASE MICRONS 2000` still produces a GDS at the family's
    required **stream** dbu 0.001 -- the native runset's `DBU` rule is an
    exact-equality check that rejects a 0.0005 stream whole-cell -- while the
    tech LEF's own truth stays readable as 0.0005 via `pdk.resolve_pdk_dbu`
    (its `DATABASE MICRONS` is DEF-reader config, not stream config)."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "gf180mcuD")
    _write_gen_dbu_tech_lef(
        variant_dir, "gf180mcu_fd_sc_mcu9t5v0", "nom", database_microns=2000
    )
    output = tmp_path / "guard_ring.gds"

    info = pdk.find_pdk(variant="gf180mcuD", root=str(root))
    assert pdk.resolve_pdk_dbu(info) == pytest.approx(0.0005)
    assert resolve_output_dbu(info) == pytest.approx(0.001)

    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "gf180mcuD", "root": str(root)},
            "options": {"output": str(output)},
        }
    )

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    assert layout.dbu == pytest.approx(0.001)
    # The response reports the written dbu too, so a caller can confirm two
    # blocks agree before handing them to `klt gen-compose` without a
    # separate `klt stats` round-trip (issue #1496).
    assert report["dbu_um"] == pytest.approx(0.001)


@pytest.mark.parametrize(
    "generator_name",
    (
        "mos_array",
        "res_array",
        "guard_ring",
        "diff_pair",
        "bjt_array",
        "bond_pad",
        "well_island",
    ),
)
def test_every_generator_writes_required_stream_dbu_on_gf180mcu(
    generator_name, tmp_path
):
    """AC coverage for #5721 across every generator a gf180mcu caller can
    reach at default params: each reports `dbu_um: 0.001` and writes a
    stream at dbu 0.001 -- even against an install whose tech LEF declares
    the disagreeing `DATABASE MICRONS 2000`. Writing at 0.001 is exact by
    construction (`_GRID_DBU_UM` pre-snaps every coordinate to 0.001 µm), so
    no drawn coordinate needs half-nanometre resolution."""
    root = tmp_path / "pdk_install"
    variant_dir = _make_install(root, "gf180mcuD")
    _write_gen_dbu_tech_lef(
        variant_dir, "gf180mcu_fd_sc_mcu9t5v0", "nom", database_microns=2000
    )
    output = tmp_path / f"{generator_name}.gds"

    report = generate(
        {
            "generator": generator_name,
            "pdk": {"variant": "gf180mcuD", "root": str(root)},
            "options": {"output": str(output)},
        }
    )

    import klayout.db as kdb

    assert report["dbu_um"] == pytest.approx(0.001)
    layout = kdb.Layout()
    layout.read(str(output))
    assert layout.dbu == pytest.approx(0.001)


def test_generate_sky130_fallthrough_writes_tech_lef_dbu(tmp_path):
    """The fall-through half of #5721: sky130 declares no family stream
    requirement, so its output dbu still comes from the tech LEF -- a LEF
    declaring `DATABASE MICRONS 2000` writes 0.0005 (the pre-#5721
    #1496 behaviour, unchanged for every family with no declared
    requirement) -- and the *drawn micrometre* geometry is identical to the
    same generator's 0.001-dbu (LEF 1000) output: a finer grid changes the
    integer coordinates, never the shapes."""
    coarse_root = tmp_path / "coarse"
    _write_gen_dbu_tech_lef(
        _make_install(coarse_root, "sky130A"),
        "sky130_fd_sc_hd",
        "tt",
        database_microns=1000,
    )
    fine_root = tmp_path / "fine"
    _write_gen_dbu_tech_lef(
        _make_install(fine_root, "sky130A"),
        "sky130_fd_sc_hd",
        "tt",
        database_microns=2000,
    )

    def _run(root, output_name):
        return generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "sky130A", "root": str(root)},
                "options": {"output": str(tmp_path / output_name)},
            }
        )

    coarse = _run(coarse_root, "coarse.gds")
    fine = _run(fine_root, "fine.gds")

    assert coarse["dbu_um"] == pytest.approx(0.001)
    assert fine["dbu_um"] == pytest.approx(0.0005)
    assert _merged_geometry_um(tmp_path / "coarse.gds") == _merged_geometry_um(
        tmp_path / "fine.gds"
    ), "sky130 fall-through: drawn geometry differs between 0.001 and 0.0005 dbu"


def test_generate_bbox_um_is_reported_at_the_required_stream_dbu(tmp_path):
    """`bbox_um` is derived from the layout's integer bbox times its dbu --
    resolving the output dbu (here: the same 0.001 family requirement on both
    sides, over two *different* tech-LEF `DATABASE MICRONS` declarations)
    must not scale the *micrometre* geometry, only the integer grid it is
    stored on. The ring's default 10x10um inner region is the same size
    whichever integer grid backs it."""
    coarse_root = tmp_path / "coarse"
    _write_gen_dbu_tech_lef(
        _make_install(coarse_root, "gf180mcuD"),
        "gf180mcu_fd_sc_mcu9t5v0",
        "nom",
        database_microns=1000,
    )
    fine_root = tmp_path / "fine"
    _write_gen_dbu_tech_lef(
        _make_install(fine_root, "gf180mcuD"),
        "gf180mcu_fd_sc_mcu9t5v0",
        "nom",
        database_microns=2000,
    )

    def _run(root, output_name):
        return generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "gf180mcuD", "root": str(root)},
                "options": {"output": str(tmp_path / output_name)},
            }
        )

    coarse = _run(coarse_root, "coarse.gds")
    fine = _run(fine_root, "fine.gds")

    # Both writes land on the family's required stream dbu -- the LEF
    # declarations above are DEF-reader truth, not stream truth (#5721).
    assert coarse["dbu_um"] == pytest.approx(0.001)
    assert fine["dbu_um"] == pytest.approx(0.001)
    for key in ("x0", "y0", "x1", "y1"):
        assert fine["bbox_um"][key] == pytest.approx(coarse["bbox_um"][key])


_PHASE2_GENERATORS = ("mos_array", "res_array", "guard_ring", "diff_pair")


@pytest.fixture()
def both_pdk_root(tmp_path):
    """Both PDK families the phase-2 generators support (see
    `klayout_tools.gen._PDK_ROLE_LAYERS`), under one install root -- the
    same shape as `tests/test_gen.py`'s fixture of the same name, kept local
    here for the moved tests."""
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    _make_install(root, "gf180mcuD")
    return root


@pytest.mark.parametrize("generator_name", _PHASE2_GENERATORS)
def test_phase2_generator_stays_drc_clean_at_pdk_required_stream_dbu(
    generator_name, tmp_path, both_pdk_root
):
    """The geometry-correctness half of #5721: writing at the gf180mcu
    family's required stream dbu (0.001 -- what its native runset's exact
    `DBU` rule demands, #5721) must not corrupt any generator's geometry.
    Every phase-2 generator's documented default `params` stays
    `klt drc --deck gf180mcu` clean at that dbu, and the *drawn micrometre*
    geometry is identical to the same generator's pre-#5721 0.0005-dbu build
    (reproduced here by the same #1496-era mechanism on a family with no
    declared requirement: a sky130A install whose tech LEF declares
    `DATABASE MICRONS 2000` still writes 0.0005 via the fall-through). A
    different grid changes the integer coordinates, never the shapes."""
    techlef_dir = (
        both_pdk_root / "gf180mcuD" / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "techlef"
    )
    required = tmp_path / f"{generator_name}_required.gds"
    generate(
        {
            "generator": generator_name,
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "options": {"output": str(required)},
        }
    )

    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / "gf180mcu_fd_sc_mcu9t5v0__nom.tlef").write_text(
        "UNITS\n  DATABASE MICRONS 2000 ;\nEND UNITS\n", encoding="utf-8"
    )
    report = generate(
        {
            "generator": generator_name,
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "options": {"output": str(tmp_path / f"{generator_name}_lef2000.gds")},
        }
    )
    # The disagreeing tech LEF is read (0.0005, as `resolve_pdk_dbu` reports)
    # but does not move the stream off the family's required dbu (#5721).
    info = pdk.find_pdk(variant="gf180mcuD", root=str(both_pdk_root))
    assert pdk.resolve_pdk_dbu(info) == pytest.approx(0.0005)
    assert report["dbu_um"] == pytest.approx(0.001)

    drc_report = run_drc(str(tmp_path / f"{generator_name}_lef2000.gds"), "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]

    assert _merged_geometry_um(required) == _merged_geometry_um(
        tmp_path / f"{generator_name}_lef2000.gds"
    ), f"{generator_name}: drawn geometry differs between required and LEF-resolved dbu"


# --------------------------------------------------------------------------- #
# `examples/dogbone-terminal/` (issue #1574) -- the documented, hand-drawn
# recipe for a unit device below `UNIT_MIN_W_UM`, checked in as a worked
# example rather than a generator param. See that directory's README and
# `docs/cli/gen.md`'s "Below `UNIT_MIN_W_UM`: dog-bone terminals drawn by
# hand".
# --------------------------------------------------------------------------- #

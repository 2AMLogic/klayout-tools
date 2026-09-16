"""Tests for standard-cell library device flavor/voltage-domain queries and
Liberty-corner resolution (`klayout_tools.pdk_cells` + `klt pdk cells`).

Split out of ``tests/test_pdk.py`` alongside the ``klt pdk cells`` region's
move into :mod:`klayout_tools.pdk_cells` (issue #1884) -- every test below
that previously called ``pdk.list_cell_libraries`` / ``pdk.list_lib_corners``
/ ``pdk.resolve_liberty_for_cell_library`` now calls the same function via
``pdk_cells.<name>`` instead; no test behavior changed. As in
``tests/test_pdk.py``, every test runs against **fabricated** open_pdks-
layout installs created under ``tmp_path`` -- CI never downloads a real PDK.
"""

import json
import re

import pytest

from klayout_tools import pdk, pdk_cells
from klayout_tools.cli import main


def _make_install(root, variant, *, sources=None, assets=("ngspice",)):
    """Fabricate an open_pdks-layout variant under ``root``.

    Creates ``root/<variant>/libs.tech`` (the layout probe) plus a ``libs.tech``
    subdir per requested asset (or ``libs.ref`` for ``libs_ref``), and an
    optional ``SOURCES`` version stamp. Returns the variant directory.
    """
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    for asset in assets:
        if asset == "libs_ref":
            (variant_dir / "libs.ref").mkdir(parents=True, exist_ok=True)
        else:
            (variant_dir / "libs.tech" / asset).mkdir(parents=True, exist_ok=True)
    if sources is not None:
        (variant_dir / "SOURCES").write_text(sources, encoding="utf-8")
    return variant_dir


def _make_cell_library(
    variant_dir,
    name,
    *,
    devices=("nfet_01v8", "pfet_01v8_hvt"),
    family="sky130",
    corners=(("tt_025C_1v80", 1.0, 25.0, 1.8),),
    with_spice=True,
    with_lib=True,
    bare_device_names=False,
    prefixed_operating_conditions=False,
):
    """Fabricate a `libs.ref/<name>` entry with `spice/`/`lib/` views.

    ``devices`` are nfet/pfet flavor suffixes embedded in one synthetic SPICE
    instance line each, matching real ``X<n> ... <model> w=... l=...``
    instance lines. By default (``bare_device_names=False``) the model is
    prefixed ``<family>_fd_pr__`` -- sky130's shape (`sky130_fd_pr__nfet_01v8`).
    ``bare_device_names=True`` omits the prefix entirely -- gf180mcu's shape
    (`nfet_06v0`), see issue #537. ``corners`` is an iterable of
    ``(corner_name, nom_process, nom_temperature, nom_voltage)`` tuples, one
    `.lib` file per entry. ``with_spice``/``with_lib`` let a test omit either
    view to exercise the missing-view fallback.

    ``prefixed_operating_conditions=True`` writes the `.lib` file's
    `default_operating_conditions` attribute as `f"{name}__{corner_name}"`
    instead of the bare ``corner_name`` -- gf180mcu_fd_sc_mcu9t5v0's real
    shape (issue #820), where the vendor's own Liberty attribute already
    carries the `<cell_library>__` prefix that sky130's files omit. The
    on-disk filename is unaffected (still `<name>__<corner_name>.lib`).
    """
    lib_dir = variant_dir / "libs.ref" / name
    if with_spice:
        spice_dir = lib_dir / "spice"
        spice_dir.mkdir(parents=True)
        if bare_device_names:
            lines = [
                f"X{i} a b c d {device} w=1u l=1u" for i, device in enumerate(devices)
            ]
        else:
            lines = [
                f"X{i} a b c d {family}_fd_pr__{device} w=1u l=1u"
                for i, device in enumerate(devices)
            ]
        (spice_dir / f"{name}.spice").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    if with_lib:
        lib_views_dir = lib_dir / "lib"
        lib_views_dir.mkdir(parents=True)
        for corner_name, process, temperature, voltage in corners:
            operating_conditions = (
                f"{name}__{corner_name}"
                if prefixed_operating_conditions
                else corner_name
            )
            content = (
                f'    default_operating_conditions : "{operating_conditions}";\n'
                f"    nom_process : {process};\n"
                f"    nom_temperature : {temperature};\n"
                f"    nom_voltage : {voltage};\n"
            )
            (lib_views_dir / f"{name}__{corner_name}.lib").write_text(
                content, encoding="utf-8"
            )
    lib_dir.mkdir(parents=True, exist_ok=True)
    return lib_dir


def _make_ihp_stdcell_library(
    variant_dir,
    name="sg13g2_stdcell",
    *,
    devices=("nfet",),
    corners=(("typ_1p20V_25C", 1.0, 25.0, 1.2),),
    with_spice=True,
    with_lib=True,
    with_lef=True,
    tech_lef_filename="sg13g2_tech.lef",
):
    """Fabricate a `libs.ref/<name>` entry mirroring IHP-Open-PDK's real
    `sg13g2_stdcell` shape (issue #1790, verified live against a real
    fetched v0.3.0 install):

    - Single-underscore `lib/` naming (`f"{name}_{corner}.lib"`), with
      `default_operating_conditions` written as the full stem too -- IHP's
      own `.lib` files embed the library name in that attribute, unlike
      sky130's bare form.
    - A `lef/` directory with **no** `techlef/` subdirectory at all -- just
      the merged cell LEF (`f"{name}.lef"`) alongside a single,
      corner-invariant tech LEF named after the *process*, not the library
      (``tech_lef_filename``, default `sg13g2_tech.lef`, matching IHP's real
      mismatch between the tech LEF's filename and `cell_library`'s name).
    """
    lib_dir = variant_dir / "libs.ref" / name
    if with_spice:
        spice_dir = lib_dir / "spice"
        spice_dir.mkdir(parents=True)
        lines = [f"X{i} a b c d {device} w=1u l=1u" for i, device in enumerate(devices)]
        (spice_dir / f"{name}.spice").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    if with_lib:
        lib_views_dir = lib_dir / "lib"
        lib_views_dir.mkdir(parents=True, exist_ok=True)
        for corner_name, process, temperature, voltage in corners:
            operating_conditions = f"{name}_{corner_name}"
            content = (
                f'    default_operating_conditions : "{operating_conditions}";\n'
                f"    nom_process : {process};\n"
                f"    nom_temperature : {temperature};\n"
                f"    nom_voltage : {voltage};\n"
            )
            (lib_views_dir / f"{name}_{corner_name}.lib").write_text(
                content, encoding="utf-8"
            )
    if with_lef:
        lef_dir = lib_dir / "lef"
        lef_dir.mkdir(parents=True, exist_ok=True)
        (lef_dir / f"{name}.lef").write_text("# merged cell lef\n")
        (lef_dir / tech_lef_filename).write_text("# tech lef\n")
    lib_dir.mkdir(parents=True, exist_ok=True)
    return lib_dir


def _make_macro_library(variant_dir, name, *, views=("gds", "lef", "lib", "spice")):
    """Fabricate a `libs.ref/<name>` hard-macro IP entry with the given view
    subdirectories present (each containing one placeholder file, mirroring
    a real SRAM/ROM-compiler-output library's shape)."""
    lib_dir = variant_dir / "libs.ref" / name
    lib_dir.mkdir(parents=True, exist_ok=True)
    for view in views:
        view_dir = lib_dir / view
        view_dir.mkdir(parents=True, exist_ok=True)
        (view_dir / f"{name}.{view}").write_text("", encoding="utf-8")
    return lib_dir


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Scrub PDK env vars, redirect HOME, and empty the host search space.

    Individual tests opt back into stores/prefixes by setting the module
    constants (or ``$PDK_ROOT``) to controlled ``tmp_path`` locations.
    """
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


# --------------------------------------------------------------------------- #
# list_lib_corners (issue #949 -- every shipped `.lib` timing corner for a
# resolved cell library, not only the nominal pick -- feeds
# `place_and_route.py`'s post-route setup/hold corner sweep).
# --------------------------------------------------------------------------- #


def test_list_lib_corners_enumerates_every_shipped_file(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "sky130_fd_sc_hd",
        corners=(
            ("tt_025C_1v80", 1.0, 25.0, 1.8),
            ("ss_100C_1v60", 1.0, 100.0, 1.6),
            ("ff_n40C_1v95", 1.0, -40.0, 1.95),
        ),
    )
    pdk_info = pdk.find_pdk(root=str(root))

    corners = pdk_cells.list_lib_corners("sky130_fd_sc_hd", pdk_info)

    # Sorted by filename, not insertion order.
    assert [c["name"] for c in corners] == [
        "ff_n40C_1v95",
        "ss_100C_1v60",
        "tt_025C_1v80",
    ]
    lib_dir = variant_dir / "libs.ref" / "sky130_fd_sc_hd" / "lib"
    for entry in corners:
        assert entry["path"] == str(lib_dir / f"sky130_fd_sc_hd__{entry['name']}.lib")


def test_list_lib_corners_excludes_ccsnoise_variant(tmp_path):
    """A `_ccsnoise` `.lib` view shares its non-suffixed sibling's exact
    `default_operating_conditions` PVT point (sky130's own noise-model-
    augmented view convention) -- loading both into the same OpenSTA session
    raises `[WARNING STA-1140] ... library <name> already exists` (live-
    verified against a real `openroad/orfs:latest` container, issue #949),
    so `list_lib_corners` excludes it rather than enumerating it as a second,
    colliding corner."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "sky130_fd_sc_hd",
        corners=(("ff_n40C_1v95", 1.0, -40.0, 1.95),),
    )
    lib_views_dir = variant_dir / "libs.ref" / "sky130_fd_sc_hd" / "lib"
    (lib_views_dir / "sky130_fd_sc_hd__ff_n40C_1v95_ccsnoise.lib").write_text(
        '    default_operating_conditions : "ff_n40C_1v95";\n'
        "    nom_process : 1.0;\n"
        "    nom_temperature : -40.0;\n"
        "    nom_voltage : 1.95;\n",
        encoding="utf-8",
    )
    pdk_info = pdk.find_pdk(root=str(root))

    corners = pdk_cells.list_lib_corners("sky130_fd_sc_hd", pdk_info)

    assert [c["name"] for c in corners] == ["ff_n40C_1v95"]


def test_list_lib_corners_name_from_filename_not_operating_conditions(tmp_path):
    """`name` is always derived from the file's own filename, never the
    `default_operating_conditions` Liberty attribute -- gf180mcu's own
    doubled-prefix convention (issue #820, `prefixed_operating_conditions`)
    must not leak into the returned tag."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuD", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "gf180mcu_fd_sc_mcu9t5v0",
        corners=(("tt_025C_5v00", 1.0, 25.0, 5.0),),
        prefixed_operating_conditions=True,
    )
    pdk_info = pdk.find_pdk(root=str(root))

    corners = pdk_cells.list_lib_corners("gf180mcu_fd_sc_mcu9t5v0", pdk_info)

    assert corners == [
        {
            "name": "tt_025C_5v00",
            "path": str(
                variant_dir
                / "libs.ref"
                / "gf180mcu_fd_sc_mcu9t5v0"
                / "lib"
                / "gf180mcu_fd_sc_mcu9t5v0__tt_025C_5v00.lib"
            ),
        }
    ]


def test_list_lib_corners_empty_when_no_lib_dir(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd", with_lib=False)
    pdk_info = pdk.find_pdk(root=str(root))

    assert pdk_cells.list_lib_corners("sky130_fd_sc_hd", pdk_info) == []


def test_list_lib_corners_empty_when_no_libs_ref(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("ngspice",))  # no libs_ref at all
    pdk_info = pdk.find_pdk(root=str(root))

    assert pdk_cells.list_lib_corners("sky130_fd_sc_hd", pdk_info) == []


# --------------------------------------------------------------------------- #
# list_cell_libraries (`klt pdk cells`)
# --------------------------------------------------------------------------- #


def test_cells_reports_device_flavors_and_nominal_supply(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "sky130_fd_sc_hd",
        devices=("nfet_01v8", "pfet_01v8_hvt"),
        corners=(("tt_025C_1v80", 1.0, 25.0, 1.8),),
    )

    report = pdk_cells.list_cell_libraries(root=str(root))

    assert report["schema_version"] == 1
    assert report["pdk"] == "sky130A"
    assert len(report["libraries"]) == 1
    library = report["libraries"][0]
    assert library["name"] == "sky130_fd_sc_hd"
    assert library["device_flavors"] == ["nfet_01v8", "pfet_01v8_hvt"]
    assert library["nominal_supply_v"] == 1.8
    assert library["nominal_corner"] == "tt_025C_1v80"
    assert library["voltage_class"] == "core"


def test_cells_high_voltage_library_classified_io(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "sky130_fd_sc_hvl",
        devices=("nfet_g5v0d10v5", "pfet_g5v0d10v5", "nfet_05v0_nvt"),
        corners=(("tt_025C_2v64_lv1v80", 1.0, 25.0, 2.64),),
    )

    report = pdk_cells.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["nominal_supply_v"] == 2.64
    assert library["voltage_class"] == "io"


def test_cells_multi_corner_picks_lowest_nominal_voltage(tmp_path):
    """A library characterised at more than one supply for the typical-process,
    room-temperature corner (a split/multi-rail library, mirroring
    sky130_fd_sc_hvl's real 2.64V/2.97V/3.3V `tt_025C` views) reports the
    lowest voltage as its nominal supply -- not the alphabetically-first file,
    not an arbitrary pick.
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "sky130_fd_sc_hvl",
        corners=(
            ("tt_025C_3v30", 1.0, 25.0, 3.30),
            ("tt_025C_2v64_lv1v80", 1.0, 25.0, 2.64),
            ("tt_025C_2v97_lv1v80", 1.0, 25.0, 2.97),
            ("ff_100C_5v50", 1.0, 100.0, 5.50),  # not room temp -- excluded
        ),
    )

    report = pdk_cells.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["nominal_supply_v"] == 2.64
    assert library["nominal_corner"] == "tt_025C_2v64_lv1v80"
    # All three room-temp/typical-process supplies surface in the full set --
    # not just the lowest-wins nominal pick above.
    assert library["supplies_v"] == [2.64, 2.97, 3.30]


def test_cells_bare_device_name_reports_device_flavors_gf180mcu_shape(tmp_path):
    """gf180mcu's SPICE instance lines name the device model as the bare
    flavor (e.g. `nfet_06v0`) with no `<family>_fd_pr__` prefix at all --
    unlike sky130's `sky130_fd_pr__nfet_01v8`. `_device_flavors()` must
    recognize both shapes (issue #537).
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuD", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "gf180mcu_fd_sc_mcu9t5v0",
        devices=("nfet_06v0", "pfet_06v0"),
        bare_device_names=True,
        corners=(("tt_025C_5v00", 1.0, 25.0, 5.0),),
    )

    report = pdk_cells.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["device_flavors"] == ["nfet_06v0", "pfet_06v0"]


def test_cells_reports_every_characterised_supply_gf180mcu_shape(tmp_path):
    """A library fully, separately characterised at multiple voltages (e.g.
    gf180mcu_fd_sc_mcu9t5v0's real 1.8V/3.3V/5.0V split) reports **every**
    supply in ``supplies_v``, not just the lowest (issue #537).
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuD", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "gf180mcu_fd_sc_mcu9t5v0",
        devices=("nfet_06v0", "pfet_06v0"),
        bare_device_names=True,
        corners=(
            ("tt_025C_1v80", 1.0, 25.0, 1.8),
            ("tt_025C_3v30", 1.0, 25.0, 3.3),
            ("tt_025C_5v00", 1.0, 25.0, 5.0),
        ),
    )

    report = pdk_cells.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["supplies_v"] == [1.8, 3.3, 5.0]
    # Backward-compatible single-value field still reports the lowest.
    assert library["nominal_supply_v"] == 1.8
    assert library["nominal_corner"] == "tt_025C_1v80"


def test_cells_nominal_corner_strips_doubled_library_prefix_gf180mcu_shape(tmp_path):
    """gf180mcu_fd_sc_mcu9t5v0's `.lib` files write their own
    `<cell_library>__` prefix into `default_operating_conditions` (e.g.
    `gf180mcu_fd_sc_mcu9t5v0__tt_025C_1v80`), unlike sky130's files, where
    the same attribute (or the filename-stem fallback) is already bare.
    `nominal_corner` must always report the bare corner regardless -- a
    naive pass-through doubles the prefix when a consumer (`synthesize`,
    `place_and_route`) later builds `<cell_library>__<corner>.lib` (issue
    #820).
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuD", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "gf180mcu_fd_sc_mcu9t5v0",
        devices=("nfet_06v0", "pfet_06v0"),
        bare_device_names=True,
        prefixed_operating_conditions=True,
        corners=(("tt_025C_1v80", 1.0, 25.0, 1.8),),
    )

    report = pdk_cells.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["nominal_corner"] == "tt_025C_1v80"
    assert not library["nominal_corner"].startswith("gf180mcu_fd_sc_mcu9t5v0__")


def test_cells_nominal_corner_strips_single_underscore_prefix_ihp_shape(tmp_path):
    """IHP-Open-PDK's `sg13g2_stdcell` `.lib` files write their own
    single-underscore `<cell_library>_` prefix into
    `default_operating_conditions` (e.g. `sg13g2_stdcell_typ_1p20V_25C`),
    distinct from both sky130's bare attribute and gf180mcu's
    double-underscore prefix. `nominal_corner` must report the bare corner
    here too -- otherwise `synthesize`/`place_and_route`'s single-underscore
    liberty-filename fallback would build a doubled-prefix path
    (`sg13g2_stdcell_sg13g2_stdcell_typ_1p20V_25C.lib`) that does not exist
    (issue #1790, verified live against a real fetched IHP-Open-PDK v0.3.0
    install)."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "ihp-sg13g2", assets=("libs_ref",))
    _make_ihp_stdcell_library(variant_dir, "sg13g2_stdcell", with_spice=False)

    report = pdk_cells.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["nominal_corner"] == "typ_1p20V_25C"
    assert not library["nominal_corner"].startswith("sg13g2_stdcell_")


def test_cells_supply_matches_against_full_supply_set_not_just_nominal(tmp_path):
    """`--supply` must match against every characterised supply, not just the
    single lowest `nominal_supply_v` -- a library characterised at 3.3V (in
    addition to a lower nominal pick) must report compatible with `--supply
    3.3` (issue #537, previously false-negatived).
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuD", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "gf180mcu_fd_sc_mcu9t5v0",
        devices=("nfet_06v0", "pfet_06v0"),
        bare_device_names=True,
        corners=(
            ("tt_025C_1v80", 1.0, 25.0, 1.8),
            ("tt_025C_3v30", 1.0, 25.0, 3.3),
            ("tt_025C_5v00", 1.0, 25.0, 5.0),
        ),
    )

    report = pdk_cells.list_cell_libraries(root=str(root), supply=3.3)

    assert report["any_compatible"] is True
    assert report["libraries"][0]["compatible"] is True


def test_cells_excludes_non_std_cell_libraries(tmp_path):
    """Only `_fd_sc_`-named entries are reported -- `_fd_io`/`_fd_pr`/macro
    entries are a deliberate exclusion (issue #147 acceptance criteria), not
    an accident of the glob used to walk `libs_ref`.
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")
    _make_cell_library(variant_dir, "sky130_fd_io", devices=("nfet_g5v0d10v5",))
    _make_cell_library(variant_dir, "sky130_sram_macros", devices=("nfet_01v8",))
    (variant_dir / "libs.ref" / "sky130_fd_pr").mkdir(parents=True)

    report = pdk_cells.list_cell_libraries(root=str(root))

    assert [lib["name"] for lib in report["libraries"]] == ["sky130_fd_sc_hd"]


def test_cells_includes_ihp_stdcell_naming(tmp_path):
    """`sg13g2_stdcell` (IHP-Open-PDK's real naming -- no `_fd_sc_` marker
    at all) is enumerated via the additive `_stdcell` marker, alongside a
    `_fd_sc_`-named library from a different install -- issue #1790's
    "additive, not a replacement" acceptance criterion."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "ihp-sg13g2", assets=("libs_ref",))
    _make_ihp_stdcell_library(variant_dir, "sg13g2_stdcell")

    report = pdk_cells.list_cell_libraries(root=str(root))

    assert [lib["name"] for lib in report["libraries"]] == ["sg13g2_stdcell"]
    library = report["libraries"][0]
    assert library["nominal_corner"] == "typ_1p20V_25C"
    assert library["nominal_supply_v"] == 1.2


def test_cells_ihp_stdcell_does_not_match_io_or_sram_siblings(tmp_path):
    """The `_stdcell` marker does not also sweep up IHP's I/O
    (`sg13g2_io`) or SRAM (`sg13g2_sram`) `libs_ref` entries -- both ship
    their own `lib/` timing views too, so only the name marker (not a
    "ships `lib/`" shape check) distinguishes the standard-cell library."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "ihp-sg13g2", assets=("libs_ref",))
    _make_ihp_stdcell_library(variant_dir, "sg13g2_stdcell")
    _make_ihp_stdcell_library(variant_dir, "sg13g2_io", devices=("nfet_io",))
    _make_ihp_stdcell_library(variant_dir, "sg13g2_sram", with_spice=False)

    report = pdk_cells.list_cell_libraries(root=str(root))

    assert [lib["name"] for lib in report["libraries"]] == ["sg13g2_stdcell"]


def test_cells_no_libs_ref_is_empty_list(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("ngspice",))

    report = pdk_cells.list_cell_libraries(root=str(root))

    assert report["libraries"] == []


def test_cells_missing_spice_view_yields_empty_device_flavors(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd", with_spice=False)

    report = pdk_cells.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["device_flavors"] == []
    assert library["device_flavors_status"] == "ok"
    assert library["nominal_supply_v"] == 1.8  # lib/ view still present


def test_cells_unknown_device_naming_reported_loudly(tmp_path):
    """A `spice/` view with SPICE instance lines that don't match any known
    device-naming shape must report `device_flavors_status: "unknown"` -- a
    loud signal distinguishable from "this library genuinely has no
    devices" (issue #537 acceptance criterion 4). `device_flavors` is still
    `[]` in this case -- only the status field distinguishes it.
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "sky130_fd_sc_hd",
        devices=("some_unrecognised_model",),
        bare_device_names=True,
    )

    report = pdk_cells.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["device_flavors"] == []
    assert library["device_flavors_status"] == "unknown"


def test_cells_no_instance_lines_is_ok_not_unknown(tmp_path):
    """A `spice/` view that exists but has zero SPICE instance lines is
    genuinely "no devices" -- `"ok"`, not `"unknown"` -- distinct from the
    "instance lines present but unrecognised" case above.
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    lib_dir = _make_cell_library(variant_dir, "sky130_fd_sc_hd", with_spice=False)
    (lib_dir / "spice").mkdir(parents=True)
    (lib_dir / "spice" / "sky130_fd_sc_hd.spice").write_text(
        "* no instance lines here\n", encoding="utf-8"
    )

    report = pdk_cells.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["device_flavors"] == []
    assert library["device_flavors_status"] == "ok"


def test_cells_missing_lib_view_yields_null_supply(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd", with_lib=False)

    report = pdk_cells.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["device_flavors"] == ["nfet_01v8", "pfet_01v8_hvt"]
    assert library["nominal_supply_v"] is None
    assert library["nominal_corner"] is None
    assert library["voltage_class"] is None


def test_cells_supply_adds_compatibility_verdict(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(
        variant_dir, "sky130_fd_sc_hd", corners=(("tt_025C_1v80", 1.0, 25.0, 1.8),)
    )
    _make_cell_library(
        variant_dir,
        "sky130_fd_sc_hvl",
        corners=(("tt_025C_2v64_lv1v80", 1.0, 25.0, 2.64),),
    )

    report = pdk_cells.list_cell_libraries(root=str(root), supply=1.8)

    assert report["supply_v"] == 1.8
    assert report["any_compatible"] is True
    by_name = {lib["name"]: lib for lib in report["libraries"]}
    assert by_name["sky130_fd_sc_hd"]["compatible"] is True
    assert by_name["sky130_fd_sc_hvl"]["compatible"] is False


def test_cells_supply_no_match_is_false(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(
        variant_dir, "sky130_fd_sc_hd", corners=(("tt_025C_1v80", 1.0, 25.0, 1.8),)
    )

    report = pdk_cells.list_cell_libraries(root=str(root), supply=5.0)

    assert report["any_compatible"] is False
    assert report["libraries"][0]["compatible"] is False


def test_cells_no_install_raises(tmp_path):
    with pytest.raises(pdk.PdkNotFoundError):
        pdk_cells.list_cell_libraries(root=str(tmp_path / "nope"))


# --------------------------------------------------------------------------- #
# CLI envelope conformance (via klt main()) -- `klt pdk cells`
# --------------------------------------------------------------------------- #


def test_cli_cells_json_on_stdout(tmp_path, capsys):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")

    exit_code = main(["pdk", "cells", "--pdk-root", str(root), "--format", "json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["pdk"] == "sky130A"
    assert payload["libraries"][0]["name"] == "sky130_fd_sc_hd"


def test_cli_cells_text_table(tmp_path, capsys):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")

    exit_code = main(["pdk", "cells", "--pdk-root", str(root)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "sky130_fd_sc_hd" in out
    assert "nfet_01v8/pfet_01v8_hvt" in out
    assert "1.8V @ tt_025C_1v80" in out


def test_cli_cells_text_table_reports_unknown_devices_not_dash(tmp_path, capsys):
    """The text table's `devices` column must render `unknown` (not `-`)
    when `device_flavors_status == "unknown"`, so a parse failure is not
    visually indistinguishable from a library that genuinely ships no
    devices (issue #537 acceptance criterion 4).
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "sky130_fd_sc_hd",
        devices=("some_unrecognised_model",),
        bare_device_names=True,
    )

    exit_code = main(["pdk", "cells", "--pdk-root", str(root)])

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "sky130_fd_sc_hd" in line]
    assert len(lines) == 1
    # devices is the second column -- must render "unknown", not "-".
    columns = re.split(r"\s{2,}", lines[0].strip())
    assert columns[1] == "unknown"


def test_cli_cells_supply_exit_zero_when_compatible(tmp_path, capsys):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")

    exit_code = main(["pdk", "cells", "--pdk-root", str(root), "--supply", "1.8"])

    assert exit_code == 0


def test_cli_cells_supply_exit_three_when_no_match(tmp_path, capsys):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")

    exit_code = main(["pdk", "cells", "--pdk-root", str(root), "--supply", "5.0"])

    assert exit_code == 3
    out = capsys.readouterr().out
    assert "NO MATCH" in out


def test_cli_cells_supply_exit_zero_for_non_nominal_characterised_voltage(
    tmp_path, capsys
):
    """A library separately characterised at multiple voltages (gf180mcu's
    1.8V/3.3V/5.0V shape) must report compatible -- and exit 0 -- for a
    `--supply` matching any of them, not only the lowest/nominal one (issue
    #537, previously exited 3 / "NO MATCH" for 3.3V despite the library
    being characterised at it).
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuD", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "gf180mcu_fd_sc_mcu9t5v0",
        devices=("nfet_06v0", "pfet_06v0"),
        bare_device_names=True,
        corners=(
            ("tt_025C_1v80", 1.0, 25.0, 1.8),
            ("tt_025C_3v30", 1.0, 25.0, 3.3),
            ("tt_025C_5v00", 1.0, 25.0, 5.0),
        ),
    )

    exit_code = main(["pdk", "cells", "--pdk-root", str(root), "--supply", "3.3"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "compatible library found" in out


def test_cli_cells_no_install_error_envelope(tmp_path, capsys):
    exit_code = main(
        [
            "pdk",
            "cells",
            "--pdk-root",
            str(tmp_path / "nope"),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "pdk cells"


def test_cells_excludes_fd_ip_hard_macro_library(tmp_path):
    """Regression test for issue #535: `klt pdk cells` must keep excluding
    `*_fd_ip_*` hard-macro IP libraries even though `klt pdk macros` now
    reports them -- the two commands' result sets do not overlap.
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")
    _make_macro_library(variant_dir, "sky130_fd_ip_sram_1k")

    report = pdk_cells.list_cell_libraries(root=str(root))

    assert [lib["name"] for lib in report["libraries"]] == ["sky130_fd_sc_hd"]


# --------------------------------------------------------------------------- #
# resolve_liberty_for_cell_library (issue #1652) -- the shared implementation
# behind synthesize._resolve_liberty / place_and_route._resolve_liberty /
# post_route_sta._resolve_liberty, which used to be three byte-identical
# (modulo which exception class each raised) copies of this same resolution.
# Basic behavior is covered by resolve_pdk_dbu-style unit tests here; the
# three verb modules' own test suites (test_synthesize.py,
# test_place_and_route.py, test_post_route_sta.py) separately cover each
# wrapper's own module-specific exception type end to end. The parity test
# below is the cross-module check: same inputs must resolve identically
# through all three wrappers, and each must still raise its own error class.
# --------------------------------------------------------------------------- #


def test_resolve_liberty_for_cell_library_resolves_nominal_corner(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")

    liberty_path, corner, info = pdk_cells.resolve_liberty_for_cell_library(
        "sky130_fd_sc_hd", None, ValueError, root=str(root)
    )

    assert corner == "tt_025C_1v80"
    assert liberty_path.endswith("sky130_fd_sc_hd__tt_025C_1v80.lib")
    assert info["variant"] == "sky130A"


def test_resolve_liberty_for_cell_library_ihp_single_underscore_fallback(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "ihp-sg13g2", assets=("libs_ref",))
    _make_ihp_stdcell_library(variant_dir, "sg13g2_stdcell", with_spice=False)

    liberty_path, corner, info = pdk_cells.resolve_liberty_for_cell_library(
        "sg13g2_stdcell", None, ValueError, root=str(root)
    )

    assert corner == "typ_1p20V_25C"
    assert liberty_path.endswith("sg13g2_stdcell_typ_1p20V_25C.lib")
    assert info["variant"] == "ihp-sg13g2"


def test_resolve_liberty_for_cell_library_raises_caller_supplied_error_cls(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")

    class _CustomError(Exception):
        pass

    with pytest.raises(_CustomError, match="liberty not found for deck"):
        pdk_cells.resolve_liberty_for_cell_library(
            "sky130_fd_sc_hd", "ff_n40C_1v95", _CustomError, root=str(root)
        )


def test_resolve_liberty_three_verb_wrappers_agree_and_raise_their_own_error(
    tmp_path,
):
    """`synthesize._resolve_liberty`/`place_and_route._resolve_liberty`/
    `post_route_sta._resolve_liberty` (issue #1652) are thin wrappers around
    the shared `resolve_liberty_for_cell_library` above. Given the identical
    fixture .lib files and identical (cell_library, corner) inputs, all three
    must resolve to the same `(liberty_path, corner, pdk_info)` -- and each
    must still raise its own module-specific exception type (never a shared
    or generic one) on a not-found corner, preserving each module's
    pre-existing exception-type contract with its own callers."""
    from klayout_tools import place_and_route, post_route_sta, synthesize
    from klayout_tools.place_and_route import PlaceAndRouteError
    from klayout_tools.post_route_sta import PostRouteStaError
    from klayout_tools.synthesize import SynthesizeError

    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(
        variant_dir,
        "sky130_fd_sc_hd",
        corners=(
            ("tt_025C_1v80", 1.0, 25.0, 1.8),
            ("ff_n40C_1v95", 1.0, -40.0, 1.95),
        ),
    )

    results = {
        "synthesize": synthesize._resolve_liberty(
            "sky130_fd_sc_hd", "ff_n40C_1v95", root=str(root)
        ),
        "place_and_route": place_and_route._resolve_liberty(
            "sky130_fd_sc_hd", "ff_n40C_1v95", root=str(root)
        ),
        "post_route_sta": post_route_sta._resolve_liberty(
            "sky130_fd_sc_hd", "ff_n40C_1v95", root=str(root)
        ),
    }
    # Same (liberty_path, corner, pdk_info) from every wrapper.
    values = list(results.values())
    assert all(value == values[0] for value in values), results
    assert values[0][1] == "ff_n40C_1v95"
    assert values[0][0].endswith("sky130_fd_sc_hd__ff_n40C_1v95.lib")

    # Not-found case: each wrapper still raises its own error class.
    with pytest.raises(SynthesizeError, match="liberty not found for deck"):
        synthesize._resolve_liberty("sky130_fd_sc_hd", "ss_100C_1v60", root=str(root))
    with pytest.raises(PlaceAndRouteError, match="liberty not found for deck"):
        place_and_route._resolve_liberty(
            "sky130_fd_sc_hd", "ss_100C_1v60", root=str(root)
        )
    with pytest.raises(PostRouteStaError, match="liberty not found for deck"):
        post_route_sta._resolve_liberty(
            "sky130_fd_sc_hd", "ss_100C_1v60", root=str(root)
        )

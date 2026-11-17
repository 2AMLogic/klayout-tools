"""Fabricated open_pdks-layout install/cell-library builders shared by
``tests/test_pdk.py`` and ``tests/test_pdk_cells.py`` (issue #5657).

The two test modules split from one file (issue #1884) and carried
byte-identical copies of this preamble; the builders moved here verbatim,
docstrings included -- the docstrings record the real PDK shapes each
parameter models. As in both test modules, every test runs against
**fabricated** installs created under ``tmp_path`` -- CI never downloads a
real PDK, and ``isolate_pdk_search_space`` (the shared body of both modules'
``_isolate`` autouse fixture) points the module's search-space constants
away from the host so results are hermetic regardless of what is installed
on the machine running the suite.
"""

from klayout_tools import pdk


def make_install(root, variant, *, sources=None, assets=("ngspice",)):
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


def make_cell_library(
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


def make_ihp_stdcell_library(
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


def isolate_pdk_search_space(monkeypatch, tmp_path):
    """Scrub PDK env vars, redirect HOME, and empty the host search space.

    Individual tests opt back into stores/prefixes by setting the module
    constants (or ``$PDK_ROOT``) to controlled ``tmp_path`` locations.
    """
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "SHARED_ROOTS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])

"""Tests for PDK discovery/resolution (`klayout_tools.pdk` + `klt pdk`).

Every test runs against **fabricated** open_pdks-layout installs created under
``tmp_path`` — CI never downloads a real PDK. The environment is scrubbed and
the module's search-space constants are pointed away from the host by default
(the ``_isolate`` autouse fixture) so results are hermetic regardless of what
is installed on the machine running the suite.
"""

import json

import pytest

from klayout_tools import pdk
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
):
    """Fabricate a `libs.ref/<name>` entry with `spice/`/`lib/` views.

    ``devices`` are nfet/pfet flavor suffixes embedded in one synthetic SPICE
    instance line each (prefixed ``<family>_fd_pr__``, matching real
    ``X<n> ... <model> w=... l=...`` instance lines). ``corners`` is an
    iterable of ``(corner_name, nom_process, nom_temperature, nom_voltage)``
    tuples, one `.lib` file per entry. ``with_spice``/``with_lib`` let a test
    omit either view to exercise the missing-view fallback.
    """
    lib_dir = variant_dir / "libs.ref" / name
    if with_spice:
        spice_dir = lib_dir / "spice"
        spice_dir.mkdir(parents=True)
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
            content = (
                f'    default_operating_conditions : "{corner_name}";\n'
                f"    nom_process : {process};\n"
                f"    nom_temperature : {temperature};\n"
                f"    nom_voltage : {voltage};\n"
            )
            (lib_views_dir / f"{name}__{corner_name}.lib").write_text(
                content, encoding="utf-8"
            )
    lib_dir.mkdir(parents=True, exist_ok=True)
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
# Resolution order / precedence
# --------------------------------------------------------------------------- #


def test_find_via_pdk_root_flag(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", sources="open_pdks abc123")

    report = pdk.find_pdk(root=str(root))

    assert report["schema_version"] == 1
    assert report["root"] == str(root)
    assert report["variant"] == "sky130A"
    assert report["version"] == "open_pdks abc123"
    assert report["resolved_via"] == "--pdk-root flag"


def test_find_via_pdk_root_env(tmp_path, monkeypatch):
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(root))

    report = pdk.find_pdk()

    assert report["variant"] == "sky130A"
    assert report["resolved_via"] == "PDK_ROOT environment variable"


def test_find_via_ciel_store(tmp_path, monkeypatch):
    # Step 3: ~/.ciel resolves via HOME expansion when earlier steps are absent.
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel", "~/.volare"])
    home = tmp_path / "home"
    _make_install(home / ".ciel", "sky130A")

    report = pdk.find_pdk()

    assert report["root"] == str(home / ".ciel")
    assert report["resolved_via"] == "search root: ~/.ciel"


def test_find_via_conventional_prefix(tmp_path, monkeypatch):
    # Step 4: ~/share/pdk resolves when stores are empty.
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", ["~/share/pdk"])
    home = tmp_path / "home"
    _make_install(home / "share" / "pdk", "sky130A")

    report = pdk.find_pdk()

    assert report["root"] == str(home / "share" / "pdk")
    assert report["resolved_via"] == "search root: ~/share/pdk"


def test_earlier_step_beats_later(tmp_path, monkeypatch):
    # Both $PDK_ROOT (step 2) and a store (step 3) hold installs: step 2 wins.
    env_root = tmp_path / "env"
    _make_install(env_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(env_root))
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel"])
    _make_install(tmp_path / "home" / ".ciel", "sky130B")

    report = pdk.find_pdk()

    assert report["resolved_via"] == "PDK_ROOT environment variable"
    assert report["variant"] == "sky130A"


def test_pdk_root_nonexistent_falls_through(tmp_path, monkeypatch):
    # $PDK_ROOT set but pointing at a nonexistent dir: skip to the store.
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel"])
    _make_install(tmp_path / "home" / ".ciel", "sky130A")

    report = pdk.find_pdk()

    assert report["resolved_via"] == "search root: ~/.ciel"


# --------------------------------------------------------------------------- #
# Variant selection ($PDK vs --pdk)
# --------------------------------------------------------------------------- #


def test_pdk_env_selects_variant(tmp_path, monkeypatch):
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    _make_install(root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(root))
    monkeypatch.setenv("PDK", "sky130B")

    report = pdk.find_pdk()

    assert report["variant"] == "sky130B"


def test_pdk_flag_beats_pdk_env(tmp_path, monkeypatch):
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    _make_install(root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(root))
    monkeypatch.setenv("PDK", "sky130B")

    report = pdk.find_pdk(variant="sky130A")

    assert report["variant"] == "sky130A"


def test_default_variant_is_first_sorted(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130B")
    _make_install(root, "sky130A")

    report = pdk.find_pdk(root=str(root))

    assert report["variant"] == "sky130A"


def test_requested_variant_absent_raises(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A")

    with pytest.raises(pdk.PdkNotFoundError) as excinfo:
        pdk.find_pdk(variant="sky130B", root=str(root))

    assert "sky130B" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Layout probe + version stamp
# --------------------------------------------------------------------------- #


def test_dir_without_libs_tech_is_not_a_variant(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    # A sibling directory with no libs.tech/ must be ignored.
    (root / "not-a-variant").mkdir()
    (root / "not-a-variant" / "libs.ref").mkdir()

    report = pdk.list_pdks(root=str(root))

    names = [v["name"] for v in report["installs"][0]["variants"]]
    assert names == ["sky130A"]


def test_version_null_when_no_sources(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", sources=None)

    report = pdk.find_pdk(root=str(root))

    assert report["version"] is None


def test_version_multiline_sources_joined(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", sources="open_pdks abc\n\nsky130 def\n")

    report = pdk.find_pdk(root=str(root))

    assert report["version"] == "open_pdks abc; sky130 def"


def test_assets_present_and_absent(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("ngspice", "klayout", "libs_ref"))

    assets = pdk.find_pdk(root=str(root))["assets"]

    assert assets["ngspice"] == str(root / "sky130A" / "libs.tech" / "ngspice")
    assert assets["klayout"] == str(root / "sky130A" / "libs.tech" / "klayout")
    assert assets["libs_ref"] == str(root / "sky130A" / "libs.ref")
    # Keys are always present; absent tools resolve to None.
    assert assets["xschem"] is None
    assert assets["magic"] is None
    assert assets["netgen"] is None


# --------------------------------------------------------------------------- #
# netgen_setup_file (issue #343)
#
# Naming convention verified against the open_pdks source tree
# (`RTimothyEdwards/open_pdks`, `sky130/Makefile.in` and
# `gf180mcu/Makefile.in`'s `netgen-%` install rule, both cloned for this
# issue): open_pdks stages `<variant>_setup.tcl` and symlinks a generic
# `setup.tcl` alongside it in the same `libs.tech/netgen/` directory.
# --------------------------------------------------------------------------- #


def test_netgen_setup_file_prefers_variant_named_file(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("netgen",))
    netgen_dir = variant_dir / "libs.tech" / "netgen"
    (netgen_dir / "sky130A_setup.tcl").write_text("# variant setup\n")
    (netgen_dir / "setup.tcl").write_text("# generic setup\n")

    result = pdk.netgen_setup_file(root=str(root))

    assert result == str(netgen_dir / "sky130A_setup.tcl")


def test_netgen_setup_file_falls_back_to_generic_setup_tcl(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuC", assets=("netgen",))
    netgen_dir = variant_dir / "libs.tech" / "netgen"
    # Only the generic symlink target survived (e.g. a copy that dropped the
    # variant-named original but kept `setup.tcl`).
    (netgen_dir / "setup.tcl").write_text("# generic setup\n")

    result = pdk.netgen_setup_file(root=str(root))

    assert result == str(netgen_dir / "setup.tcl")


def test_netgen_setup_file_none_when_directory_has_neither_file(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("netgen",))

    result = pdk.netgen_setup_file(root=str(root))

    assert result is None


def test_netgen_setup_file_none_when_pdk_ships_no_netgen_asset(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("ngspice",))  # no "netgen"

    result = pdk.netgen_setup_file(root=str(root))

    assert result is None


def test_netgen_setup_file_resolves_variant_and_root_like_find_pdk(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130B", assets=("netgen",))
    _make_install(root, "sky130A", assets=("netgen",))
    (variant_dir / "libs.tech" / "netgen" / "sky130B_setup.tcl").write_text("# x\n")

    result = pdk.netgen_setup_file(variant="sky130B", root=str(root))

    assert result == str(variant_dir / "libs.tech" / "netgen" / "sky130B_setup.tcl")


def test_netgen_setup_file_no_install_raises(tmp_path):
    with pytest.raises(pdk.PdkNotFoundError):
        pdk.netgen_setup_file(root=str(tmp_path / "does-not-exist"))


# --------------------------------------------------------------------------- #
# lef_files (issue #397 / #425 -- the OpenROAD survey's own finding that
# `_ASSET_LAYOUT` never carried a `lef` key; naming convention verified
# against a real, volare-fetched `sky130A` install for issue #425's own
# worked example: `libs.ref/<lib>/techlef/<lib>__<corner>.tlef` (a
# min/nom/max routing-parasitic corner) and `libs.ref/<lib>/lef/<lib>.lef`
# (the merged macro/cell LEF)).
# --------------------------------------------------------------------------- #


def _make_lef_library(variant_dir, name, *, corners=("min", "nom", "max")):
    """Fabricate a `libs.ref/<name>` entry with `techlef/`/`lef/` views --
    minimal placeholder content, never a real, KLayout-parseable LEF (this
    module's own resolver never reads the file contents)."""
    lib_dir = variant_dir / "libs.ref" / name
    techlef_dir = lib_dir / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    for corner in corners:
        (techlef_dir / f"{name}__{corner}.tlef").write_text("# tech lef\n")
    lef_dir = lib_dir / "lef"
    lef_dir.mkdir(parents=True, exist_ok=True)
    (lef_dir / f"{name}.lef").write_text("# merged cell lef\n")
    return lib_dir


def test_lef_files_resolves_nominal_corner_by_default(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_lef_library(variant_dir, "sky130_fd_sc_hd")

    result = pdk.lef_files("sky130_fd_sc_hd", root=str(root))

    assert result["tech_lef"] == str(
        variant_dir
        / "libs.ref"
        / "sky130_fd_sc_hd"
        / "techlef"
        / "sky130_fd_sc_hd__nom.tlef"
    )
    assert result["cell_lef"] == str(
        variant_dir / "libs.ref" / "sky130_fd_sc_hd" / "lef" / "sky130_fd_sc_hd.lef"
    )


def test_lef_files_resolves_explicit_corner(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_lef_library(variant_dir, "sky130_fd_sc_hd")

    result = pdk.lef_files("sky130_fd_sc_hd", root=str(root), corner="max")

    assert result["tech_lef"].endswith("sky130_fd_sc_hd__max.tlef")


def test_lef_files_none_when_cell_library_not_shipped(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("libs_ref",))

    result = pdk.lef_files("sky130_fd_sc_hd", root=str(root))

    assert result == {"tech_lef": None, "cell_lef": None}


def test_lef_files_none_when_pdk_ships_no_libs_ref(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("ngspice",))  # no libs_ref at all

    result = pdk.lef_files("sky130_fd_sc_hd", root=str(root))

    assert result == {"tech_lef": None, "cell_lef": None}


def test_lef_files_none_when_corner_missing(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_lef_library(variant_dir, "sky130_fd_sc_hd", corners=("min", "max"))

    result = pdk.lef_files("sky130_fd_sc_hd", root=str(root))  # default: "nom"

    assert result["tech_lef"] is None
    assert result["cell_lef"] is not None  # cell lef is corner-independent


def test_lef_files_resolves_variant_and_root_like_find_pdk(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130B", assets=("libs_ref",))
    _make_install(root, "sky130A", assets=("libs_ref",))
    _make_lef_library(variant_dir, "sky130_fd_sc_hd")

    result = pdk.lef_files("sky130_fd_sc_hd", variant="sky130B", root=str(root))

    assert result["tech_lef"] == str(
        variant_dir
        / "libs.ref"
        / "sky130_fd_sc_hd"
        / "techlef"
        / "sky130_fd_sc_hd__nom.tlef"
    )


def test_lef_files_no_install_raises(tmp_path):
    with pytest.raises(pdk.PdkNotFoundError):
        pdk.lef_files("sky130_fd_sc_hd", root=str(tmp_path / "does-not-exist"))


# --------------------------------------------------------------------------- #
# list_pdks
# --------------------------------------------------------------------------- #


def test_list_enumerates_multiple_installs_and_variants(tmp_path, monkeypatch):
    env_root = tmp_path / "env"
    _make_install(env_root, "sky130A", sources="v1")
    _make_install(env_root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(env_root))
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel"])
    _make_install(tmp_path / "home" / ".ciel", "gf180mcuD")

    report = pdk.list_pdks()

    assert report["schema_version"] == 1
    assert len(report["installs"]) == 2
    first = report["installs"][0]
    assert first["resolved_via"] == "PDK_ROOT environment variable"
    assert [v["name"] for v in first["variants"]] == ["sky130A", "sky130B"]
    assert first["variants"][0]["version"] == "v1"
    second = report["installs"][1]
    assert second["resolved_via"] == "search root: ~/.ciel"
    assert [v["name"] for v in second["variants"]] == ["gf180mcuD"]


def test_list_empty_is_success(tmp_path):
    report = pdk.list_pdks()
    assert report == {"schema_version": 1, "installs": []}


# --------------------------------------------------------------------------- #
# No-install failure
# --------------------------------------------------------------------------- #


def test_find_no_install_raises_with_search_order(tmp_path, monkeypatch):
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "nope"))
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel"])

    with pytest.raises(pdk.PdkNotFoundError) as excinfo:
        pdk.find_pdk()

    message = str(excinfo.value)
    assert "PDK_ROOT environment variable" in message  # names the search order
    assert "~/.ciel" in message
    assert "ciel enable" in message  # actionable install pointer


# --------------------------------------------------------------------------- #
# CLI envelope conformance (via klt main())
# --------------------------------------------------------------------------- #


def test_cli_find_json_on_stdout(tmp_path, capsys):
    root = tmp_path / "install"
    _make_install(root, "sky130A", sources="open_pdks abc")

    exit_code = main(["pdk", "find", "--pdk-root", str(root), "--format", "json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["variant"] == "sky130A"
    assert payload["schema_version"] == 1


def test_cli_find_no_install_error_envelope(tmp_path, capsys):
    exit_code = main(
        ["pdk", "find", "--pdk-root", str(tmp_path / "nope"), "--format", "json"]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout empty on error
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "pdk find"
    assert "ciel enable" in error["error"]["message"]


def test_cli_list_empty_exits_zero(capsys):
    exit_code = main(["pdk", "list", "--format", "json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"schema_version": 1, "installs": []}


def test_cli_env_exports_are_eval_able(tmp_path, capsys):
    root = tmp_path / "install"
    _make_install(root, "sky130A")

    exit_code = main(["pdk", "env", "--pdk-root", str(root)])

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"export PDK_ROOT={root}", "export PDK=sky130A"]


def test_cli_env_matches_find(tmp_path, capsys, monkeypatch):
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    _make_install(root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(root))
    monkeypatch.setenv("PDK", "sky130B")

    main(["pdk", "env"])
    env_out = capsys.readouterr().out
    main(["pdk", "find", "--format", "json"])
    find_payload = json.loads(capsys.readouterr().out)

    assert f"export PDK_ROOT={root}" in env_out
    assert "export PDK=sky130B" in env_out
    assert find_payload["variant"] == "sky130B"
    assert find_payload["root"] == str(root)


def test_cli_env_no_install_error_envelope(tmp_path, capsys):
    exit_code = main(
        ["pdk", "env", "--pdk-root", str(tmp_path / "nope"), "--format", "json"]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "pdk env"


def test_cli_pdk_no_subcommand_prints_help(capsys):
    exit_code = main(["pdk"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "find" in err and "list" in err and "env" in err


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

    report = pdk.list_cell_libraries(root=str(root))

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

    report = pdk.list_cell_libraries(root=str(root))

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

    report = pdk.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["nominal_supply_v"] == 2.64
    assert library["nominal_corner"] == "tt_025C_2v64_lv1v80"


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

    report = pdk.list_cell_libraries(root=str(root))

    assert [lib["name"] for lib in report["libraries"]] == ["sky130_fd_sc_hd"]


def test_cells_no_libs_ref_is_empty_list(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("ngspice",))

    report = pdk.list_cell_libraries(root=str(root))

    assert report["libraries"] == []


def test_cells_missing_spice_view_yields_empty_device_flavors(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd", with_spice=False)

    report = pdk.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["device_flavors"] == []
    assert library["nominal_supply_v"] == 1.8  # lib/ view still present


def test_cells_missing_lib_view_yields_null_supply(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd", with_lib=False)

    report = pdk.list_cell_libraries(root=str(root))

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

    report = pdk.list_cell_libraries(root=str(root), supply=1.8)

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

    report = pdk.list_cell_libraries(root=str(root), supply=5.0)

    assert report["any_compatible"] is False
    assert report["libraries"][0]["compatible"] is False


def test_cells_no_install_raises(tmp_path):
    with pytest.raises(pdk.PdkNotFoundError):
        pdk.list_cell_libraries(root=str(tmp_path / "nope"))


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

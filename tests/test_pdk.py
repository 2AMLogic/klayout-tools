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
# Flat single-PDK layout (issue #522 -- IHP-Open-PDK's SG13G2)
#
# Layout verified against a real IHP-Open-PDK v0.3.0 release tarball
# (`scripts/fetch-ihp-sg13g2.sh`): `ihp-sg13g2/libs.tech/...` /
# `ihp-sg13g2/libs.ref/...` sit directly under the PDK's own directory, with
# no further per-family variant nesting the way sky130A/gf180mcuC etc. have.
# Both real-world `$PDK_ROOT` conventions get exercised: pointed at the
# clone root (the *nested* open_pdks-shaped probe -- `ihp-sg13g2` is just an
# ordinary variant subdirectory, already covered by the tests above) and
# pointed directly at the PDK's own directory (the *flat* fallback these
# tests add).
# --------------------------------------------------------------------------- #


def _make_flat_install(root, *, assets=("ngspice",)):
    """Fabricate a *flat* single-PDK install directly at ``root`` -- no
    variant subdirectory layer, mirroring IHP-Open-PDK's SG13G2 when
    ``$PDK_ROOT``/``--pdk-root`` names the PDK's own directory.
    """
    (root / "libs.tech").mkdir(parents=True)
    for asset in assets:
        if asset == "libs_ref":
            (root / "libs.ref").mkdir(parents=True, exist_ok=True)
        else:
            (root / "libs.tech" / asset).mkdir(parents=True, exist_ok=True)
    return root


def test_flat_layout_resolves_variant_named_after_root_basename(tmp_path):
    root = tmp_path / "ihp-sg13g2"
    _make_flat_install(root, assets=("ngspice", "klayout", "libs_ref"))

    report = pdk.find_pdk(root=str(root))

    assert report["root"] == str(root)
    assert report["variant"] == "ihp-sg13g2"
    assert report["version"] is None  # no SOURCES stamp -- never guessed
    assert report["assets"]["ngspice"] == str(root / "libs.tech" / "ngspice")
    assert report["assets"]["klayout"] == str(root / "libs.tech" / "klayout")
    assert report["assets"]["libs_ref"] == str(root / "libs.ref")


def test_flat_layout_via_pdk_root_env(tmp_path, monkeypatch):
    root = tmp_path / "ihp-sg13g2"
    _make_flat_install(root)
    monkeypatch.setenv("PDK_ROOT", str(root))

    report = pdk.find_pdk()

    assert report["variant"] == "ihp-sg13g2"
    assert report["resolved_via"] == "PDK_ROOT environment variable"


def test_flat_layout_pdk_flag_matches_root_basename(tmp_path):
    root = tmp_path / "ihp-sg13g2"
    _make_flat_install(root)

    report = pdk.find_pdk(variant="ihp-sg13g2", root=str(root))

    assert report["variant"] == "ihp-sg13g2"


def test_flat_layout_pdk_flag_mismatch_raises(tmp_path):
    root = tmp_path / "ihp-sg13g2"
    _make_flat_install(root)

    with pytest.raises(pdk.PdkNotFoundError):
        pdk.find_pdk(variant="sky130A", root=str(root))


def test_flat_layout_only_tried_when_nested_scan_finds_nothing(tmp_path):
    # A root that already has a real nested variant is never reinterpreted
    # as its own flat variant, even if it also happens to ship a top-level
    # `libs.tech/` (an implausible combination in practice, but the nested
    # scan must still take priority so it can never be shadowed).
    root = tmp_path / "install"
    _make_install(root, "sky130A")
    (root / "libs.tech").mkdir()

    report = pdk.find_pdk(root=str(root))

    assert report["variant"] == "sky130A"


def test_flat_layout_netgen_setup_file_matches_ihp_naming(tmp_path):
    # IHP-Open-PDK stages its netgen setup script as `<variant>_setup.tcl`
    # too (`ihp-sg13g2_setup.tcl`, confirmed in a real fetched install) --
    # the flat variant's synthesised name feeds the same convention
    # `netgen_setup_file` already implements for open_pdks.
    root = tmp_path / "ihp-sg13g2"
    _make_flat_install(root, assets=("netgen",))
    netgen_dir = root / "libs.tech" / "netgen"
    (netgen_dir / "ihp-sg13g2_setup.tcl").write_text("# ihp setup\n")

    result = pdk.netgen_setup_file(root=str(root))

    assert result == str(netgen_dir / "ihp-sg13g2_setup.tcl")


def test_flat_layout_list_reports_single_variant(tmp_path):
    root = tmp_path / "ihp-sg13g2"
    _make_flat_install(root)

    report = pdk.list_pdks(root=str(root))

    assert len(report["installs"]) == 1
    install = report["installs"][0]
    assert install["root"] == str(root)
    assert install["variants"] == [{"name": "ihp-sg13g2", "version": None}]


def test_cli_find_flat_layout_json(tmp_path, capsys):
    root = tmp_path / "ihp-sg13g2"
    _make_flat_install(root)

    exit_code = main(["pdk", "find", "--pdk-root", str(root), "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["variant"] == "ihp-sg13g2"


def test_cli_env_flat_layout(tmp_path, capsys):
    root = tmp_path / "ihp-sg13g2"
    _make_flat_install(root)

    exit_code = main(["pdk", "env", "--pdk-root", str(root)])

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"export PDK_ROOT={root}", "export PDK=ihp-sg13g2"]


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
# drc_deck_file (issue #565)
#
# Naming convention verified against a real, volare-fetched sky130A install:
# open_pdks stages sky130's complete, ready-to-run native DRC deck as a
# single variant-named file, `libs.tech/klayout/drc/<variant>.lydrc`. A real
# gf180mcuC install, by contrast, ships no such single ready file at that
# path -- only `drc/run_drc.py` plus `drc/rule_decks/*.drc` fragments -- so
# this resolver deliberately returns `None` for that shape rather than
# guessing (see `drc_deck_file`'s own docstring).
# --------------------------------------------------------------------------- #


def test_drc_deck_file_prefers_variant_named_lydrc(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("klayout",))
    drc_dir = variant_dir / "libs.tech" / "klayout" / "drc"
    drc_dir.mkdir(parents=True)
    (drc_dir / "sky130A.lydrc").write_text("# native deck\n")

    result = pdk.drc_deck_file(root=str(root))

    assert result == str(drc_dir / "sky130A.lydrc")


def test_drc_deck_file_falls_back_to_variant_named_drc(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("klayout",))
    drc_dir = variant_dir / "libs.tech" / "klayout" / "drc"
    drc_dir.mkdir(parents=True)
    (drc_dir / "sky130A.drc").write_text("# native deck\n")

    result = pdk.drc_deck_file(root=str(root))

    assert result == str(drc_dir / "sky130A.drc")


def test_drc_deck_file_none_when_gf180mcu_shaped_fragments_only(tmp_path):
    """gf180mcu's real shape: `run_drc.py` + `rule_decks/*.drc` fragments,
    no single ready-to-run `<variant>.lydrc`/`.drc` file -- this resolver
    must not guess or assemble one; a caller wanting to run gf180mcu's
    native deck must pass `--deck-file` pointing at an already-produced
    merged deck instead (see the function's own docstring)."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuC", assets=("klayout",))
    drc_dir = variant_dir / "libs.tech" / "klayout" / "drc"
    rule_decks_dir = drc_dir / "rule_decks"
    rule_decks_dir.mkdir(parents=True)
    (drc_dir / "run_drc.py").write_text("# assembly wrapper\n")
    (rule_decks_dir / "main.drc").write_text("# fragment\n")

    result = pdk.drc_deck_file(root=str(root))

    assert result is None


def test_drc_deck_file_none_when_directory_has_neither_file(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("klayout",))
    (variant_dir / "libs.tech" / "klayout" / "drc").mkdir(parents=True)

    result = pdk.drc_deck_file(root=str(root))

    assert result is None


def test_drc_deck_file_none_when_no_drc_subdirectory(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("klayout",))  # no drc/ subdir yet

    result = pdk.drc_deck_file(root=str(root))

    assert result is None


def test_drc_deck_file_none_when_pdk_ships_no_klayout_asset(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("ngspice",))  # no "klayout"

    result = pdk.drc_deck_file(root=str(root))

    assert result is None


def test_drc_deck_file_resolves_variant_and_root_like_find_pdk(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130B", assets=("klayout",))
    _make_install(root, "sky130A", assets=("klayout",))
    drc_dir = variant_dir / "libs.tech" / "klayout" / "drc"
    drc_dir.mkdir(parents=True)
    (drc_dir / "sky130B.lydrc").write_text("# native deck\n")

    result = pdk.drc_deck_file(variant="sky130B", root=str(root))

    assert result == str(drc_dir / "sky130B.lydrc")


def test_drc_deck_file_no_install_raises(tmp_path):
    with pytest.raises(pdk.PdkNotFoundError):
        pdk.drc_deck_file(root=str(tmp_path / "does-not-exist"))


# --------------------------------------------------------------------------- #
# lvs_deck_file (issue #869)
#
# Naming convention verified against a real, volare-fetched sky130A/sky130B
# install: open_pdks stages the LVS/device-extraction deck as a single
# bare-family-named file, `libs.tech/klayout/lvs/sky130.lvs` -- unlike
# `drc_deck_file`'s own `<variant>.lydrc`, this file is *not* itself
# lettered-variant-suffixed (both sky130A and sky130B ship the identical
# `sky130.lvs`). A real gf180mcuB install ships the analogous
# `gf180mcu.lvs`.
# --------------------------------------------------------------------------- #


def test_lvs_deck_file_prefers_variant_named_lvs(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("klayout",))
    lvs_dir = variant_dir / "libs.tech" / "klayout" / "lvs"
    lvs_dir.mkdir(parents=True)
    (lvs_dir / "sky130A.lvs").write_text("# native lvs deck\n")

    result = pdk.lvs_deck_file(root=str(root))

    assert result == str(lvs_dir / "sky130A.lvs")


def test_lvs_deck_file_falls_back_to_bare_family_name(tmp_path):
    """The real-world shape: open_pdks names the LVS deck after the bare
    PDK family, not the lettered variant (`sky130A` resolves `sky130.lvs`,
    not `sky130A.lvs`)."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("klayout",))
    lvs_dir = variant_dir / "libs.tech" / "klayout" / "lvs"
    lvs_dir.mkdir(parents=True)
    (lvs_dir / "sky130.lvs").write_text("# native lvs deck\n")

    result = pdk.lvs_deck_file(root=str(root))

    assert result == str(lvs_dir / "sky130.lvs")


def test_lvs_deck_file_falls_back_to_bare_family_name_gf180mcu(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuB", assets=("klayout",))
    lvs_dir = variant_dir / "libs.tech" / "klayout" / "lvs"
    lvs_dir.mkdir(parents=True)
    (lvs_dir / "gf180mcu.lvs").write_text("# native lvs deck\n")

    result = pdk.lvs_deck_file(root=str(root))

    assert result == str(lvs_dir / "gf180mcu.lvs")


def test_lvs_deck_file_none_when_directory_has_neither_file(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("klayout",))
    (variant_dir / "libs.tech" / "klayout" / "lvs").mkdir(parents=True)

    result = pdk.lvs_deck_file(root=str(root))

    assert result is None


def test_lvs_deck_file_none_when_no_lvs_subdirectory(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("klayout",))  # no lvs/ subdir yet

    result = pdk.lvs_deck_file(root=str(root))

    assert result is None


def test_lvs_deck_file_none_when_pdk_ships_no_klayout_asset(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("ngspice",))  # no "klayout"

    result = pdk.lvs_deck_file(root=str(root))

    assert result is None


def test_lvs_deck_file_resolves_variant_and_root_like_find_pdk(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130B", assets=("klayout",))
    _make_install(root, "sky130A", assets=("klayout",))
    lvs_dir = variant_dir / "libs.tech" / "klayout" / "lvs"
    lvs_dir.mkdir(parents=True)
    (lvs_dir / "sky130.lvs").write_text("# native lvs deck\n")

    result = pdk.lvs_deck_file(variant="sky130B", root=str(root))

    assert result == str(lvs_dir / "sky130.lvs")


def test_lvs_deck_file_no_install_raises(tmp_path):
    with pytest.raises(pdk.PdkNotFoundError):
        pdk.lvs_deck_file(root=str(tmp_path / "does-not-exist"))


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

    report = pdk.list_cell_libraries(root=str(root))

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

    report = pdk.list_cell_libraries(root=str(root))

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

    report = pdk.list_cell_libraries(root=str(root))

    library = report["libraries"][0]
    assert library["nominal_corner"] == "tt_025C_1v80"
    assert not library["nominal_corner"].startswith("gf180mcu_fd_sc_mcu9t5v0__")


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

    report = pdk.list_cell_libraries(root=str(root), supply=3.3)

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


# --------------------------------------------------------------------------- #
# list_hard_macro_libraries (`klt pdk macros`) -- issue #535
# --------------------------------------------------------------------------- #


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


def test_macros_reports_fd_ip_library_and_its_views(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_macro_library(
        variant_dir,
        "sky130_fd_ip_sram_1k",
        views=("gds", "lef", "lib", "spice", "verilog"),
    )

    report = pdk.list_hard_macro_libraries(root=str(root))

    assert report["schema_version"] == 1
    assert report["pdk"] == "sky130A"
    assert len(report["macros"]) == 1
    macro = report["macros"][0]
    assert macro["name"] == "sky130_fd_ip_sram_1k"
    assert macro["views"] == {
        "gds": True,
        "lef": True,
        "lib": True,
        "spice": True,
        "cdl": False,
        "verilog": True,
    }


def test_macros_excludes_std_cell_and_other_libraries(tmp_path):
    """`klt pdk macros` reports only `_fd_ip_`-named entries -- `_fd_sc_`/
    `_fd_pr`/`_fd_io`/`_sram_macros` entries are the domain of `klt pdk
    cells` (or neither command), not this one. This is also the flip side of
    the regression this issue reports: `klt pdk cells` must keep excluding
    `_fd_ip_` entries (see test_cells_excludes_fd_ip_hard_macro_library
    below) while `klt pdk macros` now reports them.
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_macro_library(variant_dir, "sky130_fd_ip_sram_1k")
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")
    _make_cell_library(variant_dir, "sky130_fd_io", devices=("nfet_g5v0d10v5",))
    _make_cell_library(variant_dir, "sky130_sram_macros", devices=("nfet_01v8",))
    (variant_dir / "libs.ref" / "sky130_fd_pr").mkdir(parents=True)

    report = pdk.list_hard_macro_libraries(root=str(root))

    assert [macro["name"] for macro in report["macros"]] == ["sky130_fd_ip_sram_1k"]


def test_cells_excludes_fd_ip_hard_macro_library(tmp_path):
    """Regression test for issue #535: `klt pdk cells` must keep excluding
    `*_fd_ip_*` hard-macro IP libraries even though `klt pdk macros` now
    reports them -- the two commands' result sets do not overlap.
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")
    _make_macro_library(variant_dir, "sky130_fd_ip_sram_1k")

    report = pdk.list_cell_libraries(root=str(root))

    assert [lib["name"] for lib in report["libraries"]] == ["sky130_fd_sc_hd"]


def test_macros_only_hard_macro_ip_present_no_std_cell(tmp_path):
    """A variant shipping only hard-macro IP (no `_fd_sc_` standard-cell
    library at all) is not an error for either command -- `klt pdk macros`
    reports the macro, `klt pdk cells` reports an empty list.
    """
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_macro_library(variant_dir, "sky130_fd_ip_sram_1k")

    macros_report = pdk.list_hard_macro_libraries(root=str(root))
    cells_report = pdk.list_cell_libraries(root=str(root))

    assert [macro["name"] for macro in macros_report["macros"]] == [
        "sky130_fd_ip_sram_1k"
    ]
    assert cells_report["libraries"] == []


def test_macros_no_libs_ref_is_empty_list(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("ngspice",))

    report = pdk.list_hard_macro_libraries(root=str(root))

    assert report["macros"] == []


def test_macros_neither_kind_present_is_empty_list(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_cell_library(variant_dir, "sky130_fd_sc_hd")

    report = pdk.list_hard_macro_libraries(root=str(root))

    assert report["macros"] == []


def test_macros_no_install_raises(tmp_path):
    with pytest.raises(pdk.PdkNotFoundError):
        pdk.list_hard_macro_libraries(root=str(tmp_path / "nope"))


# --------------------------------------------------------------------------- #
# CLI envelope conformance (via klt main()) -- `klt pdk macros`
# --------------------------------------------------------------------------- #


def test_cli_macros_json_on_stdout(tmp_path, capsys):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_macro_library(variant_dir, "sky130_fd_ip_sram_1k")

    exit_code = main(["pdk", "macros", "--pdk-root", str(root), "--format", "json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["pdk"] == "sky130A"
    assert payload["macros"][0]["name"] == "sky130_fd_ip_sram_1k"


def test_cli_macros_text_table(tmp_path, capsys):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("libs_ref",))
    _make_macro_library(
        variant_dir, "sky130_fd_ip_sram_1k", views=("gds", "lef", "lib")
    )

    exit_code = main(["pdk", "macros", "--pdk-root", str(root)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "sky130_fd_ip_sram_1k" in out
    assert "gds/lef/lib" in out


def test_cli_macros_empty_result_text(tmp_path, capsys):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("libs_ref",))

    exit_code = main(["pdk", "macros", "--pdk-root", str(root)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "no hard-macro IP libraries found" in out


def test_cli_macros_no_install_error_envelope(tmp_path, capsys):
    exit_code = main(
        [
            "pdk",
            "macros",
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
    assert error["error"]["command"] == "pdk macros"


# --------------------------------------------------------------------------- #
# list_corners (`klt pdk corners`) -- issue #538
# --------------------------------------------------------------------------- #

#: A minimal gf180mcu-shaped ngspice deck exercising every skew path this
#: command classifies, modeled on the real shape verified against a
#: gf180mcuD volare install (2026-08-05, see the issue's own worked
#: example): `mos` skews via a different named model per corner (`skewed`);
#: `bjt`/`diode`/`mimcap` skew via `.param` overrides on an unchanged shared
#: model (`param`); `res_ff`/`res_ss` deliberately restate the *same*
#: `rsh=60` value `res_typical` uses -- a family present at a non-typical
#: corner that never actually moved, the specific "leaves a family at
#: typical" bug this command exists to catch (`complete: False` at `ff`/
#: `ss`). `fs`/`sf` define only the `mos` family (matching the real deck),
#: leaving every other family unlisted at those corners.
_GF180MCU_FIXTURE_DECK = """\
.LIB typical
 .lib 'sm141064.ngspice' nfet_03v3_t
.ENDL
*
.LIB ff
 .lib 'sm141064.ngspice' nfet_03v3_f
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' nfet_03v3_s
.ENDL
*
.LIB fs
 .lib 'sm141064.ngspice' nfet_03v3_fs
.ENDL
*
.LIB sf
 .lib 'sm141064.ngspice' nfet_03v3_sf
.ENDL
*
.LIB bjt_typical
.param
+isa=1
.lib 'sm141064.ngspice' bjt_mc
.ENDL
*
.LIB bjt_ff
.param
+isa=1.2
.lib 'sm141064.ngspice' bjt_mc
.ENDL
*
.LIB bjt_ss
.param
+isa=0.8
.lib 'sm141064.ngspice' bjt_mc
.ENDL
*
.LIB diode_typical
.param jsa=1
.lib 'sm141064.ngspice' dio
.ENDL
*
.LIB diode_ff
.param jsa=1.15
.lib 'sm141064.ngspice' dio
.ENDL
*
.LIB diode_ss
.param jsa=0.85
.lib 'sm141064.ngspice' dio
.ENDL
*
.LIB res_typical
.param rsh=60
.lib 'sm141064.ngspice' res
.ENDL
*
.lib res_ff
.param rsh=60
.lib 'sm141064.ngspice' res
.ENDL
*
.LIB res_ss
.param rsh=60
.lib 'sm141064.ngspice' res
.ENDL
*
.LIB mimcap_typical
.param mim_corner=1
.lib 'sm141064_mim.ngspice' cap_mim_new
.ENDL
*
.LIB mimcap_ff
.param mim_corner=0.9
.lib 'sm141064_mim.ngspice' cap_mim_new
.ENDL
*
.LIB mimcap_ss
.param mim_corner=1.1
.lib 'sm141064_mim.ngspice' cap_mim_new
.ENDL
*
.lib moscap_typical
.param cap_corner=1
.lib 'sm141064.ngspice' moscap
.ENDL
*
.lib moscap_ff
.param cap_corner=0.9
.lib 'sm141064.ngspice' moscap
.ENDL
*
.lib moscap_ss
.param cap_corner=1.1
.lib 'sm141064.ngspice' moscap
.ENDL
"""

#: A minimal sky130-shaped ngspice deck reproducing the real, live-verified
#: finding (2026-08-05, sky130A via volare) that sky130's own `.lib <corner>`
#: sections leave one of the two axes at typical depending on which corner
#: family they actually vary: `ss` (a MOSFET process corner) skews `mos` but
#: leaves `resistor_cap` bound to the exact same `r+c/res_typical...` include
#: (and identical `mc_mm_switch` boilerplate param) as `tt` -- the silent-
#: typical bug, caught here as `complete: False`. `ll` is the mirror case
#: (skews `resistor_cap`, leaves `mos` at typical). `mc` has neither a
#: `corners/` nor an `r+c/` include at all (a Monte-Carlo switch corner, not
#: a process corner), so both families report ``section: null``.
_SKY130_FIXTURE_DECK = """\
.lib tt
.param mc_mm_switch=0
.include "corners/tt.spice"
.include "r+c/res_typical__cap_typical.spice"
.endl tt

.lib ss
.param mc_mm_switch=0
.include "corners/ss.spice"
.include "r+c/res_typical__cap_typical.spice"
.endl ss

.lib ll
.param mc_mm_switch=0
.include "corners/tt.spice"
.include "r+c/res_low__cap_low.spice"
.endl ll

.lib mc
.param mc_pr_switch=1
.include "parameters/montecarlo.spice"
.endl mc
"""


def _make_corner_deck(variant_dir, filename, content):
    """Write ``content`` as ``libs.tech/ngspice/<filename>`` under
    ``variant_dir`` (``_make_install(..., assets=("ngspice",))`` must have
    already created the ``ngspice/`` directory)."""
    ngspice_dir = variant_dir / "libs.tech" / "ngspice"
    ngspice_dir.mkdir(parents=True, exist_ok=True)
    (ngspice_dir / filename).write_text(content, encoding="utf-8")


def test_corners_gf180mcu_classifies_skewed_param_typical_and_incomplete(tmp_path):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuD", assets=("ngspice",))
    _make_corner_deck(variant_dir, "sm141064.ngspice", _GF180MCU_FIXTURE_DECK)

    report = pdk.list_corners(root=str(root), variant="gf180mcuD")

    assert report["schema_version"] == 1
    assert report["pdk"] == "gf180mcuD"
    assert report["model_lib"] == str(
        variant_dir / "libs.tech" / "ngspice" / "sm141064.ngspice"
    )
    assert report["corner_names"] == ["typical", "ff", "ss", "fs", "sf"]

    by_corner = {corner["corner"]: corner for corner in report["corners"]}

    typical = by_corner["typical"]
    assert typical["complete"] is True
    assert {s["family"]: s["skew"] for s in typical["sections"]} == {
        "mos": "typical",
        "bjt": "typical",
        "diode": "typical",
        "resistor": "typical",
        "mim_cap": "typical",
        "mos_cap": "typical",
    }

    ff = by_corner["ff"]
    skews = {s["family"]: s["skew"] for s in ff["sections"]}
    assert skews["mos"] == "skewed"  # different named model (nfet_03v3_f)
    assert skews["bjt"] == "param"  # same shared model, differing .param
    assert skews["diode"] == "param"
    assert skews["mim_cap"] == "param"
    assert skews["resistor"] == "typical"  # same model AND same rsh=60 value
    # `resistor` never actually moved off typical at a non-typical corner:
    # the acceptance-critical "leaves a family at typical" signal.
    assert ff["complete"] is False

    fs = by_corner["fs"]
    fs_sections = {s["family"]: (s["section"], s["skew"]) for s in fs["sections"]}
    assert fs_sections["mos"] == ("fs", "skewed")
    assert fs_sections["bjt"] == (None, None)  # unlisted at this corner
    assert fs["complete"] is False


def test_corners_gf180mcu_boilerplate_param_not_misclassified_as_skew(tmp_path):
    """A `.param` line present but with an unchanged value from the family's
    typical section must classify as ``"typical"``, not ``"param"`` -- a
    naive "any `.param` line present" check would misclassify this (see
    :func:`klayout_tools.pdk._param_lines`'s docstring)."""
    deck = """\
.LIB typical
.param rsh_nplus_u_m=60
.lib 'sm141064.ngspice' res
.ENDL
*
.LIB ff
.param rsh_nplus_u_m=60
.lib 'sm141064.ngspice' res
.ENDL
"""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuA", assets=("ngspice",))
    # Reuse the "res" family prefix by naming sections after it directly --
    # the fixture only needs `_GF180MCU_FAMILY_PREFIXES`'s `res_` entry, so
    # rename the sections to match that prefix instead of "typical"/"ff".
    deck = deck.replace(".LIB typical", ".LIB res_typical").replace(
        ".LIB ff", ".LIB res_ff"
    )
    _make_corner_deck(variant_dir, "sm141064.ngspice", deck)

    report = pdk.list_corners(root=str(root), variant="gf180mcuA")

    by_corner = {corner["corner"]: corner for corner in report["corners"]}
    resistor_ff = next(
        s for s in by_corner["ff"]["sections"] if s["family"] == "resistor"
    )
    assert resistor_ff["section"] == "res_ff"
    assert resistor_ff["skew"] == "typical"


def test_corners_sky130_leaves_family_at_typical(tmp_path):
    """Reproduces the real, live-verified sky130A finding (2026-08-05): the
    `ss` process corner skews `mos` but leaves `resistor_cap` bound to the
    exact same typical R/C include -- and `ll` is the mirror case."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("ngspice",))
    _make_corner_deck(variant_dir, "sky130.lib.spice", _SKY130_FIXTURE_DECK)

    report = pdk.list_corners(root=str(root), variant="sky130A")

    assert report["corner_names"] == ["tt", "ss", "ll", "mc"]
    by_corner = {corner["corner"]: corner for corner in report["corners"]}

    tt = by_corner["tt"]
    assert tt["complete"] is True
    assert {s["family"]: s["skew"] for s in tt["sections"]} == {
        "mos": "typical",
        "resistor_cap": "typical",
    }

    ss = by_corner["ss"]
    ss_skews = {s["family"]: s["skew"] for s in ss["sections"]}
    assert ss_skews["mos"] == "skewed"
    assert ss_skews["resistor_cap"] == "typical"
    assert ss["complete"] is False

    ll = by_corner["ll"]
    ll_skews = {s["family"]: s["skew"] for s in ll["sections"]}
    assert ll_skews["mos"] == "typical"
    assert ll_skews["resistor_cap"] == "skewed"
    assert ll["complete"] is False

    mc = by_corner["mc"]
    assert {s["family"]: s["section"] for s in mc["sections"]} == {
        "mos": None,
        "resistor_cap": None,
    }
    assert mc["complete"] is False


def test_corners_unrecognised_pdk_family_is_empty_not_error(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "foobarA", assets=("ngspice",))

    report = pdk.list_corners(root=str(root), variant="foobarA")

    assert report["corners"] == []
    assert report["corner_names"] == []
    assert report["model_lib"] is None
    assert "no curated corner-family grouping" in report["resolved_via"]


def test_corners_no_ngspice_asset_is_empty_not_error(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A", assets=("magic",))

    report = pdk.list_corners(root=str(root), variant="sky130A")

    assert report["corners"] == []
    assert report["model_lib"] is None
    assert "ships no 'ngspice' asset directory" in report["resolved_via"]


def test_corners_missing_model_deck_file_is_empty_not_error(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "gf180mcuA", assets=("ngspice",))

    report = pdk.list_corners(root=str(root), variant="gf180mcuA")

    assert report["corners"] == []
    assert report["model_lib"] is None
    assert "sm141064.ngspice" in report["resolved_via"]
    assert "not found" in report["resolved_via"]


def test_corners_no_install_raises(tmp_path):
    with pytest.raises(pdk.PdkNotFoundError):
        pdk.list_corners(root=str(tmp_path / "nope"))


def test_cli_corners_json_on_stdout(tmp_path, capsys):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuD", assets=("ngspice",))
    _make_corner_deck(variant_dir, "sm141064.ngspice", _GF180MCU_FIXTURE_DECK)

    exit_code = main(["pdk", "corners", "--pdk-root", str(root), "--format", "json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["pdk"] == "gf180mcuD"
    assert payload["corner_names"] == ["typical", "ff", "ss", "fs", "sf"]


def test_cli_corners_text_table(tmp_path, capsys):
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A", assets=("ngspice",))
    _make_corner_deck(variant_dir, "sky130.lib.spice", _SKY130_FIXTURE_DECK)

    exit_code = main(["pdk", "corners", "--pdk-root", str(root)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "sky130A" in out
    assert "resistor_cap=typical" in out  # ss leaving resistor_cap at typical
    assert "no" in out  # ss/ll/mc all render "complete: no"


def test_cli_corners_empty_result_text(tmp_path, capsys):
    root = tmp_path / "install"
    _make_install(root, "foobarA", assets=("ngspice",))

    exit_code = main(["pdk", "corners", "--pdk-root", str(root), "--pdk", "foobarA"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "no corners resolved" in out


def test_cli_corners_no_install_error_envelope(tmp_path, capsys):
    exit_code = main(
        [
            "pdk",
            "corners",
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
    assert error["error"]["command"] == "pdk corners"

"""``resolve_cell_models_for_cell_library`` (issue #5782): the
``libs.ref/<lib>/verilog/`` view resolver behind ``klt
functional-verification``'s ``pdk`` request block.

Every case runs against fabricated open_pdks-layout installs under
``tmp_path`` (the ``tests/test_pdk_cells.py`` convention) -- CI never
downloads a real PDK. The per-variant view shapes the issue measured on a
real host are rebuilt here: the ``primitives.v`` + ``<lib>.v`` pair
(sky130A/sky130_fd_sc_hd), ``<lib>.v`` alone (gf180mcuD's
``gf180mcu_osu_sc_gp9t3v3``), no ``verilog/`` view at all (its sibling
``gp12t3v3``), and the ``__blackbox*.v`` siblings sky130 ships.
"""

from __future__ import annotations

import pytest

from helpers.pdk_install_fixtures import isolate_pdk_search_space
from helpers.pdk_install_fixtures import make_install as _make_install
from klayout_tools import pdk
from klayout_tools.functional_verification import FunctionalVerificationError
from klayout_tools.pdk_cell_models import resolve_cell_models_for_cell_library


def _make_verilog_view(
    variant_dir,
    lib,
    *,
    primitives=True,
    library_model=True,
    blackbox=False,
):
    """Fabricate a ``libs.ref/<lib>/verilog/`` view: ``primitives.v`` and/or
    ``<lib>.v``, plus the ``__blackbox*.v`` siblings sky130 ships."""
    view_dir = variant_dir / "libs.ref" / lib / "verilog"
    view_dir.mkdir(parents=True)
    if primitives:
        (view_dir / "primitives.v").write_text(
            "`celldefine module udp_dff; endmodule `endcelldefine\n",
            encoding="utf-8",
        )
    if library_model:
        (view_dir / f"{lib}.v").write_text(
            f"`ifdef FUNCTIONAL\nmodule {lib}__and2; endmodule\n`endif\n",
            encoding="utf-8",
        )
    if blackbox:
        (view_dir / f"{lib}__blackbox.v").write_text("", encoding="utf-8")
        (view_dir / f"{lib}__blackbox_pp.v").write_text("", encoding="utf-8")
    return variant_dir / "libs.ref" / lib


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_pdk_search_space(monkeypatch, tmp_path)


def _install(tmp_path, variant="sky130A"):
    return _make_install(tmp_path / "install", variant, assets=("libs_ref",))


def test_primitives_and_library_model_returned_in_dependency_order(tmp_path):
    variant_dir = _install(tmp_path)
    _make_verilog_view(variant_dir, "sky130_fd_sc_hd")

    models, info = resolve_cell_models_for_cell_library(
        "sky130_fd_sc_hd",
        FunctionalVerificationError,
        variant="sky130A",
        root=str(tmp_path / "install"),
    )

    # primitives.v first: it defines the UDPs <lib>.v instantiates.
    assert models == [
        str(variant_dir / "libs.ref" / "sky130_fd_sc_hd" / "verilog" / "primitives.v"),
        str(
            variant_dir
            / "libs.ref"
            / "sky130_fd_sc_hd"
            / "verilog"
            / "sky130_fd_sc_hd.v"
        ),
    ]
    assert info["variant"] == "sky130A"


def test_library_model_only_resolves_with_one_entry(tmp_path):
    """The ``gf180mcu_osu_sc_gp9t3v3`` shape (issue #5782 AC3): a library
    that ships ``<lib>.v`` with no ``primitives.v`` is usable, not an
    error."""
    variant_dir = _install(tmp_path)
    _make_verilog_view(variant_dir, "gf180mcu_osu_sc_gp9t3v3", primitives=False)

    models, _ = resolve_cell_models_for_cell_library(
        "gf180mcu_osu_sc_gp9t3v3",
        FunctionalVerificationError,
        root=str(tmp_path / "install"),
    )

    assert models == [
        str(
            variant_dir
            / "libs.ref"
            / "gf180mcu_osu_sc_gp9t3v3"
            / "verilog"
            / "gf180mcu_osu_sc_gp9t3v3.v"
        )
    ]


def test_no_verilog_view_is_a_named_refusal(tmp_path):
    """The ``gf180mcu_osu_sc_gp12t3v3`` shape (issue #5782 AC4): a library
    with ``lef``/``lib`` views but no ``verilog/`` at all resolves fine for
    `klt synthesize` and has no simulation models -- its own refusal, never
    an empty source list."""
    variant_dir = _install(tmp_path)
    lib_dir = variant_dir / "libs.ref" / "gf180mcu_osu_sc_gp12t3v3"
    (lib_dir / "lef").mkdir(parents=True)
    (lib_dir / "lib").mkdir(parents=True)

    with pytest.raises(
        FunctionalVerificationError, match="no verilog/ view"
    ) as exc_info:
        resolve_cell_models_for_cell_library(
            "gf180mcu_osu_sc_gp12t3v3",
            FunctionalVerificationError,
            root=str(tmp_path / "install"),
        )
    assert exc_info.value.code == "pdk_no_verilog_view"


def test_verilog_view_without_library_model_is_exit_one(tmp_path):
    """AC3: the ``verilog/`` view exists but ships no ``<lib>.v`` -- the
    required file, so exit 1, including the only-``__blackbox*`` case (a
    blackbox has no behaviour)."""
    variant_dir = _install(tmp_path)
    _make_verilog_view(variant_dir, "sky130_fd_sc_hd", library_model=False)

    with pytest.raises(
        FunctionalVerificationError, match="no sky130_fd_sc_hd\\.v"
    ) as exc_info:
        resolve_cell_models_for_cell_library(
            "sky130_fd_sc_hd",
            FunctionalVerificationError,
            root=str(tmp_path / "install"),
        )
    assert exc_info.value.code == "pdk_no_cell_models"


def test_blackbox_siblings_are_never_selected(tmp_path):
    """AC6: sky130's ``__blackbox.v``/``__blackbox_pp.v`` ship beside the
    real models and must not appear in the resolved list."""
    variant_dir = _install(tmp_path)
    _make_verilog_view(variant_dir, "sky130_fd_sc_hd", blackbox=True)

    models, _ = resolve_cell_models_for_cell_library(
        "sky130_fd_sc_hd",
        FunctionalVerificationError,
        variant="sky130A",
        root=str(tmp_path / "install"),
    )

    assert [name for name in (m.split("/")[-1] for m in models)] == [
        "primitives.v",
        "sky130_fd_sc_hd.v",
    ]


def test_library_absent_from_install_is_a_named_refusal(tmp_path):
    _install(tmp_path)

    with pytest.raises(
        FunctionalVerificationError, match="not found under resolved PDK install"
    ) as exc_info:
        resolve_cell_models_for_cell_library(
            "no_such_library",
            FunctionalVerificationError,
            root=str(tmp_path / "install"),
        )
    assert exc_info.value.code == "pdk_no_verilog_view"


def test_install_without_libs_ref_is_a_named_refusal(tmp_path):
    _make_install(tmp_path / "install", "sky130A")

    with pytest.raises(
        FunctionalVerificationError, match="ships no libs_ref"
    ) as exc_info:
        resolve_cell_models_for_cell_library(
            "sky130_fd_sc_hd",
            FunctionalVerificationError,
            root=str(tmp_path / "install"),
        )
    assert exc_info.value.code == "pdk_no_verilog_view"


def test_ambiguous_install_without_pinned_root_is_a_named_refusal(
    tmp_path, monkeypatch
):
    """AC4: two installs both shipping the variant and no explicit ``root``
    is the ambiguity ``find_pdk`` reports -- refuse rather than guess which
    install's models to elaborate (the plausible-looking-pass failure this
    resolver exists to prevent)."""
    for name in ("first", "second"):
        variant_dir = _make_install(tmp_path / name, "sky130A", assets=("libs_ref",))
        _make_verilog_view(variant_dir, "sky130_fd_sc_hd")
    monkeypatch.setattr(
        pdk, "STORE_DIRS", [str(tmp_path / "first"), str(tmp_path / "second")]
    )

    with pytest.raises(FunctionalVerificationError, match="ambiguous") as exc_info:
        resolve_cell_models_for_cell_library(
            "sky130_fd_sc_hd", FunctionalVerificationError, variant="sky130A"
        )
    assert exc_info.value.code == "pdk_ambiguous_install"
    assert str(tmp_path / "second") in str(exc_info.value)


def test_ambiguous_install_with_pinned_root_resolves(tmp_path):
    """The same two-install case with an explicit ``root`` (the ``--pdk-root``
    fix the refusal's own message names): ``find_pdk`` disables the search
    for it, so the ambiguity never fires."""
    for name in ("first", "second"):
        variant_dir = _make_install(tmp_path / name, "sky130A", assets=("libs_ref",))
        _make_verilog_view(variant_dir, "sky130_fd_sc_hd")

    models, info = resolve_cell_models_for_cell_library(
        "sky130_fd_sc_hd",
        FunctionalVerificationError,
        variant="sky130A",
        root=str(tmp_path / "second"),
    )

    assert str(tmp_path / "second") in models[0]
    assert info["root"] == str(tmp_path / "second")


def test_no_pdk_installed_raises_the_callers_error_class(tmp_path, monkeypatch):
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "SHARED_ROOTS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])

    with pytest.raises(FunctionalVerificationError, match="no supported-layout PDK"):
        resolve_cell_models_for_cell_library(
            "sky130_fd_sc_hd",
            FunctionalVerificationError,
            variant="sky130A",
        )

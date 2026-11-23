"""The chosen Liberty install reports how its root was discovered."""

import pytest

from helpers.pdk_install_fixtures import (
    isolate_pdk_search_space,
    make_cell_library,
    make_install,
)
from klayout_tools import pdk, pdk_cells


@pytest.mark.parametrize(
    ("mode", "label"),
    [
        ("search", "search root: ~/.volare"),
        ("environment", "PDK_ROOT environment variable"),
        ("flag", "--pdk-root flag"),
    ],
)
def test_substituted_variant_preserves_selected_root_source(
    tmp_path, monkeypatch, mode, label
):
    isolate_pdk_search_space(monkeypatch, tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel", "~/.volare"])
    root = home / ".volare"
    make_install(root, "gf180mcuA")
    make_cell_library(
        make_install(root, "sky130A", assets=("libs_ref",)), "sky130_fd_sc_hd"
    )
    if mode == "environment":
        monkeypatch.setenv("PDK_ROOT", str(root))
    if mode == "search":
        # The default variant comes from a different, higher-priority root.
        make_install(home / ".ciel", "gf180mcuA")
    explicit_root = str(root) if mode == "flag" else None
    assert pdk.find_pdk(root=explicit_root)["variant"] == "gf180mcuA"

    path, corner, info = pdk_cells.resolve_liberty_for_cell_library(
        "sky130_fd_sc_hd", None, ValueError, root=explicit_root
    )

    assert info == {
        **pdk.find_pdk(variant="sky130A", root=str(root)),
        "resolved_via": label,
    }
    assert path == str(
        root
        / "sky130A"
        / "libs.ref"
        / "sky130_fd_sc_hd"
        / "lib"
        / f"sky130_fd_sc_hd__{corner}.lib"
    )

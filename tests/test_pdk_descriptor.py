"""Tests for the explicit ``klt-pdk.json`` PDK descriptor (issue #3782).

All trees are synthetic ``tmp_path`` fabrications -- no vendored PDK data.
"""

import json

import pytest

from klayout_tools import pdk, pdk_cells
from klayout_tools.cli import main


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "SHARED_ROOTS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def _make_tree(tmp_path, descriptor=None, *, lib=True):
    root = tmp_path / "asap7"
    (root / "cells").mkdir(parents=True)
    if lib:
        (root / "cells" / "tt.lib").write_text("library (x) {}\n", encoding="utf-8")
    if descriptor is None:
        descriptor = {
            "schema_version": 1,
            "variant": "asap7",
            "assets": {"libs_ref": "cells"},
            "liberty": {"asap7sc7p5t": {"tt": "cells/tt.lib"}},
        }
    (root / "klt-pdk.json").write_text(json.dumps(descriptor), encoding="utf-8")
    return root


def test_descriptor_root_resolves(tmp_path):
    root = _make_tree(tmp_path)
    info = pdk.find_pdk(root=str(root))
    assert info["variant"] == "asap7"
    assert info["layout"] == "descriptor"
    assert info["assets"]["libs_ref"] == str(root / "cells")
    assert info["assets"]["klayout"] is None
    assert pdk.variant_dir_of(info) == str(root)


def test_descriptor_nested_variant_resolves(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    tree = _make_tree(tmp_path)
    tree.rename(store / "asap7")
    info = pdk.find_pdk(variant="asap7", root=str(store))
    assert info["layout"] == "descriptor"
    assert pdk.variant_dir_of(info) == str(store / "asap7")


def test_cli_pdk_find_json(tmp_path, capsys):
    root = _make_tree(tmp_path)
    assert main(["pdk", "find", "--pdk-root", str(root), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["layout"] == "descriptor"
    assert payload["schema_version"] == 1


def test_descriptor_wins_over_libs_tech(tmp_path):
    root = _make_tree(tmp_path)
    (root / "libs.tech" / "ngspice").mkdir(parents=True)
    info = pdk.find_pdk(root=str(root))
    assert info["layout"] == "descriptor"
    assert info["assets"]["ngspice"] is None


def test_liberty_from_descriptor_map(tmp_path):
    root = _make_tree(tmp_path)
    path, corner, info = pdk_cells.resolve_liberty_for_cell_library(
        "asap7sc7p5t", None, ValueError, root=str(root)
    )
    assert path == str(root / "cells" / "tt.lib")
    assert corner == "tt"
    assert info["layout"] == "descriptor"


def test_unmapped_cell_library_uses_convention(tmp_path):
    root = _make_tree(tmp_path)
    lib_dir = root / "cells" / "other" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "other__tt.lib").write_text("library (o) {}\n", encoding="utf-8")
    path, corner, _ = pdk_cells.resolve_liberty_for_cell_library(
        "other", "tt", ValueError, root=str(root)
    )
    assert path == str(lib_dir / "other__tt.lib")
    assert corner == "tt"


def test_ambiguous_mapped_corner_requires_explicit_corner(tmp_path):
    root = _make_tree(
        tmp_path,
        {
            "schema_version": 1,
            "assets": {"libs_ref": "cells"},
            "liberty": {"c": {"tt": "cells/tt.lib", "ss": "cells/tt.lib"}},
        },
    )
    with pytest.raises(ValueError, match="pass request.pdk.corner"):
        pdk_cells.resolve_liberty_for_cell_library(
            "c", None, ValueError, root=str(root)
        )
    path, corner, _ = pdk_cells.resolve_liberty_for_cell_library(
        "c", "ss", ValueError, root=str(root)
    )
    assert corner == "ss"


@pytest.mark.parametrize(
    "descriptor, match",
    [
        ({"schema_version": 2}, "schema_version"),
        ({"schema_version": 1, "assets": {"bogus": "cells"}}, "assets.bogus"),
        ({"schema_version": 1, "assets": {"libs_ref": "../x"}}, "escapes the PDK root"),
        ({"schema_version": 1, "assets": {"libs_ref": "/abs"}}, "must be relative"),
        (
            {"schema_version": 1, "liberty": {"c": {"tt": "../tt.lib"}}},
            "escapes the PDK root",
        ),
        (
            {"schema_version": 1, "liberty": {"c": {"tt": "cells/nope.lib"}}},
            "liberty.c.tt",
        ),
        ([], "JSON object"),
    ],
)
def test_bad_descriptor_errors(tmp_path, descriptor, match):
    root = _make_tree(tmp_path, descriptor)
    with pytest.raises(pdk.PdkNotFoundError, match=match):
        pdk.find_pdk(root=str(root))


def test_invalid_json_errors(tmp_path):
    root = _make_tree(tmp_path)
    (root / "klt-pdk.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(pdk.PdkNotFoundError, match="klt-pdk.json"):
        pdk.find_pdk(root=str(root))


def test_missing_lib_with_sibling_7z_names_policy(tmp_path):
    root = _make_tree(tmp_path, lib=False)
    (root / "cells" / "tt.lib.7z").write_bytes(b"7z")
    with pytest.raises(pdk.PdkNotFoundError, match=r"ships only as \.7z"):
        pdk.find_pdk(root=str(root))


def test_symlinked_asset_dir_inside_root_resolves(tmp_path):
    # Containment is lexical: a symlink the PDK checkout itself ships (e.g. a
    # submodule link) is followed, only `..`/absolute paths are rejected.
    root = _make_tree(tmp_path)
    (root / "link").symlink_to(root / "cells")
    (root / "klt-pdk.json").write_text(
        json.dumps({"schema_version": 1, "assets": {"libs_ref": "link"}}),
        encoding="utf-8",
    )
    assert pdk.find_pdk(root=str(root))["assets"]["libs_ref"] == str(root / "link")

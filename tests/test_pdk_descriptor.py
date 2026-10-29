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


# --- views + multi-file Liberty (issue #4087) -------------------------------

_HEADER = 'library (x) {\n  time_unit : "1ps";\n'


def _lib(*cells, header=_HEADER):
    body = "".join(f"  cell ({c}) {{ area : 1; }}\n" for c in cells)
    return header + body + "}\n"


def _views_tree(tmp_path, monkeypatch, liberty=None):
    monkeypatch.setenv("KLT_CACHE_DIR", str(tmp_path / "cache"))
    root = tmp_path / "up"
    (root / "d").mkdir(parents=True)
    for name in ("a.lef", "t_nom.tlef", "t_max.tlef", "x.gds"):
        (root / "d" / name).write_text("x", encoding="utf-8")
    (root / "d" / "g1.lib").write_text(_lib("A", "B"), encoding="utf-8")
    (root / "d" / "g2.lib").write_text(_lib("C"), encoding="utf-8")
    (root / "klt-pdk.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "variant": "up",
                "assets": {"libs_ref": "d"},
                "liberty": liberty or {"lib": {"tt": ["d/g1.lib", "d/g2.lib"]}},
                "views": {
                    "lib": {
                        "cell_lef": "d/a.lef",
                        "tech_lef": {"nom": "d/t_nom.tlef", "max": "d/t_max.tlef"},
                        "gds": "d/x.gds",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def test_views_resolve_lef_and_gds(tmp_path, monkeypatch):
    root = _views_tree(tmp_path, monkeypatch)
    lefs = pdk.lef_files("lib", root=str(root))
    assert lefs == {
        "tech_lef": str(root / "d" / "t_nom.tlef"),
        "cell_lef": str(root / "d" / "a.lef"),
    }
    assert pdk.lef_files("lib", root=str(root), corner="max")["tech_lef"].endswith(
        "t_max.tlef"
    )
    info = pdk.find_pdk(root=str(root))
    from klayout_tools.place_and_route_request import _resolve_gds_view

    assert _resolve_gds_view(info, "lib") == str(root / "d" / "x.gds")


def test_unmapped_library_keeps_convention(tmp_path, monkeypatch):
    root = _views_tree(tmp_path, monkeypatch)
    assert pdk.lef_files("other", root=str(root)) == {
        "tech_lef": None,
        "cell_lef": None,
    }


def test_unmapped_tech_lef_corner_names_mapped(tmp_path, monkeypatch):
    root = _views_tree(tmp_path, monkeypatch)
    with pytest.raises(pdk.PdkNotFoundError, match="mapped: max, nom"):
        pdk.lef_files("lib", root=str(root), corner="min")


@pytest.mark.parametrize("bad", ["/abs/a.lef", "../a.lef", "d/missing.lef"])
def test_bad_view_path_rejected(tmp_path, monkeypatch, bad):
    root = _views_tree(tmp_path, monkeypatch)
    desc = json.loads((root / "klt-pdk.json").read_text())
    desc["views"]["lib"]["cell_lef"] = bad
    (root / "klt-pdk.json").write_text(json.dumps(desc), encoding="utf-8")
    with pytest.raises(pdk.PdkNotFoundError, match="views.lib.cell_lef'"):
        pdk.find_pdk(root=str(root))


def test_liberty_list_merges_outside_pdk_tree(tmp_path, monkeypatch):
    root = _views_tree(tmp_path, monkeypatch)
    info = pdk.find_pdk(root=str(root))
    path, corner = pdk_cells._descriptor_liberty(info, "lib", None, ValueError)
    assert corner == "tt"
    assert not path.startswith(str(root))
    text = open(path, encoding="utf-8").read()
    assert all(f"cell ({c})" in text for c in "ABC")
    mtime = __import__("os").stat(path).st_mtime_ns
    again, _ = pdk_cells._descriptor_liberty(info, "lib", None, ValueError)
    assert again == path
    assert __import__("os").stat(path).st_mtime_ns == mtime


def test_liberty_one_element_list_equals_string(tmp_path, monkeypatch):
    root = _views_tree(tmp_path, monkeypatch, liberty={"lib": {"tt": ["d/g1.lib"]}})
    info = pdk.find_pdk(root=str(root))
    path, _ = pdk_cells._descriptor_liberty(info, "lib", None, ValueError)
    assert path == str(root / "d" / "g1.lib")


@pytest.mark.parametrize(
    "header",
    [
        'library (x_group2) {\n  /* generated 2026-01-02 */\n  time_unit : "1ps";\n',
        'library (x) {\n  // generated 2026-01-02\n  time_unit : "1ps";\n',
        'library (other) {\n  time_unit : "1ps";\n',
    ],
)
def test_liberty_header_name_and_comment_differences_merge(
    tmp_path, monkeypatch, header
):
    root = _views_tree(tmp_path, monkeypatch)
    info = pdk.find_pdk(root=str(root))
    (root / "d" / "g2.lib").write_text(_lib("C", header=header), encoding="utf-8")
    result = pdk_cells._descriptor_liberty(info, "lib", None, ValueError)
    path = result[0] if isinstance(result, tuple) else result
    merged = open(path, encoding="utf-8").read()
    assert all(f"cell ({c})" in merged for c in "ABC")


_TPL = (
    '  lu_table_template ("{n}") {{\n    variable_1 : input_net_transition;\n'
    '    index_1 ("{i}");\n  }}\n'
    "  power_lut_template (p_{n}) {{ variable_1 : input_transition_time; }}\n"
)


def _tpl_header(*names, extra='  time_unit : "1ps";\n', body="7"):
    return "library (x) {\n" + extra + "".join(_TPL.format(n=n, i=body) for n in names)


def _merge_g2(tmp_path, monkeypatch, h1, h2):
    root = _views_tree(tmp_path, monkeypatch)
    info = pdk.find_pdk(root=str(root))
    (root / "d" / "g1.lib").write_text(_lib("A", header=h1), encoding="utf-8")
    (root / "d" / "g2.lib").write_text(_lib("C", header=h2), encoding="utf-8")
    result = pdk_cells._descriptor_liberty(info, "lib", None, ValueError)
    path = result[0] if isinstance(result, tuple) else result
    return open(path, encoding="utf-8").read()


def test_liberty_disjoint_templates_are_unioned(tmp_path, monkeypatch):
    merged = _merge_g2(
        tmp_path, monkeypatch, _tpl_header("d_7x7"), _tpl_header("d_7x7_x1")
    )
    assert merged.count('lu_table_template ("d_7x7")') == 1
    assert merged.count('lu_table_template ("d_7x7_x1")') == 1
    assert merged.count("power_lut_template (p_d_7x7_x1)") == 1
    assert all(f"cell ({c})" in merged for c in "AC")


def test_liberty_identical_templates_dedupe(tmp_path, monkeypatch):
    h2 = _tpl_header("d", body="7").replace("  ", " /* c */ ")
    merged = _merge_g2(tmp_path, monkeypatch, _tpl_header("d"), h2)
    assert merged.count("lu_table_template") == 1
    assert merged.count("power_lut_template") == 1


def test_liberty_matching_headers_output_unchanged(tmp_path, monkeypatch):
    h = _tpl_header("d")
    merged = _merge_g2(tmp_path, monkeypatch, h, h)
    expected = h.rstrip() + "\n\ncell (A) { area : 1; }\ncell (C) { area : 1; }\n}\n"
    assert merged == expected


def test_liberty_conflicting_template_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="template 'd' differs"):
        _merge_g2(
            tmp_path,
            monkeypatch,
            _tpl_header("d", body="7"),
            _tpl_header("d", body="9"),
        )


def test_liberty_non_template_difference_with_templates_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="headers differ"):
        _merge_g2(
            tmp_path,
            monkeypatch,
            _tpl_header("a"),
            _tpl_header("b", extra='  time_unit : "1ns";\n'),
        )


def test_liberty_header_mismatch_and_duplicate(tmp_path, monkeypatch):
    root = _views_tree(tmp_path, monkeypatch)
    info = pdk.find_pdk(root=str(root))
    (root / "d" / "g2.lib").write_text(
        _lib("C", header='library (x) {\n  time_unit : "1ns";\n'), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="headers differ"):
        pdk_cells._descriptor_liberty(info, "lib", None, ValueError)
    (root / "d" / "g2.lib").write_text(_lib("B"), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate cell 'B'"):
        pdk_cells._descriptor_liberty(info, "lib", None, ValueError)

"""``$VAR`` / ``~`` expansion in the HDL verbs' request path fields (issue #5678).

``klt functional-verification`` / ``synthesize`` / ``formal`` / ``equiv`` used to
hand-roll ``isabs``/``join`` for ``sources``, so a committed request naming
``$PDK_ROOT/...`` could not run as-is. They now route through
``_paths._resolve_request_path`` (``_resolve_relative`` plus a named error for
an unset variable), matching ``klt lvs``/``drc``/``extract``/``sim``.
"""

from __future__ import annotations

import pytest

from klayout_tools import equiv, formal, functional_verification, synthesize
from klayout_tools._paths import _resolve_request_path, _unset_env_vars
from klayout_tools.equiv import EquivError
from klayout_tools.formal import FormalError
from klayout_tools.functional_verification import FunctionalVerificationError
from klayout_tools.synthesize import SynthesizeError

_VERBS = [
    pytest.param(
        functional_verification._resolve_sources,
        FunctionalVerificationError,
        id="functional-verification",
    ),
    pytest.param(synthesize._resolve_sources, SynthesizeError, id="synthesize"),
    pytest.param(formal._resolve_sources, FormalError, id="formal"),
    pytest.param(
        lambda sources, request_dir: equiv._resolve_side(
            {"sources": sources, "top": "top"}, request_dir, "gold"
        )["sources"],
        EquivError,
        id="equiv",
    ),
]


@pytest.fixture
def pdk_tree(tmp_path, monkeypatch):
    root = tmp_path / "pdk"
    (root / "libs").mkdir(parents=True)
    (root / "libs" / "primitives.v").write_text("module p; endmodule\n")
    monkeypatch.setenv("PDK_ROOT", str(root))
    request_dir = tmp_path / "req"
    request_dir.mkdir()
    return root, request_dir


@pytest.mark.parametrize("resolve, _err", _VERBS)
def test_dollar_var_source_resolves(pdk_tree, resolve, _err):
    root, request_dir = pdk_tree
    resolved = resolve(["$PDK_ROOT/libs/primitives.v"], str(request_dir))
    assert resolved == [str(root / "libs" / "primitives.v")]


@pytest.mark.parametrize("resolve, _err", _VERBS)
def test_braced_var_source_resolves(pdk_tree, resolve, _err):
    root, request_dir = pdk_tree
    resolved = resolve(["${PDK_ROOT}/libs/primitives.v"], str(request_dir))
    assert resolved == [str(root / "libs" / "primitives.v")]


@pytest.mark.parametrize("resolve, _err", _VERBS)
def test_tilde_source_resolves(tmp_path, monkeypatch, resolve, _err):
    home = tmp_path / "home"
    home.mkdir()
    (home / "a.v").write_text("module a; endmodule\n")
    monkeypatch.setenv("HOME", str(home))
    request_dir = tmp_path / "req"
    request_dir.mkdir()
    assert resolve(["~/a.v"], str(request_dir)) == [str(home / "a.v")]


@pytest.mark.parametrize("resolve, _err", _VERBS)
def test_var_expanding_to_relative_path_anchors_at_request_dir(
    tmp_path, monkeypatch, resolve, _err
):
    request_dir = tmp_path / "req"
    (request_dir / "rtl").mkdir(parents=True)
    (request_dir / "rtl" / "a.v").write_text("module a; endmodule\n")
    monkeypatch.setenv("RTL_DIR", "rtl")
    assert resolve(["$RTL_DIR/a.v"], str(request_dir)) == [
        str(request_dir / "rtl" / "a.v")
    ]


@pytest.mark.parametrize("resolve, err", _VERBS)
def test_unset_var_names_the_variable(tmp_path, monkeypatch, resolve, err):
    monkeypatch.delenv("KLT_TEST_NOPE", raising=False)
    with pytest.raises(err, match=r"unset environment variable \$KLT_TEST_NOPE"):
        resolve(["$KLT_TEST_NOPE/primitives.v"], str(tmp_path))


@pytest.mark.parametrize("resolve, err", _VERBS)
def test_unset_braced_var_names_the_variable(tmp_path, monkeypatch, resolve, err):
    monkeypatch.delenv("KLT_TEST_NOPE", raising=False)
    with pytest.raises(err, match=r"unset environment variable \$KLT_TEST_NOPE"):
        resolve(["${KLT_TEST_NOPE}/primitives.v"], str(tmp_path))


@pytest.mark.parametrize("resolve, err", _VERBS)
def test_literal_missing_path_keeps_not_found_error(tmp_path, resolve, err):
    with pytest.raises(err, match="not found: nope.v"):
        resolve(["nope.v"], str(tmp_path))


@pytest.mark.parametrize("resolve, _err", _VERBS)
def test_literal_relative_and_absolute_paths_unchanged(tmp_path, resolve, _err):
    rel = tmp_path / "rel.v"
    rel.write_text("module r; endmodule\n")
    other = tmp_path / "elsewhere"
    other.mkdir()
    ab = other / "abs.v"
    ab.write_text("module a; endmodule\n")
    assert resolve(["rel.v", str(ab)], str(tmp_path)) == [str(rel), str(ab)]


def test_dollar_not_a_variable_reference_is_literal(tmp_path):
    assert _unset_env_vars("a$/b$") == []
    assert _resolve_request_path(
        "a$/b", str(tmp_path), error_cls=ValueError, field="f"
    ) == str(tmp_path / "a$" / "b")


def test_multiple_unset_vars_are_all_named(tmp_path, monkeypatch):
    monkeypatch.delenv("KLT_A", raising=False)
    monkeypatch.delenv("KLT_B", raising=False)
    with pytest.raises(ValueError, match=r"\$KLT_A, \$KLT_B"):
        _resolve_request_path(
            "$KLT_A/$KLT_B/x.v", str(tmp_path), error_cls=ValueError, field="f"
        )


def test_formal_optional_file_expands_var(tmp_path, monkeypatch):
    lib = tmp_path / "cells.lib"
    lib.write_text("library (x) {}\n")
    monkeypatch.setenv("LIBDIR", str(tmp_path))
    assert formal._resolve_optional_file(
        "$LIBDIR/cells.lib", str(tmp_path / "other"), "liberty"
    ) == str(lib)


def test_equiv_liberty_expands_var(tmp_path, monkeypatch):
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    lib = tmp_path / "cells.lib"
    lib.write_text("library (x) {}\n")
    monkeypatch.setenv("LIBDIR", str(tmp_path))
    side = equiv._resolve_side(
        {"sources": ["a.v"], "top": "a", "liberty": "$LIBDIR/cells.lib"},
        str(tmp_path),
        "gate",
    )
    assert side["liberty"] == str(lib)


def test_includes_and_source_defines_expand_var(tmp_path, monkeypatch):
    inc = tmp_path / "inc"
    inc.mkdir()
    monkeypatch.setenv("INC_DIR", str(inc))
    assert functional_verification._resolve_includes(
        ["$INC_DIR"], str(tmp_path / "other")
    ) == [str(inc)]

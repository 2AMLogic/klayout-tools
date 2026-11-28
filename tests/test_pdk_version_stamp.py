"""Issue #3070: ``pdk.read_version`` fallbacks and the ``linked_trees`` key.

Issue #8007 (appended): install evidence -- what placed a resolved tree and
where it sits, read from the store marker independently of the version stamp.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from klayout_tools import _provenance, pdk
from klayout_tools.cli import main

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _commit_all(cwd):
    _git(cwd, "init", "-q")
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", "x")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _variant(path):
    (path / "libs.tech").mkdir(parents=True)
    return path


def test_sources_unchanged(tmp_path):
    v = _variant(tmp_path / "v")
    (v / "SOURCES").write_text("open_pdks abc\n  magic def \n", encoding="utf-8")
    (v / ".fetched-version").write_text("1.0", encoding="utf-8")
    assert pdk.read_version(str(v)) == "open_pdks abc; magic def"


def test_none_when_nothing(tmp_path):
    assert pdk.read_version(str(_variant(tmp_path / "v"))) is None


def test_fetched_marker(tmp_path):
    v = _variant(tmp_path / "v")
    (v / ".fetched-version").write_text("v1.2.3\n", encoding="utf-8")
    assert pdk.read_version(str(v)) == "fetched:v1.2.3"


@needs_git
def test_git_clean_and_dirty(tmp_path):
    v = _variant(tmp_path / "v")
    (v / "libs.tech" / "f").write_text("a", encoding="utf-8")
    head = _commit_all(v)
    assert pdk.read_version(str(v)) == f"git:{head}"
    (v / "libs.tech" / "f").write_text("b", encoding="utf-8")
    assert pdk.read_version(str(v)) == f"git:{head}-dirty"


@needs_git
def test_git_stamp_distinguishes_untracked_and_indexed_files(tmp_path):
    variant = _variant(tmp_path / "root" / "variant")
    (variant / "model.lib").write_text("model", encoding="utf-8")
    head = _commit_all(variant.parent)
    extra = variant / "extra.lib"
    extra.write_text("local model", encoding="utf-8")

    assert pdk.read_version(str(variant)) == f"git:{head}"
    _git(variant, "add", "extra.lib")
    assert pdk.read_version(str(variant)) == f"git:{head}-dirty"


@needs_git
def test_git_at_pdk_root_parent(tmp_path):
    v = _variant(tmp_path / "root" / "v")
    (v / "f").write_text("a", encoding="utf-8")
    head = _commit_all(tmp_path / "root")
    assert pdk.read_version(str(v)) == f"git:{head}"


@needs_git
def test_unrelated_enclosing_repo_ignored(tmp_path):
    (tmp_path / "klt.txt").write_text("a", encoding="utf-8")
    _commit_all(tmp_path)
    v = _variant(tmp_path / "pdks" / "deep" / "ihp")
    assert pdk.read_version(str(v)) is None
    (v / ".fetched-version").write_text("r1", encoding="utf-8")
    assert pdk.read_version(str(v)) == "fetched:r1"


def test_git_missing(tmp_path, monkeypatch):
    v = _variant(tmp_path / "v")

    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(pdk.subprocess, "run", boom)
    assert pdk.read_version(str(v)) is None


def _info(root, name):
    return {
        "root": str(root),
        "variant": name,
        "resolved_via": "--pdk-root flag",
        "version": None,
    }


def test_linked_trees_present_for_cross_variant_symlink(tmp_path):
    a = _variant(tmp_path / "A")
    b = _variant(tmp_path / "B")
    (b / "SOURCES").write_text("rev-b", encoding="utf-8")
    (b / "models").mkdir()
    (b / "models" / "lib.spice").write_text("*", encoding="utf-8")
    (a / "models").mkdir()
    (a / "models" / "lib.spice").symlink_to("../../B/models/lib.spice")
    block = _provenance._pdk_block(
        _info(tmp_path, "A"), str(a / "models" / "lib.spice")
    )
    assert block["linked_trees"] == [{"name": "B", "version": "rev-b"}]
    assert str(tmp_path) not in repr(block["linked_trees"])


def test_linked_trees_absent_without_cross_tree(tmp_path):
    a = _variant(tmp_path / "A")
    (a / "lib.spice").write_text("*", encoding="utf-8")
    block = _provenance._pdk_block(_info(tmp_path, "A"), str(a / "lib.spice"))
    assert "linked_trees" not in block
    assert block == {"name": "A", "source": "--pdk-root flag", "version": None}
    assert "linked_trees" not in _provenance._pdk_block(_info(tmp_path, "A"))


def _store_bundle(tmp_path, payload):
    bundle = tmp_path / "bundle"
    (bundle / "sky130A").mkdir(parents=True)
    if payload is not None:
        (bundle / ".pdk-store.json").write_text(payload, encoding="utf-8")
    link = tmp_path / "pdkroot" / "sky130A"
    link.parent.mkdir()
    link.symlink_to(bundle / "sky130A")
    return link


def test_store_version_fallback_via_symlink(tmp_path):
    link = _store_bundle(tmp_path, '{"version": "2026.08.03", "sha256": "x"}')
    assert pdk.read_version(str(link)) == "store:2026.08.03"


def test_sources_wins_over_store(tmp_path):
    link = _store_bundle(tmp_path, '{"version": "1"}')
    (link / "SOURCES").write_text("open_pdks abc\n", encoding="utf-8")
    assert pdk.read_version(str(link)) == "open_pdks abc"


@pytest.mark.parametrize(
    "payload", [None, "{not json", "[1]", '{"version": 3}', '{"version": "  "}']
)
def test_store_version_malformed_is_none(tmp_path, payload):
    assert pdk.read_version(str(_store_bundle(tmp_path, payload))) is None


# --------------------------------------------------------------------------- #
# Issue #5624: the stamp is also accepted at the variant root (where the
# fleet's provisioner writes it); the nearest stamp wins.
# --------------------------------------------------------------------------- #


def test_store_stamp_at_variant_root(tmp_path):
    v = _variant(tmp_path / "r" / "v")
    (v / ".pdk-store.json").write_text(
        '{"version": "2026.11.14", "sha256": "9f2c"}', encoding="utf-8"
    )
    assert pdk.read_version(str(v)) == "store:2026.11.14"


def test_store_stamp_at_variant_root_via_symlink(tmp_path):
    # The repro's shape: <root>/<variant> is a real path through a symlinked
    # install and the stamp sits inside it.
    link = _store_bundle(tmp_path, None)
    (link / ".pdk-store.json").write_text('{"version": "2026.11.14"}', encoding="utf-8")
    assert pdk.read_version(str(link)) == "store:2026.11.14"


def test_store_stamp_nearest_wins(tmp_path):
    v = _variant(tmp_path / "r" / "v")
    (v / ".pdk-store.json").write_text('{"version": "near"}', encoding="utf-8")
    (tmp_path / "r" / ".pdk-store.json").write_text(
        '{"version": "far"}', encoding="utf-8"
    )
    assert pdk.read_version(str(v)) == "store:near"


def test_store_stamp_nearest_unusable_is_not_skipped(tmp_path):
    # A present-but-malformed nearest stamp must not fall through to a valid
    # farther one (mirrors read_fetched_marker's nearest-wins discipline).
    v = _variant(tmp_path / "r" / "v")
    (v / ".pdk-store.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "r" / ".pdk-store.json").write_text(
        '{"version": "far"}', encoding="utf-8"
    )
    assert pdk.read_version(str(v)) is None


def test_store_sha256_reader(tmp_path):
    from klayout_tools import pdk_store

    v = _variant(tmp_path / "r" / "v")
    digest = "9F2c0A5b" * 8
    (v / ".pdk-store.json").write_text(
        json.dumps({"version": "1", "sha256": digest}), encoding="utf-8"
    )
    assert pdk_store.read_store_sha256(str(v)) == digest
    (tmp_path / "r" / ".pdk-store.json").write_text(
        '{"version": "2"}', encoding="utf-8"
    )
    (v / ".pdk-store.json").unlink()
    assert pdk_store.read_store_sha256(str(v)) is None


# --------------------------------------------------------------------------- #
# Issue #8007: install evidence -- what placed the tree, and where it sits,
# read independently of the version stamp above.
# --------------------------------------------------------------------------- #

_REV = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"
_DIGEST = "9F2c0A5b" * 8
_INSTALL = {
    "sha256": _DIGEST,
    "version": _REV,
    "via": "artifact-broker",
    "synced": "2026-11-23T06:48:48Z",
}
_MARKER = {  # ``key``/``name`` locate the bundle in a store: never carried
    "key": f"pdk/gf180mcu/{_REV}/{_DIGEST.lower()}",
    "name": "gf180mcu",
    **_INSTALL,
}
_NO_FACTS = dict.fromkeys(_INSTALL)
_FAMILY = f"ciel/gf180mcu/versions/{_REV}/gf180mcuD"
_LEGACY = f"gf180mcu/versions/{_REV}/gf180mcuD"
_FIND = ["pdk", "find", "--pdk", "gf180mcuD", "--format", "text"]


def _store_tree(store, relative, marker=_MARKER, at="parent"):
    """``<store>/<relative>`` with ``SOURCES`` and the marker beside the
    variant (``parent``, where the fleet's sync writes it) or inside it
    (``variant``), plus the enabled link ``<store>/gf180mcuD`` unless the
    variant sits directly in the store."""
    variant = store / relative
    _variant(variant)
    (variant / "SOURCES").write_text(f"open_pdks {_REV}\n", encoding="utf-8")
    if marker is not None:
        where = variant if at == "variant" else variant.parent
        (where / ".pdk-store.json").write_text(json.dumps(marker), encoding="utf-8")
    if relative != "gf180mcuD":
        (store / "gf180mcuD").symlink_to(os.path.relpath(variant, store))
    return variant


def _linked_root(root, target):
    """A ``--pdk-root`` holding only an enabled link to ``target``."""
    root.mkdir()
    (root / "gf180mcuD").symlink_to(os.path.relpath(target, root))
    return str(root)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """``$HOME`` under ``tmp_path``, nothing pinned, only the stores searched."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pdk, "SHARED_ROOTS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])
    return tmp_path


@pytest.mark.parametrize(
    "relative, at, marker, store_layout, install",
    [
        (_FAMILY, "parent", _MARKER, "manager_family", _INSTALL),
        (_LEGACY, "parent", _MARKER, "legacy_manager", _INSTALL),
        ("gf180mcuD", "variant", _MARKER, "flat", _INSTALL),
        ("gf180mcuD", "variant", None, "flat", None),  # a manager-named root
    ],
    ids=["manager-family", "legacy-manager", "flat-bundle", "unmarked"],
)
def test_find_reports_the_marker_beside_a_sources_version(
    tmp_path, relative, at, marker, store_layout, install
):
    """``version`` still comes from ``SOURCES``; the marker's own facts are
    ``install`` and the tree's placement is ``store_layout`` (issue #8007)."""
    store = tmp_path / ".ciel"
    _store_tree(store, relative, marker, at)

    report = pdk.find_pdk(variant="gf180mcuD", root=str(store))

    assert report["install"] == install
    assert report["store_layout"] == store_layout
    assert (report["version"], report["commit"], report["layout"]) == (
        f"open_pdks {_REV}",
        None,
        "open_pdks",
    )
    assert report["resolved_via"] == "--pdk-root flag"
    assert report["compatibility"]["status"] == "match"


@pytest.mark.parametrize("at", ["parent", "variant"])
def test_search_resolved_tree_reports_the_marker_from_either_location(home, at):
    """``resolved_via`` stays the search step; the marker is read nearest first."""
    _store_tree(home / ".ciel", _FAMILY, at=at)

    report = pdk.find_pdk(variant="gf180mcuD")

    assert report["resolved_via"] == "search root: ~/.ciel"
    assert report["install"] == _INSTALL
    assert report["store_layout"] == "manager_family"


def test_nearer_unusable_marker_is_not_skipped_for_the_parent(tmp_path):
    """A present-but-malformed nearer marker is *the* marker: install unknown."""
    store = tmp_path / "store"
    variant = _store_tree(store, _FAMILY)
    (variant / ".pdk-store.json").write_text("{not json", encoding="utf-8")

    report = pdk.find_pdk(variant="gf180mcuD", root=str(store))

    assert report["install"] is None
    assert report["version"] == f"open_pdks {_REV}"


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"sha256": _DIGEST}, {**_NO_FACTS, "sha256": _DIGEST}),
        ({"sha256": "9f2c", "version": _REV}, {**_NO_FACTS, "version": _REV}),
        ({"sha256": "z" * 64}, _NO_FACTS),  # 64 characters, not hex
        ({"sha256": f" {_DIGEST}"}, _NO_FACTS),  # padding is not a digest
        (
            {"sha256": _DIGEST, "via": "  ", "synced": 7},
            _NO_FACTS | {"sha256": _DIGEST},
        ),
        ({"version": 3, "via": ["artifact-broker"]}, _NO_FACTS),
        ({"key": "pdk/gf180mcu/x/y", "name": "gf180mcu"}, _NO_FACTS),
    ],
    ids=[
        "digest",
        "short-digest",
        "non-hex",
        "padded",
        "blank-non-string",
        "non-strings",
        "uncarried-only",
    ],
)
def test_partial_marker_reports_only_its_valid_facts(tmp_path, fields, expected):
    store = tmp_path / "store"
    _store_tree(store, _FAMILY, fields)
    # A marker naming no carried fact is no evidence at all.
    report = pdk.find_pdk(variant="gf180mcuD", root=str(store))
    assert report["install"] == (None if expected is _NO_FACTS else expected)


@pytest.mark.parametrize(
    "payload",
    [None, "{not json", "[1]", '"x"', "{}", '{"padding": "' + " " * 65536 + '"}'],
    ids=["absent", "malformed", "not-an-object", "scalar", "empty", "oversized"],
)
def test_unusable_marker_leaves_the_install_unknown(tmp_path, payload):
    store = tmp_path / "store"
    _store_tree(store, _FAMILY, payload)
    report = pdk.find_pdk(variant="gf180mcuD", root=str(store))
    assert report["install"] is None


def test_store_layout_measures_the_variant_from_its_store(home):
    def layout(name, relative, marker=_MARKER):
        store = home / name
        _store_tree(store, relative, marker)
        return pdk.find_pdk(variant="gf180mcuD", root=str(store))["store_layout"]

    assert layout("ciel", _FAMILY) == "manager_family"
    assert layout("volare", _LEGACY) == "legacy_manager"
    assert layout("plain", "gf180mcuD", None) == "flat"
    # The root *is* the variant (a flat single-PDK install, issue #522).
    flat = _variant(home / "flat")
    assert pdk.find_pdk(variant="flat", root=str(flat))["store_layout"] == "flat"


def test_layout_is_measured_from_the_store_an_enabled_link_leaves(home):
    """``$PDK_ROOT`` often holds only the enabled links, or sits inside the
    store: either way the store is what the layout is measured from."""
    target = _store_tree(home / ".volare", _LEGACY, None)
    root = _linked_root(home / "pdk-root", target)
    report = pdk.find_pdk(variant="gf180mcuD", root=root)
    assert report["store_layout"] == "legacy_manager"

    variant = _store_tree(home / ".ciel", _FAMILY, None)
    inside = pdk.find_pdk(variant="gf180mcuD", root=str(variant.parent))
    assert inside["store_layout"] == "manager_family"


def test_version_tree_under_no_known_store_is_not_called_plain(home, capsys):
    """A version tree under neither the searched root nor a conventional store
    has no establishable layout; a link to a plain directory is a plain install."""
    versions = home / "data" / "volare" / "versions" / _REV
    versioned = _store_tree(
        home / "data", os.path.relpath(versions / "gf180mcuD", home / "data"), None
    )
    for index, (target, expected) in enumerate(
        [(versioned, None), (_variant(home / "data" / "plain"), "flat")]
    ):
        root = _linked_root(home / f"root{index}", target)
        report = pdk.find_pdk(variant="gf180mcuD", root=root)
        assert report["store_layout"] == expected
        main([*_FIND, "--pdk-root", root])
        assert f"store_layout: {expected or '-'}" in capsys.readouterr().out


def test_provenance_pdk_carries_install_evidence_without_paths(tmp_path):
    store = tmp_path / ".ciel"
    variant = _store_tree(store, _FAMILY)
    report = pdk.find_pdk(variant="gf180mcuD", root=str(store))
    block = _provenance._pdk_block(report)

    assert (block["install"], block["store_layout"]) == (_INSTALL, "manager_family")
    assert str(tmp_path) not in json.dumps(block)

    (variant.parent / ".pdk-store.json").unlink()
    # An unmarked install says its placing tool is unknown here as well.
    unmarked = pdk.find_pdk(variant="gf180mcuD", root=str(store))
    assert _provenance._pdk_block(unmarked) == {
        "name": "gf180mcuD",
        "source": "--pdk-root flag",
        "version": f"open_pdks {_REV}",
        "install": None,
        "store_layout": "manager_family",
    }


def test_install_evidence_never_drifts_a_rerun_diff():
    """Install evidence says how this host came by its tree, like ``source``
    (issues #5182, #5863): the same install placed another way is not drift."""
    from klayout_tools import _report_verify

    paths = {("provenance", "pdk", "install"), ("provenance", "pdk", "store_layout")}
    assert paths <= _report_verify.VOLATILE_PROVENANCE_PATHS


def test_find_text_shows_store_layout_and_install(tmp_path, capsys):
    store = tmp_path / ".ciel"
    variant = _store_tree(store, _FAMILY)

    def after_resolved_via():
        main([*_FIND, "--pdk-root", str(store)])
        out = capsys.readouterr().out.splitlines()
        return out[out.index("resolved_via: --pdk-root flag") + 1 :]

    assert after_resolved_via()[:6] == [
        "store_layout: manager_family",
        "install:",
        f"  sha256: {_DIGEST}",
        f"  version: {_REV}",
        "  via: artifact-broker",
        "  synced: 2026-11-23T06:48:48Z",
    ]
    (variant.parent / ".pdk-store.json").unlink()
    assert after_resolved_via()[:2] == [
        "store_layout: manager_family",
        "install: none (no usable .pdk-store.json: placing tool unknown)",
    ]
    # A fact the marker does not carry reads as ``-``, like any other null here.
    (variant.parent / ".pdk-store.json").write_text(json.dumps({"sha256": _DIGEST}))
    assert after_resolved_via()[:6] == [
        "store_layout: manager_family",
        "install:",
        f"  sha256: {_DIGEST}",
        "  version: -",
        "  via: -",
        "  synced: -",
    ]

"""Issue #3070: ``pdk.read_version`` fallbacks and the ``linked_trees`` key."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from klayout_tools import _provenance, pdk

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

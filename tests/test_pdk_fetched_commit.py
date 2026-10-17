"""Issue #3786: ``.fetched-commit`` -> ``klt pdk find`` ``commit`` / provenance."""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from klayout_tools import _provenance, pdk
from klayout_tools.pdk_compat import read_fetched_commit

SHA = "95f178f8612cfeeb2e2ab10978133550327bcd06"
SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


def _variant(path):
    (path / "libs.tech").mkdir(parents=True)
    return path


def test_read_valid_and_trailing_newline(tmp_path):
    v = _variant(tmp_path / "v")
    (v / ".fetched-commit").write_text(SHA + "\n", encoding="utf-8")
    assert read_fetched_commit(str(v)) == SHA


@pytest.mark.parametrize("text", ["", "not-hex", SHA[:39], SHA.upper(), SHA + "0"])
def test_read_malformed(tmp_path, text):
    v = _variant(tmp_path / "v")
    (v / ".fetched-commit").write_text(text, encoding="utf-8")
    assert read_fetched_commit(str(v)) is None


def test_read_absent_and_parent_search(tmp_path):
    v = _variant(tmp_path / "root" / "v")
    assert read_fetched_commit(str(v)) is None
    (tmp_path / "root" / ".fetched-commit").write_text(SHA, encoding="utf-8")
    assert read_fetched_commit(str(v)) == SHA


def test_find_pdk_commit_and_version_unchanged(tmp_path):
    v = _variant(tmp_path / "v")
    (v / ".fetched-version").write_text("0.3.0\n", encoding="utf-8")
    assert pdk.find_pdk(root=str(v))["commit"] is None
    (v / ".fetched-commit").write_text(SHA, encoding="utf-8")
    report = pdk.find_pdk(root=str(v))
    assert report["commit"] == SHA
    assert report["version"] == "fetched:0.3.0"


def test_pdk_block_commit_only_when_present():
    base = {"variant": "A", "resolved_via": "x", "version": "fetched:1"}
    absent = _provenance._pdk_block({**base, "commit": None})
    assert absent == _provenance._pdk_block(base)
    assert "commit" not in absent
    assert _provenance._pdk_block({**base, "commit": SHA})["commit"] == SHA


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git/bash not on PATH",
)
@pytest.mark.parametrize("script", ["fetch-pdks.sh", "fetch-ihp-sg13g2.sh"])
def test_script_tarball_commit(tmp_path, script):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f").write_text("x", encoding="utf-8")

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "x")
    head = git("rev-parse", "HEAD")
    tarball = tmp_path / "t.tar.gz"
    git("archive", "--format=tar.gz", "-o", str(tarball), "HEAD")
    plain = tmp_path / "plain.tar.gz"
    subprocess.run(["tar", "-czf", str(plain), "-C", str(repo), "f"], check=True)

    func = subprocess.run(
        ["sed", "-n", "/^tarball_commit()/,/^}/p", str(SCRIPTS / script)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert func

    def run(path):
        return subprocess.run(
            ["bash", "-c", f'{func}\ntarball_commit "$1"', "_", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    assert run(tarball) == head
    assert run(plain) == ""

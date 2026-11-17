"""Tests for the public xschem release asset route (issue #6219).

The fleet pin lives in docker/eda-sim/pdk-versions.json `tools.xschem`; chip CI
installs that build credential-free from a public release of this repo via
scripts/install-xschem.sh. Split out of test_eda_sim_image.py to stay under the
500-line file-size limit.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
MANIFEST_PATH = REPO_ROOT / "docker" / "eda-sim" / "pdk-versions.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def _code_only(path: Path) -> str:
    """The file with `#` comment lines removed (prose may name the pins)."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


# ---------------------------------------------------------------------------
# Public xschem release asset (issue #6219): the credential-free install route.
# ---------------------------------------------------------------------------

XSCHEM_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-xschem-asset.yml"
INSTALL_XSCHEM = REPO_ROOT / "scripts" / "install-xschem.sh"


def test_xschem_release_tag_cannot_trigger_a_publish_workflow(manifest):
    """`v*` fires publish.yml (PyPI); `klt-*-native-v*` fires publish-native.yml."""
    tag = manifest["tools"]["xschem"]["release_tag"]
    assert not tag.startswith("v"), f"release_tag {tag!r} would trigger publish.yml"
    assert not re.fullmatch(r"klt-.*-native-v.*", tag)
    assert tag != manifest["tools"]["xschem"]["tag"], "bare upstream tag collides"


def test_xschem_asset_workflow_is_manual_only_and_reads_the_manifest():
    body = _code_only(XSCHEM_WORKFLOW)
    assert "workflow_dispatch:" in body
    assert "push:" not in body and "tags:" not in body, (
        "the asset workflow must never fire from a tag push"
    )
    for field in ("tag", "commit", "repo", "asset", "release_tag"):
        assert f".tools.xschem.{field}" in body, f"workflow must read {field} via jq"
    assert "resolved" in body and "!= " in body, "commit-vs-pin assertion is gone"


def _fake_asset(tmp_path: Path) -> Path:
    asset = tmp_path / "asset.tar.gz"
    with tarfile.open(asset, "w:gz") as tar:
        for name, data in (("bin/xschem", b"#!/bin/sh\necho XSCHEM V3.4.7\n"),):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return asset


def _run_install(tmp_path: Path, asset: Path, sha256: str | None):
    """Run install-xschem.sh with `curl` shimmed to serve `asset` (no network)."""
    manifest = json.loads(MANIFEST_PATH.read_text())
    if sha256 is None:
        manifest["tools"]["xschem"].pop("sha256", None)
    else:
        manifest["tools"]["xschem"]["sha256"] = sha256
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(manifest))
    shim = tmp_path / "shim"
    shim.mkdir()
    curl = shim / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
        f'cp "{asset}" "$out"\n'
    )
    curl.chmod(0o755)
    prefix = tmp_path / "prefix"
    env = {
        **os.environ,
        "PATH": f"{shim}:{os.environ['PATH']}",
        "XSCHEM_MANIFEST": str(mpath),
        "XSCHEM_INSTALL_PREFIX": str(prefix),
    }
    proc = subprocess.run(
        ["bash", str(INSTALL_XSCHEM)], env=env, capture_output=True, text=True
    )
    return proc, prefix


@pytest.mark.skipif(
    not (
        shutil.which("jq") and shutil.which("bash") and os.uname().machine == "x86_64"
    ),
    reason="install-xschem.sh is Linux x86_64 and needs jq",
)
class TestInstallXschem:
    def test_sha_mismatch_aborts_before_anything_is_unpacked(self, tmp_path):
        proc, prefix = _run_install(tmp_path, _fake_asset(tmp_path), "0" * 64)
        assert proc.returncode != 0
        assert "checksum mismatch" in proc.stderr
        assert not prefix.exists(), "asset was unpacked despite a sha256 mismatch"

    def test_missing_sha256_fails_closed(self, tmp_path):
        proc, prefix = _run_install(tmp_path, _fake_asset(tmp_path), None)
        assert proc.returncode != 0
        assert "sha256" in proc.stderr
        assert not prefix.exists()

    def test_verified_asset_is_unpacked_behind_a_relocating_wrapper(self, tmp_path):
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        proc, prefix = _run_install(tmp_path, asset, digest)
        assert proc.returncode == 0, proc.stderr
        wrapper = (prefix / "bin" / "xschem").read_text()
        assert f'XSCHEM_SHAREDIR="{prefix}/share/xschem"' in wrapper
        assert (prefix / "bin" / "xschem.real").exists()
        out = subprocess.run(
            [str(prefix / "bin" / "xschem")], capture_output=True, text=True
        )
        assert "XSCHEM V3.4.7" in out.stdout

"""Tests for the public xschem release asset route (issue #6219).

The fleet pin lives in docker/eda-sim/pdk-versions.json `tools.xschem`; chip CI
installs that build credential-free from a public release of 2AMLogic's xschem
fork (`tools.xschem.release_repo`) via scripts/install-xschem.sh. Split out of
test_eda_sim_image.py to stay under the 500-line file-size limit.
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

INSTALL_XSCHEM = REPO_ROOT / "scripts" / "install-xschem.sh"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def test_xschem_release_fields_agree_with_the_pin(manifest):
    """The fork cuts `<tag>-2am.<build>` and names the asset after the tag.

    Bumping `tools.xschem.tag` without repointing `release_tag`/`asset` would
    leave the installer fetching a build of the OLD version, which the sha256
    would still bless if the stale hash were left in place.
    """
    xschem = manifest["tools"]["xschem"]
    assert re.fullmatch(r"[\w.-]+/[\w.-]+", xschem["release_repo"]), (
        "release_repo must be an `owner/name` GitHub repository"
    )
    assert re.fullmatch(re.escape(xschem["tag"]) + r"-2am\.\d+", xschem["release_tag"])
    assert xschem["asset"] == f"xschem-{xschem['tag']}-noble-x86_64.tar.gz"


def test_this_repo_publishes_no_xschem_release():
    """Operator ruling 2026-11-19: the release asset lives in the xschem fork.

    A workflow here that built xschem and ran `gh release` would be a second,
    competing publisher (and a privileged `contents: write` one) for a binary
    whose source and licence are not in this repository.
    """
    publishers = [
        workflow.name
        for workflow in sorted(WORKFLOWS.glob("*.y*ml"))
        if "xschem" in _code_only(workflow).lower()
        and "gh release" in _code_only(workflow)
    ]
    assert not publishers, f"xschem release published from this repo: {publishers}"


def _fake_asset(tmp_path: Path) -> Path:
    asset = tmp_path / "asset.tar.gz"
    with tarfile.open(asset, "w:gz") as tar:
        for name, data in (("bin/xschem", b"#!/bin/sh\necho XSCHEM V3.4.7\n"),):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return asset


def _run_install(
    tmp_path: Path,
    asset: Path,
    sha256: str | None,
    fields: dict[str, str | None] | None = None,
):
    """Run install-xschem.sh with `curl` shimmed to serve `asset` (no network).

    `fields` overrides keys of `tools.xschem` in the manifest the script reads
    (a `None` value removes the key). The URL the script asked curl for is
    recorded in `tmp_path / "curl-url"`.
    """
    manifest = json.loads(MANIFEST_PATH.read_text())
    xschem = manifest["tools"]["xschem"]
    if sha256 is None:
        xschem.pop("sha256", None)
    else:
        xschem["sha256"] = sha256
    for key, value in (fields or {}).items():
        if value is None:
            xschem.pop(key, None)
        else:
            xschem[key] = value
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(manifest))
    shim = tmp_path / "shim"
    shim.mkdir()
    curl = shim / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; url="$1"; shift; done\n'
        f'echo "$url" > "{tmp_path}/curl-url"\n'
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

    def test_missing_release_repo_fails_closed(self, tmp_path):
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        proc, prefix = _run_install(tmp_path, asset, digest, {"release_repo": None})
        assert proc.returncode != 0
        assert "release_repo" in proc.stderr
        assert not (tmp_path / "curl-url").exists(), "downloaded with no release_repo"
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

    def test_download_url_is_read_from_the_manifest_fork_release(
        self, tmp_path, manifest
    ):
        """The repo, release tag and asset all come from the manifest.

        Overriding them proves none is hardcoded in the script; the default
        run then pins the shipped manifest to the fork's URL shape.
        """
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        override = {
            "release_repo": "example-org/xschem-fork",
            "release_tag": "9.9.9-2am.7",
            "asset": "custom-asset.tar.gz",
        }
        proc, _ = _run_install(tmp_path, asset, digest, override)
        assert proc.returncode == 0, proc.stderr
        assert (tmp_path / "curl-url").read_text().strip() == (
            "https://github.com/example-org/xschem-fork"
            "/releases/download/9.9.9-2am.7/custom-asset.tar.gz"
        )

        shipped = tmp_path / "shipped"
        shipped.mkdir()
        proc, _ = _run_install(shipped, asset, digest)
        assert proc.returncode == 0, proc.stderr
        xschem = manifest["tools"]["xschem"]
        assert (shipped / "curl-url").read_text().strip() == (
            f"https://github.com/{xschem['release_repo']}"
            f"/releases/download/{xschem['release_tag']}/{xschem['asset']}"
        )

"""Tests for the public xschem release asset route (issue #6219).

The fleet pin lives in docker/eda-sim/pdk-versions.json `tools.xschem`; chip CI
installs that build credential-free from a public release of 2AMLogic's xschem
fork (`tools.xschem.release_repo`) via scripts/install-xschem.sh. The tree is
built with `--prefix=/opt/eda/xschem` and is NOT relocatable, so the installer
unpacks it there as shipped. Split out of test_eda_sim_image.py to stay under
the 500-line file-size limit.
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


def test_xschem_sha256_is_recorded(manifest):
    """The fork's first release is published, so its digest is pinned.

    `install-xschem.sh` fails closed without it; a missing or malformed value
    would turn every chip-CI install into an error rather than a skipped check.
    """
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["tools"]["xschem"]["sha256"])


def test_installer_unpacks_at_the_compiled_in_prefix_with_no_wrapper():
    """The tree is not relocatable, so the script must not pretend it is.

    Outside its source tree xschem takes XSCHEM_LIBRARY_PATH from the
    compile-time prefix; an `XSCHEM_SHAREDIR` wrapper moves the Tcl files but
    not the symbol library, so a relocated unpack cannot find res.sym or
    lab_pin.sym (2AMLogic/xschem run 44671676966).
    """
    code = _code_only(INSTALL_XSCHEM)
    assert 'DEFAULT_PREFIX="/opt/eda/xschem"' in code
    assert 'prefix="${XSCHEM_INSTALL_PREFIX:-$DEFAULT_PREFIX}"' in code
    assert "XSCHEM_SHAREDIR" not in code
    assert "xschem.real" not in code


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


_FAKE_BINARY = b"#!/bin/sh\necho XSCHEM V3.4.7\n"
_FAKE_SYMBOL = "xschem_library/devices/res.sym"


def _fake_asset(tmp_path: Path) -> Path:
    """A tarball shaped like the fork's real asset (`./bin/xschem`, `./share/...`)."""
    asset = tmp_path / "asset.tar.gz"
    with tarfile.open(asset, "w:gz") as tar:
        top = tarfile.TarInfo("./")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        for name, data, mode in (
            ("./bin/xschem", _FAKE_BINARY, 0o755),
            (f"./share/xschem/{_FAKE_SYMBOL}", b"v {xschem}\n", 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return asset


def _run_install(
    tmp_path: Path,
    asset: Path,
    sha256: str | None,
    fields: dict[str, str | None] | None = None,
    *,
    args: tuple[str, ...] = (),
    prefix: Path | None = None,
    sudo_unlocks: Path | None = None,
):
    """Run install-xschem.sh with `curl` and `sudo` shimmed (no network, no root).

    `fields` overrides keys of `tools.xschem` in the manifest the script reads
    (a `None` value removes the key). The URL the script asked curl for is
    recorded in `tmp_path / "curl-url"`; every `sudo` call, in
    `tmp_path / "sudo-log"`. The `sudo` shim runs its command as the current
    user, first making `sudo_unlocks` writable to stand in for root's access.
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
    sudo = shim / "sudo"
    unlock = f'chmod u+w "{sudo_unlocks}"\n' if sudo_unlocks else ""
    sudo.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{tmp_path}/sudo-log"\n'
        '[ "$1" = -n ] && shift\n'
        f"{unlock}"
        'exec "$@"\n'
    )
    sudo.chmod(0o755)
    prefix = prefix or tmp_path / "prefix"
    env = {
        **os.environ,
        "PATH": f"{shim}:{os.environ['PATH']}",
        "XSCHEM_MANIFEST": str(mpath),
        "XSCHEM_INSTALL_PREFIX": str(prefix),
    }
    proc = subprocess.run(
        ["bash", str(INSTALL_XSCHEM), *args], env=env, capture_output=True, text=True
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

    def test_verified_asset_is_unpacked_as_shipped(self, tmp_path):
        """No wrapper, no renamed binary: the tree is laid down exactly as built."""
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        proc, prefix = _run_install(tmp_path, asset, digest)
        assert proc.returncode == 0, proc.stderr
        assert (prefix / "bin" / "xschem").read_bytes() == _FAKE_BINARY
        assert not (prefix / "bin" / "xschem.real").exists()
        assert (prefix / "share" / "xschem" / _FAKE_SYMBOL).is_file()
        assert (prefix / ".installed-version").read_text().split() == [
            json.loads(MANIFEST_PATH.read_text())["tools"]["xschem"]["tag"],
            digest,
        ]
        out = subprocess.run(
            [str(prefix / "bin" / "xschem")], capture_output=True, text=True
        )
        assert "XSCHEM V3.4.7" in out.stdout
        assert not (tmp_path / "sudo-log").exists(), "sudo used on a writable prefix"

    def test_a_prefix_other_than_the_compiled_in_one_warns(self, tmp_path):
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        proc, _ = _run_install(tmp_path, asset, digest)
        assert proc.returncode == 0, proc.stderr
        assert "compiled for /opt/eda/xschem" in proc.stderr
        assert "symbol library" in proc.stderr

    def test_a_relative_prefix_is_rejected(self, tmp_path):
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        proc, _ = _run_install(tmp_path, asset, digest, prefix=Path("relative/xschem"))
        assert proc.returncode != 0
        assert "absolute path" in proc.stderr
        assert not (tmp_path / "curl-url").exists()

    def test_second_run_is_a_no_op_until_forced(self, tmp_path):
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        proc, prefix = _run_install(tmp_path, asset, digest)
        assert proc.returncode == 0, proc.stderr

        again = tmp_path / "again"
        again.mkdir()
        proc, _ = _run_install(again, asset, digest, prefix=prefix)
        assert proc.returncode == 0, proc.stderr
        assert "already installed" in proc.stdout
        assert not (again / "curl-url").exists(), "re-downloaded an installed pin"

        forced = tmp_path / "forced"
        forced.mkdir()
        proc, _ = _run_install(forced, asset, digest, prefix=prefix, args=("--force",))
        assert proc.returncode == 0, proc.stderr
        assert (forced / "curl-url").exists()

    def test_refuses_to_replace_a_directory_it_did_not_install(self, tmp_path):
        """The replace step is `rm -rf <prefix>`, possibly as root."""
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        prefix = tmp_path / "prefix"
        prefix.mkdir()
        (prefix / "keep.txt").write_text("not ours")
        proc, _ = _run_install(tmp_path, asset, digest, prefix=prefix)
        assert proc.returncode != 0
        assert "not installed by this script" in proc.stderr
        assert (prefix / "keep.txt").exists()
        assert not (tmp_path / "curl-url").exists(), "downloaded before refusing"

        forced = tmp_path / "forced"
        forced.mkdir()
        proc, _ = _run_install(forced, asset, digest, prefix=prefix, args=("--force",))
        assert proc.returncode == 0, proc.stderr
        assert not (prefix / "keep.txt").exists()
        assert (prefix / "bin" / "xschem").is_file()

    def test_failed_directory_inspection_preserves_files(self, tmp_path, monkeypatch):
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        prefix = tmp_path / "prefix"
        prefix.mkdir()
        keep = prefix / "keep.txt"
        keep.write_bytes(b"existing contents")
        # Force the same empty stdout and error status as an unreadable
        # directory, including on runners where root could list a mode-000 dir.
        ls = tmp_path / "ls"
        ls.write_text("#!/bin/sh\nexit 2\n")
        ls.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

        proc, _ = _run_install(tmp_path, asset, digest, prefix=prefix)
        assert proc.returncode != 0
        assert keep.read_bytes() == b"existing contents"
        assert "cannot inspect" in proc.stderr
        assert not (tmp_path / "curl-url").exists()

    def test_preserves_a_regular_file_at_the_install_path(self, tmp_path):
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        prefix = tmp_path / "prefix"
        prefix.write_bytes(b"existing contents")

        proc, _ = _run_install(tmp_path, asset, digest, prefix=prefix)
        assert proc.returncode != 0
        assert prefix.read_bytes() == b"existing contents"
        assert not (tmp_path / "curl-url").exists()

    def test_installs_into_an_existing_empty_directory(self, tmp_path):
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        prefix = tmp_path / "prefix"
        prefix.mkdir()

        proc, _ = _run_install(tmp_path, asset, digest, prefix=prefix)
        assert proc.returncode == 0, proc.stderr
        assert (prefix / "bin" / "xschem").read_bytes() == _FAKE_BINARY

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
    def test_an_unwritable_prefix_is_installed_through_sudo(self, tmp_path):
        """`/opt/eda` is root-owned: every write step goes through `sudo -n`."""
        asset = _fake_asset(tmp_path)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        parent = tmp_path / "opt"
        parent.mkdir()
        parent.chmod(0o555)
        try:
            proc, prefix = _run_install(
                tmp_path,
                asset,
                digest,
                prefix=parent / "xschem",
                sudo_unlocks=parent,
            )
        finally:
            parent.chmod(0o755)
        assert proc.returncode == 0, proc.stderr
        assert (prefix / "bin" / "xschem").read_bytes() == _FAKE_BINARY
        logged = (tmp_path / "sudo-log").read_text().splitlines()
        assert all(line.startswith("-n ") for line in logged), logged
        for verb in ("mkdir", "tar", "tee"):
            used = any(line.startswith(f"-n {verb} ") for line in logged)
            assert used, (verb, logged)
        assert any("--no-same-owner" in line for line in logged)

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

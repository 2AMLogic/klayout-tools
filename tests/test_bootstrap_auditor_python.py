"""Tests for `scripts/bootstrap-auditor-python.sh` (issue #2829): the
repository-owned bootstrap that gives a Linux audit host with no Python and no
`uv` a pinned, checksum-verified `uv`, a uv-managed CPython and a host-local
`.venv` synced from `uv.lock`, then runs the `klt` startup smoke.

Every test is deterministic and offline. The script runs under a scrubbed
`PATH` that holds only a handful of real coreutils plus fakes for `curl`,
`sha256sum` and `uname`; the fake `curl` serves a locally built tarball whose
`uv` is itself a fake that logs its argv and lays down a fake interpreter /
virtualenv / `klt`. Nothing reaches the network, and no real `uv` or Python is
ever on the script's `PATH`.

The fake `sha256sum` reports `$FAKE_SHA256` when set -- that is how the
success path gets past the *real* pinned digest with a fake artifact -- and
otherwise delegates to the host's real `sha256sum`, which is how the
checksum-mismatch paths are exercised against the unmodified pins.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bootstrap-auditor-python.sh"
README = REPO_ROOT / "scripts" / "README.md"

# Copied from astral-sh/uv release 0.12.23's per-artifact `.sha256` files.
# Re-pinning uv means updating the script and these together.
PINNED_UV_VERSION = "0.12.23"
PINNED_SHA256 = {
    "x86_64-unknown-linux-gnu": (
        "9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6"
    ),
    "aarch64-unknown-linux-gnu": (
        "6524bd338177ed50d035d39354e12545e993bbeba2ecbddf0480c5b3a81d313f"
    ),
}
ARCH_TO_TARGET = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "amd64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
    "arm64": "aarch64-unknown-linux-gnu",
}

# Real host tools the script (and the fakes) need; everything else is absent.
_REAL_TOOLS = (
    "bash",
    "env",
    "cat",
    "mkdir",
    "rm",
    "mv",
    "mktemp",
    "readlink",
    "awk",
    "sed",
    "head",
    "date",
    "tar",
    "gzip",
    "dirname",
    "git",
    "ln",
    "cp",
)

_FAKE_CURL = r"""#!/usr/bin/env bash
dest=""
url=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o) dest="$2"; shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
    esac
done
echo "curl $url" >>"$FAKE_LOG"
case "${FAKE_CURL_MODE:-ok}" in
    ok) cp "$FAKE_UV_TARBALL" "$dest" ;;
    http_error) echo "curl: (22) HTTP error 404" >&2; exit 22 ;;
    interrupted)
        head -c 100 "$FAKE_UV_TARBALL" >"$dest"
        echo "curl: (18) partial file" >&2
        exit 18 ;;
    truncated) head -c 100 "$FAKE_UV_TARBALL" >"$dest" ;;
esac
"""

_FAKE_SHA256SUM = r"""#!/usr/bin/env bash
if [[ -n "${FAKE_SHA256:-}" ]]; then
    echo "$FAKE_SHA256  $1"
else
    exec "$REAL_SHA256SUM" "$@"
fi
"""

_FAKE_UNAME = r"""#!/usr/bin/env bash
case "$1" in
    -s) echo "${FAKE_UNAME_S:-Linux}" ;;
    -m) echo "${FAKE_UNAME_M:-x86_64}" ;;
    *) echo "${FAKE_UNAME_S:-Linux}" ;;
esac
"""

_FAKE_UV = r"""#!/usr/bin/env bash
echo "uv $* | pref=${UV_PYTHON_PREFERENCE:-} venv_env=${VIRTUAL_ENV:-}" >>"$FAKE_LOG"
interp="$UV_PYTHON_INSTALL_DIR/cpython-3.12.99-linux-fake/bin/python3.12"
case "$1" in
    --version) echo "uv ${FAKE_UV_REPORTED} (fake)"; exit 0 ;;
    python)
        case "$2" in
            install)
                if [[ "${FAKE_FAIL:-}" == python ]]; then
                    echo "error: fake install failure" >&2
                    exit 1
                fi
                mkdir -p "$(dirname "$interp")"
                cp "$FAKE_TEMPLATES/python" "$interp"
                exit 0 ;;
            find) echo "$interp"; exit 0 ;;
        esac ;;
    sync)
        if [[ "${FAKE_FAIL:-}" == sync ]]; then
            echo "error: The lockfile needs to be updated" >&2
            exit 1
        fi
        v="$UV_PROJECT_ENVIRONMENT"
        mkdir -p "$v/bin"
        echo "home = $(dirname "$interp")" >"$v/pyvenv.cfg"
        ln -sfn "$interp" "$v/bin/python"
        cp "$FAKE_TEMPLATES/klt" "$v/bin/klt"
        exit 0 ;;
esac
exit 99
"""

_FAKE_PYTHON = r"""#!/usr/bin/env bash
echo "python-exec $*" >>"$FAKE_LOG"
echo "Python 3.12.99"
"""

_FAKE_KLT = r"""#!/usr/bin/env bash
echo "klt $*" >>"$FAKE_LOG"
[[ "$*" == "${FAKE_KLT_FAIL:-}" ]] && exit 3
echo '{"schema_version": 1}'
"""

_FOREIGN_PYTHON = r"""#!/usr/bin/env bash
echo "FOREIGN-EXECUTED" >>"$FAKE_LOG"
"""


def _write_exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def _fake_tarball(dest: Path, target: str) -> None:
    data = _FAKE_UV.encode()
    with tarfile.open(dest, "w:gz") as tar:
        for name in ("uv", "uvx"):
            info = tarfile.TarInfo(f"uv-{target}/{name}")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))


def _summary(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


@dataclass
class Sandbox:
    root: Path
    repo: Path
    prefix: Path
    bin: Path
    log: Path
    env: dict[str, str] = field(default_factory=dict)

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {**self.env, **extra_env}
        script = self.repo / "scripts" / "bootstrap-auditor-python.sh"
        return subprocess.run(
            [str(self.bin / "bash"), str(script), "--prefix", str(self.prefix), *args],
            env=env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def log_lines(self, prefix: str) -> list[str]:
        if not self.log.exists():
            return []
        return [ln for ln in self.log.read_text().splitlines() if ln.startswith(prefix)]

    def head_sha(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()


def _make_bin(bin_dir: Path, omit: tuple[str, ...] = ()) -> None:
    bin_dir.mkdir(parents=True)
    host_path = "/usr/bin:/bin"
    for tool in _REAL_TOOLS:
        real = shutil.which(tool, path=host_path)
        if real is None:
            pytest.skip(f"host tool {tool!r} not available")
        (bin_dir / tool).symlink_to(real)
    _write_exe(bin_dir / "curl", _FAKE_CURL)
    _write_exe(bin_dir / "sha256sum", _FAKE_SHA256SUM)
    _write_exe(bin_dir / "uname", _FAKE_UNAME)
    for tool in omit:
        (bin_dir / tool).unlink()


def _make_repo(repo: Path, home: Path) -> None:
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "fake"\nrequires-python = ">=3.10"\n'
    )
    (repo / "uv.lock").write_text("version = 1\n")
    git_env = {**os.environ, "HOME": str(home)}
    for cmd in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(repo), *cmd], env=git_env, check=True)


@pytest.fixture
def make_sandbox(tmp_path: Path):
    real_sha = shutil.which("sha256sum")
    if real_sha is None:
        pytest.skip("host has no sha256sum")

    def _make(arch: str = "x86_64", omit: tuple[str, ...] = ()) -> Sandbox:
        root = tmp_path / "with space"
        home = root / "home"
        home.mkdir(parents=True)
        repo = root / "repo dir"
        _make_repo(repo, home)
        bin_dir = root / "fake bin"
        _make_bin(bin_dir, omit)
        templates = root / "templates"
        _write_exe(templates / "python", _FAKE_PYTHON)
        _write_exe(templates / "klt", _FAKE_KLT)
        target = ARCH_TO_TARGET.get(arch, "x86_64-unknown-linux-gnu")
        tarball = root / "uv.tar.gz"
        _fake_tarball(tarball, target)
        log = root / "calls.log"
        env = {
            "PATH": str(bin_dir),
            "HOME": str(home),
            "FAKE_LOG": str(log),
            "FAKE_UV_TARBALL": str(tarball),
            "FAKE_TEMPLATES": str(templates),
            "FAKE_UNAME_M": arch,
            "FAKE_UV_REPORTED": PINNED_UV_VERSION,
            "FAKE_SHA256": PINNED_SHA256.get(target, ""),
            "REAL_SHA256SUM": real_sha,
        }
        return Sandbox(root, repo, root / "prefix dir", bin_dir, log, env)

    return _make


def _expected_url(target: str) -> str:
    return (
        "https://github.com/astral-sh/uv/releases/download/"
        f"{PINNED_UV_VERSION}/uv-{target}.tar.gz"
    )


# --- pins -------------------------------------------------------------------


def test_script_pins_match_the_published_uv_release_digests() -> None:
    text = SCRIPT.read_text()
    assert re.search(rf'^UV_VERSION="{re.escape(PINNED_UV_VERSION)}"$', text, re.M)
    assert f'UV_SHA256_X86_64="{PINNED_SHA256["x86_64-unknown-linux-gnu"]}"' in text
    assert f'UV_SHA256_AARCH64="{PINNED_SHA256["aarch64-unknown-linux-gnu"]}"' in text


def test_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_readme_documents_the_bootstrap() -> None:
    text = README.read_text()
    assert "bootstrap-auditor-python.sh" in text
    assert "--replace-venv" in text


# --- success path -----------------------------------------------------------


@pytest.mark.parametrize("arch", sorted(ARCH_TO_TARGET))
def test_first_install_selects_exact_url_and_checksum_per_arch(
    make_sandbox, arch
) -> None:
    sb = make_sandbox(arch)
    target = ARCH_TO_TARGET[arch]
    result = sb.run()
    assert result.returncode == 0, result.stderr
    assert sb.log_lines("curl ") == [f"curl {_expected_url(target)}"]
    summary = _summary(result.stdout)
    assert summary["uv_version"] == PINNED_UV_VERSION
    assert summary["uv_target"] == target
    assert summary["uv_sha256"] == PINNED_SHA256[target]
    assert summary["uv_action"] == "downloaded"
    assert summary["python_version"] == "3.12.99"
    assert summary["environment"] == str(sb.repo / ".venv")
    assert summary["environment_action"] == "created"
    assert summary["source_revision"] == sb.head_sha()
    assert summary["smoke"] == "passed"
    assert summary["python_path"].startswith(str(sb.prefix / "python") + "/")


def test_smoke_runs_the_three_startup_commands_in_order(make_sandbox) -> None:
    sb = make_sandbox()
    result = sb.run()
    assert result.returncode == 0, result.stderr
    assert sb.log_lines("klt ") == [
        "klt --help",
        "klt version --format json",
        "klt kb validate --format json",
    ]


def test_uv_is_driven_locked_managed_and_isolated(make_sandbox) -> None:
    sb = make_sandbox()
    result = sb.run(VIRTUAL_ENV="/somewhere/else")
    assert result.returncode == 0, result.stderr
    uv_calls = sb.log_lines("uv ")
    sync = [c for c in uv_calls if c.startswith("uv sync ")]
    assert len(sync) == 1
    assert f"--project {sb.repo} --locked --extra dev" in sync[0]
    assert any(c.startswith("uv python install --no-bin 3.12 ") for c in uv_calls)
    for call in uv_calls:
        if not call.startswith("uv --version"):
            assert "pref=only-managed" in call
            assert call.endswith("venv_env=")
    assert (sb.repo / "uv.lock").read_text() == "version = 1\n"


def test_rerun_is_idempotent_without_redownload(make_sandbox) -> None:
    sb = make_sandbox()
    first = sb.run()
    second = sb.run()
    assert first.returncode == 0 and second.returncode == 0, second.stderr
    assert len(sb.log_lines("curl ")) == 1
    s1, s2 = _summary(first.stdout), _summary(second.stdout)
    assert s2["uv_action"] == "reused"
    assert s2["environment_action"] == "synced"
    for key in ("uv_version", "uv_sha256", "uv_path", "python_version", "python_path"):
        assert s1[key] == s2[key]
    assert s1["environment"] == s2["environment"]


def test_no_smoke_skips_klt(make_sandbox) -> None:
    sb = make_sandbox()
    result = sb.run("--no-smoke")
    assert result.returncode == 0, result.stderr
    assert _summary(result.stdout)["smoke"] == "skipped"
    assert sb.log_lines("klt ") == []


# --- platform / prerequisites -----------------------------------------------


@pytest.mark.parametrize(
    ("os_name", "arch"), [("Linux", "riscv64"), ("Linux", "i686"), ("Darwin", "arm64")]
)
def test_unsupported_platform_fails_before_download(
    make_sandbox, os_name, arch
) -> None:
    sb = make_sandbox(arch)
    result = sb.run(FAKE_UNAME_S=os_name)
    assert result.returncode == 10
    assert "unsupported" in result.stderr
    assert sb.log_lines("curl ") == []


@pytest.mark.parametrize("missing", ["curl", "tar", "sha256sum"])
def test_missing_host_tool_is_reported(make_sandbox, missing) -> None:
    sb = make_sandbox(omit=(missing,))
    result = sb.run()
    assert result.returncode == 11
    assert ("sha256" if missing == "sha256sum" else missing) in result.stderr


def _assert_nothing_installed(sb: Sandbox) -> None:
    leftovers = [
        p.name for p in sb.prefix.iterdir() if p.name.startswith((".uv-", "uv-"))
    ]
    assert leftovers == []


# --- download / checksum -----------------------------------------------------


@pytest.mark.parametrize("mode", ["http_error", "interrupted"])
def test_download_failure_is_distinct_and_leaves_no_partial(make_sandbox, mode) -> None:
    sb = make_sandbox()
    result = sb.run(FAKE_CURL_MODE=mode)
    assert result.returncode == 13
    assert "download of" in result.stderr
    _assert_nothing_installed(sb)


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_checksum_mismatch_names_the_pinned_digest(make_sandbox, arch) -> None:
    sb = make_sandbox(arch)
    target = ARCH_TO_TARGET[arch]
    result = sb.run(FAKE_SHA256="")  # real sha256sum over the fake artifact
    assert result.returncode == 14
    assert f"expected: {PINNED_SHA256[target]}" in result.stderr
    assert _expected_url(target) in result.stderr
    _assert_nothing_installed(sb)


def test_truncated_download_reported_as_success_still_fails_checksum(
    make_sandbox,
) -> None:
    sb = make_sandbox()
    result = sb.run(FAKE_CURL_MODE="truncated", FAKE_SHA256="")
    assert result.returncode == 14
    _assert_nothing_installed(sb)


def test_unpacked_binary_with_wrong_version_is_rejected(make_sandbox) -> None:
    sb = make_sandbox()
    result = sb.run(FAKE_UV_REPORTED="0.0.1")
    assert result.returncode == 14
    assert "expected 'uv 0.12.23'" in result.stderr
    _assert_nothing_installed(sb)


# --- prefix -----------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("existing", [True, False])
def test_unwritable_prefix_fails(make_sandbox, existing) -> None:
    sb = make_sandbox()
    locked = sb.root / "locked"
    locked.mkdir()
    sb.prefix = locked / "p" if existing else locked / "sub" / "p"
    if existing:
        sb.prefix.mkdir()
        sb.prefix.chmod(0o555)
    else:
        locked.chmod(0o555)
    try:
        result = sb.run()
    finally:
        (sb.prefix if existing else locked).chmod(0o755)
    assert result.returncode == 12
    assert "user-writable --prefix" in result.stderr
    assert sb.log_lines("curl ") == []


# --- environment provenance ---------------------------------------------------


def _plant_foreign_venv(sb: Sandbox, kind: str) -> None:
    venv = sb.repo / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /elsewhere\n")
    py = venv / "bin" / "python"
    if kind == "dangling_macos":
        py.symlink_to(
            "/Users/rwalters/.local/share/uv/python/"
            "cpython-3.12.11-macos-aarch64-none/bin/python3.12"
        )
    elif kind == "outside_prefix":
        foreign = sb.root / "other python" / "python3"
        _write_exe(foreign, _FOREIGN_PYTHON)
        py.symlink_to(foreign)
    elif kind == "not_a_venv":
        (venv / "pyvenv.cfg").unlink()
    elif kind == "windows_layout":
        (venv / "bin").rmdir()
        _write_exe(venv / "Scripts" / "python.exe", _FOREIGN_PYTHON)


FOREIGN_KINDS = ["dangling_macos", "outside_prefix", "not_a_venv", "windows_layout"]


@pytest.mark.parametrize("kind", FOREIGN_KINDS)
def test_foreign_venv_is_refused_without_executing_it(make_sandbox, kind) -> None:
    sb = make_sandbox()
    _plant_foreign_venv(sb, kind)
    result = sb.run()
    assert result.returncode == 16
    assert "--replace-venv" in result.stderr
    assert "FOREIGN-EXECUTED" not in (sb.log.read_text() if sb.log.exists() else "")
    assert sb.log_lines("curl ") == []
    assert (sb.repo / ".venv" / "pyvenv.cfg").exists() == (kind != "not_a_venv")


@pytest.mark.parametrize("kind", FOREIGN_KINDS)
def test_replace_venv_moves_foreign_env_aside_and_rebuilds(make_sandbox, kind) -> None:
    sb = make_sandbox()
    _plant_foreign_venv(sb, kind)
    result = sb.run("--replace-venv")
    assert result.returncode == 0, result.stderr
    summary = _summary(result.stdout)
    assert summary["environment_action"] == "replaced"
    aside = sorted(sb.repo.glob(".venv.foreign-*"))
    assert len(aside) == 1
    assert (aside[0] / "bin").exists() or (aside[0] / "Scripts").exists()
    resolved = (sb.repo / ".venv" / "bin" / "python").resolve()
    assert str(resolved).startswith(str(sb.prefix / "python") + "/")
    assert "FOREIGN-EXECUTED" not in sb.log.read_text()


def test_own_venv_is_reused_without_replace(make_sandbox) -> None:
    sb = make_sandbox()
    assert sb.run().returncode == 0
    result = sb.run()
    assert result.returncode == 0, result.stderr
    assert list(sb.repo.glob(".venv.foreign-*")) == []


# --- python / sync / smoke failures ------------------------------------------


def test_python_install_failure(make_sandbox) -> None:
    sb = make_sandbox()
    result = sb.run(FAKE_FAIL="python")
    assert result.returncode == 15
    assert "uv python install 3.12 failed" in result.stderr


def test_python_below_floor_is_refused_before_install(make_sandbox) -> None:
    sb = make_sandbox()
    result = sb.run("--python", "3.9")
    assert result.returncode == 15
    assert ">=3.10" in result.stderr
    assert not any(c.startswith("uv python install") for c in sb.log_lines("uv "))


def test_sync_failure_never_rewrites_lock(make_sandbox) -> None:
    sb = make_sandbox()
    result = sb.run(FAKE_FAIL="sync")
    assert result.returncode == 17
    assert "never rewrites it" in result.stderr
    assert (sb.repo / "uv.lock").read_text() == "version = 1\n"


@pytest.mark.parametrize(
    "command", ["--help", "version --format json", "kb validate --format json"]
)
def test_smoke_failure_names_the_command_and_revision(make_sandbox, command) -> None:
    sb = make_sandbox()
    result = sb.run(FAKE_KLT_FAIL=command)
    assert result.returncode == 18
    assert f"'klt {command}' failed" in result.stderr
    assert sb.head_sha() in result.stderr


# --- checkout -----------------------------------------------------------------


def test_missing_lockfile_is_refused(make_sandbox) -> None:
    sb = make_sandbox()
    (sb.repo / "uv.lock").unlink()
    result = sb.run()
    assert result.returncode == 19
    assert "uv.lock" in result.stderr


def test_uninterpretable_python_floor_is_refused(make_sandbox) -> None:
    sb = make_sandbox()
    (sb.repo / "pyproject.toml").write_text('[project]\nrequires-python = "~=3.10"\n')
    result = sb.run()
    assert result.returncode == 19
    assert "requires-python" in result.stderr


def test_unknown_argument_is_usage_error(make_sandbox) -> None:
    sb = make_sandbox()
    result = sb.run("--bogus")
    assert result.returncode == 2

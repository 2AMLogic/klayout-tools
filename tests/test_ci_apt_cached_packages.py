"""Exercise the offline KLayout cache transaction independently of host apt."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci-apt-install-cached.sh"


@pytest.mark.parametrize("apt_exit", [0, 100])
def test_cached_install_resolves_local_archives_together(tmp_path, apt_exit):
    cache = tmp_path / "cache with spaces"
    cache.mkdir()
    archives = [cache / "dependency_1.deb", cache / "klayout_1.deb"]
    for path in archives:
        path.touch()
    log = tmp_path / "apt-args"
    apt = tmp_path / "apt-get"
    apt.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$APT_LOG"\nexit "$APT_EXIT"\n')
    apt.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "APT_LOG": str(log),
        "APT_EXIT": str(apt_exit),
    }

    result = subprocess.run([SCRIPT, cache], env=env)

    assert result.returncode == apt_exit
    assert log.read_text().splitlines() == [
        "-o",
        "Dir::Etc::sourcelist=/dev/null",
        "-o",
        "Dir::Etc::sourceparts=-",
        "--no-remove",
        "-y",
        "install",
        *map(str, archives),
    ]


def test_empty_cache_returns_to_fallback_before_invoking_apt(tmp_path):
    apt = tmp_path / "apt-get"
    apt.write_text("#!/bin/sh\nexit 99\n")
    apt.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    result = subprocess.run([SCRIPT, tmp_path], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "No cached Debian packages" in result.stderr

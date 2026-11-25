"""Behavioural guard for ``scripts/deploy-site.sh``'s data step (issue #5005).

``site/src/data/cli.generated.json`` is gitignored (the release bump commits
exactly ``pyproject.toml`` + ``CHANGELOG.md``), and ``site/src/data/cliData.ts``
imports it, so the site build fails ``tsc -b`` (TS2307) on a clean checkout
unless the deploy script generates it first. The script's header documented
that step and a ``--no-regen-data`` flag while implementing neither; these
tests run the real script in a throwaway tree with stub ``uv`` / ``npm`` so the
ordering and both flag paths stay pinned.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy-site.sh"
DATA = Path("site/src/data/cli.generated.json")


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(repo root, stub bin dir, call log) for a minimal fake checkout."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "site" / "src" / "data").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "deploy-site.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    stubs = {
        # `uv run ... gen_site_cli_data.py` = write the generated file.
        "uv": f'echo "uv $*" >> "{log}"\necho \'{{}}\' > {DATA}\n',
        # `npm ... run build` must find the generated file, as `tsc -b` does.
        "npm": (
            f'echo "npm $*" >> "{log}"\n'
            'if [[ "$*" == *"run build"* ]]; then\n'
            f'  [ -f {DATA} ] || {{ echo "TS2307: {DATA} missing" >&2; exit 9; }}\n'
            "fi\n"
        ),
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text(f"#!/usr/bin/env bash\n{body}")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return root, bin_dir, log


def _run(root: Path, bin_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(root / "scripts" / "deploy-site.sh"), "--no-deploy", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_default_run_generates_the_data_before_the_site_build(tmp_path):
    root, bin_dir, log = _tree(tmp_path)

    r = _run(root, bin_dir)

    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines()
    assert calls[0].startswith("uv run") and "gen_site_cli_data.py" in calls[0]
    assert [c for c in calls if c.startswith("npm")] == [
        "npm --prefix site ci",
        "npm --prefix site run build",
    ]
    assert (root / DATA).is_file()


def test_no_regen_data_builds_from_the_file_on_disk(tmp_path):
    root, bin_dir, log = _tree(tmp_path)
    (root / DATA).write_text("{}\n")

    r = _run(root, bin_dir, "--no-regen-data")

    assert r.returncode == 0, r.stderr
    assert not any(c.startswith("uv") for c in log.read_text().splitlines())


def test_no_regen_data_without_a_file_fails_before_the_build(tmp_path):
    root, bin_dir, log = _tree(tmp_path)

    r = _run(root, bin_dir, "--no-regen-data")

    assert r.returncode == 1
    assert "cli.generated.json" in r.stderr
    assert not log.exists()  # neither uv nor npm ran

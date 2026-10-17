"""Dangling-symlink summary in scripts/fetch-ihp-sg13cmos5l.sh (issue #3133).

Exercises the cached re-run path (marker == pinned commit), so no download.
"""

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch-ihp-sg13cmos5l.sh"


def _commit() -> str:
    m = re.search(r'IHP_SG13CMOS5L_COMMIT="([0-9a-f]+)"', SCRIPT.read_text())
    assert m
    return m.group(1)


def _tree(tmp_path: Path, dangling: int, lvs_dangling: bool = False) -> Path:
    root = tmp_path / "ihp-open-pdk"
    sib = root / "ihp-sg13g2"
    (sib / "libs.tech").mkdir(parents=True)
    (sib / "libs.ref").mkdir()
    (sib / ".fetched-version").write_text("0.3.0\n")
    dest = root / "ihp-sg13cmos5l"
    (dest / "libs.tech").mkdir(parents=True)
    (dest / "libs.ref").mkdir()
    (dest / ".fetched-version").write_text(_commit())
    (dest / "libs.tech" / "ok.txt").write_text("x")
    (dest / "libs.tech" / "good").symlink_to("ok.txt")
    for i in range(dangling):
        (dest / "libs.tech" / f"bad{i}").symlink_to("../../../ihp-sg13g2/missing")
    if lvs_dangling:
        lvs = dest / "libs.tech" / "klayout" / "tech" / "lvs" / "rule_decks"
        lvs.mkdir(parents=True)
        (lvs / "cap_cmomi_derivations.lvs").symlink_to("../../../../../../ihp-sg13g2/missing")
    (dest / "libs.ref" / "dangling_dir").symlink_to("nowhere/")
    (dest / "libs.ref" / "loop").symlink_to("loop")
    return tmp_path


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={"PATH": "/usr/bin:/bin", "IHP_PDKS_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
    )


def test_warns_but_exits_zero(tmp_path):
    r = _run(_tree(tmp_path, 12))
    assert r.returncode == 0
    assert "WARNING: 14 dangling symlink(s)" in r.stderr  # 12 + dir + loop
    assert "0.3.0" in r.stderr
    assert len([ln for ln in r.stderr.splitlines() if ln.startswith("    libs.")]) == 10


def test_lvs_dir_dangle_names_runset(tmp_path):
    r = _run(_tree(tmp_path, 0, lvs_dangling=True))
    assert r.returncode == 0
    assert "PDK-native LVS runset" in r.stderr


def test_non_lvs_dangle_has_no_lvs_line(tmp_path):
    r = _run(_tree(tmp_path, 3))
    assert "LVS runset" not in r.stderr


def test_strict_fails(tmp_path):
    r = _run(_tree(tmp_path, 1), "--strict")
    assert r.returncode == 3
    assert "--strict" in r.stderr


def test_missing_sibling_guard_fires_first(tmp_path):
    (tmp_path / "ihp-open-pdk").mkdir()
    r = _run(tmp_path, "--strict")
    assert r.returncode == 1
    assert "no ihp-sg13g2 checkout" in r.stderr


def test_unknown_arg(tmp_path):
    assert _run(_tree(tmp_path, 0), "--bogus").returncode == 2


@pytest.mark.parametrize("flags", [(), ("--strict",)])
def test_clean_tree_reports_clean(tmp_path, flags):
    t = _tree(tmp_path, 0)
    for p in (t / "ihp-open-pdk/ihp-sg13cmos5l/libs.ref").iterdir():
        p.unlink()
    r = _run(t, *flags)
    assert r.returncode == 0
    assert "no dangling symlinks" in r.stdout

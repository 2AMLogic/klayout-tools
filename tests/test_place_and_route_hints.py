"""Mount-gap hint tests split out of test_place_and_route.py (issue #5626).

That file sits at its per-file size-ratchet ceiling, so new hint tests land
here instead of growing it.
"""

from __future__ import annotations

from helpers.subprocess_fakes import fake_completed
from klayout_tools import place_and_route


def test_cache_root_unreadable_hint_skips_common_ancestor_advice(tmp_path, monkeypatch):
    """Issue #5626: a readable path under the klt cache root (the merged
    `.lib` a list-form liberty descriptor produces) is a mount gap too, but
    "run from a common ancestor" is unachievable for a cache path -- the
    hint names the cache root and the achievable remediations instead."""
    cache_root_dir = tmp_path / "klt-cache"
    merged = cache_root_dir / "liberty-merged" / "merged-abc123.lib"
    merged.parent.mkdir(parents=True)
    merged.write_text("library fake {}\n", encoding="utf-8")
    monkeypatch.setenv("KLT_CACHE_DIR", str(cache_root_dir))
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    completed = fake_completed(
        returncode=1,
        stderr=f"Error: pnr_top_floorplan.tcl, 1\ncannot read file {merged}.\n",
    )

    message = place_and_route._engine_error_message("floorplan", completed)

    assert "common ancestor" not in message
    assert str(cache_root_dir) in message
    assert "install-openroad-docker.sh" in message
    assert "KLT_CACHE_DIR" in message


def test_cache_root_hint_uses_default_root_when_klt_cache_dir_unset(
    tmp_path, monkeypatch
):
    """Issue #5626: with KLT_CACHE_DIR/XDG_CACHE_HOME unset the cache-root
    hint resolves ${XDG_CACHE_HOME:-$HOME/.cache}/klt -- the same path the
    wrapper mounts."""
    merged = tmp_path / ".cache" / "klt" / "liberty-merged" / "merged-abc.lib"
    merged.parent.mkdir(parents=True)
    merged.write_text("library fake {}\n", encoding="utf-8")
    monkeypatch.delenv("KLT_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    completed = fake_completed(
        returncode=1,
        stderr=f"Error: pnr_top_floorplan.tcl, 1\ncannot read file {merged}.\n",
    )

    message = place_and_route._engine_error_message("floorplan", completed)

    assert str(tmp_path / ".cache" / "klt") in message
    assert "common ancestor" not in message


def test_non_cache_path_keeps_common_ancestor_advice(tmp_path, monkeypatch):
    """Issue #5626: only a path under the klt cache root loses the
    common-ancestor advice; an ordinary out-of-tree input keeps it."""
    outside = tmp_path / "elsewhere" / "macro.lef"
    outside.parent.mkdir()
    outside.write_text("# lef\n", encoding="utf-8")
    monkeypatch.delenv("KLT_CACHE_DIR", raising=False)
    monkeypatch.setenv("KLT_CACHE_DIR", str(tmp_path / "klt-cache"))
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    completed = fake_completed(
        returncode=1,
        stderr=f"Error: pnr_top_floorplan.tcl, 1\ncannot read file {outside}.\n",
    )

    message = place_and_route._engine_error_message("floorplan", completed)

    assert "common ancestor" in message

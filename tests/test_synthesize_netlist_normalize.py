"""Source-path normalization of ``klt synthesize`` netlists (issue #4872)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import pytest

from klayout_tools import synthesize
from klayout_tools.synthesize_netlist_normalize import (
    normalize_source_paths,
    rewrite_netlist,
)
from test_synthesize import _REAL_SKY130_VARIANT
from test_synthesize_arithmetic import _synth_script_path, requires_engine

_FUNC_RTL = """
module top (input [3:0] a, output [3:0] y);
  function [3:0] f(input [3:0] x); f = x + 4'd1; endfunction
  assign y = f(a);
endmodule
"""


def test_strips_directory_from_func_names():
    text = "wire \\f$func$/work/a/f.v:3$2.x ;\n"
    assert normalize_source_paths(text, ["/work/a/f.v"]) == (
        "wire \\f$func$f.v:3$2.x ;\n"
    )


def test_text_without_path_is_returned_unchanged():
    text = "module top;\nendmodule\n"
    assert normalize_source_paths(text, ["/work/a/f.v"]) is text


def test_multiple_sources_and_nested_dirs():
    text = "$func$/w/a/b/x.v:1 $func$/w/a/y.v:2 $func$/w/a/b/x.v:9"
    out = normalize_source_paths(text, ["/w/a/y.v", "/w/a/b/x.v"])
    assert out == "$func$x.v:1 $func$y.v:2 $func$x.v:9"


def test_regex_metacharacters_in_path_are_literal():
    text = "$func$/w/a+b(1)[x]/f.v:3"
    assert normalize_source_paths(text, ["/w/a+b(1)[x]/f.v"]) == "$func$f.v:3"


def test_rewrite_netlist_leaves_unaffected_file_untouched(tmp_path):
    path = tmp_path / "n.v"
    path.write_text("module top;\nendmodule\n")
    os.utime(path, (1, 1))
    text = path.read_text()
    rewrite_netlist(str(path), text, text, ["/w/a/f.v"])
    assert os.stat(path).st_mtime == 1


def test_rewrite_netlist_writes_normalized_text(tmp_path):
    path = tmp_path / "n.v"
    text = "$func$/w/a/f.v:3\n"
    path.write_text(text)
    rewrite_netlist(str(path), text, text, ["/w/a/f.v"])
    assert path.read_text() == "$func$f.v:3\n"


# ---------------------------------------------------------------------------
# repo-relative spelling (issue #5594): `_script_path_text` embeds sources
# inside `repo_root` as repo-relative POSIX paths, so Yosys records that
# spelling in `$func$` net names and the absolute-only strip never matches.
# ---------------------------------------------------------------------------


def test_strips_repo_relative_directory():
    text = (
        "wire \\sv2v_cast_32$func$flow/soc/.klt/synthesize/r1/top_sv2v.v:13$3.inp ;\n"
    )
    out = normalize_source_paths(
        text,
        ["/repo/flow/soc/.klt/synthesize/r1/top_sv2v.v"],
        repo_root="/repo",
    )
    assert out == "wire \\sv2v_cast_32$func$top_sv2v.v:13$3.inp ;\n"


def test_strips_repo_relative_source_path_native_frontend():
    text = "\\f$func$flow/soc/top.v:3$2.x\n"
    out = normalize_source_paths(text, ["/repo/flow/soc/top.v"], repo_root="/repo")
    assert out == "\\f$func$top.v:3$2.x\n"


def test_repo_root_none_leaves_relative_spelling_untouched():
    text = "$func$flow/soc/top.v:3"
    assert normalize_source_paths(text, ["/repo/flow/soc/top.v"]) is text


def test_source_at_repo_root_does_not_strip_a_relative_root():
    text = "$func$top.v:3 $func$elsewhere/x.v:1"
    out = normalize_source_paths(text, ["/repo/top.v"], repo_root="/repo")
    assert out == text


def test_relative_dirs_strip_longest_first():
    text = "$func$flow/soc/a/x.v:1 $func$flow/y.v:2 $func$flow/soc/a/x.v:9"
    out = normalize_source_paths(
        text,
        ["/repo/flow/y.v", "/repo/flow/soc/a/x.v"],
        repo_root="/repo",
    )
    assert out == "$func$x.v:1 $func$y.v:2 $func$x.v:9"


def test_source_outside_repo_falls_back_to_absolute_strip():
    text = "$func$/elsewhere/top.v:3 $func$flow/soc/.klt/synthesize/r1/top_sv2v.v:5"
    out = normalize_source_paths(
        text,
        ["/elsewhere/top.v", "/repo/flow/soc/.klt/synthesize/r1/top_sv2v.v"],
        repo_root="/repo",
    )
    assert out == "$func$top.v:3 $func$top_sv2v.v:5"


def test_rewrite_netlist_passes_repo_root(tmp_path):
    path = tmp_path / "n.v"
    text = "$func$flow/soc/f.v:3\n"
    path.write_text(text)
    rewrite_netlist(str(path), text, text, ["/repo/flow/soc/f.v"], repo_root="/repo")
    assert path.read_text() == "$func$f.v:3\n"


def _run(tmp_path, monkeypatch, name):
    root, variant = _REAL_SKY130_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)
    work = tmp_path / name
    work.mkdir()
    (work / "top.v").write_text(_FUNC_RTL)
    request = work / "request.json"
    request.write_text(
        json.dumps(
            {
                "sources": ["top.v"],
                "hdl_toplevel": "top",
                "pdk": {"cell_library": "sky130_fd_sc_hd"},
            }
        )
    )
    report = synthesize.run_synthesize(str(request))
    script = _synth_script_path(work, report, "top")
    with open(os.path.join(os.path.dirname(script), "top_synth.v"), "rb") as handle:
        return str(work), handle.read(), report


@requires_engine
@pytest.mark.skipif(_REAL_SKY130_VARIANT is None, reason="needs sky130 liberty")
def test_netlist_is_identical_across_directories(tmp_path, monkeypatch):
    dir_a, net_a, rep_a = _run(tmp_path, monkeypatch, "a")
    dir_b, net_b, rep_b = _run(tmp_path, monkeypatch, "b")
    assert net_a == net_b
    assert dir_a.encode() not in net_a and dir_b.encode() not in net_b
    assert rep_a["netlist_sha256"] == rep_b["netlist_sha256"]
    assert rep_a["netlist_sha256"] == hashlib.sha256(net_a).hexdigest()


# ---------------------------------------------------------------------------
# end-to-end, inside real git repositories (issue #5594). The #4872 test
# above builds under tmp_path, which has no .git ancestor: repo_root is
# None, _script_path_text spells sources absolutely, and only the
# already-working absolute branch of the normalizer is exercised. These
# runs must live in real repos at two different depths to reach the
# repo-relative branch -- and they fail before the fix.
# ---------------------------------------------------------------------------


HAVE_GIT = shutil.which("git") is not None

_in_repo = pytest.mark.skipif(not HAVE_GIT, reason="needs git")
# CI's `test` job installs a pinned sv2v (scripts/install-sv2v.sh, issue
# #5956), so the `sv_frontend: "sv2v"` cases below run there -- with a
# no-silent-skip assert step (ci.yml) failing the leg if they ever skip
# again. Hosts without a real sv2v on PATH still skip them;
# `test_synthesize.py` separately covers the sv2v route in CI through a
# stub `sv2v` on PATH.
_needs_sv2v = pytest.mark.skipif(shutil.which("sv2v") is None, reason="needs sv2v")
_needs_pdk = pytest.mark.skipif(
    _REAL_SKY130_VARIANT is None, reason="needs sky130 liberty"
)


def _git_init(repo_dir):
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)


def _run_case(monkeypatch, work, *, sv_frontend, run_id):
    """One run_synthesize over _FUNC_RTL rooted at ``work``; returns
    ``(work, netlist bytes, report)``."""
    root, variant = _REAL_SKY130_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)
    work.mkdir(parents=True, exist_ok=True)
    (work / "top.v").write_text(_FUNC_RTL)
    request = {
        "sources": ["top.v"],
        "hdl_toplevel": "top",
        "pdk": {"cell_library": "sky130_fd_sc_hd"},
    }
    if sv_frontend is not None:
        request["sv_frontend"] = sv_frontend
    if run_id is not None:
        request["run_id"] = run_id
    request_path = work / "request.json"
    request_path.write_text(json.dumps(request))
    report = synthesize.run_synthesize(str(request_path))
    script = _synth_script_path(work, report, "top")
    with open(os.path.join(os.path.dirname(script), "top_synth.v"), "rb") as handle:
        return work, handle.read(), report


@_needs_sv2v
@requires_engine
@_needs_pdk
@_in_repo
def test_netlist_identical_at_two_repo_depths(tmp_path, monkeypatch):
    repo_deep = tmp_path / "repoA" / "flow" / "soc"
    repo_top = tmp_path / "repoB"
    plain = tmp_path / "plain"
    _git_init(tmp_path / "repoA")
    _git_init(repo_top)
    deep_dir, net_deep, rep_deep = _run_case(
        monkeypatch, repo_deep, sv_frontend="sv2v", run_id="r1"
    )
    top_dir, net_top, rep_top = _run_case(
        monkeypatch, repo_top, sv_frontend="sv2v", run_id="r1"
    )
    plain_dir, net_plain, rep_plain = _run_case(
        monkeypatch, plain, sv_frontend="sv2v", run_id="r1"
    )
    assert net_deep == net_top == net_plain
    assert (
        rep_deep["netlist_sha256"]
        == rep_top["netlist_sha256"]
        == rep_plain["netlist_sha256"]
        == hashlib.sha256(net_deep).hexdigest()
    )
    for work, net in ((deep_dir, net_deep), (top_dir, net_top), (plain_dir, net_plain)):
        assert str(work).encode() not in net  # absolute spelling
        assert b".klt/synthesize" not in net  # repo-relative spelling


@requires_engine
@_needs_pdk
@_in_repo
def test_netlist_identical_at_two_repo_depths_native_frontend(tmp_path, monkeypatch):
    repo_deep = tmp_path / "repoA" / "flow" / "soc"
    repo_top = tmp_path / "repoB"
    _git_init(tmp_path / "repoA")
    _git_init(repo_top)
    deep_dir, net_deep, rep_deep = _run_case(
        monkeypatch, repo_deep, sv_frontend=None, run_id="r1"
    )
    top_dir, net_top, rep_top = _run_case(
        monkeypatch, repo_top, sv_frontend=None, run_id="r1"
    )
    assert net_deep == net_top
    assert rep_deep["netlist_sha256"] == rep_top["netlist_sha256"]
    for work, net in ((deep_dir, net_deep), (top_dir, net_top)):
        assert str(work).encode() not in net
        assert b".klt/synthesize" not in net


@_needs_sv2v
@requires_engine
@_needs_pdk
@_in_repo
def test_netlist_identical_across_defaulted_run_ids(tmp_path, monkeypatch):
    solo = tmp_path / "solo"
    _git_init(solo)
    work_a, net_a, rep_a = _run_case(monkeypatch, solo, sv_frontend="sv2v", run_id=None)
    work_b, net_b, rep_b = _run_case(monkeypatch, solo, sv_frontend="sv2v", run_id=None)
    assert rep_a["run_id"] != rep_b["run_id"]
    assert net_a == net_b
    assert rep_a["netlist_sha256"] == rep_b["netlist_sha256"]
    for work, net in ((work_a, net_a), (work_b, net_b)):
        assert str(work).encode() not in net
        assert b".klt/synthesize" not in net

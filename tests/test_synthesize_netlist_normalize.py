"""Source-path normalization of ``klt synthesize`` netlists (issue #4872)."""

from __future__ import annotations

import hashlib
import json
import os

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

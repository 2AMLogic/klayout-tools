"""Tests for `klt formal` and the `klayout_tools.formal` library.

Two tiers:

- **Stub-sby tests** always run: a small executable stands in for `sby`
  (named via `request.sby_binary`) and writes the same log lines, status
  and trace layout real sby does, so request validation, `.sby` generation,
  verdict mapping (pass / fail / unknown) and exit codes are covered with
  no solver installed.
- **Real-sby integration tests** are skipped unless `sby`, `yosys` and a
  solver (`bitwuzla`) are on `$PATH` (see `scripts/install-symbiyosys.sh`):
  a seeded-failing assertion must yield `fail` + a trace file, a passing
  one `pass`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import textwrap

import pytest

from klayout_tools.cli.parser import create_parser
from klayout_tools.formal import FormalError, run_formal

_FAKE_SBY = """\
#!/usr/bin/env python3
import os, sys
sbyfile = sys.argv[-1]
work = os.path.splitext(sbyfile)[0]
os.makedirs(os.path.join(work, "engine_0"), exist_ok=True)
{body}
"""

_PASS_BODY = """\
print("SBY [formal] engine_0: smtbmc: Checking assertions in step 0..")
print("SBY [formal] DONE (PASS, rc=0)")
sys.exit(0)
"""

_FAIL_BODY = """\
print("SBY [formal] engine_0: smtbmc: Checking assertions in step 0..")
print("SBY [formal] engine_0: smtbmc: Checking assertions in step 3..")
print("SBY [formal] engine_0: smtbmc: BMC failed!")
open(os.path.join(work, "engine_0", "trace.vcd"), "w").write("$end\\n")
print("SBY [formal] DONE (FAIL, rc=2)")
sys.exit(2)
"""

_UNKNOWN_BODY = """\
print("SBY [formal] engine_0: smtbmc: Checking assertions in step 5..")
print("SBY [formal] DONE (UNKNOWN, rc=4)")
sys.exit(4)
"""

_TIMEOUT_BODY = """\
print("SBY [formal] engine_0: smtbmc: Checking assertions in step 7..")
print("SBY [formal] DONE (TIMEOUT, rc=8)")
sys.exit(8)
"""

_ERROR_BODY = """\
print("SBY [formal] ERROR: Yosys script failed: module 'foo' not found")
print("SBY [formal] DONE (ERROR, rc=16)")
sys.exit(16)
"""

_COVER_PASS_BODY = """\
print("SBY [formal] engine_0: ##  0:00:00  Reached cover statement in step 4 at x.v:3")
open(os.path.join(work, "engine_0", "trace0.vcd"), "w").write("$end\\n")
print("SBY [formal] DONE (PASS, rc=0)")
sys.exit(0)
"""

_COVER_FAIL_BODY = """\
print("SBY [formal] DONE (FAIL, rc=2)")
sys.exit(2)
"""


def _fake_sby(tmp_path, body: str) -> str:
    path = tmp_path / "fake_sby"
    path.write_text(_FAKE_SBY.format(body=body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def fake_solver(tmp_path, monkeypatch):
    """Put a no-op `bitwuzla` on PATH so the solver pre-check passes."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    solver = bindir / "bitwuzla"
    solver.write_text("#!/bin/sh\nexit 0\n")
    solver.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _request(tmp_path, sby: str, **overrides) -> str:
    (tmp_path / "d.v").write_text("module top(input a); endmodule\n")
    doc = {"top": "top", "sources": ["d.v"], "sby_binary": sby}
    doc.update(overrides)
    path = tmp_path / "req.json"
    path.write_text(json.dumps(doc))
    return str(path)


def test_pass(tmp_path, fake_solver):
    report = run_formal(_request(tmp_path, _fake_sby(tmp_path, _PASS_BODY), depth=12))
    assert report["status"] == "pass"
    assert report["depth_reached"] == 12
    assert report["trace_path"] is None
    assert report["netlist_path"] is None
    assert report["schema_version"] == 1


def test_fail_has_trace_and_failing_depth(tmp_path, fake_solver):
    report = run_formal(_request(tmp_path, _fake_sby(tmp_path, _FAIL_BODY)))
    assert report["status"] == "fail"
    assert report["depth_reached"] == 3
    assert report["trace_path"].endswith("engine_0/trace.vcd")
    assert os.path.isfile(report["trace_path"])


def test_unknown_when_induction_unproven(tmp_path, fake_solver):
    report = run_formal(
        _request(tmp_path, _fake_sby(tmp_path, _UNKNOWN_BODY), mode="prove")
    )
    assert report["status"] == "unknown"
    assert report["depth_reached"] == 5
    assert report["trace_path"] is None


def test_sby_timeout_status_is_unknown_never_pass(tmp_path, fake_solver):
    report = run_formal(_request(tmp_path, _fake_sby(tmp_path, _TIMEOUT_BODY)))
    assert report["status"] == "unknown"
    assert [d["code"] for d in report["diagnostics"]] == ["timeout"]


def test_process_timeout_is_unknown(tmp_path, fake_solver, monkeypatch):
    import klayout_tools.formal as formal

    monkeypatch.setattr(formal, "_KILL_GRACE_S", 0.0)
    body = "import time\ntime.sleep(30)\n"
    report = run_formal(
        _request(tmp_path, _fake_sby(tmp_path, body), timeout_s=0.5),
    )
    assert report["status"] == "unknown"
    assert report["diagnostics"][0]["code"] == "timeout"


def test_cover_hit_has_trace_and_unreached_is_fail(tmp_path, fake_solver):
    hit = run_formal(
        _request(tmp_path, _fake_sby(tmp_path, _COVER_PASS_BODY), mode="cover")
    )
    assert hit["status"] == "pass"
    assert hit["depth_reached"] == 4
    assert hit["trace_path"].endswith("trace0.vcd")

    miss = run_formal(
        _request(tmp_path, _fake_sby(tmp_path, _COVER_FAIL_BODY), mode="cover")
    )
    assert miss["status"] == "fail"
    assert miss["trace_path"] is None
    assert miss["diagnostics"][0]["code"] == "cover_unreached"


def test_sby_error_raises(tmp_path, fake_solver):
    with pytest.raises(FormalError, match="module 'foo' not found"):
        run_formal(_request(tmp_path, _fake_sby(tmp_path, _ERROR_BODY)))


def test_sby_file_has_no_absolute_paths_and_reads_liberty(tmp_path, fake_solver):
    (tmp_path / "cells.lib").write_text("library(x){}\n")
    (tmp_path / "n_synth.v").write_text("module top(input a); endmodule\n")
    req = tmp_path / "req.json"
    req.write_text(
        json.dumps(
            {
                "top": "top",
                "netlist_path": "n_synth.v",
                "liberty": "cells.lib",
                "mode": "prove",
                "depth": 7,
                "sby_binary": _fake_sby(tmp_path, _PASS_BODY),
            }
        )
    )
    report = run_formal(str(req))
    assert report["netlist_path"] == str(tmp_path / "n_synth.v")
    sby_text = open(report["artifacts"]["sby_path"]).read()
    script = sby_text.split("[script]")[1].split("[files]")[0]
    assert "/" not in script
    assert (
        "read_liberty -overwrite -ignore_miss_dir -ignore_miss_func 0_cells.lib"
        in script
    )
    assert "read_verilog -formal -sv 1_n_synth.v" in script
    assert "mode prove" in sby_text and "depth 7" in sby_text
    assert f"1_n_synth.v {tmp_path / 'n_synth.v'}" in sby_text


def test_same_basename_inputs_do_not_collide(tmp_path, fake_solver):
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "x.v").write_text("module m; endmodule\n")
    req = _request(
        tmp_path, _fake_sby(tmp_path, _PASS_BODY), sources=["a/x.v", "b/x.v"]
    )
    report = run_formal(req)
    files = open(report["artifacts"]["sby_path"]).read().split("[files]")[1].split()
    assert files[0] != files[2]


def test_missing_sby_is_structured_error(tmp_path, fake_solver, monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(shutil.which("bitwuzla")))
    req = _request(tmp_path, "")
    doc = json.loads(open(req).read())
    del doc["sby_binary"]
    open(req, "w").write(json.dumps(doc))
    with pytest.raises(FormalError, match="install-symbiyosys.sh"):
        run_formal(req)


def test_missing_solver_is_structured_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    with pytest.raises(FormalError, match="solver"):
        run_formal(_request(tmp_path, _fake_sby(tmp_path, _PASS_BODY)))


@pytest.mark.parametrize(
    "override, message",
    [
        ({"mode": "live"}, "unsupported mode"),
        ({"depth": 0}, "depth"),
        ({"timeout_s": -1}, "timeout_s"),
        ({"solver": "nope"}, "unsupported solver"),
        ({"sources": ["missing.v"]}, "source not found"),
        ({"liberty": "missing.lib"}, "liberty file not found"),
        ({"sources": None, "netlist_path": None}, "sources"),
    ],
)
def test_request_validation(tmp_path, fake_solver, override, message):
    req = _request(tmp_path, _fake_sby(tmp_path, _PASS_BODY), **override)
    with pytest.raises(FormalError, match=message):
        run_formal(req)


def test_absolute_tmp_source_is_staged_not_referenced(tmp_path, fake_solver):
    other = tmp_path / "elsewhere"
    other.mkdir()
    src = other / "abs.v"
    src.write_text("module top; endmodule\n")
    req = _request(tmp_path, _fake_sby(tmp_path, _PASS_BODY), sources=[str(src)])
    report = run_formal(req)
    script = open(report["artifacts"]["sby_path"]).read().split("[script]")[1]
    assert str(src) not in script.split("[files]")[0]


@pytest.mark.parametrize(
    "body, code",
    [(_PASS_BODY, 0), (_FAIL_BODY, 3), (_UNKNOWN_BODY, 4)],
)
def test_cli_exit_codes_and_envelope(tmp_path, fake_solver, capsys, body, code):
    req = _request(tmp_path, _fake_sby(tmp_path, body))
    args = create_parser().parse_args(["formal", req, "--format", "json"])
    assert args.func(args) == code
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == 1
    assert doc["status"] in ("pass", "fail", "unknown")


def test_cli_error_envelope(tmp_path, capsys):
    args = create_parser().parse_args(
        ["formal", str(tmp_path / "no.json"), "--format", "json"]
    )
    assert args.func(args) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "formal"


# --------------------------------------------------------------------------- #
# Real sby
# --------------------------------------------------------------------------- #

_have_sby = all(shutil.which(t) for t in ("sby", "yosys", "bitwuzla"))

_COUNTER = textwrap.dedent(
    """\
    module top(input clk, input rst, output reg [3:0] q);
      always @(posedge clk) q <= rst ? 4'd0 : q + 4'd1;
      reg init = 1;
      always @(posedge clk) begin
        init <= 0;
        if (!init && $past(rst)) assert (q == 0);
        if (!init && !$past(rst)) assert (q == $past(q) + 4'd1);
        {extra}
      end
    endmodule
    """
)


@pytest.mark.skipif(not _have_sby, reason="sby/yosys/bitwuzla not on PATH")
def test_real_sby_pass_and_seeded_failure(tmp_path):
    (tmp_path / "ok.v").write_text(_COUNTER.format(extra=""))
    (tmp_path / "bad.v").write_text(_COUNTER.format(extra="assert (q != 4'd5);"))
    for name, expected in (("ok", "pass"), ("bad", "fail")):
        req = tmp_path / f"{name}.json"
        req.write_text(
            json.dumps({"top": "top", "sources": [f"{name}.v"], "depth": 10})
        )
        report = run_formal(str(req))
        assert report["status"] == expected
        if expected == "fail":
            assert os.path.isfile(report["trace_path"])


_COVER_DESIGN = textwrap.dedent(
    """\
    module top(input clk, input rst, output reg [3:0] q);
      always @(posedge clk) q <= rst ? 4'd0 : q + 4'd1;
      always @(posedge clk) cover ({cond});
    endmodule
    """
)

_UNINDUCTIVE = textwrap.dedent(
    """\
    module top(input clk, output reg [3:0] q);
      initial q = 0;
      always @(posedge clk) q <= q + 4'd1;
      always @(posedge clk) assert (q != 4'd15);
    endmodule
    """
)


def _real_request(tmp_path, name, text, **fields):
    (tmp_path / f"{name}.v").write_text(text)
    req = tmp_path / f"{name}.json"
    req.write_text(json.dumps({"top": "top", "sources": [f"{name}.v"], **fields}))
    return str(req)


@pytest.mark.skipif(not _have_sby, reason="sby/yosys/bitwuzla not on PATH")
def test_real_sby_cover_reachable_has_trace_and_step(tmp_path):
    req = _real_request(
        tmp_path,
        "reach",
        _COVER_DESIGN.format(cond="q == 4'd3"),
        depth=8,
        mode="cover",
    )
    report = run_formal(req)
    assert report["status"] == "pass"
    # q is unconstrained at step 0, so sby reaches q == 3 at step 1; a
    # depth_reached of 8 would mean the "Reached ... in step N" line was
    # not parsed.
    assert report["depth_reached"] == 1
    assert os.path.isfile(report["trace_path"])


@pytest.mark.skipif(not _have_sby, reason="sby/yosys/bitwuzla not on PATH")
def test_real_sby_cover_unreachable_is_fail(tmp_path):
    req = _real_request(
        tmp_path,
        "unreach",
        _COVER_DESIGN.format(cond="q == 4'd3 && q == 4'd4"),
        depth=8,
        mode="cover",
    )
    report = run_formal(req)
    assert report["status"] == "fail"
    assert report["trace_path"] is None
    assert [d["code"] for d in report["diagnostics"]] == ["cover_unreached"]


@pytest.mark.skipif(not _have_sby, reason="sby/yosys/bitwuzla not on PATH")
def test_real_sby_prove_failing_induction_is_unknown(tmp_path):
    req = _real_request(tmp_path, "ind", _UNINDUCTIVE, depth=8, mode="prove")
    report = run_formal(req)
    assert report["status"] == "unknown"
    assert [d["code"] for d in report["diagnostics"]] == ["unproven"]

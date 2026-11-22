"""The #5784 *input* envelopes follow the request's repo, not the cwd (#6762).

`environment.sdf.file`, `environment.initial_state.netlist` and the
`environment.source_defines[].source` entries are judged against the repo
containing the request -- the same anchor #6762 gave the verb's sources and
artifact envelopes -- so one request yields one committed report whether
`klt` runs from its own checkout, from an unrelated one or from outside
every checkout. Split out of `test_functional_verification_path_envelopes.py`
(its helpers are imported) to keep that file under the size ratchet.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from helpers.cocotb_fakes import FakeCocotbRunner
from klayout_tools import functional_verification_verilog_top as _vtop
from klayout_tools.functional_verification import run_functional_verification
from test_functional_verification_path_envelopes import (
    MINIMAL_SDF,
    RESULTS_XML,
    _request,
    _sha256,
    _stage,
    _stub,
    _write,
)


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "checkout-a" / "proj"
    proj.mkdir(parents=True)
    (tmp_path / "checkout-a" / ".git").mkdir()
    return proj


def _cwds(tmp_path: Path, proj: Path) -> list[Path]:
    """Own checkout, outside every checkout, an unrelated checkout."""
    (tmp_path / "driver").mkdir()
    (tmp_path / "checkout-b" / ".git").mkdir(parents=True)
    return [proj, tmp_path / "driver", tmp_path / "checkout-b"]


def _inputs(report) -> dict:
    env = report["environment"]
    return {
        "sdf": env["sdf"] and env["sdf"]["file"],
        "initial_state": env["initial_state"] and env["initial_state"]["netlist"],
        "source_defines": env["source_defines"],
    }


def _assert_same_from_every_cwd(tmp_path, monkeypatch, proj, run) -> dict:
    runs = []
    for cwd in _cwds(tmp_path, proj):
        monkeypatch.chdir(cwd)
        runs.append(_inputs(run()))
    for got in runs[1:]:
        assert json.dumps(got, sort_keys=True) == json.dumps(runs[0], sort_keys=True)
    return runs[0]


def test_cocotb_sdf_and_source_defines_follow_the_request_repo(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    _stage(proj)
    _write(proj / "route.sdf", MINIMAL_SDF)
    _stub(monkeypatch, FakeCocotbRunner(RESULTS_XML))
    request = _request(
        proj,
        sdf={"file": "route.sdf", "corner": "typ"},
        source_defines={"gcd.v": {"MODE": "x"}},
    )
    got = _assert_same_from_every_cwd(
        tmp_path,
        monkeypatch,
        proj,
        lambda: run_functional_verification(request, str(proj / "out")),
    )
    assert got["sdf"] == {
        "path": "proj/route.sdf",
        "scope": "repo",
        "sha256": _sha256(proj / "route.sdf"),
    }
    assert got["source_defines"] == [
        {
            "source": {
                "path": "proj/gcd.v",
                "scope": "repo",
                "sha256": _sha256(proj / "gcd.v"),
            },
            "defines": {"MODE": "x"},
        }
    ]


def test_initial_state_netlist_follows_the_request_repo(tmp_path, monkeypatch):
    from test_functional_verification_initial_state import MODELS, NETLIST

    proj = _project(tmp_path)
    _write(proj / "models.v", MODELS)
    _write(proj / "net.v", NETLIST)
    _write(proj / "test_tb.py", "# stub testbench\n")
    _stub(monkeypatch, FakeCocotbRunner(RESULTS_XML))
    request = _write(
        proj / "request.json",
        json.dumps(
            {
                "engine": "icarus",
                "sources": ["models.v", "net.v"],
                "hdl_toplevel": "top",
                "testbench": {"module": "test_tb", "testcase": None},
                "options": {"initial_state": {"mode": "x", "netlist": "net.v"}},
            }
        ),
    )
    got = _assert_same_from_every_cwd(
        tmp_path,
        monkeypatch,
        proj,
        lambda: run_functional_verification(request, str(proj / "out")),
    )
    assert got["initial_state"] == {
        "path": "proj/net.v",
        "scope": "repo",
        "sha256": _sha256(proj / "net.v"),
    }


def test_verilog_top_inputs_follow_the_request_repo(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    _write(proj / "dut.v", "module dut(input wire clk); endmodule\n")
    _write(
        proj / "tb.v",
        'module tb; gcd dut(); initial begin $display("PASS"); $finish; end\n'
        "endmodule\n",
    )
    _write(proj / "route.sdf", MINIMAL_SDF)
    request = _write(
        proj / "request.json",
        json.dumps(
            {
                "engine": "icarus",
                "sources": ["dut.v", "tb.v"],
                "hdl_toplevel": "dut",
                "testbench": {
                    "pass_marker": "PASS",
                    "verilog_top": "tb",
                    "dut_instance": "tb.dut",
                },
                "options": {
                    "sdf": {"file": "route.sdf"},
                    "source_defines": {"dut.v": {"MODE": "x"}},
                },
            }
        ),
    )

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        # Stub only the simulator pair; main's provenance `git` probe runs real.
        if Path(cmd[0]).name not in ("iverilog", "vvp"):
            return real_run(cmd, **kwargs)
        handle = kwargs.get("stdout")
        if handle is not None:
            handle.write("fake transcript\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(_vtop.subprocess, "run", fake_run)
    got = _assert_same_from_every_cwd(
        tmp_path,
        monkeypatch,
        proj,
        lambda: run_functional_verification(request, str(proj / "out")),
    )
    assert got["sdf"]["path"] == "proj/route.sdf"
    assert got["sdf"]["scope"] == "repo"
    assert got["source_defines"][0]["source"]["path"] == "proj/dut.v"
    assert got["source_defines"][0]["source"]["scope"] == "repo"

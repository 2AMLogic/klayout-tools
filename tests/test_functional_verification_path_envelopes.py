"""Response-side path envelopes for `klt functional-verification` (issue
#5784). The three request-resolved *input* paths the response echoes --
`environment.sdf.file`, `environment.initial_state.netlist` and the
`environment.source_defines` entries -- now carry the same hashed
`{path, scope, sha256}` envelope the verb's output paths have carried since
#4279, so a report committed as evidence stays checkout-free.

Resolution is **unchanged**: the engine still receives the absolute path
(the generated `$sdf_annotate` shim, the define wrappers), only the echoed
report changed. `--format text` still prints the real absolute paths, fed
by the builders' `local_paths` side-channel.

Hermetic: every test drives the stubbed cocotb runner (or a stubbed
`iverilog`/`vvp` subprocess pair for the Verilog-bench builder) -- no
simulator needed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import types
from pathlib import Path

import pytest

from helpers.cocotb_fakes import FakeCocotbRunner
from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_verilog_top as _vtop
from klayout_tools.cli import main
from klayout_tools.env_provenance import find_absolute_path_fields
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)

RTL = "module gcd(input wire clk); endmodule\n"

TB_PY = "# stub testbench\n"

RESULTS_XML = """\
<testsuites name="results"><testsuite name="all" package="all">
<property name="random_seed" value="1" />
<testcase name="test_gcd" classname="test_gcd" time="0.1" sim_time_ns="10.0" />
</testsuite></testsuites>
"""

MINIMAL_SDF = """\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "gcd")
  (DIVIDER .)
  (TIMESCALE 1ns)
)
"""


def _sha256(path: Path) -> str:
    """The digest a report must pin, computed directly from the bytes --
    deliberately not via the production `content_hash`."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _fake_repo(tmp_path: Path, monkeypatch) -> None:
    """tmp_path as a standalone repo root: paths inside it report `repo`
    scope, exactly as a caller's own checkout would."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)


def _request(tmp_path: Path, **options) -> str:
    request = {
        "engine": "icarus",
        "sources": ["gcd.v"],
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd", "testcase": None},
    }
    if options:
        request["options"] = options
    return _write(tmp_path / "request.json", json.dumps(request))


def _stage(tmp_path: Path) -> None:
    _write(tmp_path / "gcd.v", RTL)
    _write(tmp_path / "test_gcd.py", TB_PY)


def _stub(monkeypatch, runner) -> None:
    monkeypatch.setattr(
        fv, "_import_runner", lambda: types.SimpleNamespace(get_runner=lambda n: runner)
    )
    monkeypatch.setattr(fv, "_engine_version", lambda engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.1.0")


# --------------------------------------------------------------------------- #
# repo-scoped inputs: the committed report names the checkout, not the host
# --------------------------------------------------------------------------- #


def test_sdf_file_and_source_defines_are_enveloped_inside_a_repo(tmp_path, monkeypatch):
    _fake_repo(tmp_path, monkeypatch)
    _stage(tmp_path)
    _write(tmp_path / "route.sdf", MINIMAL_SDF)
    _stub(monkeypatch, FakeCocotbRunner(RESULTS_XML))

    report = run_functional_verification(
        _request(
            tmp_path,
            sdf={"file": "route.sdf", "corner": "typ"},
            source_defines={"gcd.v": {"MODE": "x"}},
        ),
        output_dir="out",
    )

    assert report["schema_version"] == 3
    assert report["environment"]["sdf"]["file"] == {
        "path": "route.sdf",
        "scope": "repo",
        "sha256": _sha256(tmp_path / "route.sdf"),
    }
    assert report["environment"]["source_defines"] == [
        {
            "source": {
                "path": "gcd.v",
                "scope": "repo",
                "sha256": _sha256(tmp_path / "gcd.v"),
            },
            "defines": {"MODE": "x"},
        }
    ]
    assert report.local_paths["sdf"] == str(tmp_path / "route.sdf")
    assert find_absolute_path_fields(report) == []


def test_symlinked_input_reports_the_real_path(tmp_path, monkeypatch):
    _fake_repo(tmp_path, monkeypatch)
    _stage(tmp_path)
    _write(tmp_path / "real.sdf", MINIMAL_SDF)
    (tmp_path / "route.sdf").symlink_to(tmp_path / "real.sdf")
    _stub(monkeypatch, FakeCocotbRunner(RESULTS_XML))

    report = run_functional_verification(
        _request(tmp_path, sdf={"file": "route.sdf"}), output_dir="out"
    )

    assert report["environment"]["sdf"]["file"] == {
        "path": "real.sdf",
        "scope": "repo",
        "sha256": _sha256(tmp_path / "real.sdf"),
    }


# --------------------------------------------------------------------------- #
# external inputs: no path at all, pinned by identity (sha256)
# --------------------------------------------------------------------------- #


def test_external_inputs_carry_no_path_but_keep_their_sha256(tmp_path, monkeypatch):
    """tmp_path is outside any repo: `path` is `null` and `scope` is
    `external`, yet the digest still pins the input by identity."""
    _stage(tmp_path)
    sdf_path = _write(tmp_path / "elsewhere.sdf", MINIMAL_SDF)
    _stub(monkeypatch, FakeCocotbRunner(RESULTS_XML))

    report = run_functional_verification(
        _request(
            tmp_path,
            sources=[str(tmp_path / "gcd.v")],
            sdf={"file": sdf_path},
            source_defines={str(tmp_path / "gcd.v"): {"MODE": "x"}},
        )
    )

    external = {"path": None, "scope": "external", "sha256": _sha256(Path(sdf_path))}
    assert report["environment"]["sdf"]["file"] == external
    assert report["environment"]["source_defines"] == [
        {
            "source": {
                "path": None,
                "scope": "external",
                "sha256": _sha256(tmp_path / "gcd.v"),
            },
            "defines": {"MODE": "x"},
        }
    ]
    assert str(tmp_path) not in json.dumps(report)


# --------------------------------------------------------------------------- #
# initial_state.netlist
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "netlist_key",
    ["net.v", None],
    ids=["inside_repo", "external"],
)
def test_initial_state_netlist_is_enveloped(tmp_path, monkeypatch, netlist_key):
    from test_functional_verification_initial_state import MODELS, NETLIST

    if netlist_key is not None:
        _fake_repo(tmp_path, monkeypatch)
    _write(tmp_path / "models.v", MODELS)
    netlist_path = _write(tmp_path / "net.v", NETLIST)
    _write(tmp_path / "test_tb.py", TB_PY)
    _stub(monkeypatch, FakeCocotbRunner(RESULTS_XML))

    netlist = netlist_key if netlist_key is not None else netlist_path
    request = {
        "engine": "icarus",
        "sources": ["models.v", "net.v"],
        "hdl_toplevel": "top",
        "testbench": {"module": "test_tb", "testcase": None},
        "options": {"initial_state": {"mode": "x", "netlist": netlist}},
    }
    request_path = _write(tmp_path / "request.json", json.dumps(request))

    report = run_functional_verification(request_path, output_dir=str(tmp_path / "out"))

    netlist_echo = report["environment"]["initial_state"]["netlist"]
    assert netlist_echo["sha256"] == _sha256(tmp_path / "net.v")
    if netlist_key is not None:
        assert netlist_echo == {
            "path": "net.v",
            "scope": "repo",
            "sha256": _sha256(tmp_path / "net.v"),
        }
    else:
        assert netlist_echo == {
            "path": None,
            "scope": "external",
            "sha256": _sha256(tmp_path / "net.v"),
        }
    assert report.local_paths["initial_state_netlist"] == netlist_path
    assert find_absolute_path_fields(report) == []


# --------------------------------------------------------------------------- #
# the mechanical gate: no absolute path survives either builder's report
# --------------------------------------------------------------------------- #


def test_no_absolute_path_survives_a_full_options_report(tmp_path, monkeypatch):
    """`options.sdf` and `options.initial_state` are mutually exclusive, so
    the gate runs twice: sdf + source_defines, then initial_state +
    source_defines. `find_absolute_path_fields` is the in-process form of
    `klt env-provenance lint-envelope`."""
    from test_functional_verification_initial_state import MODELS, NETLIST

    _fake_repo(tmp_path, monkeypatch)
    _stage(tmp_path)
    _write(tmp_path / "route.sdf", MINIMAL_SDF)
    _write(tmp_path / "models.v", MODELS)
    _write(tmp_path / "net.v", NETLIST)
    _write(tmp_path / "test_tb.py", TB_PY)
    _stub(monkeypatch, FakeCocotbRunner(RESULTS_XML))

    sdf_report = run_functional_verification(
        _request(
            tmp_path,
            sdf={"file": "route.sdf"},
            source_defines={"gcd.v": {"MODE": "x"}},
        ),
        output_dir="out",
    )
    state_request = {
        "engine": "icarus",
        "sources": ["models.v", "net.v"],
        "hdl_toplevel": "top",
        "testbench": {"module": "test_tb", "testcase": None},
        "options": {
            "initial_state": {"mode": "x", "netlist": "net.v"},
            "source_defines": {"net.v": {"MODE": "x"}},
        },
    }
    state_report = run_functional_verification(
        _write(tmp_path / "request-state.json", json.dumps(state_request)),
        output_dir="out",
    )

    assert find_absolute_path_fields(sdf_report) == []
    assert find_absolute_path_fields(state_report) == []


# --------------------------------------------------------------------------- #
# resolution is unchanged: the engine still sees the absolute path
# --------------------------------------------------------------------------- #


def test_generated_shim_still_names_the_absolute_sdf(tmp_path, monkeypatch):
    _fake_repo(tmp_path, monkeypatch)
    _stage(tmp_path)
    _write(tmp_path / "route.sdf", MINIMAL_SDF)
    runner = FakeCocotbRunner(RESULTS_XML)
    _stub(monkeypatch, runner)

    run_functional_verification(
        _request(tmp_path, sdf={"file": "route.sdf"}), output_dir="out"
    )

    shim = tmp_path / "out" / "klt_sdf_annotate.v"
    assert f'$sdf_annotate("{tmp_path}/route.sdf"' in shim.read_text(encoding="utf-8")
    shim_source = runner.build_kwargs["sources"][-1]
    assert Path(shim_source).name == "klt_sdf_annotate.v"


def test_missing_sdf_file_is_still_a_request_error(tmp_path):
    _stage(tmp_path)
    with pytest.raises(FunctionalVerificationError, match="SDF file not found"):
        run_functional_verification(
            _request(tmp_path, sdf={"file": "nope.sdf"}),
            output_dir=str(tmp_path / "out"),
        )


# --------------------------------------------------------------------------- #
# the Verilog-bench builder reports the same envelopes
# --------------------------------------------------------------------------- #


def test_verilog_top_builder_reports_the_envelopes(tmp_path, monkeypatch):
    """The Verilog-bench response builder assembles its own `environment`
    block, so its envelopes are asserted separately from the cocotb path's.
    `iverilog`/`vvp` are stubbed at the `subprocess` boundary."""
    _fake_repo(tmp_path, monkeypatch)
    _write(tmp_path / "dut.v", "module dut(input wire clk); endmodule\n")
    _write(
        tmp_path / "tb.v",
        'module tb; gcd dut(); initial begin $display("PASS"); $finish; end\n'
        "endmodule\n",
    )
    _write(tmp_path / "route.sdf", MINIMAL_SDF)
    request = {
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
    request_path = _write(tmp_path / "request.json", json.dumps(request))

    def fake_run(cmd, **kwargs):
        handle = kwargs.get("stdout")
        if handle is not None:
            handle.write("fake transcript\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(_vtop.subprocess, "run", fake_run)

    report = run_functional_verification(request_path, output_dir="out")

    assert report["schema_version"] == 3
    assert report["environment"]["sdf"]["file"] == {
        "path": "route.sdf",
        "scope": "repo",
        "sha256": _sha256(tmp_path / "route.sdf"),
    }
    assert report["environment"]["source_defines"] == [
        {
            "source": {
                "path": "dut.v",
                "scope": "repo",
                "sha256": _sha256(tmp_path / "dut.v"),
            },
            "defines": {"MODE": "x"},
        }
    ]
    assert report.local_paths["sdf"] == str(tmp_path / "route.sdf")
    assert find_absolute_path_fields(report) == []


# --------------------------------------------------------------------------- #
# `--format text` still prints the real absolute paths
# --------------------------------------------------------------------------- #


def test_text_format_prints_the_absolute_sdf_path(tmp_path, monkeypatch, capsys):
    _fake_repo(tmp_path, monkeypatch)
    _stage(tmp_path)
    _write(tmp_path / "route.sdf", MINIMAL_SDF)
    _stub(monkeypatch, FakeCocotbRunner(RESULTS_XML))
    outdir = tmp_path / "text_out"

    exit_code = main(
        [
            "functional-verification",
            _request(tmp_path, sdf={"file": "route.sdf"}),
            "--outdir",
            str(outdir),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"sdf: {tmp_path / 'route.sdf'}" in out
    assert "scope" not in out

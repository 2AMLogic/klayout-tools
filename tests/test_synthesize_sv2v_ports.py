"""Exercise the real sv2v CLI on an unpacked-array port and a separate header."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools.cli import main
from test_synthesize import _REAL_SKY130_VARIANT
from test_synthesize_arithmetic import _synth_script_path, requires_engine


@requires_engine
@pytest.mark.skipif(_REAL_SKY130_VARIANT is None, reason="needs sky130 liberty")
@pytest.mark.skipif(shutil.which("sv2v") is None, reason="needs sv2v")
def test_unpacked_array_port_with_header(tmp_path, monkeypatch, capsys):
    root, variant = _REAL_SKY130_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)
    monkeypatch.chdir(tmp_path)
    headers = tmp_path / "includes"
    headers.mkdir()
    (headers / "word.svh").write_text("`define WORD_BITS 32\n")
    (tmp_path / "top.sv").write_text(
        '`include "word.svh"\n'
        "module top(input logic [`WORD_BITS-1:0] a_i[2], "
        "output logic [`WORD_BITS-1:0] y);\n"
        "  assign y = a_i[0] ^ a_i[1];\nendmodule\n"
    )
    (tmp_path / "request.json").write_text(
        json.dumps(
            {
                "sources": ["top.sv", "includes/word.svh"],
                "hdl_toplevel": "top",
                "pdk": {"cell_library": "sky130_fd_sc_hd"},
                "sv_frontend": "sv2v",
            }
        )
    )

    args = ["synthesize", "request.json", "--format", "json"]
    exit_code = main(args)
    output = capsys.readouterr()
    assert exit_code == 0, output.err
    report = json.loads(output.out)
    assert report["status"] == "ok"
    version = subprocess.check_output(["sv2v", "--version"], text=True).splitlines()[0]
    frontend = report["provenance"]["sv_frontend"]
    assert frontend == {"route": "sv2v", "sv2v_version": version}

    run_dir = Path(_synth_script_path(tmp_path, report, "top")).parent
    assert "[63:0] a_i" in (run_dir / "top_sv2v.v").read_text()
    assert "sky130_fd_sc_hd__" in (run_dir / "top_synth.v").read_text()
    assert report["instance_count"] > 0
    print(
        f"klt {' '.join(args)} exited {exit_code}, status={report['status']}, "
        f"provenance.sv_frontend={json.dumps(frontend)}, "
        f"mapped instances={report['instance_count']}"
    )

"""Native DEF reader diagnostics must leave the CLI JSON stream parseable."""

import io
import json
import os
from contextlib import redirect_stdout

import pytest

from klayout_tools._c_stdout import native_stdout_to_stderr
from klayout_tools.cli import main
from test_place_and_route import (
    _setup_success_env,
    _stub_openroad_success,
    _write_matching_cell_gds,
    _write_tiny_cell_lef,
    _write_tiny_def,
    _write_tiny_tech_lef,
)


@pytest.mark.usefixtures("real_build_identity_git")
def test_json_cli_forwards_real_partial_map_warnings(tmp_path, monkeypatch, capfd):
    request = _setup_success_env(tmp_path, monkeypatch)
    variant = tmp_path / "install" / "sky130A"
    library = variant / "libs.ref" / "sky130_fd_sc_hd"
    _write_tiny_tech_lef(next((library / "techlef").glob("*.tlef")))
    _write_tiny_cell_lef(next((library / "lef").glob("*.lef")), "testcell")
    _write_matching_cell_gds(next((library / "gds").glob("*.gds")), "testcell")
    tech = variant / "libs.tech" / "klayout" / "tech"
    tech.mkdir(parents=True)
    (tech / "sky130A.map").write_text("met1 NET,SPNET,VIA 68 20\n")
    routed = tmp_path / "routed.def"
    _write_tiny_def(routed, design_name="gcd", cell_name="testcell")
    _stub_openroad_success(monkeypatch, def_text=routed.read_text())

    code = main(["place-and-route", request, "--format", "json"])
    captured = capfd.readouterr()
    assert code == 0, captured.err
    assert json.loads(captured.out)["status"] == "ok"
    assert "Warning: No mapping for purpose 'OUTLINE'" in captured.err
    assert "LEFPIN" in captured.err


def test_error_restores_stdout_even_with_a_python_only_stream(capfd):
    # The wheel writes fd 1 even if Python's stdout is a StringIO.
    with redirect_stdout(io.StringIO()), pytest.raises(RuntimeError, match="bad DEF"):
        with native_stdout_to_stderr():
            os.write(1, b"reader warning\n")
            raise RuntimeError("bad DEF")
    os.write(1, b"after read\n")
    out, err = capfd.readouterr()
    assert out == "after read\n"
    assert err == "reader warning\n"


def test_closed_python_stdout_forwards_native_warnings(tmp_path, capfd):
    caller_stdout = (tmp_path / "caller.log").open("w")
    caller_stdout.close()
    with redirect_stdout(caller_stdout), native_stdout_to_stderr():
        os.write(1, b"reader warning\n")
    os.write(1, b"after read\n")
    out, err = capfd.readouterr()
    assert out == "after read\n"
    assert err == "reader warning\n"


def test_missing_native_stdout_allows_reader_to_run(monkeypatch):
    import errno
    import sys

    def fail_dup(fd):
        raise OSError(errno.EBADF, "fd is closed")

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", None)
        patch.setattr(os, "dup", fail_dup)
        with native_stdout_to_stderr():
            assert sys.stdout is None


def test_other_descriptor_errors_propagate(monkeypatch):
    import errno

    def fail_dup(fd):
        raise OSError(errno.EMFILE, "too many open files")

    with monkeypatch.context() as patch:
        patch.setattr(os, "dup", fail_dup)
        with pytest.raises(OSError) as exc, native_stdout_to_stderr():
            pytest.fail("reader opened after the duplicate failed")
    assert exc.value.errno == errno.EMFILE

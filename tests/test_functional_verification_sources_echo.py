"""``environment.sources``: the report names what it simulated (issue #5771).

The helper and the cocotb path are simulator-free (stubbed runner); the
Verilog-top path needs only ``iverilog``/``vvp``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from klayout_tools import functional_verification as fv
from klayout_tools.functional_verification import run_functional_verification
from klayout_tools.functional_verification_run_inputs import sources_echo
from test_functional_verification import (
    _RESULTS_XML_WITH_SKIP,
    _base_request,
    _FakeRunner,
    _setup_inputs,
    _stub_runner,
    _write,
    _write_request,
)
from test_functional_verification_verilog_top import _stage, needs_icarus

_HEX64 = set("0123456789abcdef")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_envelope(root: Path):
    return lambda p: fv.repo_relative_path(p, repo_root=root)


def _assert_no_absolute(echo) -> None:
    for entry in echo:
        assert entry["path"] is None or not os.path.isabs(entry["path"])
        assert len(entry["sha256"]) == 64 and set(entry["sha256"]) <= _HEX64


# -- the helper --------------------------------------------------------------


def test_echo_is_ordered_repo_relative_and_hashed(tmp_path):
    (tmp_path / "rtl").mkdir()
    a = tmp_path / "rtl" / "a.v"
    b = tmp_path / "b.v"
    a.write_text("module a; endmodule\n")
    b.write_text("module b; endmodule\n")

    echo = sources_echo([str(b), str(a)], _repo_envelope(tmp_path))

    assert echo == [
        {"path": "b.v", "scope": "repo", "sha256": _sha(b)},
        {"path": "rtl/a.v", "scope": "repo", "sha256": _sha(a)},
    ]
    _assert_no_absolute(echo)


def test_echo_external_source_has_null_path_but_a_digest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "pdk_cells.v"
    outside.write_text("module cell; endmodule\n")

    (entry,) = sources_echo([str(outside)], _repo_envelope(repo))

    assert entry == {"path": None, "scope": "external", "sha256": _sha(outside)}


def test_echo_duplicate_basenames_are_told_apart(tmp_path):
    for sub, body in (
        ("rtl", "module t; endmodule\n"),
        ("gl", "module t; endmodule // gl\n"),
    ):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "top.v").write_text(body)

    rtl, gl = sources_echo(
        [str(tmp_path / "rtl" / "top.v"), str(tmp_path / "gl" / "top.v")],
        _repo_envelope(tmp_path),
    )

    assert (rtl["path"], gl["path"]) == ("rtl/top.v", "gl/top.v")
    assert rtl["sha256"] != gl["sha256"]


# -- cocotb path (stubbed runner) --------------------------------------------


def test_cocotb_report_echoes_resolved_sources(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    monkeypatch.setattr(fv, "find_repo_root", lambda *a, **k: str(tmp_path))
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))
    request = _write_request(tmp_path / "request.json", _base_request())

    report = run_functional_verification(request)

    assert report["environment"]["sources"] == [
        {"path": "gcd.v", "scope": "repo", "sha256": _sha(tmp_path / "gcd.v")}
    ]
    assert report["schema_version"] == fv.SCHEMA_VERSION


def test_cocotb_absolute_source_in_request_is_not_leaked(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _setup_inputs(repo)
    outside = Path(_write(tmp_path / "ext.v", "module ext; endmodule\n"))
    monkeypatch.setattr(fv, "find_repo_root", lambda *a, **k: str(repo))
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))
    request = _write_request(
        repo / "request.json", _base_request(sources=["gcd.v", str(outside)])
    )

    report = run_functional_verification(request)

    echo = report["environment"]["sources"]
    assert [e["scope"] for e in echo] == ["repo", "external"]
    assert echo[1] == {"path": None, "scope": "external", "sha256": _sha(outside)}
    assert str(tmp_path) not in repr(echo)


def test_cocotb_reports_differ_when_only_sources_differ(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    _write(
        tmp_path / "gcd_gl.v", "module gcd(input wire clk); /* netlist */ endmodule\n"
    )
    monkeypatch.setattr(fv, "find_repo_root", lambda *a, **k: str(tmp_path))
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))
    rtl = run_functional_verification(
        _write_request(tmp_path / "rtl.json", _base_request())
    )
    gl = run_functional_verification(
        _write_request(tmp_path / "gl.json", _base_request(sources=["gcd_gl.v"]))
    )

    assert rtl["environment"]["sources"] != gl["environment"]["sources"]


# -- Verilog-top path (iverilog) ---------------------------------------------


@needs_icarus
def test_verilog_top_report_carries_the_same_field(tmp_path):
    request = _stage(tmp_path)
    names = json.loads(request.read_text())["sources"]

    report = run_functional_verification(str(request))

    echo = report["environment"]["sources"]
    assert len(echo) == len(names) >= 1
    for entry, name in zip(echo, names, strict=True):
        assert entry["sha256"] == _sha(tmp_path / name)
    _assert_no_absolute(echo)


@needs_icarus
def test_verilog_top_reports_differ_when_only_sources_differ(tmp_path):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = run_functional_verification(str(_stage(first_dir)))
    request = _stage(second_dir)
    # Same bench; one DUT source gains a harmless comment.
    dut = second_dir / "and_cell.v"
    dut.write_text(dut.read_text() + "// edited\n")

    second = run_functional_verification(str(request))

    assert first["environment"]["sources"] != second["environment"]["sources"]

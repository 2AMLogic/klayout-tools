"""``klt netlist --relative-paths`` (issue #5046): checkout-independent
``** sch_path:`` / ``** sym_path:`` comments in the written netlist."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from klayout_tools import netlist_batch as nb
from klayout_tools import netlist_paths
from klayout_tools.cli import main
from klayout_tools.netlist import run_netlist
from test_netlist import (  # noqa: F401
    _NETLIST_BODY,
    _STUB_NONZERO_EXIT,
    _write_schematic,
    _write_stub,
)

RUNNER_TREE = "/tmp/tmp.Runner01"


@pytest.fixture
def argv_log(tmp_path, monkeypatch) -> Path:
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    return log


def _paths_body(checkout: Path) -> str:
    return (
        f"** sch_path: {checkout}/sub/block.sch\n"
        f"** sym_path: {checkout}/sub/block.sym\n"
        "** sym_path: /opt/pdk/sky130A/nfet.sym\n"
        ".subckt block a b\n.ends\n.end"
    )


def _generate_from(tmp_path: Path, name: str, monkeypatch, **kw) -> tuple[str, dict]:
    checkout = tmp_path / name
    (checkout / "sub").mkdir(parents=True, exist_ok=True)
    schematic = _write_schematic(checkout / "sub")
    monkeypatch.setenv("NETLIST_BODY", _paths_body(checkout))
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT, name=f"xschem-{name}")
    output = tmp_path / f"{name}.spice"
    report = run_netlist(schematic, str(output), xschem_binary=binary, **kw)
    return output.read_text(encoding="utf-8"), report


def test_relative_paths_make_two_checkouts_byte_identical(
    tmp_path, argv_log, monkeypatch
):
    a, report_a = _generate_from(tmp_path, "a", monkeypatch, relative_paths=True)
    b, report_b = _generate_from(tmp_path, "b", monkeypatch, relative_paths=True)
    assert a == b
    assert report_a["netlist"]["content_hash"] == report_b["netlist"]["content_hash"]
    # No git repo under tmp_path: root is the schematic's parent directory.
    assert "** sch_path: block.sch\n" in a
    assert "** sym_path: block.sym\n" in a
    # Outside the root: basename only.
    assert "** sym_path: nfet.sym\n" in a
    assert str(tmp_path) not in a


def test_default_output_keeps_absolute_paths(tmp_path, argv_log, monkeypatch):
    text, _ = _generate_from(tmp_path, "a", monkeypatch)
    assert f"** sch_path: {tmp_path}/a/sub/block.sch\n" in text


def test_relative_paths_use_git_toplevel(tmp_path, argv_log, monkeypatch):
    checkout = tmp_path / "a"
    (checkout / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    text, _ = _generate_from(tmp_path, "a", monkeypatch, relative_paths=True)
    assert "** sch_path: sub/block.sch\n" in text
    assert "** sym_path: sub/block.sym\n" in text


def test_relative_paths_check_matches_either_way(tmp_path, argv_log, monkeypatch):
    for relative in (True, False):
        text, _ = _generate_from(tmp_path, "a", monkeypatch, relative_paths=relative)
        out = tmp_path / "a.spice"
        for check_relative in (True, False):
            binary = str(tmp_path / "xschem-a")
            report = run_netlist(
                str(tmp_path / "a" / "sub" / "block.sch"),
                str(out),
                check=True,
                xschem_binary=binary,
                relative_paths=check_relative,
            )
            assert report["status"] == "match"
        assert out.read_text(encoding="utf-8") == text


def test_relativize_handles_crlf_symlink_and_trailing_space(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    data = (
        f"** sch_path: {link}/x.sch  \r\n** sym_path: {real}/y.sym\r\n* keep\n"
    ).encode()
    out = netlist_paths.relativize_path_comments(data, str(link))
    assert out == b"** sch_path: x.sch\r\n** sym_path: y.sym\r\n* keep\n"


def test_cli_relative_paths_flag(tmp_path, argv_log, monkeypatch, capsys):
    checkout = tmp_path / "a"
    (checkout / "sub").mkdir(parents=True)
    schematic = _write_schematic(checkout / "sub")
    monkeypatch.setenv("NETLIST_BODY", _paths_body(checkout))
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    out = tmp_path / "o.spice"
    argv = [
        "netlist", schematic, "-o", str(out), "--xschem-binary", binary,
        "--relative-paths", "--format", "json",
    ]  # fmt: skip
    assert main(argv) == 0
    capsys.readouterr()
    assert "** sch_path: block.sch\n" in out.read_text(encoding="utf-8")


def test_runner_rewrite_then_relativize_is_checkout_independent():
    data = (
        f"** sch_path: {RUNNER_TREE}/design/top.sch\n"
        "** sym_path: /opt/pdk/sky130A/libs.tech/xschem/nfet.sym\n"
    ).encode()
    outs = []
    for root in ("/work/one", "/other/two"):
        rewritten = nb._rewrite_runner_paths(data, [RUNNER_TREE], root)
        outs.append(netlist_paths.relativize_path_comments(rewritten, root))
    assert outs[0] == outs[1]
    assert outs[0].decode().splitlines() == [
        "** sch_path: design/top.sch",
        "** sym_path: nfet.sym",
    ]

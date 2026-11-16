"""``klt netlist --symbol-path`` (issue #4952) on the batch backend."""

# ruff: noqa: F811  (fixtures imported from the sibling test module)

from __future__ import annotations

import pytest

from klayout_tools import netlist as netlist_module
from klayout_tools import netlist_batch as nb
from test_netlist_batch import (  # noqa: F401  (repo, fake are fixtures)
    _BODY,
    RUNNER_TREE,
    _git,
    _package,
    _runner_outputs,
    _status,
    fake,
    repo,
)

# --------------------------------------------------------------------------- #
# --symbol-path (issue #4952)
# --------------------------------------------------------------------------- #


def test_symbol_path_is_forwarded_root_relative_and_quoted(repo, fake, tmp_path):
    sym_dir = repo / "my syms"
    sym_dir.mkdir()
    (sym_dir / "cell.sym").write_text("v {}\n", encoding="utf-8")
    _git(repo, "add", "my syms/cell.sym")
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    report = nb.run_netlist_batch(
        str(repo / "design" / "block.sch"),
        str(tmp_path / "o.spice"),
        timeout_s=120.0,
        pdk="sky130A",
        symbol_path=[str(sym_dir), str(repo / "design")],
    )
    cmd = fake.job_json["cmd"]
    assert "--symbol-path 'my syms' --symbol-path design " in cmd
    assert report["xschem"]["symbol_path"] == ["my syms", "design"]


def test_absent_symbol_path_leaves_cmd_unchanged(repo, fake, tmp_path):
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    report = nb.run_netlist_batch(
        str(repo / "design" / "block.sch"),
        str(tmp_path / "o.spice"),
        timeout_s=120.0,
        pdk="sky130A",
    )
    assert "--symbol-path" not in fake.job_json["cmd"]
    assert report["xschem"]["symbol_path"] == []


def test_symbol_path_outside_root_or_untracked_is_refused(repo, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(netlist_module.NetlistError, match="outside the batch root"):
        _package(repo, symbol_path=[str(outside)])
    empty = repo / "emptydir"
    empty.mkdir()
    with pytest.raises(netlist_module.NetlistError, match="no git-tracked files"):
        _package(repo, symbol_path=[str(empty)])
    with pytest.raises(netlist_module.NetlistError, match="not a directory"):
        _package(repo, symbol_path=[str(repo / "missing")])

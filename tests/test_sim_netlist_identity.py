"""Regression tests for netlist identity's shared inline-comment rule (#5438)."""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import _stub_subprocess_run
from klayout_tools import sim

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def _identity_for(tmp_path, monkeypatch, top_text):
    (tmp_path / "top.spice").write_text(top_text)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "top.spice",
                "analysis": {"kind": "tran", "args": "1n 1u"},
            }
        )
    )
    _stub_subprocess_run(monkeypatch)
    return sim.run_sim(str(request))["environment"]


def test_netlist_identity_distinguishes_anonymous_net_and_cell_edits(
    tmp_path, monkeypatch
):
    # The old normaliser cut each line at the first whitespace-preceded ``$``,
    # so any card using KLayout's anonymous-net names collapsed to its first
    # token and distinct circuits shared one identity.
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    base = _identity_for(tmp_path / "a", monkeypatch, "X$1 $3 $4 inv\n")
    net = _identity_for(tmp_path / "b", monkeypatch, "X$1 $7 $4 inv\n")
    cell = _identity_for(tmp_path / "c", monkeypatch, "X$1 $3 $4 nand\n")
    assert (
        len(
            {
                base["netlist_identity"],
                net["netlist_identity"],
                cell["netlist_identity"],
            }
        )
        == 3
    )


def test_netlist_identity_strips_attached_semicolon_and_trailing_dollar_comments(
    tmp_path, monkeypatch
):
    # Shared rule: a comment starts at any ``;`` (attached to the previous
    # token or not) and at a whitespace-delimited ``$`` token.
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    plain = _identity_for(tmp_path / "a", monkeypatch, "R1 a b 1k\n")
    semi = _identity_for(tmp_path / "b", monkeypatch, "R1 a b 1k; load\n")
    dollar = _identity_for(tmp_path / "c", monkeypatch, "R1 a b 1k $ note\n")
    assert plain["netlist_identity"] == semi["netlist_identity"]
    assert plain["netlist_identity"] == dollar["netlist_identity"]


def test_netlist_identity_folds_continuation_carrying_anonymous_nets(
    tmp_path, monkeypatch
):
    # A ``+`` continuation's ``$N`` nets survive the fold onto their card.
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    folded = _identity_for(tmp_path / "a", monkeypatch, "X$1 $3 inv\n+ $4 $5\n")
    one_line = _identity_for(tmp_path / "b", monkeypatch, "X$1 $3 inv $4 $5\n")
    edited = _identity_for(tmp_path / "c", monkeypatch, "X$1 $3 inv\n+ $9 $5\n")
    assert folded["netlist_identity"] == one_line["netlist_identity"]
    assert folded["netlist_identity"] != edited["netlist_identity"]

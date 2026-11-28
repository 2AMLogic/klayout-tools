"""CLI and full flag-parity tests for multi-top ``klt pex --dry-run`` (#5511).

Split out of ``test_pex_multi_top_runs.py`` for the 500-line file-size
ratchet: exercises the full ``_MULTI_TOP_REFUSED_FLAGS`` table against
``run_pex`` message parity and the CLI ``-o`` / ``--outdir`` regression
controls from issue #5511.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.pex import PexError, run_pex
from klayout_tools.pex_dry_run import run_pex_dry_run
from klayout_tools.pex_multi_top_flags import MULTI_TOP_UNSUPPORTED_CODE
from test_pex import _write_gds, _write_request
from test_pex_dry_run import _tree
from test_pex_multi_top import (
    _make_two_top_resistor_layout,
    _two_top_request,
    _write_two_top_testbench,
)
from test_pex_multi_top_runs import _MULTI_TOP_REFUSED_FLAGS


@pytest.fixture
def two_top_layout(tmp_path):
    return _write_gds(_make_two_top_resistor_layout(), tmp_path / "two_tops.gds")


@pytest.mark.parametrize(
    ("kwargs", "flag", "takes_testbenches"),
    [
        item
        for item in _MULTI_TOP_REFUSED_FLAGS
        if item[1] not in {"--engine klayout-runset", "--submit-only", "--attach"}
    ],
    ids=[
        item[1].lstrip("-")
        for item in _MULTI_TOP_REFUSED_FLAGS
        if item[1] not in {"--engine klayout-runset", "--submit-only", "--attach"}
    ],
)
def test_dry_run_multi_top_refuses_every_unsupported_flag_with_same_message(
    tmp_path, two_top_layout, kwargs, flag, takes_testbenches
):
    request = _two_top_request(tmp_path)
    if "reference_netlist" in kwargs:
        kwargs = {**kwargs, "reference_netlist": str(tmp_path / "schematic_dut.spice")}
    testbenches = [str(request)] if takes_testbenches else []
    before = _tree(tmp_path)

    with pytest.raises(PexError) as dry_exc:
        run_pex_dry_run(
            str(two_top_layout),
            testbenches,
            "sky130",
            top=["RESA", "RESB"],
            **kwargs,
        )
    assert flag in str(dry_exc.value)
    assert dry_exc.value.code == MULTI_TOP_UNSUPPORTED_CODE
    assert _tree(tmp_path) == before

    with pytest.raises(PexError) as real_exc:
        run_pex(
            str(two_top_layout),
            testbenches,
            "sky130",
            top=["RESA", "RESB"],
            **kwargs,
        )
    assert str(dry_exc.value) == str(real_exc.value)


def test_cli_dry_run_multi_top_valid_and_regression_controls_write_nothing(
    tmp_path, two_top_layout, capsys
):
    """All three regression controls from issue #5511 via the real CLI, plus
    the valid two-top invocation in both `--top` orders: neither `-o` nor
    `--outdir` is created on any of them."""
    request = _two_top_request(tmp_path)
    netlist = tmp_path / "combined.spice"
    outdir = tmp_path / "art"

    def _run(*extra: str) -> int:
        return main(
            [
                "pex",
                str(two_top_layout),
                str(request),
                "--deck",
                "sky130",
                "--dry-run",
                "-o",
                str(netlist),
                "--outdir",
                str(outdir),
                "--format",
                "json",
                *extra,
            ]
        )

    assert _run("--top", "RESA", "--top", "RESB") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "valid"
    assert report["top"] == "RESA"
    assert report["tops"] == [{"top": "RESA"}, {"top": "RESB"}]

    assert _run("--top", "RESB", "--top", "RESA") == 0
    flipped = json.loads(capsys.readouterr().out)
    assert flipped["top"] == "RESB"
    assert flipped["tops"] == [{"top": "RESB"}, {"top": "RESA"}]

    # 1. Missing stream cell in 2nd position.
    assert _run("--top", "RESA", "--top", "NOPE") == 1
    assert "NOPE" in json.loads(capsys.readouterr().err)["error"]["message"]

    # 2. Two tops with --device-only-leg.
    assert _run("--top", "RESA", "--top", "RESB", "--device-only-leg") == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["code"] == MULTI_TOP_UNSUPPORTED_CODE
    assert "--device-only-leg" in err["message"]

    # 3. Schematic declares only the first of two requested cells.
    one_subckt = tmp_path / "one_dut.spice"
    one_subckt.write_text(".SUBCKT RESA RA RB\nR1 RA RB 289.2\n.ENDS RESA\n")
    tb = _write_two_top_testbench(tmp_path / "one_tb.spice", one_subckt)
    bad_req = _write_request(tmp_path / "one_req.json", tb, node="RD")
    assert (
        main(
            [
                "pex",
                str(two_top_layout),
                str(bad_req),
                "--deck",
                "sky130",
                "--dry-run",
                "--top",
                "RESA",
                "--top",
                "RESB",
                "-o",
                str(netlist),
                "--outdir",
                str(outdir),
                "--format",
                "json",
            ]
        )
        == 1
    )
    subckt_err = json.loads(capsys.readouterr().err)["error"]
    assert subckt_err["code"] == MULTI_TOP_UNSUPPORTED_CODE
    assert "RESB" in subckt_err["message"]

    assert not netlist.exists()
    assert not outdir.exists()

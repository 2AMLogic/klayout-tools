"""Tests for ``klt lvs --deck`` (issue #5733): the layout.deck convenience
spelling.

Split out of ``test_lvs.py`` (whose per-file size ratchet ceiling this
keeps, issue #2766); the local fixtures mirror that module's helpers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from klayout_tools.cli.lvs_cmd import run as lvs_run
from klayout_tools.cli.parser import create_parser
from klayout_tools.lvs import LvsError, run_lvs

_INVERTER_SPICE = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.65U L=0.15U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request))
    return str(path)


# --------------------------------------------------------------------------- #
# --deck: the layout.deck convenience spelling (issue #5733)
# --------------------------------------------------------------------------- #


def test_deck_flag_fills_layout_deck(tmp_path):
    """``--deck`` (issue #5733) fills the request's ``layout.deck`` and is
    visible exactly where the request field would be: the compare runs with
    that extraction deck, pinned in ``provenance.deck``."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    report = run_lvs(path, deck="sky130")

    assert report["status"] == "match"
    assert report["provenance"]["deck"]["name"] == "sky130"


def test_deck_flag_does_not_touch_reference_deck(tmp_path):
    """``reference.deck`` is a different knob (the reference-netlist
    rewriting deck) and ``--deck`` must never fill it: a request that sets
    only ``reference.deck`` takes ``--deck`` without a conflict, and the
    layout-side fill lands in ``layout.deck`` alone."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {
                "netlist": reference_path,
                "top": "inv",
                "deck": "sg13g2",
            },
        },
    )
    report = run_lvs(path, deck="sky130")

    assert report["status"] == "match"
    assert report["provenance"]["deck"]["name"] == "sky130"


def test_deck_flag_conflicts_with_request_layout_deck(tmp_path):
    """Setting both ``--deck`` and the request's own ``layout.deck`` is the
    same clean flag-vs-request error ``--deck-file``/``--deck-var`` raise --
    a flag never silently overrides the request document."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv", "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    with pytest.raises(LvsError, match="--deck conflicts with request.layout.deck"):
        run_lvs(path, deck="gf180mcu")


def test_deck_flag_unknown_deck_name_is_clean_error(tmp_path):
    """An unknown ``--deck`` name surfaces from the existing unknown-deck
    path once it reaches ``layout.deck`` -- a clean ``LvsError``, not an
    argparse error and not a silent no-op."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    with pytest.raises(LvsError, match="unknown deck 'nosuchdeck'"):
        run_lvs(path, deck="nosuchdeck")


def test_cli_deck_flag_parser_and_check_guard(capsys):
    """The ``lvs`` parser carries ``--deck`` (resolving the former
    ``ambiguous option: --deck`` prefix clash with ``--deck-file``/
    ``--deck-var``), and ``run()`` refuses ``--deck`` with ``--check`` the
    same way it refuses ``--deck-file``/``--deck-var``."""
    parser = create_parser()
    args = parser.parse_args(["lvs", "req.json", "--deck", "sky130"])
    assert args.deck == "sky130"
    # Prefix spellings still reach their own flags, unchanged.
    args = parser.parse_args(["lvs", "req.json", "--deck-f", "pdk.lvs"])
    assert args.deck_file == "pdk.lvs"
    args = parser.parse_args(["lvs", "req.json", "--deck-v", "feol=true"])
    assert args.deck_var == ["feol=true"]
    # An exact ``--deck`` never reports as ambiguous (argparse raises
    # SystemExit(2) for a genuinely unknown option instead).
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["lvs", "req.json", "--deckx"])
    assert excinfo.value.code == 2
    capsys.readouterr()  # drop the usage text the probe wrote to stderr

    check_args = argparse.Namespace(
        request=None,
        format="json",
        check="report.json",
        rerun=False,
        deck="sky130",
        deck_file=None,
        deck_var=None,
        layout=None,
        progress=None,
        output=None,
    )
    assert lvs_run(check_args) == 1
    payload = json.loads(capsys.readouterr().err)
    assert "--deck" in payload["error"]["message"]

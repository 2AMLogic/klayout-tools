"""Unknown-request-key rejection and SPEF name-mismatch hint for ``klt sta``
(issue #5065). Split from ``test_post_route_sta.py`` (file-size ratchet)."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from klayout_tools._sta_request_diag import (
    KNOWN_REQUEST_FIELDS,
    SPEF_CONNECTIONS_FLAG,
    SPEF_EXTRACT_RECIPE,
    SPEF_NAME_MISMATCH_HINT,
    SPEF_NO_PIN_ATTACH_HINT,
)
from klayout_tools.cli import main
from klayout_tools.cli.parser import create_parser
from klayout_tools.post_route_sta import PostRouteStaError, run_sta
from test_asap7_finfet import _draw_device
from test_asap7_finfet import _write as _write_finfet_gds
from test_extract import _make_inverter_layout, _write_gds

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _request(tmp_path: Path, **overrides) -> str:
    (tmp_path / "top.def").write_text("# def\n", encoding="utf-8")
    request = {
        "def": "top.def",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
        "constraints": {"clock_port": "clk", "clock_period_ns": 1.1},
    }
    request.update(overrides)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def test_run_rejects_unknown_top_level_key(tmp_path):
    request_path = _request(tmp_path, netlist="top.v")
    with pytest.raises(PostRouteStaError, match="unknown field.*netlist.*known fields"):
        run_sta(request_path)


def test_run_accepts_schema_key(tmp_path):
    request_path = _request(
        tmp_path, schema="klt.sta.request/1", pdk={"corner": "tt_025C_1v80"}
    )
    with pytest.raises(PostRouteStaError, match="pdk.cell_library is required"):
        run_sta(request_path)


def test_known_fields_cover_documented_keys():
    assert {"def", "verilog", "spef", "sdf", "macros"} <= set(KNOWN_REQUEST_FIELDS)


def test_spef_name_mismatch_hint_names_extract_flags():
    """Issue #7808: the recipe names the pin-attaching flag and both
    declared-pin spellings, each with where it applies."""
    for flag in ("--def-net-names", "--def-net-connections", "--def-pins", "--pins"):
        assert flag in SPEF_NAME_MISMATCH_HINT
    assert SPEF_EXTRACT_RECIPE in SPEF_NAME_MISMATCH_HINT
    assert "planar" in SPEF_NAME_MISMATCH_HINT
    assert "FinFET" in SPEF_NAME_MISMATCH_HINT
    assert "`--def-pins` is refused" in SPEF_NAME_MISMATCH_HINT
    assert "`PINS` section" in SPEF_NAME_MISMATCH_HINT


def test_spef_no_pin_attach_hint_names_connections_flag():
    """Issue #7808: the `delay_changed is False` reason points at
    `--def-net-connections`, sharing the constant the mismatch hint uses."""
    assert SPEF_CONNECTIONS_FLAG in SPEF_NO_PIN_ATTACH_HINT
    assert SPEF_CONNECTIONS_FLAG in SPEF_NAME_MISMATCH_HINT
    assert "*CONN" in SPEF_NO_PIN_ATTACH_HINT


_HINTS = {
    "name-mismatch": SPEF_NAME_MISMATCH_HINT,
    "no-pin-attach": SPEF_NO_PIN_ATTACH_HINT,
}


def _hint_flags(*hints: str) -> set[str]:
    return {flag for hint in hints for flag in re.findall(r"--[a-z][a-z-]*", hint)}


def _extract_option_actions() -> dict[str, argparse.Action]:
    parser = create_parser()
    subparsers = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return subparsers.choices["extract"]._option_string_actions


@pytest.mark.parametrize("hint", _HINTS.values(), ids=_HINTS.keys())
def test_spef_hint_flags_are_klt_extract_options(hint):
    """Issue #7808: every `--flag` a hint names is a real `klt extract`
    option, so a hint can never name a flag the parser has dropped."""
    flags = _hint_flags(hint)
    assert flags, "a hint must name at least one flag"
    assert flags <= set(_extract_option_actions())


# Each deck family takes one declared-pin spelling; every other flag the hints
# name must run on that deck. A FinFET deck refuses `--def-pins`; the planar
# recipe uses `--def-pins` in place of `--pins`.
_FINFET_REFUSES = "--def-pins"
_PLANAR_OMITS = "--pins"
_DEF = (
    "PINS 2 ;\n"
    " - {a} + NET {a} + DIRECTION INPUT ;\n"
    " - {b} + NET {b} + DIRECTION OUTPUT ;\n"
    "END PINS\n"
    "NETS 2 ;\n"
    " - {a} ( u1 A ) ;\n"
    " - {b} ( u1 Y ) ;\n"
    "END NETS\n"
)


def _run_hinted_extract(tmp_path, capsys, gds, deck, skip, nets):
    """Run `klt extract` with every flag the SPEF hints name except ``skip``
    (the declared-pin spelling for the other deck family); return
    ``(exit code, parsed stdout)``."""
    def_path = tmp_path / "routed.def"
    def_path.write_text(_DEF.format(a=nets[0], b=nets[1]), encoding="utf-8")
    values = {
        "--def-net-connections": str(def_path),
        "--def-pins": str(def_path),
        "--pins": ",".join(nets),
    }
    actions = _extract_option_actions()
    argv = [
        "extract",
        gds,
        "--deck",
        deck,
        "--parasitics",
        "--spef",
        str(tmp_path / "o.spef"),
        "-o",
        str(tmp_path / "o.spice"),
        "--format",
        "json",
    ]
    for flag in sorted(_hint_flags(SPEF_NAME_MISMATCH_HINT, SPEF_NO_PIN_ATTACH_HINT)):
        if flag == skip:
            continue
        argv.append(flag)
        if actions[flag].nargs != 0:
            argv.append(values[flag])
    rc = main(argv)
    return rc, json.loads(capsys.readouterr().out)


def test_spef_hint_flags_run_on_a_finfet_deck(tmp_path, capsys):
    """Issue #7808: the FinFET variant of the recipe (`--pins`, not
    `--def-pins`) is accepted by `klt extract --deck asap7` and writes a SPEF
    whose nets carry instance `*CONN`."""
    gds = _write_finfet_gds(_draw_device(fins=2), tmp_path)
    rc, report = _run_hinted_extract(
        tmp_path, capsys, gds, "asap7", _FINFET_REFUSES, ("G", "S")
    )
    assert rc == 0, report
    assert "*CONN" in (tmp_path / "o.spef").read_text()


def test_spef_hint_flags_run_on_a_planar_deck(tmp_path, capsys):
    """Issue #7808: the planar variant (`--def-pins`) is accepted by `klt
    extract --deck sky130`."""
    gds = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    rc, report = _run_hinted_extract(
        tmp_path, capsys, gds, "sky130", _PLANAR_OMITS, ("A", "Y")
    )
    assert rc == 0, report
    assert "*CONN" in (tmp_path / "o.spef").read_text()


def test_def_pins_is_still_refused_on_a_finfet_deck(tmp_path, capsys):
    """The hint's "`--def-pins` is refused" qualifier is a real contract
    (docs/cli/extract.md, "Not supported (refused by name)"): if `--def-pins`
    ever becomes valid on a FinFET deck, the hint should be simplified."""
    gds = _write_finfet_gds(_draw_device(fins=2), tmp_path)
    def_path = tmp_path / "routed.def"
    def_path.write_text(_DEF.format(a="G", b="S"), encoding="utf-8")
    rc = main(
        [
            "extract",
            gds,
            "--deck",
            "asap7",
            "--def-pins",
            str(def_path),
            "-o",
            str(tmp_path / "o.spice"),
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    err = json.loads(captured.err or captured.out)
    assert rc == 1
    assert "--def-pins" in err["error"]["message"]
    assert "FinFET extraction deck" in err["error"]["message"]

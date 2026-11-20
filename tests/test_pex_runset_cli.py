"""``klt pex --engine klayout-runset`` on the command line (issue #6559):
flag spelling, usage errors (exit 2), refusals by name, the text output and
what a ``--submit-only`` manifest records.

Split out of ``tests/test_pex_runset.py`` (file-size ratchet); same
faked-subprocess layer as ``tests/test_extract_runset.py``.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main, pex_cmd
from klayout_tools.cli.parser import create_parser
from klayout_tools.pex_runset import runset_from_args
from test_extract_runset import _inputs
from test_extract_runset import fake_klayout as fake_klayout


def _cli(tmp_path, *argv, capsys):
    gds, deck = _inputs(tmp_path)
    code = main(
        [
            "pex",
            gds,
            *[a.replace("DECKFILE", deck) for a in argv],
            "--output",
            str(tmp_path / "out.spice"),
            "--outdir",
            str(tmp_path / "art"),
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out, json.loads(captured.err or "null")


def test_cli_runset_route_needs_no_deck_and_emits_the_disclosure(
    tmp_path, fake_klayout, capsys
):
    code, out, _err = _cli(
        tmp_path,
        "--engine",
        "klayout-runset",
        "--deck-file",
        "DECKFILE",
        "--extract-only",
        capsys=capsys,
    )

    assert code == 4  # extract-only: ran, graded nothing (`not_checked`)
    extraction = json.loads(out)["extraction"]
    assert extraction["engine"] == "klayout-runset"
    assert extraction["parasitics_carried"] is False
    assert extraction["device_counts"] == {"NMOS": 1}


def test_cli_text_output_names_the_engine(tmp_path, fake_klayout, capsys):
    gds, deck = _inputs(tmp_path)
    code = main(
        [
            "pex",
            gds,
            "--engine",
            "klayout-runset",
            "--deck-file",
            deck,
            "--extract-only",
            "--output",
            str(tmp_path / "out.spice"),
            "--outdir",
            str(tmp_path / "art"),
        ]
    )

    assert code == 4
    assert "engine: klayout-runset  parasitics: none" in capsys.readouterr().out


def test_cli_device_param_reaches_the_cards_and_the_disclosure(
    tmp_path, fake_klayout, capsys
):
    code, out, _err = _cli(
        tmp_path,
        "--engine",
        "klayout-runset",
        "--deck-file",
        "DECKFILE",
        "--device-param",
        "NMOS:W=w:u",
        "--device-param",
        "NMOS:L=l:u",
        "--extract-only",
        capsys=capsys,
    )

    assert code == 4
    assert json.loads(out)["extraction"]["params_not_carried"] == {
        "NMOS": ["AD", "AS", "PD", "PS"]
    }
    assert "l=0.2u w=0.4u" in (tmp_path / "out.spice").read_text()


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (["--engine", "klayout-runset", "--extract-only"], "requires --deck-file"),
        (
            ["--deck", "sky130", "--deck-file", "DECKFILE", "--extract-only"],
            "--deck-file is only used",
        ),
        (
            ["--deck", "sky130", "--deck-var", "a=b", "--extract-only"],
            "--deck-var is only used",
        ),
        (
            ["--deck", "sky130", "--device-param", "NMOS:W=w", "--extract-only"],
            "only used by engine",
        ),
        (
            ["--deck", "sky130", "--runset-timeout", "5", "--extract-only"],
            "only used by engine",
        ),
        (
            [
                "--engine",
                "klayout-runset",
                "--deck-file",
                "DECKFILE",
                "--deck-var",
                "input=x",
                "--extract-only",
            ],
            "may not set 'input'",
        ),
        (
            [
                "--engine",
                "klayout-runset",
                "--deck-file",
                "DECKFILE",
                "--deck-var",
                "novalue",
                "--extract-only",
            ],
            "not KEY=VALUE",
        ),
        (
            [
                "--engine",
                "klayout-runset",
                "--deck-file",
                "DECKFILE",
                "--device-param",
                "bogus",
                "--extract-only",
            ],
            "not CLASS:PARAM=NAME",
        ),
    ],
)
def test_cli_runset_flag_usage_errors_exit_2(
    tmp_path, fake_klayout, capsys, argv, needle
):
    code, out, err = _cli(tmp_path, *argv, capsys=capsys)

    assert code == 2
    assert out == ""
    assert needle in err["error"]["message"]
    assert err["error"]["command"] == "pex"
    assert "cmd" not in fake_klayout


def test_cli_default_engine_still_requires_a_deck(tmp_path, fake_klayout, capsys):
    code, _out, err = _cli(tmp_path, "--extract-only", capsys=capsys)

    assert code == 2
    assert "--deck" in err["error"]["message"]


def test_cli_curated_deck_under_the_runset_engine_runs_the_merged_route(
    tmp_path, fake_klayout, capsys
):
    """Issue #6606: `--deck` under the runset engine selects the merged
    route instead of exiting 1 -- the CLI envelope carries the
    correspondence even when (as on this fixture) nothing matches."""
    code, out, _err = _cli(
        tmp_path,
        "--engine",
        "klayout-runset",
        "--deck-file",
        "DECKFILE",
        "--deck",
        "sky130",
        "--extract-only",
        capsys=capsys,
    )

    assert code == 4  # extract-only's own `not_checked`, not a refusal
    extraction = json.loads(out)["extraction"]
    assert extraction["deck"] == "sky130"
    assert extraction["net_correspondence"]["deck"] == "sky130"


def test_cli_parasitic_only_flag_is_refused_by_name_under_the_runset_engine(
    tmp_path, fake_klayout, capsys
):
    code, _out, err = _cli(
        tmp_path,
        "--engine",
        "klayout-runset",
        "--deck-file",
        "DECKFILE",
        "--critical-net",
        "n1",
        "--distributed-rc",
        "--extract-only",
        capsys=capsys,
    )

    assert code == 1
    message = err["error"]["message"]
    assert "--critical-net" in message
    assert "--distributed-rc" in message


def test_cli_dry_run_is_refused_under_the_runset_engine(tmp_path, fake_klayout, capsys):
    """`--dry-run`'s preflight models the curated deck and parasitics; it does
    not pretend to model an engine it does not."""
    code, _out, err = _cli(
        tmp_path,
        "--engine",
        "klayout-runset",
        "--deck-file",
        "DECKFILE",
        "--dry-run",
        "--extract-only",
        capsys=capsys,
    )

    assert code == 1
    assert "--dry-run does not model --engine klayout-runset" in err["error"]["message"]
    assert "cmd" not in fake_klayout


def test_runset_from_args_is_none_for_the_default_engine():
    assert runset_from_args("klayout", None, None, None, None) is None


def test_submit_manifest_records_the_runset_arguments_with_an_absolute_deck_file(
    tmp_path, monkeypatch
):
    """`--attach` rebuilds the request from the manifest, so the engine and the
    runset path (made absolute) have to be recorded -- a bare `--attach` from
    another working directory must resolve the runset identically."""
    monkeypatch.chdir(tmp_path)
    args = create_parser().parse_args(
        [
            "pex",
            "l.gds",
            "tb.json",
            "--engine",
            "klayout-runset",
            "--deck-file",
            "pdk.lvs",
            "--deck-var",
            "run_mode=flat",
            "--device-param",
            "NMOS:W=w:u",
            "--runset-timeout",
            "30",
            "--submit-only",
            "--backend",
            "batch",
        ]
    )

    request = pex_cmd._submit_request(args)

    assert request["engine"] == "klayout-runset"
    assert request["deck_file"] == str(tmp_path / "pdk.lvs")
    assert request["deck_var"] == ["run_mode=flat"]
    assert request["device_params"] == ["NMOS:W=w:u"]
    assert request["runset_timeout"] == 30.0

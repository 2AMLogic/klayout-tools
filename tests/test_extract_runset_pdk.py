"""Real-PDK and CLI tests for ``klt extract --engine klayout-runset``
(issue #5481).

Split out of ``tests/test_extract_runset.py`` (whose faked-subprocess layer
needs no PDK) so neither file carries file-size debt. Everything here is
skipped when the standalone ``klayout`` binary, the ``ihp-sg13cmos5l``
install, or ``ngspice`` is absent -- the same skip shape
``tests/test_lvs_runset_pnpmpa_cmos5l.py`` uses.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract


def _make_gds(path: str) -> None:
    """A device-free stream, enough for the CLI-level flag tests below."""
    ly = kdb.Layout()
    ly.dbu = 0.001
    ly.create_cell("TOP")
    ly.write(path)


def _inputs(tmp_path):
    gds = tmp_path / "l.gds"
    _make_gds(str(gds))
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    return str(gds), str(deck)


def _pnpmpa_inputs():
    from klayout_tools import pdk

    try:
        root = pdk.find_pdk(variant="ihp-sg13cmos5l")["assets"]["klayout"]
    except Exception:  # noqa: BLE001 - any resolution failure means "absent"
        pytest.skip("no ihp-sg13cmos5l PDK install")
    lvs_dir = os.path.join(root, "tech", "lvs")
    case = os.path.join(lvs_dir, "testing", "testcases", "unit", "bjt_devices")
    paths = (
        os.path.join(lvs_dir, "sg13cmos5l.lvs"),
        os.path.join(case, "layout", "pnpMPA.gds"),
    )
    if not all(os.path.isfile(p) for p in paths):
        pytest.skip("ihp-sg13cmos5l install lacks the pnpMPA LVS testcase")
    return paths


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_real_runset_extracts_pnpmpa(tmp_path):
    """The gap this engine closes: the curated deck sees nothing here."""
    deck, gds = _pnpmpa_inputs()
    report = run_extract(
        gds,
        None,
        engine="klayout-runset",
        deck_file=deck,
        top="pnpMPA_lvs",
        output=str(tmp_path / "pnpMPA.spice"),
    )
    assert report["engine"] == "klayout-runset"
    assert report["status"] == "extracted"
    assert report["device_count"] == 2
    # The bipolar only the runset recognises, plus the testcase's own tap.
    assert report["device_counts"] == {"pnpMPA": 1, "ptap1": 1}
    assert report["provenance"]["runset"]["klayout_application_version"]

    text = open(report["netlist_path"]).read()
    # Both classes are subcircuits in the PDK's ngspice library, so both must
    # be X calls -- KLayout's default `Q`/`R` cards do not bind (issue #5481).
    assert "pnpMPA" in text and "ptap1" in text
    for line in text.splitlines():
        if line and not line.startswith(("*", ".")):
            assert line.startswith("X"), line


def test_curated_deck_still_extracts_no_pnpmpa(tmp_path):
    """The contrast the engine exists for, re-measured here rather than
    assumed: `--deck sg13cmos5l` reports zero devices on the same layout."""
    _, gds = _pnpmpa_inputs()
    report = run_extract(
        gds, "sg13cmos5l", top="pnpMPA_lvs", output=str(tmp_path / "curated.spice")
    )
    assert report["device_count"] == 0
    # A curated-deck report is unchanged by this engine's existence.
    assert "engine" not in report
    assert report["deck"] == "sg13cmos5l"


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice absent")
def test_written_netlist_runs_an_op_against_the_pdk_models(tmp_path):
    """The acceptance criterion this engine exists for: the written netlist
    plus the PDK's own model library completes a `.op` with no unbound-model
    or unknown-subcircuit error."""
    from klayout_tools import pdk

    deck, gds = _pnpmpa_inputs()
    models = os.path.join(pdk.find_pdk(variant="ihp-sg13cmos5l")["assets"]["ngspice"])
    models_dir = os.path.join(models, "models")
    for lib in ("cornerPNP.lib", "cornerRES.lib"):
        if not os.path.isfile(os.path.join(models_dir, lib)):
            pytest.skip(f"ihp-sg13cmos5l install lacks {lib}")

    netlist = tmp_path / "pnpMPA.spice"
    run_extract(
        gds,
        None,
        engine="klayout-runset",
        deck_file=deck,
        top="pnpMPA_lvs",
        output=str(netlist),
    )
    deck_text = f"""* klt extract --engine klayout-runset op testbench (issue #5481)
.lib {os.path.join(models_dir, "cornerPNP.lib")} typ
.lib {os.path.join(models_dir, "cornerRES.lib")} res_typ
.include {netlist}
Xdut pad vdd vss pnpMPA_lvs
Vvdd vdd 0 1.5
Vpad pad 0 0.7
Vvss vss 0 0
.op
.end
"""
    tb = tmp_path / "tb.cir"
    tb.write_text(deck_text)
    completed = subprocess.run(
        ["ngspice", "-b", str(tb)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(tmp_path),
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    for bad in ("unknown subcircuit", "could not find a valid modelname", "fatal"):
        assert bad not in output.lower(), output


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_cli_json_report(tmp_path, capsys):
    from klayout_tools.cli import main

    deck, gds = _pnpmpa_inputs()
    rc = main(
        [
            "extract",
            gds,
            "--engine",
            "klayout-runset",
            "--deck-file",
            deck,
            "--top",
            "pnpMPA_lvs",
            "-o",
            str(tmp_path / "cli.spice"),
            "--format",
            "json",
        ]
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["engine"] == "klayout-runset"
    assert report["device_count"] == 2
    assert report["deck_file"] == deck


def test_cli_refuses_batch_backend(tmp_path, capsys):
    from klayout_tools.cli import main

    gds, deck = _inputs(tmp_path)
    rc = main(
        [
            "extract",
            gds,
            "--engine",
            "klayout-runset",
            "--deck-file",
            deck,
            "--backend",
            "batch",
            "--format",
            "json",
        ]
    )
    assert rc == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "--backend batch cannot run engine 'klayout-runset'" in message


def test_cli_deck_is_not_required_under_the_runset_engine(tmp_path, capsys):
    """`--deck` is required for the curated engine only; omitting it under
    `--engine klayout-runset` must not be the "argument --deck is required"
    error."""
    from klayout_tools.cli import main

    gds, _ = _inputs(tmp_path)
    rc = main(["extract", gds, "--engine", "klayout-runset", "--format", "json"])
    assert rc == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "requires --deck-file" in message
    assert "--deck is required" not in message


def test_cli_deck_still_required_for_the_curated_engine(tmp_path, capsys):
    from klayout_tools.cli import main

    gds, _ = _inputs(tmp_path)
    rc = main(["extract", gds, "--format", "json"])
    assert rc == 1
    assert (
        "--deck is required" in json.loads(capsys.readouterr().err)["error"]["message"]
    )

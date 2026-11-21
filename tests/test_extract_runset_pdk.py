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
    except Exception:  # any resolution failure means "absent"
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
# --device-param (issue #5513)
# --------------------------------------------------------------------------- #

#: The recipe docs/cli/extract.md gives for a MOS class: geometry under its own
#: names, and the junction parameters cross-declared because the call's S/D
#: order is the extractor's (S G D B) while the PDK subcircuit is `d g s b`.
_HV_NMOS_PARAMS = {
    "sg13_hv_nmos": {
        "W": ("w", "u"),
        "L": ("l", "u"),
        "AS": ("ad", "p"),
        "AD": ("as", "p"),
        "PS": ("pd", "u"),
        "PD": ("ps", "u"),
    }
}


def _hv_nmos_inputs(tmp_path):
    """``(runset, gds)`` for the PDK's own hv NMOS testcase. The cell is
    renamed in a scratch copy: its only cell is called ``sg13_hv_nmos``, the
    PDK subcircuit the extracted ``X`` calls instantiate, so an extracted
    ``.SUBCKT sg13_hv_nmos`` would shadow the very model it calls."""
    from klayout_tools import pdk

    try:
        root = pdk.find_pdk(variant="ihp-sg13cmos5l")["assets"]["klayout"]
    except Exception:  # any resolution failure means "absent"
        pytest.skip("no ihp-sg13cmos5l PDK install")
    lvs_dir = os.path.join(root, "tech", "lvs")
    case = os.path.join(lvs_dir, "testing", "testcases", "unit", "mos_devices")
    deck = os.path.join(lvs_dir, "sg13cmos5l.lvs")
    source = os.path.join(case, "layout", "sg13_hv_nmos.gds")
    if not all(os.path.isfile(p) for p in (deck, source)):
        pytest.skip("ihp-sg13cmos5l install lacks the hv NMOS LVS testcase")
    layout = kdb.Layout()
    layout.read(source)
    layout.top_cell().name = "hv_nmos_tb_top"
    gds = tmp_path / "hv_nmos.gds"
    layout.write(str(gds))
    return deck, str(gds)


@pytest.fixture(scope="module")
def hv_nmos_report(tmp_path_factory):
    """One real runset run, shared: ``(report, netlist path)``."""
    if shutil.which("klayout") is None:
        pytest.skip("klayout binary absent")
    tmp_path = tmp_path_factory.mktemp("hv_nmos")
    deck, gds = _hv_nmos_inputs(tmp_path)
    netlist = tmp_path / "hv_nmos.spice"
    report = run_extract(
        gds,
        None,
        engine="klayout-runset",
        deck_file=deck,
        top="hv_nmos_tb_top",
        output=str(netlist),
        device_params=_HV_NMOS_PARAMS,
    )
    return report, str(netlist)


def _micron(token: str, suffix: str) -> float:
    assert token.endswith(suffix), token
    return float(token.removesuffix(suffix))


def _x_cards(netlist: str) -> list[tuple[tuple[str, ...], dict[str, str]]]:
    """``(nets, {name: token})`` per ``X`` card, ``+`` continuations folded."""
    cards = []
    for line in netlist.replace("\n+", " ").splitlines():
        if line.startswith("X"):
            words = line.split()
            params = dict(w.split("=", 1) for w in words if "=" in w)
            cards.append((tuple(words[1:5]), params))
    return cards


def test_real_runset_carries_the_measured_mos_geometry(hv_nmos_report):
    """The gap this flag closes: before it, every one of these devices was a
    parameter-free call, simulated at the subcircuit's own 0.35u/0.34u."""
    report, netlist = hv_nmos_report
    assert report["device_counts"] == {"sg13_hv_nmos": 4}
    cards = _x_cards(open(netlist).read())
    assert len(cards) == 4
    measured = {
        tuple(d["nets"][t] for t in "sgdb"): d["params"] for d in report["devices"]
    }
    for nets, carried in cards:
        params = measured[nets]
        # The card's numbers are the report's own measured micron values.
        assert _micron(carried["w"], "u") == pytest.approx(params["w_um"])
        assert _micron(carried["l"], "u") == pytest.approx(params["l_um"])
        # Cross-declared: the S-side area rides the call's `d` pin.
        assert _micron(carried["ad"], "p") == pytest.approx(params["as_um2"])
        assert _micron(carried["as"], "p") == pytest.approx(params["ad_um2"])
        assert _micron(carried["pd"], "u") == pytest.approx(params["ps_um"])
        assert _micron(carried["ps"], "u") == pytest.approx(params["pd_um"])
    # Four different drawn sizes, none of them the subcircuit's default.
    sizes = {(carried["w"], carried["l"]) for _, carried in cards}
    assert len(sizes) == 4
    assert ("0.35u", "0.34u") not in sizes
    # Everything the class defines is declared, so nothing is disclosed.
    assert report["warnings"] == []
    assert "not carried" not in open(netlist).read()
    declared = report["device_params"]["sg13_hv_nmos"]
    assert set(declared) == {"W", "L", "AS", "AD", "PS", "PD"}


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice absent")
def test_real_runset_carried_netlist_runs_an_op(hv_nmos_report, tmp_path):
    """The netlist, with its carried ``w=``/``l=``/``as=``..., loads against the
    PDK's own model library and completes a single ``.op``. The PDK's MOS model
    is PSP103, which ngspice can only load as a compiled OSDI binary."""
    from klayout_tools import pdk

    _, netlist = hv_nmos_report
    assets = pdk.find_pdk(variant="ihp-sg13cmos5l")["assets"]["ngspice"]
    corner = os.path.join(assets, "models", "cornerMOShv.lib")
    osdi = [os.path.join(assets, "osdi", f"{m}.osdi") for m in ("psp103", "psp103_nqs")]
    for needed in (corner, *osdi):
        if not os.path.isfile(needed):
            pytest.skip(f"ihp-sg13cmos5l install lacks {os.path.basename(needed)}")

    pre_osdi = "\n".join(f"pre_osdi {path}" for path in osdi)
    tb = tmp_path / "tb.cir"
    tb.write_text(
        f"""* klt extract --engine klayout-runset --device-param op (issue #5513)
.lib {corner} mos_tt
.include {netlist}
Xdut hv_nmos_tb_top
.control
{pre_osdi}
op
.endc
.end
"""
    )
    completed = subprocess.run(
        ["ngspice", "-b", str(tb)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(tmp_path),
    )
    output = (completed.stdout + completed.stderr).lower()
    assert completed.returncode == 0, output
    for bad in (
        "unknown subcircuit",
        "unable to find definition",
        "could not find a valid modelname",
        "error on line",
        "fatal",
    ):
        assert bad not in output, output


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

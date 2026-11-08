"""Extracted-netlist ASAP7 simulation through ``klt sim`` + BSIM-CMG OSDI
(issue #2814).

The unit-level card contract lives in ``test_asap7_finfet.py``; this module
is the integration run. It needs the real toolchain -- the lambdapdk corpus,
a ``bsimcmg.osdi`` built by ``scripts/fetch-asap7-sim-toolchain.sh`` and an
ngspice that actually loads it -- and **skips with the missing capability
named** when any of them is absent. It never reports a pass without a
simulation having run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from klayout_tools import sim
from klayout_tools.extract import run_extract

_ROOT = Path(__file__).resolve().parent.parent
_PDKS = Path(os.environ.get("KLT_PDKS_DIR", _ROOT / "pdks"))
_LAMBDA = _PDKS / "lambdapdk" / "lambdapdk" / "asap7"
_PM = _LAMBDA / "base" / "spice" / "hspice"
_GDS = _LAMBDA / "libs" / "asap7sc7p5t_rvt" / "gds" / "asap7sc7p5t_28_R.gds.gz"
_OSDI = Path(
    os.environ.get("KLT_ASAP7_OSDI", _PDKS / "asap7-sim" / "osdi" / "bsimcmg.osdi")
)


def _skip_reason() -> str | None:
    if not _GDS.is_file() or not (_PM / "7nm_TT_160803.pm").is_file():
        return "missing capability: ASAP7 corpus (run scripts/fetch-pdks.sh)"
    if shutil.which(os.environ.get("KLT_NGSPICE_BINARY", "ngspice")) is None:
        return "missing capability: ngspice on PATH"
    if not _OSDI.is_file():
        return (
            f"missing capability: BSIM-CMG OSDI module {_OSDI} "
            "(run scripts/fetch-asap7-sim-toolchain.sh)"
        )
    binary = os.environ.get("KLT_NGSPICE_BINARY", "ngspice")
    with tempfile.TemporaryDirectory() as tmp:
        deck = Path(tmp) / "probe.cir"
        deck.write_text(
            f"* osdi probe\n.control\npre_osdi {_OSDI}\ndevhelp\nquit\n.endc\n.end\n"
        )
        probe = subprocess.run(
            [binary, "-b", str(deck)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    if "bsimcmg_va" not in probe.stdout + probe.stderr:
        return (
            "missing capability: this ngspice cannot load the BSIM-CMG OSDI "
            f"module {_OSDI} (platform / OSDI ABI mismatch)"
        )
    return None


_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")

_RAMP = "PWL(0 0 100p 0 120p 0.7 300p 0.7 320p 0 500p 0)"


def _request(tmp_path: Path, cell: str, stimulus: str) -> Path:
    spice = tmp_path / f"{cell}.spice"
    run_extract(str(_GDS), "asap7", output=str(spice), top=f"{cell}_ASAP7_75t_R")
    # The raw extracted netlist plus the testbench is one file: `adapt`
    # (issue #3873) rewrites its FinFET cards, so nothing is adapted by hand.
    extracted = "\n".join(
        line for line in spice.read_text().splitlines() if line.strip() != ".end"
    )
    (tmp_path / "body.spice").write_text(
        f"{extracted}\nVdd vdd 0 0.7\nVin a 0 {_RAMP}\n{stimulus}\nCl y 0 1f\n"
    )
    cards = {}
    for c in ("TT", "FF", "SS"):
        card = tmp_path / f"7nm_{c}.pm"
        shutil.copy(_PM / f"7nm_{c}_160803.pm", card)
        cards[c.lower()] = card.name
    request = {
        "netlist": "body.spice",
        "adapt": {"kind": "asap7_bsimcmg", "model_cards": cards},
        "analysis": {"kind": "tran", "args": "1p 500p"},
        "corners": {
            "process": ["tt", "ff", "ss"],
            "supply_v": {"vdd": [0.7]},
            "temperature_c": [25],
        },
        "measurements": [
            {"name": "vout_low", "spice": ".meas tran vout_low FIND v(y) AT=290p"},
            {"name": "vout_high", "spice": ".meas tran vout_high FIND v(y) AT=490p"},
            {
                "name": "tpd_fall",
                "spice": ".meas tran tpd_fall TRIG v(a) VAL=0.35 RISE=1 "
                "TARG v(y) VAL=0.35 FALL=1",
            },
        ],
        "options": {"osdi_preload": [str(_OSDI)]},
    }
    path = tmp_path / "req.json"
    path.write_text(json.dumps(request))
    return path


@pytest.mark.parametrize(
    "cell, stimulus",
    [
        ("INVx1", "Xinv a vdd vdd 0 y 0 INVx1_ASAP7_75t_R"),
        (
            "NAND2xp5",
            "Vb b 0 0.7\nXn a b vdd vdd 0 y 0 NAND2xp5_ASAP7_75t_R",
        ),
    ],
)
def test_extracted_cell_switches_in_every_corner(tmp_path, cell, stimulus):
    report = sim.run_sim(str(_request(tmp_path, cell, stimulus)))
    assert report["status"] == "pass", report["corners"]
    assert [c["process"] for c in report["corners"]] == ["tt", "ff", "ss"]
    delays = {}
    for corner in report["corners"]:
        values = {m["name"]: m["value"] for m in corner["measurements"]}
        assert values["vout_low"] < 0.05
        assert values["vout_high"] > 0.65
        assert 1e-12 < values["tpd_fall"] < 1e-10
        delays[corner["process"]] = values["tpd_fall"]
    # Process ordering: FF faster than TT faster than SS.
    assert delays["ff"] < delays["tt"] < delays["ss"]
    adapt = report["environment"]["adapt"]
    assert adapt["kind"] == "asap7_bsimcmg"
    assert [m["corner"] for m in adapt["model_cards"]] == ["tt", "ff", "ss"]
    assert adapt["netlist_sha256_in"] != adapt["netlist_sha256_out"]
    (preload,) = report["environment"]["osdi_preload"]
    assert preload["name"] == "bsimcmg.osdi" and len(preload["sha256"]) == 64

"""Static guards for the unscored device-level charge-pump PLL (issue #2744).

``benchmarks/design-agent/reference/charge-pump-pll/device-pll/pll_device.spice``
reuses the delivered ``vco_dev`` (``relaxation-vco/device-vco/vco_device.spice``)
and ``cp_dev`` (``charge_pump_device.spice``) blocks as verbatim copies: the
delivered files cannot be ``.include``d whole because each carries its own
standalone testbench. These tests make that copy non-silent -- if either
delivered block (or a parameter it reads) changes, or the copy is edited, the
test fails and the PLL's 18-corner evidence must be re-run.

No simulation is run here (the transistor-level sweep takes tens of minutes
per corner set and stays out of default CI); the request is only checked
structurally.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REF = REPO / "benchmarks" / "design-agent" / "reference"
PLL_DIR = REF / "charge-pump-pll" / "device-pll"
PLL = PLL_DIR / "pll_device.spice"
VCO = REF / "relaxation-vco" / "device-vco" / "vco_device.spice"
PUMP = PLL_DIR / "charge_pump_device.spice"
REQUEST = PLL_DIR / "sim_request.json"


def _region(text: str, first_subckt: str, last_subckt: str) -> str:
    """The source text from the line ``.subckt <first>`` through the line
    ``.ends <last>`` (line-anchored, so prose mentioning them is skipped)."""
    start = re.search(rf"^\.subckt {first_subckt} ", text, re.MULTILINE)
    assert start is not None, first_subckt
    end = re.search(rf"^\.ends {last_subckt}$", text[start.start() :], re.MULTILINE)
    assert end is not None, last_subckt
    return text[start.start() : start.start() + end.end()]


def _params(text: str) -> dict[str, str]:
    return dict(re.findall(r"^\.param\s+(\w+)=(\S+)\s*$", text, flags=re.MULTILINE))


def test_vco_blocks_are_verbatim_copies_of_the_delivered_vco():
    delivered = _region(VCO.read_text(), "vco_cmp_n", "vco_dev")
    assert delivered in PLL.read_text()


def test_pump_block_is_a_verbatim_copy_of_the_delivered_pump():
    delivered = _region(PUMP.read_text(), "cp_dev", "cp_dev")
    assert delivered in PLL.read_text()


def test_parameters_read_by_the_copied_blocks_match_the_delivered_files():
    pll = _params(PLL.read_text())
    vco = _params(VCO.read_text())
    # Every vco_device.spice parameter except its testbench-only control
    # points must be present with the identical value.
    for name, value in vco.items():
        if name.startswith("vctrl_"):
            continue
        assert pll.get(name) == value, name
    # The pump is characterized at icp = 20 uA only.
    assert pll["icp"] == _params(PUMP.read_text())["icp"] == "20u"


def test_external_bias_diodes_follow_the_delivered_contracts():
    lines = PLL.read_text().splitlines()
    expected = [
        # cp_dev: W=8 L=1 nf=1 NMOS/PMOS diodes fed with icp.
        "Icpn vdd cpn DC {icp}",
        "XMcn cpn cpn 0 0 sky130_fd_pr__nfet_01v8 L=1 W=8 nf=1 mult=1",
        "Icpp cpp 0 DC {icp}",
        "XMcp cpp cpp vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=8 nf=1 mult=1",
        # vco_dev: 5 uA NMOS W=10 L=1 / PMOS W=40 nf=2 L=1 diodes.
        "Ibn  vdd nbias DC {ib}",
        "XMbn nbias nbias 0 0 sky130_fd_pr__nfet_01v8 L=1 W=10 nf=1 mult=1",
        "Ibp  pbias 0 DC {ib}",
        "XMbp pbias pbias vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=40 nf=2 mult=1",
    ]
    for line in expected:
        assert line in lines, line


def test_tuning_path_has_no_behavioral_element():
    """The pump output / loop filter / VCO control node is driven only by
    cp_dev and passive R/C elements -- no B/E/G/F/H source or XSPICE
    instance touches it."""
    body = _region(PLL.read_text(), "pll_loop", "pll_loop")
    for line in body.splitlines():
        tokens = line.split()
        if not tokens or tokens[0].startswith(("*", ".")):
            continue
        if "vctrl" in tokens[1:] or "lf" in tokens[1:]:
            assert tokens[0][0].upper() in {"X", "R", "C"}, line


def test_request_is_a_standalone_18_corner_sky130_sweep():
    request = json.loads(REQUEST.read_text())
    assert request["netlist"] == "pll_device.spice"
    assert request["models"]["pdk"] == "sky130A"
    corners = request["corners"]
    assert corners["process"] == ["tt", "ss", "ff"]
    assert corners["supply_v"] == {"vdd": [1.62, 1.98]}
    assert corners["temperature_c"] == [-40, 27, 125]
    names = [m["name"] for m in request["measurements"]]
    assert len(names) == len(set(names))
    limited = {m["name"] for m in request["measurements"] if m.get("limits")}
    for loop in ("lo", "hi"):
        for required in (
            f"period_div_a_{loop}",
            f"period_div_b_{loop}",
            f"phase_err_a_{loop}",
            f"phase_err_b_{loop}",
            f"vctrl_min_{loop}",
            f"vctrl_max_{loop}",
        ):
            assert required in limited, required
    assert "up_acq_lo" in limited and "dn_acq_hi" in limited


def test_new_files_do_not_overwrite_the_pump_characterization():
    assert (PLL_DIR / "README-pump.md").is_file()
    assert (PLL_DIR / "sim_request_pump.json").is_file()
    assert json.loads((PLL_DIR / "sim_request_pump.json").read_text())["netlist"] == (
        "charge_pump_device.spice"
    )

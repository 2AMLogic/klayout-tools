"""Tests for `klayout_tools.netlist_digest` (issue #1130, Phase A of
`docs/design/netlist-driven-layout-spike.md`).

Covers both `form="plain-element"` and `form="subckt-call"` fixtures, mixed
MOS/resistor/capacitor/bipolar device coverage, deterministic ordering, and
the documented edge cases (zero devices of a family, an unresolvable device
name in `form="subckt-call"`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.lvs import LvsError
from klayout_tools.netlist_digest import build_netlist_digest
from klayout_tools.netlist_normalize import NormalizeError

_INVERTER_PLAIN_ELEMENT = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet L=0.15U W=0.65U
M2 Y A VPWR VPWR pfet L=0.15U W=1U
.ends
"""

# Mixed MOS/resistor/capacitor/bipolar in the *simulation* (subckt-call)
# form a real xschem/ngspice flow emits (mirrors `tests/test_lvs.py`'s own
# `_INVERTER_SUBCKT_CALL_SKY130`, extended with the device families #1130
# adds to `netlist_normalize.py`).
_MIXED_DEVICE_SUBCKT_CALL_SKY130 = """
.subckt analog_block A Y GND VPWR VGND
XM1 Y A VGND VGND sky130_fd_pr__nfet_01v8 L=0.15u W=0.65u
XM2 Y A VPWR VPWR sky130_fd_pr__pfet_01v8 L=0.15u W=1.0u
XR1 Y GND sky130_fd_pr__res_generic_po l=48.2u w=1u
XC1 Y GND sky130_fd_pr__cap_mim_m3_1 w=5u l=5u
XQ1 GND A Y sky130_fd_pr__pnp_05v5_W0p68L0p68 mult=1
.ends
"""


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def test_plain_element_digest_shape(tmp_path):
    path = _write(tmp_path / "inv.spice", _INVERTER_PLAIN_ELEMENT)
    digest = build_netlist_digest(path, top="inv")

    assert digest["circuit"] == "INV"
    assert len(digest["devices"]) == 2
    assert {d["device_class"] for d in digest["devices"]} == {"NFET", "PFET"}

    nfet = next(d for d in digest["devices"] if d["device_class"] == "NFET")
    assert nfet["terminals"] == {"S": "VGND", "G": "A", "D": "Y", "B": "VGND"}
    assert nfet["params"]["L"] == pytest.approx(0.15)
    assert nfet["params"]["W"] == pytest.approx(0.65)

    net_names = {n["name"] for n in digest["nets"]}
    assert net_names == {"A", "Y", "VPWR", "VGND"}
    y_net = next(n for n in digest["nets"] if n["name"] == "Y")
    assert y_net["is_pin"] is True
    assert {t["device"] for t in y_net["terminals"]} == {"1", "2"}


def test_digest_is_deterministic_across_runs(tmp_path):
    path = _write(tmp_path / "inv.spice", _INVERTER_PLAIN_ELEMENT)
    first = build_netlist_digest(path, top="inv")
    second = build_netlist_digest(path, top="inv")
    assert first == second
    # Diffable/testable per the acceptance criteria: round-trips through
    # JSON byte-for-byte, and device/net order is stable (sorted), not just
    # value-equal.
    assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)
    assert [d["name"] for d in first["devices"]] == sorted(
        d["name"] for d in first["devices"]
    )
    assert [n["name"] for n in first["nets"]] == sorted(
        n["name"] for n in first["nets"]
    )


def test_subckt_call_mixed_device_family_digest(tmp_path):
    path = _write(tmp_path / "analog.spice", _MIXED_DEVICE_SUBCKT_CALL_SKY130)
    digest = build_netlist_digest(
        path, top="analog_block", form="subckt-call", deck="sky130"
    )

    classes = sorted(d["device_class"] for d in digest["devices"])
    assert classes == [
        "NFET",
        "PFET",
        "PNP",
        "RES_GENERIC_PO",
        "SKY130_FD_PR__MODEL__CAP_MIM",
    ]

    resistor = next(
        d for d in digest["devices"] if d["device_class"] == "RES_GENERIC_PO"
    )
    assert resistor["params"]["L"] == pytest.approx(48.2)
    assert resistor["params"]["W"] == pytest.approx(1.0)

    capacitor = next(
        d
        for d in digest["devices"]
        if d["device_class"] == "SKY130_FD_PR__MODEL__CAP_MIM"
    )
    assert capacitor["params"]["A"] == pytest.approx(25.0)
    assert capacitor["params"]["P"] == pytest.approx(20.0)

    bipolar = next(d for d in digest["devices"] if d["device_class"] == "PNP")
    assert bipolar["params"]["NE"] == pytest.approx(1.0)
    assert bipolar["terminals"] == {"C": "GND", "B": "A", "E": "Y"}


def test_digest_empty_device_family_is_empty_list_not_error(tmp_path):
    # A circuit with zero devices of a given family (here, zero devices at
    # all) digests to an empty list, never an error (issue #1130's edge
    # case).
    path = _write(tmp_path / "wire.spice", ".subckt passthrough A B\n.ends\n")
    digest = build_netlist_digest(path, top="passthrough")
    assert digest["devices"] == []
    # The circuit's own declared pins still surface as nets (with no
    # terminals, since no device is attached to either) -- only the
    # *device* list is empty here, per the acceptance criterion.
    assert [n["name"] for n in digest["nets"]] == ["A", "B"]
    assert all(n["terminals"] == [] and n["is_pin"] for n in digest["nets"])


def test_digest_unresolvable_device_name_raises(tmp_path):
    path = _write(
        tmp_path / "bad.spice",
        ".subckt inv A Y VPWR VGND\n"
        "XM1 Y A VGND VGND not_a_real_device L=0.15u W=0.65u\n"
        ".ends\n",
    )
    with pytest.raises(LvsError):
        build_netlist_digest(path, top="inv", form="subckt-call", deck="sky130")


def test_digest_propagates_normalize_error_cause(tmp_path):
    path = _write(
        tmp_path / "bad.spice",
        ".subckt inv A Y VPWR VGND\n"
        "XM1 Y A VGND VGND not_a_real_device L=0.15u W=0.65u\n"
        ".ends\n",
    )
    with pytest.raises(LvsError) as excinfo:
        build_netlist_digest(path, top="inv", form="subckt-call", deck="sky130")
    assert isinstance(excinfo.value.__cause__, NormalizeError)


def test_digest_top_selects_named_circuit(tmp_path):
    path = _write(
        tmp_path / "multi.spice",
        ".subckt inv A Y VPWR VGND\n"
        "M1 Y A VGND VGND nfet L=0.15U W=0.65U\n"
        ".ends\n"
        ".subckt buf A Y VPWR VGND\n"
        "M1 Y A VGND VGND nfet L=0.3U W=1U\n"
        ".ends\n",
    )
    digest = build_netlist_digest(path, top="buf")
    assert digest["circuit"] == "BUF"
    assert digest["devices"][0]["params"]["L"] == pytest.approx(0.3)


def test_digest_ambiguous_top_raises(tmp_path):
    path = _write(
        tmp_path / "multi.spice",
        ".subckt inv A Y VPWR VGND\n"
        "M1 Y A VGND VGND nfet L=0.15U W=0.65U\n"
        ".ends\n"
        ".subckt buf A Y VPWR VGND\n"
        "M1 Y A VGND VGND nfet L=0.3U W=1U\n"
        ".ends\n",
    )
    with pytest.raises(LvsError, match="top circuits"):
        build_netlist_digest(path)

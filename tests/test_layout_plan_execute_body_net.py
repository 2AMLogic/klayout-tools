"""Tests for the group-level MOS body (``B``) terminal binding (#5546).

A MOS body is not a per-device port -- it is one well/substrate tie shared by
every device a ``device_groups[]`` entry declares -- so
``layout_plan_execute`` binds it group-level, via
``layout_plan_ports.bind_body_nets``, to the group's own ``TAP_PAD<k>`` pad
(#4143) or, for a group that draws its own guard ring, that ring's first
``TAP_<side>`` port.

Split out of `test_layout_plan_execute.py` to keep it under its file-size
ceiling. Like its siblings, PDK resolution runs against a fabricated
open_pdks-layout install under `tmp_path` -- CI never downloads a real PDK.
The extraction round-trip at the bottom needs no install at all: `klt
extract` reads the curated in-tree gf180mcu deck.
"""

from __future__ import annotations

import pytest

from klayout_tools import pdk
from klayout_tools.layout_plan_execute import (
    LayoutPlanExecuteError,
    execute_layout_plan_document,
    exit_code_for,
    partial_success,
)
from klayout_tools.layout_plan_ports import body_tie_port, group_body_nets
from test_layout_plan_execute import _make_install, _write


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def gf180mcu_pdk_root(tmp_path):
    root = tmp_path / "gf180mcu_pdk_install"
    _make_install(root, "gf180mcuD")
    return root


@pytest.fixture()
def sky130_pdk_root(tmp_path):
    root = tmp_path / "sky130_pdk_install"
    _make_install(root, "sky130A")
    return root


# One PMOS and one NMOS, each on its own output net so no route has to cross
# the other block (the `klt gen-compose` behind-the-pin guard, #1895), and
# each with its bulk on the supply its source already sits on -- the ordinary
# shape the plan path could not express before this issue.
_AMP_NETLIST = """
.subckt amp VDD VSS INP INN OUTP OUTN
MP1 OUTP INP VDD VDD pfet_03v3 L=0.28U W=0.42U
MN1 OUTN INN VSS VSS nfet_03v3 L=0.28U W=0.42U
.ends
"""

# The same two devices, but the PMOS group's two devices disagree about their
# body net (VDD vs. VDDA) -- one tie cannot serve both.
_SPLIT_BODY_NETLIST = """
.subckt amp VDD VDDA VSS INP OUTP
MP1 OUTP INP VDD VDD pfet_03v3 L=0.28U W=0.42U
MP2 OUTP INP VDD VDDA pfet_03v3 L=0.28U W=0.42U
.ends
"""

# A 3-terminal MOS: the digest reports `B` as the source net here, so this
# fixture instead drops the bulk by tying it to the source net already bound.
_NO_SEPARATE_BODY_NETLIST = """
.subckt amp VDD VSS INP OUTP
MP1 OUTP INP VDD VDD pfet_03v3 L=0.28U W=0.42U
.ends
"""


def _mos_group(group_id, devices, flavor, **params):
    resolved = {"rows": 1, "cols": 1, "dummy": 0, "flavor": flavor}
    resolved["gate_contact"] = True
    resolved.update(params)
    return {
        "id": group_id,
        "devices": devices,
        "generator": "mos_array",
        "topology": "array",
        "params": resolved,
    }


def _amp_request(tmp_path, pdk_root, variant, netlist_text, groups, routing=None):
    netlist_path = _write(tmp_path, "amp.spice", netlist_text)
    return {
        "schema": "klt.layout_plan.request/1",
        "netlist": {"path": netlist_path, "top": "amp"},
        "pdk": {"variant": variant, "root": str(pdk_root)},
        "device_groups": groups,
        "rows": [
            {
                "order": [group["id"] for group in groups],
                "spacing_um": 4.0,
                "align": "bottom",
            }
        ],
        "routing": routing or {"layer_role": "metal", "width_um": 0.44},
        "options": {
            "cell_name": "amp_0",
            "output": str(tmp_path / "amp_0.gds"),
        },
    }


def _gf180mcu_amp_request(tmp_path, gf180mcu_pdk_root, routing=None, **params):
    groups = [
        _mos_group(
            "mp", [{"name": "P1", "device_class": "PFET_03V3"}], "pfet", **params
        ),
        _mos_group(
            "mn", [{"name": "N1", "device_class": "NFET_03V3"}], "nfet", **params
        ),
    ]
    return _amp_request(
        tmp_path, gf180mcu_pdk_root, "gf180mcuD", _AMP_NETLIST, groups, routing
    )


def _pins_of(response, net_name):
    net = next((n for n in response["nets"] if n["net"] == net_name), None)
    return [] if net is None else [(p["block"], p["port"]) for p in net["pins"]]


# ---------------------------------------------------------------------------
# Binding: tap pads, and a group's own ring.
# ---------------------------------------------------------------------------


def test_tap_pad_is_bound_to_the_declared_bulk_net(tmp_path, gf180mcu_pdk_root):
    """A ring-less gf180mcu `mos_array` whose devices share one bulk net gets
    exactly one of its reported `TAP_PAD<k>` pads as a pin on that net."""
    request = _gf180mcu_amp_request(tmp_path, gf180mcu_pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert ("mp", "TAP_PAD0") in _pins_of(response, "VDD")
    assert ("mn", "TAP_PAD0") in _pins_of(response, "VSS")
    # One tie per group, never a pin per pad.
    for net_name, block in (("VDD", "mp"), ("VSS", "mn")):
        taps = [
            port
            for owner, port in _pins_of(response, net_name)
            if owner == block and port.startswith("TAP_")
        ]
        assert taps == ["TAP_PAD0"]
    assert response["warnings"] == []


def test_tap_pad_binding_routes_and_exits_zero(tmp_path, gf180mcu_pdk_root):
    request = _gf180mcu_amp_request(tmp_path, gf180mcu_pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert response["unrouted_nets"] == []
    assert response["unmapped_netlist_nets"] == []
    assert partial_success(response) is False
    assert exit_code_for(response) == 0


def test_own_guard_ring_is_bound_when_no_tap_pads_are_drawn(
    tmp_path, gf180mcu_pdk_root
):
    """`add_guard_ring` suppresses the tap pads (`gen._optional_tap_pads`), so
    the ring's own first `TAP_<side>` port carries the body net instead.

    The ring really is the devices' body tie, not merely a nearby conductor:
    `_ring_layer_params` gives a `flavor="pfet"` ring a well tie inside the
    same nwell the array draws, and a `flavor="nfet"` ring a p-substrate tap
    -- confirmed end to end by
    `test_own_guard_ring_binding_extracts_bodies_onto_named_nets` below."""
    request = _gf180mcu_amp_request(
        tmp_path,
        gf180mcu_pdk_root,
        routing={
            "layer_role": "metal",
            "width_um": 0.44,
            "cross_block_layer_role": "metal2",
            "cross_block_width_um": 0.44,
        },
        add_guard_ring=True,
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert ("mp", "TAP_N") in _pins_of(response, "VDD")
    assert ("mn", "TAP_N") in _pins_of(response, "VSS")
    assert not any(port.startswith("TAP_PAD") for _, port in _pins_of(response, "VDD"))
    assert response["warnings"] == []


def test_an_unroutable_body_tie_is_reported_not_dropped(tmp_path, gf180mcu_pdk_root):
    """The behaviour change this issue ships: a plan that exited 0 with its
    bodies silently untied can now exit 3, because the tie it gained is a real
    net the router has to reach. It is never dropped silently -- the net is in
    `unrouted_nets[]` with a parsed reason."""
    request = _gf180mcu_amp_request(tmp_path, gf180mcu_pdk_root, add_guard_ring=True)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert "VDD" in response["unrouted_nets"]
    reasons = {entry["net"]: entry["reason"] for entry in response["unrouted_reasons"]}
    assert reasons.get("VDD")
    assert partial_success(response) is True
    assert exit_code_for(response) == 3


# ---------------------------------------------------------------------------
# Conflicts and gaps.
# ---------------------------------------------------------------------------


def test_two_bulk_nets_in_one_group_is_an_application_error(
    tmp_path, gf180mcu_pdk_root
):
    groups = [
        _mos_group(
            "mp",
            [
                {"name": "P1", "device_class": "PFET_03V3"},
                {"name": "P2", "device_class": "PFET_03V3"},
            ],
            "pfet",
            cols=2,
        )
    ]
    request = _amp_request(
        tmp_path, gf180mcu_pdk_root, "gf180mcuD", _SPLIT_BODY_NETLIST, groups
    )
    with pytest.raises(LayoutPlanExecuteError) as excinfo:
        execute_layout_plan_document(request, request_dir=str(tmp_path))

    message = str(excinfo.value)
    assert "'mp'" in message and "'VDD'" in message and "'VDDA'" in message


def test_no_tie_port_warns_and_binds_nothing(tmp_path, sky130_pdk_root):
    """sky130 has no tap-distance rule, so a ring-less array draws no pads and
    reports no tie port. The plan still executes; the body net gains no pin
    and the gap is a same-run `warnings[]` entry rather than a later LVS
    surprise."""
    groups = [
        _mos_group("mp", [{"name": "P1", "device_class": "PFET_03V3"}], "pfet"),
    ]
    request = _amp_request(
        tmp_path, sky130_pdk_root, "sky130A", _NO_SEPARATE_BODY_NETLIST, groups
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert not any(port.startswith("TAP_") for _, port in _pins_of(response, "VDD"))
    body_warnings = [w for w in response["warnings"] if "left untied" in w]
    assert len(body_warnings) == 1
    assert "'mp'" in body_warnings[0] and "'VDD'" in body_warnings[0]


def test_two_bulk_nets_without_a_tie_port_warn_instead_of_erroring(
    tmp_path, sky130_pdk_root
):
    """The conflict is only an error when there is a tie to misuse. With no
    tie port there is nothing to short, so each untied net just gets its own
    `warnings[]` entry and the plan still executes."""
    groups = [
        _mos_group(
            "mp",
            [
                {"name": "P1", "device_class": "PFET_03V3"},
                {"name": "P2", "device_class": "PFET_03V3"},
            ],
            "pfet",
            cols=2,
        )
    ]
    request = _amp_request(
        tmp_path, sky130_pdk_root, "sky130A", _SPLIT_BODY_NETLIST, groups
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    body_warnings = [w for w in response["warnings"] if "left untied" in w]
    assert len(body_warnings) == 2
    assert any("'VDD'" in w for w in body_warnings)
    assert any("'VDDA'" in w for w in body_warnings)


def test_no_tie_port_warning_is_suppressed_by_a_guard_ring_group_net(
    tmp_path, sky130_pdk_root
):
    """A `guard_ring` group declaring `net` (#5095) already ties that net, so
    the untied-body warning would be noise."""
    groups = [
        _mos_group("mp", [{"name": "P1", "device_class": "PFET_03V3"}], "pfet"),
        {"id": "ring", "devices": [], "generator": "guard_ring", "net": "VDD"},
    ]
    request = _amp_request(
        tmp_path, sky130_pdk_root, "sky130A", _NO_SEPARATE_BODY_NETLIST, groups
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert not any("left untied" in w for w in response["warnings"])


# ---------------------------------------------------------------------------
# Helper-level behaviour.
# ---------------------------------------------------------------------------


def test_body_tie_port_prefers_a_tap_pad_over_a_ring_tap():
    ports = [
        {"name": "U0_S"},
        {"name": "TAP_N"},
        {"name": "TAP_PAD0"},
        {"name": "TAP_PAD1"},
    ]
    assert body_tie_port(ports) == "TAP_PAD0"
    assert body_tie_port([{"name": "U0_S"}, {"name": "TAP_W"}]) == "TAP_W"
    assert body_tie_port([{"name": "U0_S"}, {"name": "GAP_N"}]) is None
    assert body_tie_port([]) is None


def test_group_body_nets_skips_null_terminals_and_foreign_generators():
    digest_devices_by_key = {
        ("P1", "PFET_03V3"): {"terminals": {"S": "VDD", "B": "VDD"}},
        ("P2", "PFET_03V3"): {"terminals": {"S": "VDD", "B": None}},
        ("P3", "PFET_03V3"): {"terminals": {"S": "VDD"}},
    }
    devices = [
        {"name": name, "device_class": "PFET_03V3"} for name in ("P1", "P2", "P3")
    ]
    mos = {"id": "mp", "generator": "mos_array", "devices": devices}
    assert group_body_nets(mos, digest_devices_by_key) == ["VDD"]

    ring = {"id": "ring", "generator": "guard_ring", "devices": devices}
    assert group_body_nets(ring, digest_devices_by_key) == []


# ---------------------------------------------------------------------------
# End to end: the LVS symptom the issue reports.
# ---------------------------------------------------------------------------


def _extracted_bodies(gds_path, tmp_path, name):
    from klayout_tools.extract import run_extract

    report = run_extract(gds_path, "gf180mcu", output=str(tmp_path / f"{name}.spice"))
    bodies = {device["class"]: device["nets"]["b"] for device in report["devices"]}
    return bodies, report


def test_tap_pad_binding_extracts_bodies_onto_named_nets(tmp_path, gf180mcu_pdk_root):
    """The regression this issue is really about: before the binding, the PMOS
    body extracted onto an anonymous `$<n>` well net and the NMOS body onto the
    deck-synthesized `vsubs` global, so every MOS came back `unmatched` from
    `klt lvs`. Both now land on the netlist's own named bulk nets."""
    request = _gf180mcu_amp_request(tmp_path, gf180mcu_pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert response["unrouted_nets"] == []

    bodies, report = _extracted_bodies(response["gds_path"], tmp_path, "pads")
    assert bodies["pfet"] == "VDD"
    assert bodies["nfet"] == "VSS"
    assert "$" not in bodies["pfet"] and "$" not in bodies["nfet"]
    assert report["unbiased_pmos_body_nets"] == []


def test_own_guard_ring_binding_extracts_bodies_onto_named_nets(
    tmp_path, gf180mcu_pdk_root
):
    """The evidence behind this issue's conditional ring criterion: a group's
    own ring does contact its devices' body, so binding the ring's `TAP_<side>`
    port is electrically the same tie the pads give a ring-less array."""
    request = _gf180mcu_amp_request(
        tmp_path,
        gf180mcu_pdk_root,
        routing={
            "layer_role": "metal",
            "width_um": 0.44,
            "cross_block_layer_role": "metal2",
            "cross_block_width_um": 0.44,
        },
        add_guard_ring=True,
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert response["unrouted_nets"] == []

    bodies, report = _extracted_bodies(response["gds_path"], tmp_path, "ring")
    assert bodies["pfet"] == "VDD"
    assert bodies["nfet"] == "VSS"
    assert report["unbiased_pmos_body_nets"] == []

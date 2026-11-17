"""diff_pair distinct-legs guard in ``layout-plan execute`` (issue #5765).

A ``diff_pair`` group's two declared devices are its two legs, so they
must be electrically distinct: two digest devices that share their ``G``
net or their ``D`` net are parallel fingers of one schematic instance --
typically a folded ``nf > 1`` MOS, which a ``form: "subckt-call"`` digest
expands into per-finger devices named ``<instance>_F0``, ``<instance>_F1``
... (issue #1487) -- or the same device declared twice, never two legs.
Binding such a pair as legs shorts the pair's gates and/or drains and
leaves the second leg's own nets unmapped, with no ``warnings[]`` entry.

This file pins the application error (``LayoutPlanExecuteError``, exit 1)
for every sharing shape -- the ``["1_F0", "1_F1"]`` repro (shared G *and*
D), shared-D-only, shared-G-only, and the same device declared twice --
plus the declarations that must keep executing: one finger per leg (the
``["1_F0", "2_F0"]`` negative control, across the default/``mirror``/
``splits: 1`` param variants), an unfolded ``["1", "2"]`` pair, a single
declared device, and a shared-gate ``mos_array`` group (a bias rail is
legitimate there -- the guard is scoped to ``diff_pair`` only).
"""

from __future__ import annotations

import pytest

from klayout_tools import pdk
from klayout_tools.layout_plan_execute import (
    LayoutPlanExecuteError,
    execute_layout_plan_document,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


# The #5765 repro: `XM1 ... nf=2` digests (form "subckt-call") as per-finger
# devices `1_F0`/`1_F1` and `2_F0`/`2_F1`, each at the per-finger W=1.0, and
# offers no device named `1` or `2` at all.
_FOLDED_PAIR_NETLIST = """
.subckt dp INP INN OUTP OUTN TAIL VSS
XM1 OUTP INP TAIL VSS sky130_fd_pr__nfet_01v8 L=0.15u W=2u nf=2
XM2 OUTN INN TAIL VSS sky130_fd_pr__nfet_01v8 L=0.15u W=2u nf=2
.ends
"""

_UNFOLDED_PAIR_NETLIST = """
.subckt dp INP INN OUTP OUTN TAIL VSS
XM1 OUTP INP TAIL VSS sky130_fd_pr__nfet_01v8 L=0.15u W=2u
XM2 OUTN INN TAIL VSS sky130_fd_pr__nfet_01v8 L=0.15u W=2u
.ends
"""

_SHARED_DRAIN_NETLIST = """
.subckt dp INP INN OUTP OUTN TAIL VSS
XM1 OUTP INP TAIL VSS sky130_fd_pr__nfet_01v8 L=0.15u W=2u
XM2 OUTP INN TAIL VSS sky130_fd_pr__nfet_01v8 L=0.15u W=2u
.ends
"""

_SHARED_GATE_NETLIST = """
.subckt dp INP INN OUTP OUTN TAIL VSS
XM1 OUTP INP TAIL VSS sky130_fd_pr__nfet_01v8 L=0.15u W=2u
XM2 OUTN INP TAIL VSS sky130_fd_pr__nfet_01v8 L=0.15u W=2u
.ends
"""

# A bias-rail `mos_array` group: both declared devices share their gate
# net -- legitimate for `mos_array`, and exactly the shape the diff_pair
# guard must NOT reject (#5765 scoping; mirrors
# test_layout_plan_gate_contact.py's _GATE_RAIL_NETLIST shape).
_MOS_ARRAY_RAIL_NETLIST = """
.subckt rail VBIAS D1 D2 VSS
XM1 D1 VBIAS VSS VSS sky130_fd_pr__nfet_01v8 L=0.15u W=0.42u
XM2 D2 VBIAS VSS VSS sky130_fd_pr__nfet_01v8 L=0.15u W=0.42u
.ends
"""


def _request(
    tmp_path,
    pdk_root,
    netlist_text: str,
    devices: list[str],
    params: dict | None = None,
    generator: str = "diff_pair",
    top: str = "dp",
) -> dict:
    netlist_path = tmp_path / "plan.spice"
    netlist_path.write_text(netlist_text)
    group: dict = {
        "id": "dp",
        "devices": devices,
        "generator": generator,
    }
    if generator == "diff_pair":
        group["topology"] = "common_centroid"
    else:
        group["topology"] = "array"
        group["params"] = {"rows": 1, "cols": len(devices), "dummy": 0}
    if params:
        group["params"] = {**group.get("params", {}), **params}
    return {
        "netlist": {
            "path": str(netlist_path),
            "top": top,
            "form": "subckt-call",
            "deck": "sky130",
        },
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "device_groups": [group],
        "rows": [{"order": ["dp"], "spacing_um": 0.0}],
        "routing": {"layer_role": "metal", "width_um": 0.3},
        "options": {"output": str(tmp_path / "dp_0.gds")},
    }


def test_fingers_of_one_instance_rejected(tmp_path, pdk_root):
    """The #5765 repro: `["1_F0", "1_F1"]` are both fingers of XM1, so the
    k-th-device binding ties leg 1's and leg 2's gates on INP and their
    drains on OUTP -- a shorted pair, not a differential pair. It must be
    an application error naming both devices, the shared terminals and
    nets, and the remedy, not a silent exit-3 'success'."""
    request = _request(tmp_path, pdk_root, _FOLDED_PAIR_NETLIST, ["1_F0", "1_F1"])
    with pytest.raises(LayoutPlanExecuteError) as excinfo:
        execute_layout_plan_document(request, request_dir=str(tmp_path))
    message = str(excinfo.value)
    assert "1_F0" in message and "1_F1" in message
    assert "terminal G on net 'INP'" in message
    assert "terminal D on net 'OUTP'" in message
    # The remedy: name both working alternatives.
    assert "splits" in message
    assert "gen-compose" in message


def test_shared_drain_only_rejected(tmp_path, pdk_root):
    """Two legs sharing only their drain net are the same defect: parallel
    devices, not a pair. The error names the shared D terminal and net,
    and does not claim the distinct gate is shared."""
    request = _request(tmp_path, pdk_root, _SHARED_DRAIN_NETLIST, ["1", "2"])
    with pytest.raises(LayoutPlanExecuteError) as excinfo:
        execute_layout_plan_document(request, request_dir=str(tmp_path))
    message = str(excinfo.value)
    assert "terminal D on net 'OUTP'" in message
    assert "terminal G" not in message


def test_shared_gate_only_rejected(tmp_path, pdk_root):
    """Two legs sharing only their gate net (one input driving both) is
    the classic mis-declaration. The error names the shared G terminal
    and net, and does not claim the distinct drain is shared."""
    request = _request(tmp_path, pdk_root, _SHARED_GATE_NETLIST, ["1", "2"])
    with pytest.raises(LayoutPlanExecuteError) as excinfo:
        execute_layout_plan_document(request, request_dir=str(tmp_path))
    message = str(excinfo.value)
    assert "terminal G on net 'INP'" in message
    assert "terminal D" not in message


def test_same_device_declared_twice_rejected(tmp_path, pdk_root):
    """Declaring the same digest device as both legs trips the same
    check -- its own G and D nets are trivially shared."""
    request = _request(tmp_path, pdk_root, _FOLDED_PAIR_NETLIST, ["1_F0", "1_F0"])
    with pytest.raises(LayoutPlanExecuteError) as excinfo:
        execute_layout_plan_document(request, request_dir=str(tmp_path))
    message = str(excinfo.value)
    assert "1_F0" in message
    assert "terminal G on net 'INP'" in message
    assert "terminal D on net 'OUTP'" in message


def test_one_finger_per_leg_still_executes(tmp_path, pdk_root):
    """`["1_F0", "2_F0"]` -- one finger per leg -- is a legitimate (if
    #5557-incomplete) declaration and must NOT be rejected by the new
    check."""
    request = _request(tmp_path, pdk_root, _FOLDED_PAIR_NETLIST, ["1_F0", "2_F0"])
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert response["schema_version"] == 1
    assert [d["name"] for d in response["device_groups"][0]["devices"]] == [
        "1_F0",
        "2_F0",
    ]


@pytest.mark.parametrize(
    "params",
    [{}, {"mirror": True}, {"splits": 1}],
    ids=["default_splits", "mirror", "splits_1"],
)
def test_valid_legs_survive_generator_param_variants(tmp_path, pdk_root, params):
    """`mirror` renames the generator's leg prefixes and `splits: 1`
    removes the folding entirely -- neither changes which digest devices
    the plan declares, so a valid two-leg declaration must keep executing
    under every variant (edge cases from the issue's test plan)."""
    request = _request(
        tmp_path, pdk_root, _FOLDED_PAIR_NETLIST, ["1_F0", "2_F0"], params=params
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert response["schema_version"] == 1


def test_unfolded_pair_still_executes(tmp_path, pdk_root):
    """The no-`nf` shape the doc note recommends: both legs are single
    digest devices `1`/`2`, electrically distinct -- must keep executing
    exactly as before this issue."""
    request = _request(tmp_path, pdk_root, _UNFOLDED_PAIR_NETLIST, ["1", "2"])
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert response["schema_version"] == 1
    assert [d["name"] for d in response["device_groups"][0]["devices"]] == ["1", "2"]


def test_single_declared_device_unchanged(tmp_path, pdk_root):
    """A diff_pair group with one declared device has no pair to compare --
    the guard must not start erroring on it (edge case from the issue's
    test plan)."""
    request = _request(tmp_path, pdk_root, _UNFOLDED_PAIR_NETLIST, ["1"])
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert response["schema_version"] == 1


def test_mos_array_shared_gate_group_still_executes(tmp_path, pdk_root):
    """The guard is scoped to `diff_pair`: a matched `mos_array` group
    whose two declared devices share their gate net (a bias rail) is
    legitimate and must keep executing."""
    request = _request(
        tmp_path,
        pdk_root,
        _MOS_ARRAY_RAIL_NETLIST,
        ["1", "2"],
        generator="mos_array",
        top="rail",
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert response["schema_version"] == 1
    assert [d["name"] for d in response["device_groups"][0]["devices"]] == ["1", "2"]

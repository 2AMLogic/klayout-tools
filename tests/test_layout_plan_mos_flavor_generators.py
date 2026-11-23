"""Which generators `klt layout-plan execute` derives a MOS `flavor` for (#6912).

`layout_plan_ports.apply_mos_flavor` reads a MOS group's `flavor` from its
declared `device_class` (PFET -> pfet, NFET -> nfet) and injects it into the
group's `params`. `gen._resolve_params` rejects any param a generator does not
declare, so the derivation may only list generators whose PCell declares
`flavor` -- `esd_device` does not (it is always an NMOS-style device), and
listing it aborted every netlist-backed `esd_device` group with
`unknown params: flavor`, because a bare `devices[]` reference is bound
class-qualified (`NFET`) and so reaches the derivation without anyone
declaring a class by hand.

One case per generator in the derivation's tuple, plus the generator it must
not touch, so a generator can neither be added without declaring `flavor` nor
quietly start receiving one it cannot accept.
"""

from __future__ import annotations

import os

import pytest

from klayout_tools import pdk
from klayout_tools.gen import list_generators
from klayout_tools.layout_plan_execute import (
    execute_layout_plan_document,
    exit_code_for,
)
from klayout_tools.layout_plan_ports import _MOS_FLAVOR_GENERATORS, apply_mos_flavor


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


def _declared_params(generator: str) -> set[str]:
    by_name = {g["name"]: g for g in list_generators()["generators"]}
    return {p["name"] for p in by_name[generator]["params"]}


def _request(tmp_path, pdk_root, generator: str, model: str, params: dict) -> dict:
    """One netlist-backed group, referenced by its bare name -- the digest
    binding returns it class-qualified (`NFET`/`PFET`), exactly as in a real
    plan that never declares a class by hand."""
    netlist = tmp_path / "one_fet.spice"
    netlist.write_text(
        f".subckt one_fet D G VSS\nM1 D G VSS VSS {model} L=0.28U W=1U\n.ends\n"
    )
    return {
        "netlist": {"path": str(netlist), "top": "one_fet"},
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "device_groups": [
            {"id": "g1", "devices": ["1"], "generator": generator, "params": params}
        ],
        "rows": [{"order": ["g1"], "spacing_um": 0.0}],
        "options": {"output": str(tmp_path / f"{generator}_0.gds")},
    }


def _group(generator: str, device_class: str) -> dict:
    return {
        "id": "g1",
        "generator": generator,
        "devices": [{"name": "1", "device_class": device_class}],
    }


@pytest.mark.parametrize("generator", _MOS_FLAVOR_GENERATORS)
def test_every_flavor_derived_generator_declares_a_flavor_param(generator):
    # The invariant the derivation rests on, measured from the generators' own
    # PCell declarations (`klt gen --list`) rather than a hand-kept list.
    assert "flavor" in _declared_params(generator)


def test_esd_device_declares_no_flavor_param_so_none_may_be_derived():
    assert "flavor" not in _declared_params("esd_device")
    assert "esd_device" not in _MOS_FLAVOR_GENERATORS


@pytest.mark.parametrize("device_class", ["NFET", "PFET"])
def test_apply_mos_flavor_leaves_an_esd_device_group_alone(device_class):
    params: dict = {}
    assert apply_mos_flavor(_group("esd_device", device_class), params) == []
    assert params == {}


@pytest.mark.parametrize(
    ("generator", "model", "flavor"),
    [
        ("mos_array", "nfet", "nfet"),
        ("mos_array", "pfet", "pfet"),
        ("diff_pair", "nfet", "nfet"),
        ("diff_pair", "pfet", "pfet"),
    ],
)
def test_execute_derives_flavor_for_each_flavor_declaring_generator(
    tmp_path, pdk_root, generator, model, flavor
):
    request = _request(tmp_path, pdk_root, generator, model, {})
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0
    assert os.path.isfile(response["gds_path"])
    assert response["device_groups"][0]["resolved_params"]["flavor"] == flavor
    assert [w for w in response["warnings"] if "params.flavor" in w] == [
        f"device_groups (id 'g1'): derived params.flavor='{flavor}' "
        "from the declared MOS class"
    ]


def test_execute_runs_a_netlist_backed_esd_device_group(tmp_path, pdk_root):
    # The regression: pre-fix this aborted the whole run with
    # `generator 'esd_device': unknown params: flavor`.
    request = _request(tmp_path, pdk_root, "esd_device", "nfet", {})
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0
    assert os.path.isfile(response["gds_path"])
    assert "flavor" not in response["device_groups"][0]["resolved_params"]
    assert not [w for w in response["warnings"] if "params.flavor" in w]

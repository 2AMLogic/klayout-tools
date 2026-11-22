"""The PDK selected for a local corner supplies the netlist and the child."""

import json
import os
from pathlib import Path

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE
from klayout_tools import sim
from test_sim import (
    _isolate_pdk_search,
    _make_pdk_store,
    _stub_subprocess_run,
    _write_request,
)


def setup_campaign(tmp_path, monkeypatch):
    first = _make_pdk_store(tmp_path, "first", stamp="open_pdks aaa")
    second = _make_pdk_store(tmp_path, "second", stamp="open_pdks bbb")
    for volts, root in enumerate((first, second), 1):
        (root / "sky130A/bias.inc").write_text(f"Vbias out 0 {volts}\n")
    _isolate_pdk_search(monkeypatch, [first, second])
    (tmp_path / "body.spice").write_text(
        '.include "${PDK_ROOT}/sky130A/bias.inc"\nR1 out 0 1k\n'
    )
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 10n"},
        "measurements": [
            {"name": "bias", "spice": ".meas tran bias find v(out) at=5n"}
        ],
        "models": {"pdk": "sky130A"},
    }
    return first, second, request


@pytest.mark.parametrize(
    ("pin", "backend", "threads"),
    [
        ("cli", "local", 2),
        ("request", "local-parallel", 3),
        ("discovered", "local", None),
    ],
)
def test_child_and_include_share_the_resolved_root(
    tmp_path, monkeypatch, pin, backend, threads
):
    first, second, request = setup_campaign(tmp_path, monkeypatch)
    monkeypatch.setenv("KLT_PDK_ENV_CONTROL", "preserved")
    monkeypatch.setenv("OMP_NUM_THREADS", "7")
    if pin != "discovered":
        monkeypatch.setenv("PDK_ROOT", str(first))
        request["models"]["pdk_root"] = str(first if pin == "cli" else second)
    if threads:
        request["options"] = {"threads_per_worker": threads}
    path = _write_request(tmp_path, request)
    before = os.environ.get("PDK_ROOT")
    expected = str(first if pin == "discovered" else second)
    _stub_subprocess_run(monkeypatch, log_text="bias = 1\n")
    stub = sim.subprocess.run
    seen = []

    def capture(cmd, **kwargs):
        if "-o" in cmd:
            env = kwargs.get("env") or os.environ
            body = Path(kwargs["cwd"]) / "netlist.expanded.cir"
            seen.append(
                (
                    env.get("PDK_ROOT"),
                    env.get("KLT_PDK_ENV_CONTROL"),
                    env.get("OMP_NUM_THREADS"),
                    body.read_text() if body.exists() else None,
                )
            )
        return stub(cmd, **kwargs)

    monkeypatch.setattr(sim.subprocess, "run", capture)
    sim.run_sim(
        str(path), backend=backend, pdk_root=str(second) if pin == "cli" else None
    )
    assert len(seen) == 1
    child_root, control, omp, body_text = seen[0]
    assert (child_root, control, omp) == (expected, "preserved", str(threads or 7))
    assert body_text is not None and f"{expected}/sky130A/bias.inc" in body_text
    parent_root = os.environ.get("PDK_ROOT")
    assert parent_root == before


@SKIP_NO_NGSPICE
@pytest.mark.parametrize("ambient", [False, True])
def test_ngspice_measures_the_pinned_store_voltage(tmp_path, monkeypatch, ambient):
    first, second, request = setup_campaign(tmp_path, monkeypatch)
    if ambient:
        monkeypatch.setenv("PDK_ROOT", str(second))
    path = _write_request(tmp_path, request)
    for expected, root in [(1, first), (2, second)]:
        report = sim.run_sim(str(path), backend="local", pdk_root=str(root))
        corner = report["corners"][0]
        assert corner["status"] == "pass", json.dumps(corner["diagnostics"])
        assert corner["measurements"][0]["value"] == expected
    parent_root = os.environ.get("PDK_ROOT")
    assert parent_root == (str(second) if ambient else None)


def test_pinned_root_is_literal_and_longer_variable_names_remain_independent(
    tmp_path, monkeypatch
):
    root = tmp_path / "selected $store"
    monkeypatch.setenv("PDK_ROOT", "ambient")
    monkeypatch.setenv("PDK_ROOT_EXT", "different")
    body = tmp_path / "body.spice"
    body.write_text(
        '.include "$PDK_ROOT/model.inc"\n.include "$PDK_ROOT_EXT/extra.inc"\n'
    )
    out, diagnostics = sim._expand_netlist_includes(str(body), str(tmp_path), str(root))
    assert not diagnostics
    expected = f'.include "{root}/model.inc"\n.include "different/extra.inc"\n'
    assert Path(out).read_text() == expected


def test_unquoted_include_gains_quotes_for_a_pinned_root_with_spaces(tmp_path):
    root = tmp_path / "selected store"
    body = tmp_path / "body.spice"
    body.write_text(".include $PDK_ROOT/model.inc\n")
    out, diagnostics = sim._expand_netlist_includes(str(body), str(tmp_path), str(root))
    assert not diagnostics
    assert Path(out).read_text() == f'.include "{root}/model.inc"\n'


def test_missing_body_is_left_to_the_simulator(tmp_path):
    missing = str(tmp_path / "absent.spice")
    assert sim._expand_netlist_includes(missing, str(tmp_path), "/pdk") == (missing, [])

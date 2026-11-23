"""Relocating a PEX testbench preserves its OSDI model inputs (#7485)."""

import json
from pathlib import Path

import pytest

from klayout_tools.pex import _prepare_dut_swapped_request
from klayout_tools.sim_errors import SimError
from klayout_tools.sim_osdi_pin import resolve_osdi_preload
from klayout_tools.sim_staging import runner_resolved_osdi


def _swap(request_dir, work_dir, entries, side):
    dut = request_dir / "dut.spice"
    dut.write_text(".subckt DUT A B\nR1 A B 1k\n.ends DUT\n")
    testbench = request_dir / "bench.spice"
    testbench.write_text('.include "dut.spice"\nX1 A B DUT\n')
    req = request_dir / "bench.json"
    original = json.dumps(
        {
            "netlist": "bench.spice",
            "analysis": {"kind": "op"},
            "options": {"osdi_preload": entries},
        }
    )
    req.write_text(original)
    path = _prepare_dut_swapped_request(
        testbench_path=str(req),
        dut_netlist_path=str(dut),
        work_dir=str(work_dir),
        label="bench",
        side=side,
    )
    assert req.read_text() == original
    return json.loads(Path(path).read_text())


@pytest.mark.parametrize("side", ["schematic", "extracted", "device-only"])
def test_relocated_request_resolves_original_inputs(tmp_path, side, monkeypatch):
    request_dir = tmp_path / "design"
    request_dir.mkdir()
    work = tmp_path / "elsewhere" / "artifacts"
    work.mkdir(parents=True)
    model = request_dir / "local.osdi"
    model.write_bytes(b"original")
    (work / model.name).write_bytes(b"decoy")
    extra = tmp_path / "extra.osdi"
    extra.touch()
    monkeypatch.setenv("MY_MODEL", "local.osdi")
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "client-pdk"))
    runner_entries = ["$PDK_ROOT/worker.osdi", "${PDK_ROOT}/worker2.osdi"]
    entries = [model.name, *runner_entries, str(extra), "$MY_MODEL"]
    swapped = _swap(request_dir, work, entries, side)
    deferred = runner_resolved_osdi(swapped, str(work))
    assert deferred == {1: "env_var", 2: "env_var"}
    assert swapped["options"]["osdi_preload"][1:3] == runner_entries
    resolved = resolve_osdi_preload(swapped["options"], str(work), frozenset(deferred))
    assert resolved == (str(model), str(extra), str(model))
    assert Path(resolved[0]).read_bytes() == b"original"


def test_a_missing_original_cannot_bind_to_an_artifact(tmp_path):
    work = tmp_path / "artifacts"
    work.mkdir()
    (work / "missing.osdi").touch()
    swapped = _swap(tmp_path, work, ["missing.osdi"], "extracted")
    with pytest.raises(SimError, match="osdi_preload file not found") as error:
        resolve_osdi_preload(swapped["options"], str(work))
    assert str(tmp_path / "missing.osdi") in str(error.value)


@pytest.mark.parametrize("invalid", ["one.osdi", [""], [None]])
def test_invalid_entries_reach_sim_validation(tmp_path, invalid):
    work = tmp_path / "artifacts"
    work.mkdir()
    swapped = _swap(tmp_path, work, invalid, "extracted")
    assert swapped["options"]["osdi_preload"] == invalid
    with pytest.raises(SimError, match="osdi_preload"):
        resolve_osdi_preload(swapped["options"], str(work))

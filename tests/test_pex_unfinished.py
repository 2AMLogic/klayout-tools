"""`klt pex` ``unfinished[]`` (issue #5282): sim legs that lost shards.

Split out of ``test_pex.py`` to keep that file under its size ceiling.
"""

# ruff: noqa: F811  (imported pytest fixture is used as a test argument)

from __future__ import annotations

from klayout_tools.pex import run_pex
from klayout_tools.pex_sim_evidence import unfinished_legs
from test_pex import (
    _measurement,
    _two_corner_schematic_report,
    _write_request,
    _write_schematic_dut,
    _write_testbench,
    resistor_layout,  # noqa: F401  (pytest fixture)
)


def test_run_pex_reports_unfinished_shards_and_keeps_finished_corners(
    tmp_path, resistor_layout, monkeypatch
):
    """Issue #5282: an extracted leg that lost one of its shards keeps the
    delta rows of the corners that finished, and `unfinished[]` names the
    lost corner, the shard and the shard map to `--resume`."""
    import klayout_tools.pex as pex_module

    lost_corner = {
        "corner_id": "ss/1.620V/-40C",
        "status": "error",
        "measurements": [{**_measurement(None), "status": "error"}],
        "diagnostics": [
            {"severity": "error", "code": "lost_shard", "message": "shard lost"}
        ],
    }
    partial = _two_corner_schematic_report()
    partial["corners"] = [
        {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]},
        lost_corner,
    ]
    partial["environment"] = {
        "remote": {
            "unfinished_shards": [{"index": 1, "job_id": "j2", "state": "pending"}],
            "resumable": True,
            "shard_map": "/tmp/art/batch-shards.json",
        }
    }
    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        return _two_corner_schematic_report() if calls["n"] == 1 else partial

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    by_corner = {row["corner_id"]: row for row in report["delta"]}
    assert by_corner["tt/1.800V/27C"]["extracted_value"] == 1.4
    assert by_corner["tt/1.800V/27C"]["status"] != "error"
    assert by_corner["ss/1.620V/-40C"]["status"] == "error"
    (leg,) = report["unfinished"]
    assert leg["side"] == "extracted"
    assert leg["corner_ids"] == ["ss/1.620V/-40C"]
    assert leg["unfinished_shards"][0]["job_id"] == "j2"
    assert leg["resumable"] is True
    assert leg["shard_map"] == "/tmp/art/batch-shards.json"


def test_run_pex_unfinished_is_empty_when_every_shard_finished(
    tmp_path, resistor_layout, monkeypatch
):
    import klayout_tools.pex as pex_module

    monkeypatch.setattr(
        pex_module, "run_sim", lambda *_a, **_k: _two_corner_schematic_report()
    )
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)
    assert run_pex(resistor_layout, [str(request)], "sky130")["unfinished"] == []


def test_unfinished_legs_unit_cases():
    lost = {
        "corners": [
            {
                "corner_id": "c1",
                "diagnostics": [{"code": "lost_shard"}],
            },
            {"corner_id": "c2", "diagnostics": None},
        ],
        "environment": {"resumable": True, "shard_map": "m.json"},
    }
    assert unfinished_legs("tb", None, None) == []
    assert unfinished_legs("tb", {"corners": []}, {"corners": []}) == []
    (leg,) = unfinished_legs("tb", {"corners": []}, lost)
    assert leg == {
        "testbench": "tb",
        "side": "extracted",
        "corner_ids": ["c1"],
        "unfinished_shards": [],
        "resumable": True,
        "shard_map": "m.json",
    }

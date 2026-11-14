"""`klt pex --submit-only/--attach --hosts N` (issue #5292): every leg is
sharded on submit and every leg's shard map is collected on attach."""

# ruff: noqa: F401, F811
from __future__ import annotations

import json

from klayout_tools.pex import run_pex
from test_pex import (
    _async_inputs,
    _fake_async_sim,
    resistor_layout,
)


def test_run_pex_hosts_shards_every_leg_on_submit_and_attach(
    tmp_path,
    resistor_layout,
    monkeypatch,  # noqa: F811
):
    calls: list = []
    _fake_async_sim(monkeypatch, calls, sharded=True)
    request = _async_inputs(tmp_path)
    outdir = tmp_path / "art"
    common = dict(artifacts_dir=str(outdir), backend="batch", hosts=3)

    submitted = run_pex(
        resistor_layout, [request], "sky130", submit_only=True, **common
    )
    assert submitted["status"] == "submitted"
    assert len(calls) == 2
    assert all(call["hosts"] == 3 and call["submit_only"] for call in calls)
    manifest = json.loads((outdir / "pex-submit.json").read_text())
    assert [
        (leg["side"], leg["sharded"], leg["attach"]) for leg in manifest["legs"]
    ] == [
        ("schematic", True, "/shards/schematic/batch-shards.json"),
        ("extracted", True, "/shards/extracted/batch-shards.json"),
    ]

    calls.clear()
    attached = run_pex(
        resistor_layout,
        [request],
        "sky130",
        attach=submitted["manifest_path"],
        **common,
    )
    # One run_sim per leg, each pointed at that leg's own shard map; `hosts`
    # is forwarded on attach too (run_sim ignores it when attaching).
    assert [call["attach_job_id"] for call in calls] == [
        "/shards/schematic/batch-shards.json",
        "/shards/extracted/batch-shards.json",
    ]
    assert all(call["hosts"] == 3 for call in calls)
    assert "delta" in attached

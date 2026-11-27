"""Best-effort per-unit ``peak_rss_bytes`` (issue #4867)."""

from __future__ import annotations

import json
from itertools import count

import pytest

from klayout_tools import sim, sim_peak_rss
from test_sim import _stub_subprocess_run, _write_body, _write_request

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def test_peak_rss_since_reports_only_when_high_water_mark_rose(monkeypatch):
    seq = iter([2048, 2048, 4096])
    monkeypatch.setattr(sim_peak_rss, "children_max_rss_bytes", lambda: next(seq))
    assert sim_peak_rss.peak_rss_since(1024) == 2048  # rose: attributable
    assert sim_peak_rss.peak_rss_since(2048) is None  # equal: not attributable
    assert sim_peak_rss.peak_rss_since(None) is None  # no baseline
    monkeypatch.setattr(sim_peak_rss, "children_max_rss_bytes", lambda: None)
    assert sim_peak_rss.peak_rss_since(0) is None  # resource unavailable


def test_children_max_rss_bytes_is_nonnegative_int_or_none():
    value = sim_peak_rss.children_max_rss_bytes()
    assert value is None or (isinstance(value, int) and value >= 0)


def test_run_watch_field_present_only_when_attributable(monkeypatch):
    seq = iter([1000, 5000, 1000, 1000])
    monkeypatch.setattr(sim_peak_rss, "children_max_rss_bytes", lambda: next(seq))
    rose = sim_peak_rss.RunWatch()
    assert rose.report_field() == {}  # before elapsed(): nothing snapshotted
    assert rose.elapsed() >= 0
    assert rose.report_field() == {"peak_rss_bytes": 5000}
    flat = sim_peak_rss.RunWatch()
    flat.elapsed()
    assert flat.report_field() == {}


def test_unit_report_omits_peak_rss_when_unattributable(tmp_path, monkeypatch):
    _write_body(tmp_path)
    monkeypatch.setattr(sim_peak_rss, "children_max_rss_bytes", lambda: 1000)
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [],
        },
    )
    report = sim.run_sim(str(request))
    assert all("peak_rss_bytes" not in c for c in report["corners"])


def test_unit_report_carries_peak_rss_when_high_water_mark_rose(tmp_path, monkeypatch):
    _write_body(tmp_path)
    # First call: RunWatch baseline; later calls: after the engine ran.
    calls = iter([1000])
    monkeypatch.setattr(
        sim_peak_rss, "children_max_rss_bytes", lambda: next(calls, 9000)
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [],
        },
    )
    report = sim.run_sim(str(request))
    assert report["corners"][0]["peak_rss_bytes"] == 9000


@pytest.mark.parametrize(
    "backend,hosts,worker_cap,temperatures,expect_rss",
    [
        pytest.param("local-parallel", 1, 2, [10, 20], False, id="two-workers"),
        pytest.param("local", 2, 1, [10, 20], False, id="two-local-shards"),
        pytest.param("local-parallel", 1, 1, [10, 20], True, id="capped-worker"),
        pytest.param("local-parallel", 1, 2, [10], True, id="single-corner"),
    ],
)
def test_local_peak_rss_depends_on_possible_overlap(
    tmp_path, monkeypatch, backend, hosts, worker_cap, temperatures, expect_rss
):
    _write_body(tmp_path)
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")
    monkeypatch.setenv("KLT_SIM_MAX_WORKERS", str(worker_cap))
    # Every read raises the shared high-water mark, regardless of thread order.
    monkeypatch.setattr(sim_peak_rss, "children_max_rss_bytes", count(1000).__next__)
    # Keep the completed checkpoint so we can inspect what was persisted.
    monkeypatch.setattr(sim._Checkpoint, "clear", lambda self: None)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {"temperature_c": temperatures},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"max_workers": 2, "resume": True},
        },
    )
    report = sim.run_sim(str(request), backend=backend, hosts=hosts)
    checkpoint = json.loads((tmp_path / ".klt/sim/checkpoint.json").read_text())
    assert len(report["corners"]) == len(temperatures)
    for corner in report["corners"]:
        persisted = checkpoint["completed"][corner["corner_id"]]
        assert corner["status"] == "pass"
        assert persisted["runtime_s"] == corner["runtime_s"] >= 0
        for record in (persisted, corner):
            assert ("peak_rss_bytes" in record) == expect_rss

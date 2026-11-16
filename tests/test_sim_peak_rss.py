"""Best-effort per-unit ``peak_rss_bytes`` (issue #4867)."""

from __future__ import annotations

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

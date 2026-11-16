"""MOS participation in `klt lvs`'s ``device_parameter_coverage`` block
(issue #5653): a MOS class's ``L``/``W`` are compared, and the block now says
so positively, while the ``device.geometry_not_compared`` warning stays
resistor/capacitor-only. Split out of test_lvs.py for the file-size ratchet."""

from __future__ import annotations

from klayout_tools.lvs import run_lvs
from test_lvs import (
    _INVERTER_SPICE,
    _geometry_entries,
    _write,
    _write_request,
)


def _mos_report(tmp_path, options=None):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    payload = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    if options is not None:
        payload["options"] = options
    return run_lvs(_write_request(tmp_path / "request.json", payload))


def test_device_parameter_coverage_reports_mos_without_a_warning(tmp_path):
    """Issue #5653: a MOS class's `L`/`W` *are* compared, and the coverage
    block now says so positively (so `[]` is no longer ambiguous) -- but the
    `device.geometry_not_compared` warning stays resistor/capacitor-only
    (#2461), so no per-run noise is added."""
    report = _mos_report(tmp_path)

    assert report["status"] == "match"
    assert report["error_count"] == 0
    assert _geometry_entries(report) == []
    coverage = report["device_parameter_coverage"]
    assert coverage, "participating MOS classes must be reported"
    for entry in coverage:
        assert entry["device_kind"] == "mos"
        assert entry["compared"] == ["L", "W"]
        assert entry["layout_devices"] >= 1
        assert entry["reference_devices"] >= 1
        assert {item["parameter"] for item in entry["not_compared"]} == {
            "AS",
            "AD",
            "PS",
            "PD",
        }
        assert {item["reason"] for item in entry["not_compared"]} == {"secondary"}
    assert [e["class"] for e in coverage] == sorted(e["class"] for e in coverage)


def test_device_parameter_coverage_mos_compare_parameters_narrowing(tmp_path):
    """`compare_parameters` narrowing a MOS class to `W` moves `L` to
    `reason: "compare_parameters"`, not `"secondary"` (issue #2654)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    baseline = _mos_report(tmp_path / "a")
    class_name = baseline["device_parameter_coverage"][0]["class"]
    report = _mos_report(tmp_path / "b", {"compare_parameters": {class_name: ["W"]}})

    entry = next(
        e for e in report["device_parameter_coverage"] if e["class"] == class_name
    )
    assert entry["compared"] == ["W"]
    assert {"parameter": "L", "reason": "compare_parameters"} in entry["not_compared"]
    assert _geometry_entries(report) == []

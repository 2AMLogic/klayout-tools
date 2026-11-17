"""MOS participation in `klt lvs`'s ``device_parameter_coverage`` block
(issue #5653): a MOS class's ``L``/``W`` are compared, and the block now says
so positively, while the ``device.geometry_not_compared`` warning stays
resistor/capacitor-only. Split out of test_lvs.py for the file-size ratchet."""

from __future__ import annotations

from klayout_tools.lvs import run_lvs
from test_lvs import (
    _INVERTER_SPICE,
    _INVERTER_SUBCKT_CALL_SKY130,
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


_MIXED_MOS_RESISTOR_SPICE = """
.subckt mix A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.65U L=0.15U
R1 A VGND 1000 L=48.2U W=1U
.ends
"""


def test_device_parameter_coverage_agrees_across_reference_forms(tmp_path):
    """Issue #5653 was filed from a `subckt-call` reference, so the curated
    Test Plan requires the MOS coverage entry on that path too -- and it must
    be *identical* to the plain-element path's, proving the two agree.

    The sky130 subckt-call normalisation keeps `L`/`W` and drops the
    parasitic-only parameters (`ad`/`as`/`pd`/`ps`/`nf`), and
    `placeholder_value_classes` records only resistor/capacitor classes, so
    the MOS class is never placeholder-excluded there and the compare really
    does cover its geometry. This is the executed form of that argument."""
    (tmp_path / "plain").mkdir()
    (tmp_path / "subckt").mkdir()
    plain = _mos_report(tmp_path / "plain")

    layout_path = _write(tmp_path / "subckt" / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(
        tmp_path / "subckt" / "ref.spice", _INVERTER_SUBCKT_CALL_SKY130
    )
    subckt = run_lvs(
        _write_request(
            tmp_path / "subckt" / "request.json",
            {
                "layout": {"netlist": layout_path, "top": "inv"},
                "reference": {
                    "netlist": reference_path,
                    "top": "inv",
                    "form": "subckt-call",
                },
            },
        )
    )

    assert subckt["status"] == "match"
    assert subckt["error_count"] == 0
    assert _geometry_entries(subckt) == []
    assert subckt["device_parameter_coverage"], "MOS classes must be reported"
    assert subckt["device_parameter_coverage"] == plain["device_parameter_coverage"]


def test_device_parameter_coverage_mixed_mos_and_resistor(tmp_path):
    """The one case where the coverage block's class filter (#5653) and the
    #2461 warning's own gate interact: both kinds participate, entries sorted
    by class name, and only the resistor raises
    `device.geometry_not_compared` -- exactly once, never for the MOS."""
    report = run_lvs(
        _write_request(
            tmp_path / "request.json",
            {
                "layout": {
                    "netlist": _write(
                        tmp_path / "layout.spice", _MIXED_MOS_RESISTOR_SPICE
                    ),
                    "top": "mix",
                },
                "reference": {
                    "netlist": _write(
                        tmp_path / "ref.spice", _MIXED_MOS_RESISTOR_SPICE
                    ),
                    "top": "mix",
                },
            },
        )
    )

    assert report["status"] == "match"
    assert report["error_count"] == 0

    geometry = _geometry_entries(report)
    assert len(geometry) == 1, "the warning is resistor/capacitor-only"
    assert geometry[0]["device"]["class"] == "RES"

    coverage = report["device_parameter_coverage"]
    assert [e["class"] for e in coverage] == sorted(e["class"] for e in coverage)
    by_kind = {e["device_kind"]: e for e in coverage}
    assert by_kind["mos"]["compared"] == ["L", "W"]
    assert {item["parameter"] for item in by_kind["mos"]["not_compared"]} == {
        "AS",
        "AD",
        "PS",
        "PD",
    }
    assert by_kind["resistor"]["compared"] == ["R"]


_DIODE_ONLY_SPICE = """
.subckt dio A K
D1 A K dmod
.ends
"""


def test_device_parameter_coverage_is_empty_without_a_participating_class(tmp_path):
    """The documented meaning of `[]` (issue #6105): a run with no
    participating MOS, resistor or capacitor class reports an empty list --
    not `null` (that is `"engine": "netgen"` only) and not an absent key. A
    diode is neither a geometry-disclosure kind nor a coverage-only kind."""
    report = run_lvs(
        _write_request(
            tmp_path / "request.json",
            {
                "layout": {
                    "netlist": _write(tmp_path / "layout.spice", _DIODE_ONLY_SPICE),
                    "top": "dio",
                },
                "reference": {
                    "netlist": _write(tmp_path / "ref.spice", _DIODE_ONLY_SPICE),
                    "top": "dio",
                },
            },
        )
    )

    assert report["status"] == "match"
    assert report["error_count"] == 0
    assert report["device_parameter_coverage"] == []
    assert _geometry_entries(report) == []

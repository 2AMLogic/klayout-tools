"""Diagnostics for unmerged fingers and mis-selected flavours (issue #4729)."""

import json
from pathlib import Path

from klayout_tools.lvs import run_lvs

_REF = ".subckt top d g s b\nM1 d g s b nfet L=0.5u W=40u\n.ends top\n"


def _fingers(*widths_lengths):
    body = "".join(
        f"M{i} d g s b nfet L={length}u W={width}u\n"
        for i, (width, length) in enumerate(widths_lengths, 1)
    )
    return f".subckt top d g s b\n{body}.ends top\n"


def _run(tmp_path: Path, layout: str, ref: str = _REF, options=None, **layout_extra):
    (tmp_path / "layout.spice").write_text(layout)
    (tmp_path / "ref.spice").write_text(ref)
    request = {
        "layout": {
            "netlist": str(tmp_path / "layout.spice"),
            "top": "top",
            **layout_extra,
        },
        "reference": {"netlist": str(tmp_path / "ref.spice"), "top": "top"},
    }
    if options:
        request["options"] = options
    (tmp_path / "req.json").write_text(json.dumps(request))
    return run_lvs(str(tmp_path / "req.json"))


def _cats(report, category):
    return [m for m in report["mismatches"] if m["category"] == category]


def test_unmerged_fingers_grouped_into_one_finding(tmp_path):
    report = _run(tmp_path, _fingers(*[(10, 0.5)] * 4))
    assert report["status"] == "mismatch"
    [entry] = _cats(report, "device.unmerged_fingers")
    assert entry["severity"] == "error"
    assert entry["details"]["finger_count"] == 4
    assert entry["details"]["summed_w_um"] == 40.0
    assert "combine_devices" in entry["description"]
    assert _cats(report, "device.unmatched") == []


def test_combine_devices_true_matches_unchanged(tmp_path):
    report = _run(
        tmp_path, _fingers(*[(10, 0.5)] * 4), options={"combine_devices": True}
    )
    assert report["status"] == "match"
    assert _cats(report, "device.unmerged_fingers") == []


def test_unequal_summed_width_is_not_grouped(tmp_path):
    report = _run(tmp_path, _fingers(*[(10, 0.5)] * 3))
    assert _cats(report, "device.unmerged_fingers") == []
    assert _cats(report, "device.unmatched")


def test_fingers_with_different_length_are_not_grouped(tmp_path):
    report = _run(tmp_path, _fingers((10, 0.5), (10, 0.5), (10, 0.5), (10, 0.6)))
    assert _cats(report, "device.unmerged_fingers") == []


def test_flavour_warning_names_selected_and_reference_class(tmp_path):
    layout = ".subckt top a b\nR1 a b 1k ppolyf_u_1k L=10u W=1u\n.ends top\n"
    ref = ".subckt top a b\nR1 a b 3k PPOLYF_U_3K L=10u W=1u\n.ends top\n"
    report = _run(tmp_path, layout, ref, deck="gf180mcu")
    [entry] = _cats(report, "device.flavour_mismatch")
    assert entry["severity"] == "warning"
    assert entry["details"]["suggested_value"] == "3k"
    assert "ppolyf_u_1k" in entry["description"]
    assert "PPOLYF_U_3K" in entry["description"]


def test_flavour_warning_absent_when_option_selects_reference_class(tmp_path):
    layout = ".subckt top a b\nR1 a b 3k ppolyf_u_3k L=10u W=1u\n.ends top\n"
    ref = ".subckt top a b\nR1 a b 3k PPOLYF_U_3K L=10u W=1u\n.ends top\n"
    report = _run(
        tmp_path, layout, ref, deck="gf180mcu", deck_options={"poly_res": "3k"}
    )
    assert _cats(report, "device.flavour_mismatch") == []

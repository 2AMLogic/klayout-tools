"""Device dimensions that suggest a missing SPICE length suffix (#7494)."""

import json

import pytest

from klayout_tools.lvs import run_lvs


def compare(tmp_path, layout, reference, *, deck=None, options=None):
    request = {"engine": "klayout", "options": options or {}}
    for side, text in (("layout", layout), ("reference", reference)):
        path = tmp_path / f"{side}.spice"
        path.write_text(text)
        request[side] = {"netlist": str(path), "top": "top"}
        if deck is not None:
            request[side]["deck"] = deck
    path = tmp_path / "lvs.json"
    path.write_text(json.dumps(request))
    report = run_lvs(str(path))
    warnings = [
        finding
        for finding in report["mismatches"]
        if finding["category"] == "device.implausible_dimensions"
    ]
    return report, warnings


def test_reports_both_oversized_dimensions_on_the_reference_capacitor(tmp_path):
    text = ".subckt top x y\nXC1 x y cap_cmomi W=40U L=10U\n.ends top\n"
    bare = text.replace("40U", "40").replace("10U", "10")
    report, warnings = compare(tmp_path, text, bare, deck="sg13cmos5l")
    assert report["status"] == "mismatch"
    (finding,) = warnings
    assert finding["severity"] == "warning"
    assert finding["side"] == "reference"
    assert finding["device"] == {
        "layout": None,
        "reference": "C1",
        "class": "cap_cmomi",
    }
    assert finding["details"] == {
        "circuit": "TOP",
        "dimensions_um": {"W": 40_000_000, "L": 10_000_000},
    }
    assert "40U" in finding["description"]


@pytest.mark.parametrize("flatten", [False, True])
@pytest.mark.parametrize("length, expected", [("1", True), ("999999U", False)])
def test_shared_mos_at_threshold_preserves_match_and_error_counts(
    tmp_path, length, expected, flatten
):
    text = (
        f".subckt child d g s b\nM1 d g s b nch W=10U L={length}\n.ends child\n"
        ".subckt top a b c d\nX1 a b c d child\nX2 a b c d child\n.ends top\n"
        ".subckt unused a b c d\nM2 a b c d nch W=40 L=40\n.ends unused\n"
    )
    report, warnings = compare(
        tmp_path,
        text,
        text,
        options={"flatten_layout": flatten, "flatten_reference": flatten},
    )
    assert report["status"] == "match"
    assert report["error_count"] == 0
    assert sorted(f["side"] for f in warnings) == (
        ["layout", "reference"] if expected else []
    )
    for finding in warnings:
        assert finding["details"] == {
            "circuit": "CHILD",
            "dimensions_um": {"L": 1_000_000},
        }


def test_plausible_dimensions_and_megohm_resistance_are_quiet(tmp_path):
    text = ".subckt top a b\nR1 a b 40meg\nXC1 a b cap_cmomi W=40U L=10U\n.ends top\n"
    report, warnings = compare(tmp_path, text, text, deck="sg13cmos5l")
    assert report["status"] == "match"
    assert not warnings

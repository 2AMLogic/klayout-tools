"""Repeated-label pin families (``PAD``/``PAD$1``/...) in ``run_lvs``
(issue #4798): not name-anchored, and a mismatch is retried once with neutral
names. Lives apart from ``test_lvs.py`` to respect the file-size ratchet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import test_lvs as base
from klayout_tools.lvs import run_lvs

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def _tolerance_report(tmp_path, layout_text, reference_text, top="inv", **extra):
    request = {
        "layout": {
            "netlist": _write(tmp_path / "layout.spice", layout_text),
            "top": top,
        },
        "reference": {
            "netlist": _write(tmp_path / "ref.spice", reference_text),
            "top": top,
        },
        **extra,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return run_lvs(str(path))


_REPEATED_LABEL_LAYOUT_SPICE = """
.subckt top PAD PAD$1 DOUT DOUT$1 VSS
M1 DOUT PAD VSS VSS nfet W=1U L=1U
M2 DOUT$1 PAD$1 VSS VSS nfet W=1U L=1U
.ends
"""

#: Same two identical units, but the `$k` numbering pairs `PAD` with `DOUT$1`
#: (extractor order, issue #4798) instead of `DOUT`.
_REPEATED_LABEL_REFERENCE_SPICE = """
.subckt top PAD PAD$1 DOUT DOUT$1 VSS
M1 DOUT$1 PAD VSS VSS nfet W=1U L=1U
M2 DOUT PAD$1 VSS VSS nfet W=1U L=1U
.ends
"""


def _repeated_label_report(tmp_path, layout, reference, **options):
    return _tolerance_report(
        tmp_path,
        layout,
        reference,
        top="top",
        options={"anchor_top_level_pins": True, **options},
    )


def test_run_lvs_repeated_label_pin_family_is_not_name_anchored(tmp_path):
    """Issue #4798: `PAD`/`PAD$1` are one repeated label whose `$k` pairing is
    extractor-order dependent, so identical units must not be mismatched by a
    swapped pairing; the skip is disclosed."""
    report = _repeated_label_report(
        tmp_path, _REPEATED_LABEL_LAYOUT_SPICE, _REPEATED_LABEL_REFERENCE_SPICE
    )

    assert report["status"] == "match"
    (entry,) = [
        m
        for m in report["mismatches"]
        if m["category"] == "topology.top_level_pins_anchored"
    ]
    assert entry["severity"] == "warning"
    assert entry["details"]["skipped_repeated_label_pins"] == ["DOUT", "PAD"]
    assert entry["details"]["pins"] == ["VSS"]
    assert "extractor-order dependent" in entry["description"]


def test_run_lvs_repeated_label_swapped_pairing_matches_with_anchoring_off(tmp_path):
    """Issue #4798: the false mismatch is the comparer's name tie-break, so it
    happens with anchoring off too; the neutral-name retry matches and says so."""
    report = _repeated_label_report(
        tmp_path,
        _REPEATED_LABEL_LAYOUT_SPICE,
        _REPEATED_LABEL_REFERENCE_SPICE,
        anchor_top_level_pins=False,
    )

    assert report["status"] == "match"
    (entry,) = [
        m
        for m in report["mismatches"]
        if m["category"] == "topology.top_level_pins_anchored"
    ]
    assert entry["severity"] == "warning"
    assert entry["details"]["repeated_label_pin_families"] == ["DOUT", "PAD"]


def test_run_lvs_repeated_label_structurally_different_units_still_mismatch(tmp_path):
    """The retry is topology-only, so a genuinely different unit still fails
    and the first-pass findings are reported."""
    different = _REPEATED_LABEL_REFERENCE_SPICE.replace(
        "M2 DOUT PAD$1 VSS VSS nfet", "M2 DOUT PAD$1 PAD$1 VSS nfet"
    )
    for anchoring in (True, False):
        report = _repeated_label_report(
            tmp_path,
            _REPEATED_LABEL_LAYOUT_SPICE,
            different,
            anchor_top_level_pins=anchoring,
        )
        assert report["status"] == "mismatch"
        assert report["error_count"] >= 1
        assert not any(
            "repeated_label_pin_families" in (m.get("details") or {})
            for m in report["mismatches"]
        )


def test_run_lvs_lone_suffixed_pin_stays_anchored(tmp_path):
    """A single pin literally named `X$1` with no `X` sibling is not a family."""
    layout = ".subckt top A$1 B VSS\nM1 B A$1 VSS VSS nfet W=1U L=1U\n.ends\n"
    report = _repeated_label_report(tmp_path, layout, layout)

    (entry,) = [
        m
        for m in report["mismatches"]
        if m["category"] == "topology.top_level_pins_anchored"
    ]
    assert entry["details"]["pins"] == ["A$1", "B", "VSS"]
    assert "skipped_repeated_label_pins" not in entry["details"]


def test_run_lvs_anchoring_discloses_layout_only_pins(tmp_path):
    """Issue #5396: layout top-level pins with no same-named reference pin
    (DEF-named internal nets) are listed additively in the anchoring entry's
    `details`; a run without any omits the keys entirely."""
    extra = base._MACRO_BUS_LAYOUT_SPICE.replace(
        ".subckt top din[3] din[2] din[1] din[0] dout",
        ".subckt top din[3] din[2] din[1] din[0] dout n_int2 n_int1",
    )
    report = run_lvs(base._macro_bus_request(tmp_path / "extra", extra))
    (entry,) = [
        m
        for m in report["mismatches"]
        if m["category"] == "topology.top_level_pins_anchored"
    ]
    assert entry["details"]["pins"] == ["DIN[0]", "DIN[1]", "DIN[2]", "DIN[3]", "DOUT"]
    assert entry["details"]["layout_only_pins"] == ["N_INT1", "N_INT2"]
    assert entry["details"]["layout_only_pin_count"] == 2
    assert entry["details"]["layout_only_pins_truncated"] is False

    clean = run_lvs(base._macro_bus_request(tmp_path / "clean"))
    (clean_entry,) = [
        m
        for m in clean["mismatches"]
        if m["category"] == "topology.top_level_pins_anchored"
    ]
    assert "layout_only_pins" not in clean_entry["details"]

"""``floating_gate_allowed`` spec key (issue #4820): a disclosed per-gate
carve-out for ``erc.floating_gate``. Split from ``test_erc.py`` to respect
the file-size ratchet."""

from __future__ import annotations

import json
import re

import klayout.db as kdb
import pytest

from klayout_tools.erc import ErcError, run_erc

DBU = 0.001


def _um(v):
    return round(v / DBU)


def _layout(path):
    """Three uncontacted gates: gate0 at x 0-0.5 (inside the box 0-2),
    gate1 at x 10-10.5 (outside), gate2 at x 1.5-3 (straddles the box
    edge). A li1 box far away keeps the li1 layer present."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    poly = layout.layer(1, 0)
    li1 = layout.layer(3, 0)
    for x0, x1 in ((0, 0.5), (10, 10.5), (1.5, 3)):
        top.shapes(poly).insert(kdb.Box.new(_um(x0), _um(0), _um(x1), _um(1)))
    top.shapes(li1).insert(kdb.Box.new(_um(50), _um(50), _um(51), _um(51)))
    layout.write(str(path))


def _spec(path, allowed="absent"):
    spec = {
        "stackup": [
            {"name": "poly", "layer": "1/0", "role": "gate"},
            {"name": "li1", "layer": "3/0"},
        ],
    }
    if allowed != "absent":
        spec["floating_gate_allowed"] = allowed
    path.write_text(json.dumps(spec))


def _run(tmp_path, allowed="absent", **kw):
    gds, spec = tmp_path / "t.gds", tmp_path / "t.erc.json"
    _layout(gds)
    _spec(spec, allowed)
    return run_erc(str(gds), str(spec), **kw)


def _floating(report):
    return sorted(
        f["gate_id"] for f in report["erc_findings"] if f["rule"] == "erc.floating_gate"
    )


BOX = {"box": [-1, -1, 2, 2], "reason": "LIG-only dummy gates"}


def test_inside_excused_partial_and_outside_still_flagged(tmp_path):
    report = _run(tmp_path, [BOX])
    assert _floating(report) == ["gate1", "gate2"]
    entry = report["provenance"]["floating_gate_allowed"][0]
    assert entry["matched_gates"] == 1
    assert entry["gates"][0]["gate_id"] == "gate0"
    assert entry["reason"] == "LIG-only dummy gates"
    cov = report["erc_coverage"]
    assert {
        "id": 'erc.floating_gate:["gate0"]',
        "reason": "floating_gate_disclosed",
    } in (cov["inapplicable"])
    assert 'erc.floating_gate:["gate0"]' not in cov["checked"]
    assert report["erc_finding_count"] == 2


def test_unused_box_reports_zero_matches(tmp_path):
    report = _run(tmp_path, [{"box": [100, 100, 101, 101], "reason": "stale"}])
    assert _floating(report) == ["gate0", "gate1", "gate2"]
    assert report["provenance"]["floating_gate_allowed"][0]["matched_gates"] == 0


def _without_spec_hash(report):
    """The report minus the spec-file digest (the spec text differs by the
    key under test, so its hash legitimately does)."""
    text = json.dumps(report, sort_keys=True)
    return re.sub(r'"sha256:[0-9a-f]{64}"', '"sha256:X"', text)


def test_undeclared_is_byte_identical(tmp_path):
    absent = _run(tmp_path)
    for empty in (None, []):
        other = _run(tmp_path, empty)
        assert _without_spec_hash(other) == _without_spec_hash(absent)
    assert "floating_gate_allowed" not in absent["provenance"]


def test_findings_only_parity(tmp_path):
    full = _run(tmp_path, [BOX])
    fast = _run(tmp_path, [BOX], findings_only=True)
    assert _floating(fast) == _floating(full)
    assert (
        fast["provenance"]["floating_gate_allowed"]
        == full["provenance"]["floating_gate_allowed"]
    )


def test_compact_coverage_stays_consistent(tmp_path):
    report = _run(tmp_path, [BOX], findings_only=True, compact=True)
    cov = report["erc_coverage"]
    assert [c for c in cov["checked"] if c.startswith("erc.floating_gate")]
    assert any(r["reason"] == "floating_gate_disclosed" for r in cov["inapplicable"])


def test_overlapping_boxes_first_match_wins(tmp_path):
    other = {"box": [-1, -1, 1, 2], "reason": "second"}
    report = _run(tmp_path, [BOX, other])
    counts = [e["matched_gates"] for e in report["provenance"]["floating_gate_allowed"]]
    assert counts == [1, 0]


@pytest.mark.parametrize(
    "bad",
    [
        {"box": [0, 0, 1, 1]},
        {"box": [0, 0, 1, 1], "reason": " "},
        {"box": [1, 1, 0, 0], "reason": "x"},
        {"box": [0, 0, 1], "reason": "x"},
        {"reason": "x"},
        {"box": [0, 0, 1, 1], "reason": "x", "extra": 1},
        "nope",
    ],
)
def test_malformed_entries_are_spec_errors(tmp_path, bad):
    with pytest.raises(ErcError):
        _run(tmp_path, [bad])


def test_non_array_is_spec_error(tmp_path):
    with pytest.raises(ErcError):
        _run(tmp_path, {"box": [0, 0, 1, 1], "reason": "x"})

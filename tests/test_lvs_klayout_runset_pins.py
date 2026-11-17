"""Pin-pair scoring for ``klt lvs`` ``"engine": "klayout-runset"`` (issue
#5664).

KLayout's own comparer grades a reference pin with no layout counterpart
``Match`` -- the layout pin is *unlabelled*, not mismatched (the shape a
runset global like IHP sg13g2's ``ignore_top_ports_mismatch`` exists to
accept). ``_pin_findings`` must disclose such pairs as ``severity:
"warning"`` instead of scoring them as errors, so the wrapper's verdict
agrees with the engine it wraps. Split out of
``tests/test_lvs_klayout_runset.py`` (whose fixture preamble, shared from
``helpers/lvs_runset_fixtures.py``, this module reuses) for the file-size
ratchet.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from helpers import lvs_runset_fixtures as fx
from helpers.lvs_runset_fixtures import (
    SPICE_PINNED,
    SPICE_PINNED_EXTRA_DEVICE,
)
from helpers.lvs_runset_fixtures import (
    request as _request,
)
from klayout_tools.lvs import check_lvs_report, run_lvs
from klayout_tools.lvs_runset_findings import _pin_findings


@pytest.fixture
def fake_klayout(monkeypatch, tmp_path):
    return fx.install_fake_klayout(monkeypatch, tmp_path)


class _StubPinPair:
    """Minimal ``NetlistCrossReference.PinPair`` stand-in for ``_pin_findings``."""

    def __init__(self, first, second, status):
        self._first, self._second, self._status = first, second, status

    def first(self):
        return self._first

    def second(self):
        return self._second

    def status(self):
        return self._status


class _StubPin:
    def __init__(self, name):
        self.name = name


class _StubXref:
    def __init__(self, pin_pairs):
        self._pin_pairs = pin_pairs

    def each_pin_pair(self, _cp):
        return list(self._pin_pairs)


def _x_status(name):
    return getattr(kdb.NetlistCrossReference, name)


def test_unlabelled_layout_pins_are_warnings_and_the_verdict_stays_match(
    tmp_path, fake_klayout
):
    """KLayout grades a reference pin with no layout counterpart ``Match``,
    so ``klt lvs`` must not override the runset's PASS with its own scoring."""
    report = run_lvs(_request(tmp_path, 1, spice=SPICE_PINNED))
    pins = [m for m in report["mismatches"] if m["category"] == "pin.unmatched"]
    assert len(pins) == 4, "one disclosed entry per reference pin"
    assert all(p["severity"] == "warning" for p in pins)
    assert all("graded" in p["description"] for p in pins)
    assert report["status"] == "match"
    assert report["error_count"] == 0
    assert report["category_counts"]["pin.unmatched"] == 4
    assert "pin.unmatched" not in report["category_error_counts"]
    # Structural pin presence stays factual (unchanged by the demotion).
    assert report["counts"]["pins"] == {"layout": 0, "reference": 4, "matched": 0}


def test_unlabelled_layout_pin_report_matches_shipped_schema(tmp_path, fake_klayout):
    import jsonschema

    from klayout_tools.lvs import SCHEMA_VERSION

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "schemas"
            / "lvs-response.schema.json"
        ).read_text()
    )
    report = run_lvs(_request(tmp_path, 1, spice=SPICE_PINNED))
    assert report["schema_version"] == SCHEMA_VERSION == 3
    # The demoted findings must conform to the shipped mismatches[] item
    # shape -- `severity` already admits "warning" (additive change only).
    mismatch_schema = {"$ref": "#/$defs/mismatch", "$defs": schema["$defs"]}
    for entry in report["mismatches"]:
        jsonschema.validate(entry, mismatch_schema)
    # ...and the report still round-trips through `klt lvs --check`'s
    # cheap path (hashes incl. the runset itself).
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    result = check_lvs_report(str(path))
    assert result["status"] == "match", [c for c in result["checks"] if not c["match"]]


def test_circuit_mismatch_keeps_pin_warnings_from_overriding_the_verdict(
    tmp_path, fake_klayout
):
    """A genuinely failed compare (extra reference device -> circuit pair not
    Match) keeps its verdict even though its pin pairs are graded Match."""
    report = run_lvs(_request(tmp_path, 1, spice=SPICE_PINNED_EXTRA_DEVICE))
    pins = [m for m in report["mismatches"] if m["category"] == "pin.unmatched"]
    assert report["status"] == "mismatch"
    assert report["error_count"] > 0
    assert pins, "pin pairs still disclosed"
    assert all(p["severity"] == "warning" for p in pins)
    other_errors = {
        m["category"]
        for m in report["mismatches"]
        if m["severity"] == "error" and m["category"] != "pin.unmatched"
    }
    assert other_errors, "the real mismatch must still carry errors"


def test_genuine_pin_mismatch_stays_error(tmp_path):
    """A pin pair whose own ``status()`` is not Match/MatchWithWarning keeps
    ``severity: "error"`` -- no regression in failure detection."""
    xref = _StubXref(
        [
            # layout-side pin with no reference counterpart, graded NoMatch:
            _StubPinPair(_StubPin("X"), None, _x_status("NoMatch")),
            # both sides present but graded Mismatch:
            _StubPinPair(_StubPin("A"), _StubPin("B"), _x_status("Mismatch")),
        ]
    )
    cname = {"layout": "SUB", "reference": "SUB"}  # not the top circuit
    out = _pin_findings(xref, None, cname)
    assert [m["severity"] for m in out] == ["error", "error"]
    assert all(m["category"] == "pin.unmatched" for m in out)
    assert out[0]["circuit"] == cname


def test_reference_side_absent_pin_graded_matched_is_also_a_warning(tmp_path):
    """The demotion is by pair grade, not by which side is missing."""
    xref = _StubXref([_StubPinPair(_StubPin("X"), None, _x_status("MatchWithWarning"))])
    out = _pin_findings(xref, None, {"layout": "TOP", "reference": "TOP"})
    assert len(out) == 1
    assert out[0]["severity"] == "warning"
    assert "MatchWithWarning" in out[0]["description"]

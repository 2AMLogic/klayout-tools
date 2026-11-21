"""Whole-sentence pins for ``slope_band``'s two context refusals (issue #5706).

Split out of tests/test_sim_slope_band_meas.py, which the file-size ratchet
caps at 500 lines; the request-validation / deck / grading tests stay there.

The two refusals are rendered by the shared helpers in
``sim_measurement_kind``; the ``match=`` tests in that file pin only their
stable stems, so these pin the whole sentence -- the per-form
``explanation=`` / ``form=`` clauses included. The expected text is the
wording the module carried inline before it delegated, written out literally
(not rebuilt from the helper's template) so a drifted clause fails here.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim, sim_slope_band_meas

# Same minimal declaration as tests/test_sim_slope_band_meas.py; only the
# context validator is exercised here, so nothing else from that file is needed.
_BODY = {
    "y": "v(out)",
    "x": "v(in)",
    "anchor": 0.5,
    "min_slope": 1.0,
    "max_slope": 2.5,
    "report": "y_span",
}
_DECL = {"name": "usable_range", "slope_band": _BODY}


@pytest.mark.parametrize(
    ("analysis", "got"),
    [
        ({"kind": "tran"}, "'tran'"),
        ({"kind": "AC"}, "'AC'"),
        ({"kind": ""}, "''"),
        ({}, "None"),
    ],
)
def test_non_dc_refusal_wording_is_pinned(analysis, got):
    with pytest.raises(sim.SimError) as exc:
        sim_slope_band_meas.validate_slope_band_context([_DECL], analysis)
    assert str(exc.value) == (
        "measurement 'usable_range' declares 'slope_band', which reduces a "
        'swept transfer curve and so requires analysis.kind "dc" '
        f"(got {got})"
    )


def test_duplicate_name_refusal_wording_is_pinned():
    # Case-insensitive match, reported with the *second* entry's spelling.
    other = {"name": "USABLE_RANGE", "slope_band": _BODY}
    with pytest.raises(sim.SimError) as exc:
        sim_slope_band_meas.validate_slope_band_context([_DECL, other], {"kind": "DC"})
    assert str(exc.value) == (
        "duplicate measurement name 'USABLE_RANGE': names must be unique when "
        "measurements[] declares a slope_band entry"
    )


def test_analysis_kind_is_checked_before_duplicate_names():
    other = {"name": "usable_range", "slope_band": _BODY}
    with pytest.raises(sim.SimError, match='requires analysis.kind "dc"'):
        sim_slope_band_meas.validate_slope_band_context(
            [_DECL, other], {"kind": "tran"}
        )

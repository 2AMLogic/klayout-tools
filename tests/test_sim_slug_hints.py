"""Measurement-diagnostic-hint and slug helper tests for ``klayout_tools.sim``.

Split out of ``test_sim.py`` (issue #4746) to keep that file under its
per-file size ceiling. Tests moved verbatim.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim


def test_measurement_diagnostic_hints_extracts_signal_window_and_target():
    hints = sim._measurement_diagnostic_hints(
        ".meas tran vout_avg AVG v(out) FROM=100n TO=900n"
    )
    assert hints["signal"] == "v(out)"
    assert hints["window"] == pytest.approx((1e-7, 9e-7))
    assert hints["target"] is None


def test_measurement_diagnostic_hints_extracts_when_target():
    hints = sim._measurement_diagnostic_hints(
        ".meas tran startup_time WHEN v(out)=1.5 RISE=1"
    )
    assert hints["signal"] == "v(out)"
    assert hints["window"] is None
    assert hints["target"] == pytest.approx(1.5)


def test_measurement_diagnostic_hints_unrecognised_card_returns_all_none():
    """A `TRIG`/`TARG` delay measurement has no single window/target this
    simple parser understands, and no `v(...)`/`i(...)` token at all when
    both endpoints are parameters -- must degrade to all-`None`, never
    raise."""
    hints = sim._measurement_diagnostic_hints(".meas tran tphl PARAM='a+b'")
    assert hints == {"signal": None, "window": None, "target": None}


def test_measurement_diagnostic_hints_is_case_insensitive_and_lowercases_signal():
    hints = sim._measurement_diagnostic_hints(
        ".MEAS TRAN VOUT FIND V(OUT) WHEN V(OUT)=1"
    )
    assert hints["signal"] == "v(out)"


@pytest.mark.parametrize(
    ("corner_id", "expected"),
    [
        ("tt/1.800V/27C", "tt_1p800V_27C"),
        ("ss/1.620V/-40C", "ss_1p620V_n40C"),
        ("tt/1.800V/27C/mc3", "tt_1p800V_27C_mc3"),
    ],
)
def test_slugify_corner_id_matches_cornerpoint_slug(corner_id, expected):
    """Mirrors :attr:`sim.CornerPoint.slug`'s own transform exactly -- a
    plain corner report dict has no live `CornerPoint` to ask, so this is a
    deliberate, tested duplication of that same string transform."""
    assert sim._slugify_corner_id(corner_id) == expected


def test_slugify_signal_produces_filesystem_safe_name():
    assert sim._slugify_signal("v(out)") == "v_out"
    assert sim._slugify_signal("I(Vdd)") == "i_vdd"
    assert sim._slugify_signal("") == "signal"

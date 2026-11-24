import pytest

from klayout_tools import sim


def test_validate_measurement_forms_rejects_case_variant_spice_names():
    """Issue #5373: `ta` / `tA` spice cards collide in ngspice."""
    with pytest.raises(sim.SimError, match="'ta' and 'tA'.*case-insensitively"):
        sim._validate_measurement_forms(
            [
                {"name": "ta", "spice": ".meas tran ta FIND v(out) AT=1u"},
                {"name": "tA", "spice": ".meas tran tA FIND v(out) AT=2u"},
            ]
        )


def test_validate_measurement_forms_rejects_case_variant_among_three():
    with pytest.raises(sim.SimError, match="'ta' and 'tA'"):
        sim._validate_measurement_forms(
            [
                {"name": "ta", "spice": ".meas tran ta FIND v(out) AT=1u"},
                {"name": "other", "spice": ".meas tran other FIND v(out) AT=1u"},
                {"name": "tA", "spice": ".meas tran tA FIND v(out) AT=2u"},
            ]
        )


def test_validate_measurement_forms_allows_distinct_names():
    sim._validate_measurement_forms(
        [
            {"name": "ta", "spice": ".meas tran ta FIND v(out) AT=1u"},
            {"name": "tb", "spice": ".meas tran tb FIND v(out) AT=2u"},
        ]
    )  # must not raise


def test_validate_measurement_forms_rejects_duplicate_spice_names():
    """Issue #6289: two byte-identical `name`s escaped the JSON error
    envelope as an unhandled `ValueError` (coverage's work-identity check)
    instead of a `SimError` up front."""
    with pytest.raises(sim.SimError, match="'m_dup'.*unique name"):
        sim._validate_measurement_forms(
            [
                {"name": "m_dup", "spice": ".meas tran m_dup FIND v(abo) AT=1u"},
                {"name": "m_dup", "spice": ".meas tran m_dup FIND v(bo) AT=1u"},
            ]
        )


def test_validate_measurement_forms_rejects_duplicate_name_among_three():
    with pytest.raises(sim.SimError, match="'ta' is declared more than once"):
        sim._validate_measurement_forms(
            [
                {"name": "ta", "spice": ".meas tran ta FIND v(out) AT=1u"},
                {"name": "other", "spice": ".meas tran other FIND v(out) AT=1u"},
                {"name": "ta", "spice": ".meas tran ta FIND v(out) AT=2u"},
            ]
        )


def test_validate_measurement_forms_case_variant_beats_later_duplicate():
    """`ta` / `tA` / `ta`: the case-variant hazard fires before the second
    byte-identical spelling is reached -- the #5373 message is unchanged."""
    with pytest.raises(sim.SimError, match="'ta' and 'tA'.*case-insensitively"):
        sim._validate_measurement_forms(
            [
                {"name": "ta", "spice": ".meas tran ta FIND v(out) AT=1u"},
                {"name": "tA", "spice": ".meas tran tA FIND v(out) AT=2u"},
                {"name": "ta", "spice": ".meas tran ta FIND v(out) AT=3u"},
            ]
        )


def test_validate_measurement_forms_duplicate_name_defers_to_self_checking_form():
    """A duplicate among `snm`/`transfer`/`tian`/`search` entries defers to
    that form's own validator (more specific message), as before #6289."""
    sim._validate_measurement_forms(
        [
            {
                "name": "snm_a",
                "snm": {"x": "v(a)", "y1": "v(out_a)", "y2": "v(out_b)"},
            },
            {
                "name": "snm_a",
                "snm": {"x": "v(a)", "y1": "v(out_c)", "y2": "v(out_d)"},
            },
        ]
    )  # must not raise here

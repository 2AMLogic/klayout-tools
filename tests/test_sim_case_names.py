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

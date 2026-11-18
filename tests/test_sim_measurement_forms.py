"""Tests for the no-form error of ``_validate_measurement_forms`` (issue #6328).

The no-form ``SimError`` is the discovery surface for the form registry: it
must name every form in ``sim_forms.FORMS``, or a newly registered form ships
undiscoverable (``eye``/``step_resolution``/``settled`` once were). This lives
in its own file because ``tests/test_sim.py`` sits at its file-size-ratchet
ceiling and cannot grow.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim, sim_forms


def test_validate_measurement_forms_no_form_error_lists_every_form():
    """The no-form error is the discovery surface for the form registry: every
    form in `sim_forms.FORMS` must appear in it, or a newly registered form
    ships undiscoverable (the `eye`/`step_resolution`/`settled` gap, #6328)."""
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_measurement_forms([{"name": "vout", "unit": "V"}])
    missing = [
        form for form in sim_forms.FORMS if f"'{form}'" not in str(excinfo.value)
    ]
    assert not missing, f"forms missing from the no-form error: {missing}"

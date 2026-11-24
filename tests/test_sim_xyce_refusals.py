"""Direct tests of the ``engine: "xyce"`` measurement-form refusals in
``sim_xyce_refusals.refuse_control_block_measurements``, reached through the
private gate ``sim._enforce_xyce_support_boundary`` (issue #6879: the gate
takes the measurement list and selects the first applicable diagnostic from
its own fixed form order). The end-to-end ``run_sim`` refusals live next to
the other engine gates in ``tests/test_sim.py`` and in each form's own test
module."""

from __future__ import annotations

import pytest

from klayout_tools import sim

_EXPR = {"name": "vout", "expr": "v(out)"}
_CROSSINGS = {
    "name": "tcross",
    "crossings": {"signal": "v(a)", "level": 0, "from": 1e-8, "to": "5u"},
}


def _enforce(measurements_spec: list[dict]) -> None:
    sim._enforce_xyce_support_boundary(
        engine="xyce",
        backend="local",
        corners_spec={},
        monte_carlo_declared=False,
        fail_fast_probe=False,
        measurements_spec=measurements_spec,
    )


def test_refusal_picks_the_first_unsupported_form_not_declaration_order():
    """The table's fixed form order (``expr`` before ``crossings``) picks the
    refusal, so a request declaring several unsupported forms is refused
    identically whatever order it declared them in."""
    for specs in ([_EXPR, _CROSSINGS], [_CROSSINGS, _EXPR]):
        with pytest.raises(sim.SimError, match=r"measurements\[\]\.expr"):
            _enforce(specs)


def test_noise_measurements_are_refused_as_expr_forms():
    """A ``noise`` measurement is an ``expr`` form (see
    ``sim_validate._expr_measurements``: non-null ``expr`` or ``noise``), so
    the boundary refuses it with the ``expr`` diagnostic."""
    with pytest.raises(sim.SimError, match=r"measurements\[\]\.expr"):
        _enforce([{"name": "n0", "noise": {"quantity": "inoise", "at_hz": 1e3}}])


def test_plain_spice_measurements_pass_the_boundary():
    """A ``.meas`` card is the supported measurement form for ``xyce`` --
    the end-to-end run is ``test_sim.test_run_sim_xyce_still_accepts_meas_cards``."""
    _enforce([{"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}])


def test_the_gate_is_a_no_op_without_measurements():
    """The measurement parameter is optional at the gate: callers with no
    measurement list (option-refusal probes, say) pass ``nothing``."""
    sim._enforce_xyce_support_boundary(
        engine="xyce",
        backend="local",
        corners_spec={},
        monte_carlo_declared=False,
        fail_fast_probe=False,
    )

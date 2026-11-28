"""The per-kind primitives every ``measurements[]`` form module shares.

Issue #8109 moved three blocks that were copied verbatim across the form
modules into :mod:`klayout_tools.sim_measurement_kind`: the object /
allowed-keys prelude, the single-ended ``"v(node)"`` selector check and the
first-declared-order key dedup. Each diagnostic must read exactly as the
per-kind module wrote it before the block moved, so the messages are pinned
whole rather than by substring.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools import sim_measurement_kind
from klayout_tools.sim_errors import SimError

_ALLOWED = frozenset({"source", "voltage", "from", "to"})
_EXAMPLE = '{"source": "vdd", "voltage": "v(a)"}'


def _validate(body):
    return sim_measurement_kind.validate_form_object(
        "m1", body, form="energy", allowed_keys=_ALLOWED, example=_EXAMPLE
    )


def test_validate_form_object_accepts_an_object_with_only_allowed_keys():
    _validate({"source": "vdd", "voltage": "v(a)", "to": "5n"})
    # Missing keys are the form's own check to raise, not this prelude's.
    _validate({})


@pytest.mark.parametrize("body", ["vdd", ["vdd"], 5])
def test_validate_form_object_quotes_the_form_and_shows_its_example(body):
    with pytest.raises(SimError) as excinfo:
        _validate(body)
    assert str(excinfo.value) == (
        "measurement 'm1': 'energy' must be an object, e.g. "
        '{"source": "vdd", "voltage": "v(a)"}'
    )


def test_validate_form_object_lists_unknown_keys_then_the_allowed_ones_sorted():
    with pytest.raises(SimError) as excinfo:
        _validate({"source": "vdd", "voltge": "v(a)", "zz": 1})
    assert str(excinfo.value) == (
        "measurement 'm1': unknown energy key(s) ['voltge', 'zz']; "
        "allowed: ['from', 'source', 'to', 'voltage']"
    )


def test_require_voltage_selectors_accepts_every_normalized_selector():
    body = {"x": "V(in)", "y1": "v(out_a)", "y2": "v(out_b)"}
    sim_measurement_kind.require_voltage_selectors(
        "m1", body, ("x", "y1", "y2"), form="snm"
    )


@pytest.mark.parametrize(
    ("body", "key", "got"),
    [
        ({"x": " v(in)", "y1": "v(out_a)", "y2": "v(out_b)"}, "x", "' v(in)'"),
        (
            {"x": "v(in)", "y1": "v(a) - v(b)", "y2": "v(out_b)"},
            "y1",
            "'v(a) - v(b)'",
        ),
        # ``y2`` is bad too, but ``y1`` comes first.
        ({"x": "v(in)", "y1": 5}, "y1", "5"),
    ],
)
def test_require_voltage_selectors_names_the_first_offending_key(body, key, got):
    """``keys`` is checked in order, so a body with several bad selectors is
    reported by the first."""
    with pytest.raises(SimError) as excinfo:
        sim_measurement_kind.require_voltage_selectors(
            "m1", body, ("x", "y1", "y2"), form="snm"
        )
    assert str(excinfo.value) == (
        f"measurement 'm1': snm.{key} must be a single-ended "
        f'"v(node)" selector with one node token (no whitespace, '
        f"comma or parenthesis); got {got}"
    )


def test_unique_keys_keeps_first_declared_order_and_drops_repeats():
    specs = [
        {"name": "a", "pair": ("v(in)", "v(out)")},
        {"name": "b", "pair": ("v(in)", "v(out)")},
        {"name": "c", "pair": ("v(in)", "v(dd)")},
        {"name": "d", "pair": ("v(in)", "v(out)")},
    ]
    assert sim_measurement_kind.unique_keys(specs, lambda spec: spec["pair"]) == [
        ("v(in)", "v(out)"),
        ("v(in)", "v(dd)"),
    ]
    assert sim_measurement_kind.unique_keys([], lambda spec: spec["pair"]) == []


def test_shared_helpers_leave_sim_runner_unloaded():
    """``require_voltage_selectors`` reaches ``sim_forms`` for the selector
    grammar; that must not drag the runner in behind it."""
    code = """
import sys
from klayout_tools import sim_measurement_kind

sim_measurement_kind.validate_form_object(
    "m", {}, form="energy", allowed_keys=frozenset(), example="{}"
)
sim_measurement_kind.require_voltage_selectors("m", {"x": "v(a)"}, ("x",), form="snm")
assert sim_measurement_kind.unique_keys([{"k": 1}, {"k": 1}], lambda s: s["k"]) == [1]
assert "klayout_tools.sim" not in sys.modules
"""
    source = Path(__file__).resolve().parents[1] / "src"
    subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(source)},
        check=True,
    )

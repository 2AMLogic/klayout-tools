"""Huge-JSON-int robustness regressions (issue #6233).

Two defect classes left behind by #5994/#6114/#6188, both triggered by an
integer literal in request/report JSON too large for a ``float``:

- **Part A** -- the remaining ``is_number(x)`` -> ``float(x)`` pairs
  (``op_sanity._triode_margin_v``, ``signoff._sta_worst_corner``,
  ``gen_compose._parse_drc_hints``, ``gen_compose._snap_port_to_grid``):
  ``float(10**400)`` raises ``OverflowError``, so a 400-digit literal in an
  advisory field escaped as an uncaught traceback instead of the field's
  documented "nothing claimed" behaviour.
- **Part B** -- Python's ``sys.get_int_max_str_digits()`` (4300 by default)
  makes ``json.loads``/``json.load`` raise a plain ``ValueError`` at *parse*
  time for a longer literal. That is not a ``json.JSONDecodeError``, so the
  ``_paths.py`` loaders let it escape. ``JSONDecodeError`` is a
  ``ValueError`` subclass, so widening the handlers preserves every existing
  message.
"""

from __future__ import annotations

import io
import json
import re
import sys

import pytest

from klayout_tools import gen_compose, op_sanity, signoff
from klayout_tools._paths import (
    load_request_arg,
    load_request_json,
    load_spec_json,
)

HUGE = 10**400


class _LoaderError(Exception):
    """Stand-in for a caller's own error class (``EvalError`` et al.)."""


# --------------------------------------------------------------------------- #
# Part A: advisory is_number() -> float() pairs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", ["triode_margin_v", "triode_margin_kt"])
@pytest.mark.parametrize("value", [HUGE, -HUGE])
def test_triode_margin_thresholds_refuse_a_huge_int(field, value):
    """Both ``op_lint.thresholds`` margin reads in ``_triode_margin_v`` are
    advisory (issue #6233): a huge int falls through to the thermal-voltage
    default instead of escaping as an uncaught ``OverflowError``."""
    default = op_sanity._triode_margin_v(27, {})
    assert op_sanity._triode_margin_v(27, {field: value}) == pytest.approx(default)


def test_sta_worst_corner_treats_a_huge_slack_as_unreported():
    """``signoff._sta_worst_corner`` sorts a corner whose ``worst_slack_ns``
    is a huge int as the worst of all (no usable slack), never an uncaught
    ``OverflowError`` -- and a finite slack still beats it."""
    huge = {"corner": "huge", "worst_slack_ns": HUGE}
    finite = {"corner": "ok", "worst_slack_ns": 0.5}
    assert signoff._sta_worst_corner({"corners": [finite, huge]}) is huge
    assert signoff._sta_worst_corner({"corners": [huge]}) is huge


@pytest.mark.parametrize("field", ["min_spacing_um", "foreign_clearance_um"])
def test_drc_hints_refuse_a_huge_int(field):
    """``generator_report.drc_hints`` numbers are advisory (#434/#2638): a
    huge int is "nothing claimed", so the defaults survive untouched."""
    matched, min_spacing, foreign = gen_compose._parse_drc_hints({field: HUGE})
    assert matched is None
    assert min_spacing == 0.0
    assert foreign is None


def test_snap_port_to_grid_treats_a_huge_width_as_unconstrained():
    """A generator-report port's ``width_um`` is trusted as-is (only ``name``
    is checked by ``_parse_blocks``), so it can be a huge int. The snap then
    treats the pad extent as unconstrained -- a finite snap target, never an
    uncaught ``OverflowError``."""
    port = {
        "name": "p",
        "x_um": 1.003,
        "y_um": 2.0,
        "width_um": HUGE,
        "direction_deg": 0,
        "layer": {"layer": 68, "datatype": 20},
    }
    pin = {"block": "b", "port": "p"}
    offset = {"x": 0.0, "y": 0.0}
    notes: list[str] = []
    snapped = gen_compose._snap_port_to_grid(port, pin, offset, 0.005, notes)
    assert snapped["x_um"] == pytest.approx(1.005)
    assert notes and "manufacturing grid" in notes[0]


# --------------------------------------------------------------------------- #
# Part B: _paths.py JSON loaders vs. a >4300-digit integer literal
# --------------------------------------------------------------------------- #


def _over_limit_literal() -> str:
    """An integer literal one digit past ``sys.get_int_max_str_digits()`` --
    the DoS-guard limit ``json`` enforces at parse time (4300 by default)."""
    return "1" * (sys.get_int_max_str_digits() + 1)


def _at_limit_literal() -> str:
    return "1" * sys.get_int_max_str_digits()


def test_load_request_json_file_form_names_the_path(tmp_path):
    """A file whose integer literal exceeds the digit budget reports the
    module's 'not valid JSON' envelope error, not an uncaught ValueError."""
    path = tmp_path / "huge.json"
    path.write_text('{"x": ' + _over_limit_literal() + "}")
    with pytest.raises(_LoaderError, match=re.escape(str(path))) as excinfo:
        load_request_json(str(path), _LoaderError)
    assert "is not valid JSON" in str(excinfo.value)


def test_load_spec_json_file_form_names_the_path(tmp_path):
    path = tmp_path / "huge.json"
    path.write_text('{"x": ' + _over_limit_literal() + "}")
    with pytest.raises(_LoaderError, match=re.escape(str(path))) as excinfo:
        load_spec_json(str(path), _LoaderError)
    assert "is not valid JSON" in str(excinfo.value)


def test_load_request_arg_stdin_form_names_the_source(monkeypatch):
    stdin = io.StringIO('{"x": ' + _over_limit_literal() + "}")
    monkeypatch.setattr("sys.stdin", stdin)
    with pytest.raises(_LoaderError, match="stdin request is not valid JSON"):
        load_request_arg(
            "-",
            error_cls=_LoaderError,
            required_fields=(),
            load_request_fn=load_request_json,
        )


def test_load_request_arg_inline_form_names_the_value():
    value = '{"x": ' + _over_limit_literal() + "}"
    with pytest.raises(_LoaderError, match="nor valid inline JSON"):
        load_request_arg(
            value,
            error_cls=_LoaderError,
            required_fields=(),
            load_request_fn=load_request_json,
        )


@pytest.mark.parametrize(
    "form",
    ["file_request", "file_spec", "stdin", "inline"],
)
def test_at_limit_literal_still_parses(tmp_path, monkeypatch, form):
    """A literal exactly at the digit budget is valid JSON and must keep
    parsing -- the fix widens the error net, never the accepted input."""
    document = '{"x": ' + _at_limit_literal() + "}"
    if form in ("file_request", "file_spec"):
        path = tmp_path / "ok.json"
        path.write_text(document)
        if form == "file_request":
            assert load_request_json(str(path), _LoaderError)["x"] >= 0
        else:
            assert load_spec_json(str(path), _LoaderError)["x"] >= 0
        return
    if form == "stdin":
        monkeypatch.setattr("sys.stdin", io.StringIO(document))
        request, _ = load_request_arg(
            "-",
            error_cls=_LoaderError,
            required_fields=(),
            load_request_fn=load_request_json,
        )
    else:
        request, _ = load_request_arg(
            document,
            error_cls=_LoaderError,
            required_fields=(),
            load_request_fn=load_request_json,
        )
    assert request["x"] >= 0


def test_malformed_json_still_gets_the_jsondecode_message(tmp_path):
    """``JSONDecodeError`` is a ``ValueError`` subclass, so the widened
    handlers keep the existing wording for ordinary malformed input."""
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(_LoaderError, match="is not valid JSON"):
        load_request_json(str(path), _LoaderError)
    assert json.loads('{"x": 1}') == {"x": 1}

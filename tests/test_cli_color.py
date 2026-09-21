"""Tests for the shared ``klt`` colour policy (issue #2227).

``tests/test_signoff.py`` covers the policy end-to-end through the one verb
that uses it today. These tests pin the *precedence rules* directly, so the
next verb that calls :func:`add_color_args` inherits a specified contract
rather than whatever ``signoff``'s renderers happened to exercise.
"""

from __future__ import annotations

import argparse
import io

import pytest

from klayout_tools.cli.color import (
    COLOR,
    PLAIN,
    Palette,
    add_color_args,
    resolve_palette,
    use_color,
)


class _Stream(io.StringIO):
    """A stdout stand-in with a settable ``isatty()``."""

    def __init__(self, is_tty: bool) -> None:
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_color_args(parser)
    return parser.parse_args(argv)


@pytest.fixture(autouse=True)
def _no_ambient_no_color(monkeypatch):
    """Never let the developer's own `$NO_COLOR` decide these assertions."""
    monkeypatch.delenv("NO_COLOR", raising=False)


def test_defaults_to_colour_at_a_tty():
    assert use_color(_parse([]), _Stream(is_tty=True)) is True


def test_defaults_to_plain_when_not_a_tty():
    """The committed-artifact default: redirected output carries no escapes."""
    assert use_color(_parse([]), _Stream(is_tty=False)) is False


def test_no_color_env_var_beats_the_tty_default(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert use_color(_parse([]), _Stream(is_tty=True)) is False


def test_no_color_env_var_honours_any_non_empty_value(monkeypatch):
    """https://no-color.org/: "present and not an empty string (regardless of
    its value)" -- notably including `NO_COLOR=0`, which is *not* an opt-in."""
    monkeypatch.setenv("NO_COLOR", "0")
    assert use_color(_parse([]), _Stream(is_tty=True)) is False


def test_empty_no_color_env_var_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "")
    assert use_color(_parse([]), _Stream(is_tty=True)) is True


def test_no_color_flag_beats_the_tty_default():
    assert use_color(_parse(["--no-color"]), _Stream(is_tty=True)) is False


def test_color_never_beats_the_tty_default():
    assert use_color(_parse(["--color", "never"]), _Stream(is_tty=True)) is False


def test_color_always_opts_back_in_through_a_pipe():
    assert use_color(_parse(["--color", "always"]), _Stream(is_tty=False)) is True


def test_color_always_beats_the_no_color_env_var(monkeypatch):
    """An option the caller typed outranks an inherited environment variable:
    `$NO_COLOR` governs the default, not an explicit request."""
    monkeypatch.setenv("NO_COLOR", "1")
    assert use_color(_parse(["--color", "always"]), _Stream(is_tty=False)) is True


def test_no_color_flag_beats_color_always():
    """The two suppressing spellings are equivalent, and a contradictory pair
    resolves to the suppressing one -- the safe answer for an artifact."""
    args = _parse(["--no-color", "--color", "always"])
    assert use_color(args, _Stream(is_tty=True)) is False


def test_a_stream_with_no_isatty_is_treated_as_not_a_tty():
    class _Bare:
        pass

    assert use_color(_parse([]), _Bare()) is False


def test_missing_args_attributes_fall_back_to_the_default_policy():
    """A caller whose parser never registered the flags (every verb that does
    not colour) still gets a well-defined answer, not an AttributeError."""
    bare = argparse.Namespace()
    assert use_color(bare, _Stream(is_tty=True)) is True
    assert use_color(bare, _Stream(is_tty=False)) is False


def test_resolve_palette_maps_the_decision_onto_the_two_palettes():
    assert resolve_palette(_parse([]), _Stream(is_tty=True)) is COLOR
    assert resolve_palette(_parse([]), _Stream(is_tty=False)) is PLAIN


def test_plain_palette_is_the_colour_palette_with_every_escape_emptied():
    """The invariant renderers rely on: interpolating a palette field is
    unconditional, and `PLAIN` makes those interpolations vanish."""
    assert PLAIN == Palette(red="", green="", reset="")
    assert set(PLAIN._fields) == set(COLOR._fields)
    assert all(value == "" for value in PLAIN)
    assert all(value.startswith("\033[") for value in COLOR)


def test_use_color_reads_sys_stdout_when_no_stream_is_given(monkeypatch):
    """Resolved at call time, not import time -- pytest (and any embedding
    caller) replaces `sys.stdout` long after this module is imported."""
    monkeypatch.setattr("sys.stdout", _Stream(is_tty=True))
    assert use_color(_parse([])) is True
    monkeypatch.setattr("sys.stdout", _Stream(is_tty=False))
    assert use_color(_parse([])) is False

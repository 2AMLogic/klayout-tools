"""Shared ANSI-colour policy for ``klt`` subcommands (issue #2227).

``--format text`` is a courtesy rendering (see :mod:`.output`), and a couple
of verbs colour it so a scan of the output shows what is wrong at a glance.
But the same text rendering is *also* an artifact: ``klt signoff --manifest``
exists so a block repo can commit its tier verdict as an evidence record, and
a committed file whose every verdict line carries ``\\033[31m`` is unreadable
in a pull-request diff and forces every consumer to strip ANSI before
grepping it.

So colour is a **policy decision made once per invocation**, not a property
of the renderer. This module owns that policy for every verb:

- ``--no-color`` / ``--color=never`` force it off.
- ``--color=always`` forces it on -- the way a caller opts back in through a
  pipe (e.g. ``klt signoff ... | less -R``).
- ``$NO_COLOR`` (https://no-color.org/), set to any non-empty value, turns it
  off. An explicit ``--color=always`` still wins: the standard governs the
  *default* behaviour, not an option the caller typed on purpose.
- Otherwise it follows ``stream.isatty()`` -- colour at a terminal, plain
  text when redirected to a file or piped to another process. This is the
  conventional default, and it is what makes the committed-artifact case
  correct with no flags at all.

Renderers never read this policy from a global. They take a :class:`Palette`
-- :data:`COLOR` or :data:`PLAIN` -- so the "no colour" rendering is the same
code path with empty strings substituted, and a test can render either one
without monkeypatching module state.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import IO, NamedTuple


class Palette(NamedTuple):
    """The escape sequences a text renderer may wrap output in.

    :data:`PLAIN` is the same shape with every field empty, so a renderer
    interpolates ``palette.red``/``palette.reset`` unconditionally and the
    uncoloured rendering falls out as the identical string minus the
    escapes -- no ``if use_color`` branch per call site.
    """

    red: str
    green: str
    reset: str


#: The colouring palette: red for "wrong/missing", green for "met/ok".
COLOR = Palette(red="\033[31m", green="\033[32m", reset="\033[0m")

#: The escape-free palette used whenever colour is suppressed.
PLAIN = Palette(red="", green="", reset="")

#: ``--color`` choices, in the conventional ``auto``/``always``/``never``
#: spelling (git, grep, ls, ripgrep).
COLOR_CHOICES = ("auto", "always", "never")


def add_color_args(parser: argparse.ArgumentParser) -> None:
    """Register the shared ``--color``/``--no-color`` options on ``parser``.

    Registered per-subcommand (next to :func:`_add_format_arg` in
    ``parser.py``) rather than on the top-level parser, so a verb that emits
    no colour does not advertise an option that does nothing.
    """
    parser.add_argument(
        "--color",
        choices=list(COLOR_CHOICES),
        default="auto",
        help=(
            "when to colour --format text output: 'auto' (default) colours "
            "only when stdout is a terminal and $NO_COLOR is unset, "
            "'always' colours even through a pipe or into a file, 'never' "
            "never colours. --format json is never coloured."
        ),
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help=(
            "suppress ANSI colour in --format text output (equivalent to "
            "--color=never) -- use this when committing the text rendering "
            "as an artifact. $NO_COLOR in the environment does the same."
        ),
    )


def use_color(args: argparse.Namespace, stream: IO[str] | None = None) -> bool:
    """Whether ``--format text`` output should carry ANSI colour.

    Precedence, highest first: ``--no-color``/``--color=never`` off,
    ``--color=always`` on, ``$NO_COLOR`` (non-empty) off, else
    ``stream.isatty()`` (``sys.stdout`` when ``stream`` is ``None``).

    ``stream`` is resolved at call time, never at import, because the tests
    -- and any embedding caller -- replace ``sys.stdout`` after this module
    is imported.
    """
    if getattr(args, "no_color", False) or getattr(args, "color", "auto") == "never":
        return False
    if getattr(args, "color", "auto") == "always":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if stream is None:
        stream = sys.stdout
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except ValueError:  # pragma: no cover - closed stream
        return False


def resolve_palette(args: argparse.Namespace, stream: IO[str] | None = None) -> Palette:
    """:data:`COLOR` or :data:`PLAIN`, per :func:`use_color`."""
    return COLOR if use_color(args, stream) else PLAIN

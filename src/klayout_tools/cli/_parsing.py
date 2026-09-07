"""Shared CLI-argument parsing helpers.

Several ``klt`` subcommands accept a "path-or-inline-JSON" value for
flags like ``--layers``/``--allowed-layers``/``--conductors`` (a path to a
JSON file, or an inline JSON string) and/or an inline-JSON ``--region``
micrometre tuple. These conventions were hand-rolled independently in each
command module; this module is the single implementation they all delegate
to, each supplying its own exception class and ``--flag`` name so error
messages stay command-specific.

The same applies to the extraction-flag pair ``--deck-option``/``--pins``
(:func:`parse_deck_options`/:func:`parse_declared_pins`), which both
``klt extract`` and ``klt pex`` accept and forward to the *same*
``run_extract`` parameters -- issue #1558 added the ``klt pex`` half, and
factored the parsing here rather than duplicating it, so the two commands
cannot drift on what they accept or on the message a malformed entry
produces.

Private to the :mod:`klayout_tools.cli` package -- not part of the public
``klayout_tools`` API.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable

ErrorFactory = Callable[[str], Exception]


def load_json_path_or_inline(
    value: str,
    flag: str,
    error_cls: ErrorFactory,
    *,
    inline_hint: str = "an inline JSON array",
) -> list:
    """Resolve ``value`` into a decoded JSON array.

    ``value`` is either a path to a JSON file or an inline JSON string.
    ``flag`` is the CLI flag name (e.g. ``"--layers"``) embedded in error
    messages, and ``error_cls`` is the caller's own exception class, raised
    on any resolution/decode/shape failure.

    ``inline_hint`` customizes the phrase used when a bare (non-file) value
    fails to decode as JSON, so callers can describe the expected shape,
    e.g. ``"an inline JSON array of [layer, datatype] pairs"``.
    """
    if os.path.isfile(value):
        try:
            with open(value, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise error_cls(f"could not read {flag} file '{value}': {exc}") from exc
        except json.JSONDecodeError as exc:
            raise error_cls(f"{flag} file '{value}' is not valid JSON: {exc}") from exc
    else:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise error_cls(
                f"{flag} must be a path to a JSON file or {inline_hint}: {exc}"
            ) from exc

    if not isinstance(data, list):
        raise error_cls(f"{flag} must decode to a JSON array")

    return data


def parse_layer_pairs(
    data: list,
    flag: str,
    error_cls: ErrorFactory,
    *,
    reject_bool: bool = True,
    require_nonempty: bool = True,
) -> list[tuple[int, int]]:
    """Validate a decoded JSON array as a list of ``(layer, datatype)`` pairs.

    ``data`` is the already-decoded JSON value (typically the result of
    :func:`load_json_path_or_inline`). ``flag`` and ``error_cls`` behave as
    in :func:`load_json_path_or_inline`.

    ``reject_bool`` controls whether ``bool`` values (which are ``int``
    subclasses in Python) are rejected as layer/datatype numbers.
    ``require_nonempty`` controls whether an empty pair list raises.
    """
    layers: list[tuple[int, int]] = []
    for entry in data:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(
                isinstance(v, int) and (not reject_bool or not isinstance(v, bool))
                for v in entry
            )
        ):
            raise error_cls(
                f"{flag} entries must each be a [layer, datatype] pair of "
                f"integers, got {entry!r}"
            )
        layers.append((entry[0], entry[1]))

    if require_nonempty and not layers:
        raise error_cls(f"{flag} must contain at least one layer")

    return layers


def load_region(
    value: str | None,
    error_cls: ErrorFactory,
    flag: str = "--region",
) -> tuple[float, float, float, float] | None:
    """Resolve an optional inline-JSON ``--region``-style value into a
    micrometre ``(left, bottom, right, top)`` tuple, or ``None`` when
    ``value`` is ``None``.

    ``value`` is an inline JSON array of four numbers in micrometres, e.g.
    ``[0, 0, 100, 100]``. ``flag`` and ``error_cls`` behave as in
    :func:`load_json_path_or_inline`.
    """
    if value is None:
        return None

    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise error_cls(
            f"{flag} must be an inline JSON array of four numbers "
            f"[left, bottom, right, top] in micrometres: {exc}"
        ) from exc

    if (
        not isinstance(data, list)
        or len(data) != 4
        or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in data
        )
    ):
        raise error_cls(
            f"{flag} must be a JSON array of four numbers "
            f"[left, bottom, right, top] in micrometres, got {data!r}"
        )

    left, bottom, right, top = (float(v) for v in data)
    if right <= left or top <= bottom:
        raise error_cls(
            f"{flag} must have right > left and top > bottom, got "
            f"[{left}, {bottom}, {right}, {top}]"
        )

    return (left, bottom, right, top)


def parse_deck_options(
    raw: list[str] | None,
    error_cls: ErrorFactory,
    flag: str = "--deck-option",
) -> dict[str, str] | None:
    """Parse the ``--deck-option`` flag's ``KEY=VALUE`` entries (issue #595,
    repeatable) into a ``dict``, or ``None`` when the flag was never given.

    Shared by ``klt extract`` and ``klt pex`` (issue #1558) -- both forward
    the result to ``run_extract``'s own ``deck_options`` parameter, so the
    accepted spelling must stay identical between them. ``flag`` and
    ``error_cls`` behave as in :func:`load_json_path_or_inline`.

    Raises ``error_cls`` for a malformed entry (no ``=``, or a blank key) --
    a likely typo, not a meaningful "no options" request. A later ``KEY``
    overrides an earlier one with the same key (last-one-wins, matching how
    argparse's own ``append`` action preserves given order).
    """
    if raw is None:
        return None
    options: dict[str, str] = {}
    for entry in raw:
        key, sep, value = entry.partition("=")
        key = key.strip()
        if not sep or not key:
            raise error_cls(
                f"{flag} entry {entry!r} is not KEY=VALUE -- e.g. {flag} poly_res=2k"
            )
        options[key] = value.strip()
    return options


def parse_declared_pins(
    raw: str | None,
    error_cls: ErrorFactory,
    flag: str = "--pins",
) -> frozenset[str] | None:
    """Parse the ``--pins`` flag's comma-separated value (issue #514) into a
    ``frozenset`` of declared pin names, or ``None`` when the flag was
    omitted entirely (skips the declared-pin-set reconciliation).

    Shared by ``klt extract`` and ``klt pex`` (issue #1558), like
    :func:`parse_deck_options` above. ``flag`` and ``error_cls`` behave as in
    :func:`load_json_path_or_inline`.

    Raises ``error_cls`` if the flag was given but every comma-separated
    token is blank (e.g. ``--pins ""`` or ``--pins ,,``) -- a likely
    mistake, not a meaningful "declare zero pins" request.
    """
    if raw is None:
        return None
    names = frozenset(name.strip() for name in raw.split(",") if name.strip())
    if not names:
        raise error_cls(
            f"{flag} was given but contains no non-empty name "
            f"(got {raw!r}) -- pass a comma-separated list of net names, "
            f"e.g. {flag} A,B,VDD,VSS"
        )
    return names

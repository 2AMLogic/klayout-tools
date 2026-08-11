"""Shared CLI-internal parsing helpers for ``klt`` command modules.

Private to :mod:`klayout_tools.cli` -- not part of the public
``klayout_tools`` API. Several ``*_cmd.py`` modules accept flags that follow
the same two conventions and previously hand-rolled the parsing
independently:

- **path-or-inline-JSON**: a flag value that is either a path to a JSON file
  or an inline JSON string (``--layers``, ``--allowed-layers``,
  ``--conductors``, ``--vias``, ``--label-layers``).
- **micrometre region**: an inline JSON array of four numbers
  ``[left, bottom, right, top]`` in micrometres (``--region``).

Each helper takes the caller's own exception class (``error_cls``) so the
raised error still carries that command's error type (``RingCheckError``,
``RenderError``, ``PrecheckError``, ``ComponentsError``) for
``emit_error``/``run()`` to catch, and the caller's own ``--flag`` name so
error messages stay specific to the flag that failed.
"""

from __future__ import annotations

import json
import os


def load_json_path_or_inline(
    value: str,
    flag: str,
    error_cls: type[Exception],
    *,
    array_kind: str = "array",
) -> list:
    """Resolve ``value`` into a decoded JSON array.

    ``value`` is a path to a JSON file, or an inline JSON string -- the
    "path-or-inline-JSON" convention shared across ``klt`` CLI flags that
    accept JSON arrays. ``array_kind`` customises the "an inline JSON
    <array_kind>" phrase used in the inline-parse-failure message (e.g.
    ``"array of [layer, datatype] pairs"``).

    Raises ``error_cls`` on an unreadable file, invalid JSON, or JSON that
    doesn't decode to a JSON array.
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
                f"{flag} must be a path to a JSON file or an inline JSON "
                f"{array_kind}: {exc}"
            ) from exc

    if not isinstance(data, list):
        raise error_cls(f"{flag} must decode to a JSON array")

    return data


def parse_layer_pairs(
    data: list,
    flag: str,
    error_cls: type[Exception],
) -> list[tuple[int, int]]:
    """Validate a decoded JSON array as a list of ``[layer, datatype]``
    integer pairs, the convention shared by ``--layers``/``--allowed-layers``.

    Raises ``error_cls`` if any entry isn't a two-element list of (non-bool)
    integers, or if the resulting list is empty.
    """
    layers: list[tuple[int, int]] = []
    for entry in data:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in entry)
        ):
            raise error_cls(
                f"{flag} entries must each be a [layer, datatype] pair of "
                f"integers, got {entry!r}"
            )
        layers.append((entry[0], entry[1]))

    if not layers:
        raise error_cls(f"{flag} must contain at least one layer")

    return layers


def load_region(
    value: str | None,
    error_cls: type[Exception],
    flag: str = "--region",
) -> tuple[float, float, float, float] | None:
    """Resolve an optional micrometre region flag value into a
    ``(left, bottom, right, top)`` tuple, or ``None`` when omitted.

    ``value`` is an inline JSON array of four numbers in micrometres, e.g.
    ``[0, 0, 100, 100]``.

    Raises ``error_cls`` on invalid JSON, a malformed array, or a degenerate
    window (``right <= left`` or ``top <= bottom``).
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

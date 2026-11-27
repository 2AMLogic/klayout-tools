"""Waveform-artifact options for ``klt sim`` (issues #3211, #5112).

``options.waveform_max_bytes`` caps the rawfile size converted to the
``waveform.raw.json`` artifact; ``options.waveform_decimate`` thins the points
written to that artifact. Kept out of :mod:`klayout_tools.sim` so the size
ratchet on that module does not grow.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import IO, Any

from ._jsonnum import is_int
from .sim_errors import SimError

#: Default for ``options.waveform_max_bytes`` (issue #3211): the largest
#: ``waveform.raw`` klt will convert to the ``waveform.raw.json`` artifact.
#: The conversion materializes the whole ASCII rawfile in memory, so an
#: unbounded transient can be OOM-killed with no diagnostic; above this size
#: the corner keeps its measurements and gets a ``waveform_too_large`` warning.
DEFAULT_WAVEFORM_MAX_BYTES = 1024**3


def validate_max_bytes(options: dict[str, Any]) -> int | None:
    """Validate ``options.waveform_max_bytes`` (issue #3211).

    Returns the cap in bytes (:data:`DEFAULT_WAVEFORM_MAX_BYTES` when the key
    is omitted); an explicit ``null`` disables the guard and returns ``None``.
    """
    if "waveform_max_bytes" not in options:
        return DEFAULT_WAVEFORM_MAX_BYTES
    value = options["waveform_max_bytes"]
    if value is None:
        return None
    if not is_int(value) or value < 1:
        raise SimError(
            "options.waveform_max_bytes must be a positive integer byte count "
            f"or null to disable the guard (got {json.dumps(value, default=repr)})"
        )
    return value


def validate_decimate(options: dict[str, Any]) -> int | None:
    """Validate ``options.waveform_decimate`` (issue #5112).

    Returns the stride ``k`` (keep every k-th point of ``waveform.raw.json``,
    always including the first and last), or ``None`` when the key is absent.
    """
    if "waveform_decimate" not in options:
        return None
    value = options["waveform_decimate"]
    if not is_int(value) or value < 1:
        raise SimError(
            "options.waveform_decimate must be a positive integer stride "
            f"(got {json.dumps(value, default=repr)})"
        )
    return value


def write_points(handle: IO[str], points: Iterable[Any], decimate: int | None) -> None:
    """Stream ``points`` into ``handle`` as JSON array items.

    With ``decimate`` = k only every k-th point is written, plus the final
    point; ``None`` (or 1) writes every point unchanged.
    """
    stride = decimate or 1
    written = 0
    last = None
    for index, point in enumerate(points):
        if index % stride:
            last = point
            continue
        last = None
        handle.write((", " if written else "") + json.dumps(point))
        written += 1
    if last is not None:
        handle.write(", " + json.dumps(last))


def report_field(decimate: int | None, artifacts: dict[str, Any]) -> dict[str, int]:
    """The applied stride, present only when the waveform was actually decimated."""
    if decimate is not None and "waveform" in artifacts:
        return {"waveform_decimate": decimate}
    return {}

"""Complex-preserving ngspice ASCII rawfile reader (issue #2923).

:func:`klayout_tools.sim.parse_ascii_rawfile` flattens every ``Flags: complex``
value to its magnitude -- the documented public waveform JSON -- so phase is
unrecoverable from it. ``measurements[].transfer`` needs the phase, so this
private reader keeps each value as a Python ``complex``. It is deliberately
strict: any malformed header, ragged point or non-numeric token raises
:class:`RawFileError` rather than being skipped, because a silently shortened
sweep would shift every crossing the caller then reports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class RawFileError(ValueError):
    """The rawfile is unreadable or not a well-formed ngspice ASCII rawfile."""


@dataclass
class ComplexRawfile:
    """A decoded rawfile: variable names (lower-cased) and per-point values."""

    plotname: str
    names: list[str]
    points: list[list[complex]]

    def column(self, index: int) -> list[complex]:
        return [point[index] for point in self.points]


def _parse_value(token: str, is_complex: bool) -> complex:
    try:
        if not is_complex:
            return complex(float(token), 0.0)
        real, _, imag = token.partition(",")
        return complex(float(real), float(imag) if imag else 0.0)
    except ValueError as exc:
        raise RawFileError(f"non-numeric rawfile value {token!r}") from exc


def _read_header(text: str) -> tuple[str, list[str], bool, int | None, int]:
    """``(plotname, names, is_complex, declared_points, values_offset)``."""
    plot = re.search(r"^Plotname:\s*(.*)$", text, re.MULTILINE)
    count = re.search(r"^No\. Variables:\s*(\d+)\s*$", text, re.MULTILINE)
    npoints = re.search(r"^No\. Points:\s*(\d+)\s*$", text, re.MULTILINE)
    block = re.search(r"^Variables:\n(.*?)^Values:\n", text, re.MULTILINE | re.DOTALL)
    if plot is None or count is None or block is None:
        raise RawFileError("not a recognisable ngspice ASCII rawfile")
    flags = re.search(r"^Flags:\s*(.*)$", text, re.MULTILINE)
    is_complex = flags is not None and "complex" in flags.group(1).lower()
    names = [
        parts[1].lower()
        for parts in (line.split() for line in block.group(1).splitlines())
        if len(parts) >= 3
    ]
    if len(names) != int(count.group(1)):
        raise RawFileError(
            f"declared {count.group(1)} variables but the header lists {len(names)}"
        )
    declared = int(npoints.group(1)) if npoints is not None else None
    return plot.group(1).strip(), names, is_complex, declared, block.end()


def _read_points(values: str, var_count: int, is_complex: bool) -> list[list[complex]]:
    points: list[list[complex]] = []
    current: list[complex] = []
    for raw_line in values.splitlines():
        tokens = raw_line.split()
        if not tokens:
            continue
        if len(tokens) == 2 and not current:
            current = [_parse_value(tokens[1], is_complex)]
        elif len(tokens) == 1 and current:
            current.append(_parse_value(tokens[0], is_complex))
        else:
            raise RawFileError(f"unexpected rawfile line {raw_line.strip()!r}")
        if len(current) == var_count:
            points.append(current)
            current = []
    if current:
        raise RawFileError("rawfile ends in the middle of a point")
    return points


def read_complex_ascii_rawfile(raw_path: str) -> ComplexRawfile:
    """Decode an ngspice ASCII rawfile, keeping complex values complex."""
    try:
        with open(raw_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise RawFileError(f"could not read rawfile: {exc}") from exc
    plotname, names, is_complex, declared, offset = _read_header(text)
    points = _read_points(text[offset:], len(names), is_complex)
    if declared is not None and declared != len(points):
        raise RawFileError(f"declared {declared} points but decoded {len(points)}")
    return ComplexRawfile(plotname=plotname, names=names, points=points)

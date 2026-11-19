"""ngspice ASCII rawfile readers: the public waveform artifact and the
complex-preserving transfer-measurement decode.

Two decoders live here on purpose, because their contracts differ (issue
#2923):

* :func:`parse_ascii_rawfile` / :func:`_parse_and_persist_waveform` -- the
  documented public waveform JSON. Every ``Flags: complex`` value is flattened
  to its magnitude (``hypot(real, imag)``), so ``points[]`` is always plain
  real floats and phase is unrecoverable. Lenient about stray lines, and
  streamed point by point (issue #3360) so memory does not scale with the
  rawfile. Failures raise :class:`klayout_tools.sim_errors.SimError`.
* :func:`read_complex_ascii_rawfile` -- ``measurements[].transfer`` needs the
  phase, so this private reader keeps each value as a Python ``complex``. It
  is deliberately strict: any malformed header, ragged point or non-numeric
  token raises :class:`RawFileError` rather than being skipped, because a
  silently shortened sweep would shift every crossing the caller then reports.

Living here (not in :mod:`klayout_tools.sim`) lets the measurement modules read
a waveform without importing the corner runner (issue #6752).
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any

from . import sim_waveform_opts
from .sim_errors import SimError


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


# --------------------------------------------------------------------------- #
# Public waveform artifact (rawfile -> JSON; magnitude-flattened)
# --------------------------------------------------------------------------- #


def parse_ascii_rawfile(raw_path: str) -> dict[str, Any]:
    """Parse an ngspice ASCII rawfile (``set filetype=ascii`` + ``write``)
    into the documented waveform JSON shape (see ``docs/cli/sim.md``)::

        {
            "plotname": str,
            "variables": [{"index": int, "name": str, "type": str}, ...],
            "points": [[v0, v1, ...], ...],
        }

    ``variables[0]`` is always the sweep variable (``time`` for ``.tran``,
    frequency for ``.ac``, the swept source for ``.dc``). Raises
    :class:`SimError` if the file is not a recognisable ngspice ASCII
    rawfile.

    ``ac``/``sp`` analyses declare ``Flags: complex`` in the header, and
    every value in the ``Values:`` section is then written as a
    comma-separated ``real,imag`` pair rather than a bare float (verified
    against ngspice 46). Per the JSON contract's additive-only convention,
    ``points[]`` always holds plain real-valued floats -- for a ``Flags:
    complex`` rawfile, each column's magnitude (``hypot(real, imag)``) is
    emitted instead of the raw pair, applied uniformly across all columns
    including the sweep variable (whose imaginary part is always ``0``, so
    its magnitude equals its real part). See ``docs/cli/sim.md``'s waveform
    JSON shape section for the documented representation.
    """
    with _open_ascii_rawfile(raw_path) as (plotname, variables, points):
        return {"plotname": plotname, "variables": variables, "points": list(points)}


def _note_rawfile_header_field(meta: dict[str, Any], line: str) -> None:
    """Record the first ``Plotname:`` / ``Flags:`` / ``No. Variables:`` header
    line seen into ``meta`` (later duplicates are ignored)."""
    if meta["plotname"] is None and line.startswith("Plotname:"):
        meta["plotname"] = line[len("Plotname:") :].strip()
    elif meta["is_complex"] is None and line.startswith("Flags:"):
        meta["is_complex"] = "complex" in line.lower()
    elif meta["var_count"] is None:
        count_match = re.match(r"No\. Variables:\s*(\d+)$", line)
        if count_match is not None:
            meta["var_count"] = int(count_match.group(1))


def _scan_rawfile_header(
    handle: IO[str],
) -> tuple[dict[str, Any], bool, bool, list[str]]:
    """Read ``handle`` up to the ``Values:`` marker.

    Returns ``(meta, in_variables, saw_values, variable_lines)``.
    """
    meta: dict[str, Any] = {"plotname": None, "is_complex": None, "var_count": None}
    in_variables = False
    saw_values = False
    variable_lines: list[str] = []
    try:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            if line == "Values:":
                saw_values = True
                break
            if in_variables:
                variable_lines.append(line)
            elif line == "Variables:":
                in_variables = True
            else:
                _note_rawfile_header_field(meta, line)
    except OSError as exc:
        raise SimError(f"could not read rawfile: {exc}") from exc
    return meta, in_variables, saw_values, variable_lines


def _parse_rawfile_variables(
    variable_lines: list[str], var_count: int
) -> list[dict[str, Any]]:
    """Parse the ``Variables:`` table lines, checking the declared count."""
    variables: list[dict[str, Any]] = []
    for line in variable_lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        variables.append({"index": int(parts[0]), "name": parts[1], "type": parts[2]})
    if len(variables) != var_count:
        raise SimError(
            f"malformed rawfile: declared {var_count} variables, "
            f"parsed {len(variables)}"
        )
    return variables


def _parse_rawfile_value(token: str, complex_values: bool) -> float:
    if not complex_values:
        return float(token)
    # "real,imag" -- reduce to a single real-valued magnitude, applied
    # uniformly to every column (including the sweep variable, whose
    # imaginary part is always 0 so its magnitude equals its real part).
    real_str, _, imag_str = token.partition(",")
    real = float(real_str)
    imag = float(imag_str) if imag_str else 0.0
    return math.hypot(real, imag)


def _iter_rawfile_points(
    handle: IO[str], var_count: int, complex_values: bool
) -> Iterator[list[float]]:
    """Lazily yield one ``list[float]`` per point from the ``Values:`` body."""
    current: list[float] = []
    for raw_line in handle:
        tokens = raw_line.split()
        if not tokens:
            continue
        if len(tokens) == 2 and current == []:
            # "<point_index> <value0>" -- start of a new point.
            current = [_parse_rawfile_value(tokens[1], complex_values)]
        elif len(tokens) == 1:
            current.append(_parse_rawfile_value(tokens[0], complex_values))
        else:
            continue
        if len(current) == var_count:
            yield current
            current = []


@contextlib.contextmanager
def _open_ascii_rawfile(
    raw_path: str,
) -> Iterator[tuple[str, list[dict[str, Any]], Iterator[list[float]]]]:
    """Open an ngspice ASCII rawfile and yield ``(plotname, variables,
    points)``, where ``points`` lazily yields one ``list[float]`` per point.

    The header is read line by line up to the ``Values:`` marker, so a
    non-rawfile or malformed header raises :class:`SimError` before any point
    is produced, and peak memory does not scale with the size of the
    ``Values:`` section (issue #3360). See :func:`parse_ascii_rawfile` for the
    format notes (``Flags: complex`` magnitude reduction in particular).
    """
    try:
        handle = open(raw_path, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SimError(f"could not read rawfile: {exc}") from exc
    with handle:
        meta, in_variables, saw_values, variable_lines = _scan_rawfile_header(handle)
        plotname = meta["plotname"]
        var_count = meta["var_count"]
        if plotname is None:
            raise SimError(f"not a recognisable ngspice rawfile: {raw_path}")
        if var_count is None:
            raise SimError(f"malformed rawfile (no variable count): {raw_path}")
        if not (in_variables and saw_values):
            raise SimError(
                f"malformed rawfile (no Variables/Values section): {raw_path}"
            )
        variables = _parse_rawfile_variables(variable_lines, var_count)
        points = _iter_rawfile_points(handle, var_count, bool(meta["is_complex"]))
        yield plotname, variables, points


def _parse_and_persist_waveform(raw_path: str, decimate: int | None = None) -> str:
    """Parse the rawfile at ``raw_path`` to the waveform JSON shape and write
    it alongside as ``<raw_path>.json``, returning that path.

    Kept as its own step (rather than inlining waveform data into the
    response) so the JSON contract's own response never grows to
    waveform-shaped size -- per the spike, waveform data is an optional
    *artifact*, referenced by path, not inlined.
    """
    json_path = raw_path + ".json"
    # Streamed point by point (issue #3360) so peak memory does not scale with
    # the rawfile; the output is byte-identical to `json.dump` of the dict
    # `parse_ascii_rawfile` returns.
    try:
        with _open_ascii_rawfile(raw_path) as (plotname, variables, points):
            with open(json_path, "w", encoding="utf-8") as handle:
                handle.write('{"plotname": ' + json.dumps(plotname))
                handle.write(', "variables": ' + json.dumps(variables))
                handle.write(', "points": [')
                sim_waveform_opts.write_points(handle, points, decimate)
                handle.write("]}")
    except (SimError, ValueError):
        # Do not leave a truncated artifact behind a failed parse.
        with contextlib.suppress(OSError):
            os.remove(json_path)
        raise
    return json_path

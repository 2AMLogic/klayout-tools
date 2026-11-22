"""Verify the frequency grid in a capture-only ngspice AC corner."""

from __future__ import annotations

import math
import re

from . import _spice_number, sim_raw

DATA_ROWS_RE = re.compile(r"No\. of Data Rows\s*:\s*(\d+)", re.IGNORECASE)
_SCALE_LENGTH_RE = re.compile(
    r"^[ \t]*frequency[ \t]*:[ \t]*frequency, complex, (\d+) long\b", re.MULTILINE
)


def _increments(
    mode: str, count: int, start: float, stop: float
) -> tuple[float, float]:
    """The additive step and multiplier used by ngspice's ACan."""
    if mode == "lin":
        # ngspice 46/47 emit just the start for both lin 1 and lin 2.
        return ((stop - start) / (count - 1) if count > 2 else 0.0), 1.0
    if mode == "dec" and stop / 10 >= start:
        span = stop / start
        intervals = math.floor(math.log10(span) * count)
        if intervals < 1:
            raise ValueError("indeterminate decade step")
        return 0.0, math.exp(math.log(span) / intervals)
    if mode == "dec" and start == stop:
        return 0.0, 1.0
    base = 10 if mode == "dec" else 2
    return 0.0, math.exp(math.log(base) / count)


def _parse_grid(args: str) -> tuple[str, int, float, float]:
    mode, count_token, start_token, stop_token = args.lower().split()
    if not count_token.removeprefix("+").isdecimal():
        raise ValueError("invalid point count")
    count = int(count_token)
    start = _spice_number.parse(start_token)
    stop = _spice_number.parse(stop_token)
    if mode not in {"lin", "dec", "oct"} or not 1 <= count <= 2**31 - 1:
        raise ValueError("unrecognized sweep")
    if start is None or stop is None or not (0 <= start <= stop < math.inf):
        raise ValueError("invalid bounds")
    if mode != "lin" and start == 0:
        raise ValueError("logarithmic sweeps require a positive start")
    return mode, count, start, stop


def _next_frequency(
    raw: sim_raw.ComplexRawfile, expected: float, step: float, ratio: float
) -> float | None:
    """Check every serialized frequency against the engine's recurrence."""
    previous = -math.inf
    for point in raw.points:
        frequency = point[0]
        following = expected * ratio + step
        # Allow ASCII rounding without accepting a neighboring grid point.
        tolerance = min(abs(expected) * 1e-9, abs(following - expected) / 4)
        if frequency.imag or not previous < frequency.real < math.inf:
            return None
        if not math.isclose(frequency.real, expected, rel_tol=1e-15, abs_tol=tolerance):
            return None
        previous, expected = frequency.real, following
    return expected


def _matches_grid(raw: sim_raw.ComplexRawfile, args: str, log_text: str) -> bool:
    mode, count, start, stop = _parse_grid(args)
    step, ratio = _increments(mode, count, start, stop)
    following = _next_frequency(raw, start, step, ratio)
    if following is None:
        return False
    rows = DATA_ROWS_RE.findall(log_text)
    counts = _SCALE_LENGTH_RE.findall(log_text)
    if len(rows) == 1:
        counts.extend(rows)
    if any(int(count) != len(raw.points) for count in counts):
        return False
    if mode == "lin":
        return len(raw.points) == (count if step else 1)
    if ratio == 1:
        return len(raw.points) == 1
    # The last logarithmic point depends on ngspice's effective RELTOL. Its
    # original frequency-vector length identifies the exact endpoint even
    # when the generic analysis row reports are missing or ambiguous.
    return bool(counts) and following > stop


def complete(raw_path: str, args: str, log_text: str | None) -> bool:
    """True only for a decoded AC plot covering the requested frequency grid."""
    try:
        raw = sim_raw.read_complex_ascii_rawfile(raw_path)
        if raw.plotname.lower() != "ac analysis" or raw.names[:1] != ["frequency"]:
            return False
        return bool(raw.points) and _matches_grid(raw, args, log_text or "")
    except (ValueError, OverflowError):
        # Malformed rawfiles and extents for which no grid can be established.
        return False

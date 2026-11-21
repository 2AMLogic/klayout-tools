"""Touchstone 1.1 ``.s2p`` text from raw two-port S-parameter arrays.

Shared by ``klt mom`` (:func:`klayout_tools.mom.write_touchstone_s2p`) and
``klt sim``'s ``analysis.kind: "twoport"`` (issue #3338), so both emit the
identical layout. Follows the Touchstone(R) File Format Specification, Rev 1.1
(IBIS Open Forum, 2002-04-24): option line ``# HZ S RI R <z0>`` (frequency in
Hz, S-parameters, real/imaginary pairs -- no lossy magnitude/phase round-trip)
and, for two ports, the data order ``<freq> S11 S21 S12 S22``.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Touchstone spec version the writer targets.
TOUCHSTONE_VERSION = "1.1"


def _touchstone_float(value: float) -> str:
    """Render ``value`` losslessly: ``repr()`` is the shortest decimal string
    that round-trips a Python ``float`` exactly."""
    return repr(float(value))


def format_s2p(
    freqs_hz: Sequence[float],
    s11: Sequence[complex],
    s21: Sequence[complex],
    s12: Sequence[complex],
    s22: Sequence[complex],
    z0: float,
    comments: Sequence[str] = (),
) -> str:
    """Touchstone 1.1 ``.s2p`` text for one reference impedance ``z0`` shared
    by both ports. ``comments`` become leading ``! ...`` lines."""
    lines = [f"! {comment}" if comment else "!" for comment in comments]
    lines.append(f"# HZ S RI R {_touchstone_float(z0)}")
    for freq, a, c, b, d in zip(freqs_hz, s11, s21, s12, s22, strict=True):
        fields = [freq, a.real, a.imag, c.real, c.imag, b.real, b.imag, d.real, d.imag]
        lines.append(" ".join(_touchstone_float(f) for f in fields))
    return "\n".join(lines) + "\n"

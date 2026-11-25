"""``artifacts.waveform_spectrum``: a noise run's per-frequency spectrum (#7866).

``options.waveforms`` on an ``analysis.kind: "noise"`` request writes ngspice's
*current* plot, and after ``noise`` that is the integrated one (``noise2``:
``onoise_total``/``inoise_total``, a single point; issue #2893). The spectrum
plot (``noise1``: ``frequency``, ``inoise_spectrum``, ``onoise_spectrum``, one
point per swept frequency) was therefore unreachable as an artifact: a caller
that must keep the whole spectrum as evidence could only declare one scalar
measurement per bin.

This module adds the spectrum as a **sibling** artifact rather than widening
``waveform.raw`` / ``waveform.raw.json``. Those files are one plot each
(``plotname`` / ``variables`` / ``points``), and a second plot inside them
would break every consumer of that shape, ``noise_representation`` included.
Nor can the two plots share one rawfile: ``set appendwrite`` would produce a
two-plot file whose second header :func:`klayout_tools.sim_raw.parse_ascii_rawfile`
never reads.

On a ``noise`` corner with ``options.waveforms`` the deck therefore also dumps
``noise1`` to ``waveform_spectrum.raw`` beside ``waveform.raw``, and the run
converts it to ``waveform_spectrum.raw.json`` -- the same JSON shape,
``options.waveform_max_bytes`` guard and ``options.waveform_decimate`` stride
as the main artifact -- reported as ``artifacts.waveform_spectrum``. The dump
mirrors the ``noise_mechanism.raw`` one in
:mod:`klayout_tools.sim_noise_mechanism`: it moves ngspice's current plot and
puts it back, so every later line (the ``expr`` lets, the mechanism dump) still
reads the totals plot it always did.

The artifact rides the ordinary per-corner artifact directory, so the ``batch``
and ``remote`` backends return it through the collection and path rewriting
every other artifact uses. A runner that predates this module simply never
writes it; see "Noise spectrum" in ``docs/cli/sim.md`` for why that is not a
refused feature.
"""

from __future__ import annotations

import os
from typing import Any

from .sim_errors import SimError
from .sim_raw import persist_waveform_with_header

#: The additive ``corners[].artifacts`` key holding the path of
#: ``waveform_spectrum.raw.json``.
ARTIFACT_KEY = "waveform_spectrum"

#: The spectrum rawfile's name, written beside ``waveform.raw``.
RAW_NAME = "waveform_spectrum.raw"

#: ``sim.WAVEFORM_TOO_LARGE_CODE`` (issue #3211): the spectrum shares the main
#: artifact's size-guard diagnostic rather than adding a code of its own.
TOO_LARGE_CODE = "waveform_too_large"

#: Holds the plot that is current when the dump starts. Distinct from
#: ``sim_noise_mechanism``'s variable so neither group depends on the other.
_PLOT_VARIABLE = "__klt_nzw_plot"


def applies(analysis: dict[str, Any], raw_path: str | None) -> bool:
    """Whether this corner dumps a spectrum: a ``noise`` analysis whose
    ``waveform.raw`` was requested (``raw_path`` is None without
    ``options.waveforms``)."""
    return raw_path is not None and analysis.get("kind") == "noise"


def raw_path_for(raw_path: str) -> str:
    """Where the spectrum rawfile lands: beside the corner's ``waveform.raw``."""
    return os.path.join(os.path.dirname(raw_path), RAW_NAME)


def control_lines(analysis: dict[str, Any], raw_path: str | None) -> list[str]:
    """The ``.control`` lines that dump ``noise1`` to :func:`raw_path_for`.

    Empty unless :func:`applies`, so a non-``noise`` deck and a ``noise`` deck
    without ``options.waveforms`` stay byte-identical. The current plot is
    captured via ``$curplot`` rather than hard-coded and restored afterwards,
    which is what lets the caller emit this group straight after the main
    ``write`` without moving the plot the ``expr`` lines read.
    """
    if not applies(analysis, raw_path):
        return []
    assert raw_path is not None  # `applies` is False for None
    return [
        f"set {_PLOT_VARIABLE} = $curplot",
        "setplot noise1",
        f"write {raw_path_for(raw_path)}",
        f"setplot ${_PLOT_VARIABLE}",
    ]


def persist(
    analysis: dict[str, Any],
    raw_path: str | None,
    max_bytes: int | None,
    decimate: int | None,
    diagnostics: list[dict[str, Any]],
) -> dict[str, str | None]:
    """The ``artifacts`` entry for the spectrum, or ``{}`` when this corner
    dumps none (see :func:`applies`).

    The value is the path of ``waveform_spectrum.raw.json``, or ``None`` when
    there is none -- the rawfile was not written, was over ``max_bytes``, or
    could not be parsed -- with a warning diagnostic saying which. All three
    are warnings and never touch ``status``: the spectrum is an optional
    artifact, like the main waveform's own conversion failures.
    """
    if not applies(analysis, raw_path):
        return {}
    assert raw_path is not None
    spectrum = raw_path_for(raw_path)
    if not os.path.isfile(spectrum):
        diagnostics.append(
            {
                "severity": "warning",
                "code": "unknown",
                "message": (
                    f"{RAW_NAME} was not written, so artifacts.{ARTIFACT_KEY} "
                    "is null (ngspice produced no `noise1` plot to dump)"
                ),
            }
        )
        return {ARTIFACT_KEY: None}
    size = os.path.getsize(spectrum)
    if max_bytes is not None and size > max_bytes:
        diagnostics.append(
            {
                "severity": "warning",
                "code": TOO_LARGE_CODE,
                "message": (
                    f"{RAW_NAME} is {size} bytes, over "
                    f"options.waveform_max_bytes ({max_bytes}); skipped the "
                    f"{RAW_NAME}.json conversion (the raw file is kept beside "
                    "artifacts.raw, measurements are unaffected). Narrow the "
                    "noise sweep (points per decade or frequency range), drop "
                    "the trailing pts_per_summary argument, or raise/disable "
                    "options.waveform_max_bytes"
                ),
            }
        )
        return {ARTIFACT_KEY: None}
    try:
        path, _header = persist_waveform_with_header(spectrum, decimate)
    except (SimError, ValueError) as exc:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "unknown",
                "message": f"could not parse {RAW_NAME}: {exc}",
            }
        )
        return {ARTIFACT_KEY: None}
    return {ARTIFACT_KEY: path}

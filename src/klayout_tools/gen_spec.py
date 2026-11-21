"""``_GeneratorSpec``: per-generator knowledge for the ``klt gen`` harness.

Split out of ``gen.py`` (file-size ratchet) so generator modules can define
their own registry entry without importing ``gen.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _GeneratorSpec:
    """Generator-specific knowledge the generic harness (:func:`_produce`)
    doesn't have: sanity-check bounds, how to describe the produced
    geometry as the response envelope's ``device_count``/``ports``/
    ``drc_hints`` fields, and how to resolve its hidden layer params against
    a resolved PDK (see :func:`_device_layer_params` and friends).
    """

    name: str
    summary: str
    #: Fallback output database unit (um) -- used only when the resolved
    #: PDK's own tech LEF declares no ``DATABASE MICRONS`` value (or ships
    #: no readable tech LEF at all), since :func:`_produce` prefers
    #: :func:`~klayout_tools.pdk.resolve_pdk_dbu`'s PDK-derived answer
    #: (issue #1496). Every registered generator declares ``0.001``, the dbu
    #: `klt gen` unconditionally wrote at before that change.
    dbu: float
    validate: Callable[[dict[str, Any]], None]
    describe: Callable[[dict[str, Any], float, dict[str, Any]], dict[str, Any]]
    layer_params: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    #: Flatten the PCell variant into the top cell (``asap7_finfet``: ``klt
    #: extract``'s FinFET path finds no devices in a hierarchical stream).
    flatten_output: bool = False

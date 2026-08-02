"""Shared layout-loading preamble for ``klt`` library modules.

Every ``*_report``/``run_*`` entry point that reads a GDSII/OASIS stream via
``klayout.db`` (``layers.py``, ``stats.py``, ``cells.py``, ``drc.py``) starts
with the same three checks: does the path exist, is it a file (not a
directory), and does ``klayout.db`` accept it as a recognisable stream. This
module is the single place that logic lives, so a future fix (e.g. a better
read-error message) only needs to land once.

The per-verb exception classes (``LayersError``, ``StatsError``, ...) stay in
their own modules -- they are importable API and let callers write
verb-specific ``except`` clauses -- and are passed in here as ``error_cls``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def load_layout(path: str, error_cls: type[Exception]) -> kdb.Layout:
    """Validate ``path`` and read it into a fresh ``klayout.db.Layout``.

    KLayout auto-detects the stream format on read, so both ``.gds`` and
    ``.oas`` inputs are handled by the same code path.

    Raises ``error_cls`` (constructed with a single message string) if the
    file is missing, is a directory, or is not a recognisable layout stream.
    """
    if not os.path.exists(path):
        raise error_cls(f"file not found: {path}")
    if os.path.isdir(path):
        raise error_cls(f"not a file: {path}")

    # Imported lazily so that `klt --version` and argument parsing do not pay
    # the cost of loading the KLayout database module.
    import klayout.db as kdb

    layout = kdb.Layout()
    try:
        layout.read(path)
    except Exception as exc:  # klayout raises RuntimeError for bad/unknown streams
        raise error_cls(f"could not read layout '{path}': {exc}") from exc

    return layout


def deterministic_save_options() -> kdb.SaveLayoutOptions:
    """Return ``SaveLayoutOptions`` that make written streams byte-reproducible.

    By default KLayout stamps the current wall-clock time into the GDSII
    ``BGNLIB`` (0x0102) and ``BGNSTR`` (0x0502) timestamp records, so two runs
    of the same generator produce byte-different streams even when the geometry
    is identical. That breaks the "regenerate and byte-diff against a committed
    golden artefact" regression pattern and makes generated GDS unusable as a
    content-addressed / cache-keyed build input.

    Setting ``gds2_write_timestamps = False`` writes all-zero timestamp fields
    (the standard convention for generated GDSII), which is deterministic. The
    flag is GDS2-specific and harmless for other formats KLayout may infer from
    the output path (e.g. OASIS), so callers can pass these options
    unconditionally.
    """
    import klayout.db as kdb

    options = kdb.SaveLayoutOptions()
    options.gds2_write_timestamps = False
    return options


def write_layout(layout: kdb.Layout, path: str, error_cls: type[Exception]) -> None:
    """Write ``layout`` to ``path`` with deterministic (zeroed) timestamps.

    Wraps ``kdb.Layout.write`` with :func:`deterministic_save_options` so every
    ``klt`` verb that emits a stream produces byte-reproducible output. Raises
    ``error_cls`` (constructed with a single message string) on any write error
    (bad format, unwritable path), matching :func:`load_layout`.
    """
    try:
        layout.write(path, deterministic_save_options())
    except Exception as exc:  # klayout raises RuntimeError for bad formats/paths
        raise error_cls(f"could not write output '{path}': {exc}") from exc

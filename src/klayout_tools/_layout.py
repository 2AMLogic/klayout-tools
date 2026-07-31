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

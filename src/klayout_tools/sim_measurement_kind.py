"""Shared per-kind measurement primitives for the ``klt sim`` sidecar forms.

Each ``measurements[]`` form module (:mod:`klayout_tools.sim_crossings`,
:mod:`klayout_tools.sim_energy`, ...) declares one form and carries the same
small primitives around it: a one-key "is this entry declared with my form"
test, the rawfile path its ``.control`` block writes, the filter-and-index
loop that reads those rawfiles back per corner, the ``analysis.kind`` gate
its context validation opens with, and (for the forms whose responses index
by name) the duplicate-measurement-name guard. Those are identical modulo
one string -- the form key, the rawfile prefix, the diagnostic noun -- so
they live here instead of once per form module. The forms differ in what
they *compute* from a measurement, not in how entries are declared, numbered,
or read back.

Every parameter (``key=``, ``prefix=``, ``form=``) names the **caller's own
form**, so a path or diagnostic reads exactly as the per-kind module wrote
it -- the same convention :mod:`klayout_tools.sim_window` established.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, TypeVar

OutcomeT = TypeVar("OutcomeT")


def declares(spec: dict[str, Any], key: str) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``key``."""
    return spec.get(key) is not None


def rawfile_path(corner_dir: str, prefix: str, index: int) -> str:
    """Where measurement number ``index`` of the ``prefix`` kind writes its
    ASCII rawfile -- the on-disk name its ``.control`` ``write`` line and
    its ``extract_corner_*`` read-back must agree on."""
    return os.path.join(corner_dir, f"{prefix}_{index}.raw")


def extract_indexed(
    measurements_spec: list[dict[str, Any]],
    corner_dir: str,
    *,
    prefix: str,
    predicate: Callable[[dict[str, Any]], bool],
    extractor: Callable[[dict[str, Any], str], OutcomeT],
) -> dict[int, OutcomeT]:
    """``{position in measurements_spec: outcome}`` for every entry matching
    ``predicate`` of one finished corner. Matching entries are numbered in
    declaration order -- the order the kind's ``.control`` block wrote its
    rawfiles -- and ``extractor(spec, raw_path)`` reduces each."""
    outcomes: dict[int, OutcomeT] = {}
    index = 0
    for position, spec in enumerate(measurements_spec):
        if not predicate(spec):
            continue
        outcomes[position] = extractor(spec, rawfile_path(corner_dir, prefix, index))
        index += 1
    return outcomes

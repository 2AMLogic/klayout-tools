"""Replay of a committed DRC report's own recorded run choices, for
``klt drc --check --rerun``.

The selectors a ``--rules`` subset ran (issue #2768), the abutment ``tile``
context (issue #4226) and -- added for issue #6986 -- the PDK variant the
report resolved into ``provenance.pdk`` are all choices the *committed
report itself* carries, replayed by
:func:`klayout_tools.drc.rerun_drc_report` so the fresh run reproduces the
original invocation instead of re-deriving a different one. Split out of
:mod:`klayout_tools.drc` (joining :mod:`.drc_report_check`) so that module
stays under its per-file size ceiling; imported function-level there, the
same deferred-import relation ``drc`` already has with
:mod:`.drc_klayout_engine`.
"""

from __future__ import annotations

import os
from typing import Any

from .drc import DrcError, parse_rule_selectors


def _recorded_rule_selectors(committed: dict[str, Any]) -> list[str] | None:
    """The ``--rules`` selectors a committed curated report was run with
    (issue #2768), or ``None`` for a full-deck report (no ``rule_selection``
    key -- including every report predating the flag).

    Raises :class:`DrcError` for a present but malformed block rather than
    silently replaying it as a full run, which would turn a recorded subset
    into a full-deck claim.
    """
    selection = committed.get("rule_selection")
    if selection is None:
        return None
    selectors = selection.get("selectors") if isinstance(selection, dict) else None
    if (
        not isinstance(selectors, list)
        or not selectors
        or not all(isinstance(token, str) and token.strip() for token in selectors)
    ):
        raise DrcError(
            "committed report's 'rule_selection.selectors' must be a non-empty "
            "list of rule selector strings to replay the selection"
        )
    return parse_rule_selectors(selectors)


def _recorded_tile(committed: dict[str, Any]) -> tuple[int, int] | None:
    """The ``(columns, rows)`` a committed report's ``tile`` echo records
    (issue #4226), or ``None`` for a report that carries none."""
    recorded = committed.get("tile")
    if not isinstance(recorded, dict):
        return None
    cols, rows = recorded.get("columns"), recorded.get("rows")
    if isinstance(cols, int) and isinstance(rows, int):
        return cols, rows
    return None


def _rerun_pdk_selection(
    committed: dict[str, Any],
    pdk_variant: str | None,
    pdk_root: str | None,
) -> tuple[str | None, str | None]:
    """The PDK selection a ``--rerun`` resolves ``provenance.pdk`` with
    (issue #6986): the explicit ``--pdk``/``--pdk-root`` flags first, then
    ``$PDK``, then the variant name the committed report itself recorded --
    or ``(None, None)`` when the report records no PDK and no flag was
    given, so a null-PDK report re-runs with ``provenance.pdk: null``
    exactly as it was produced (even on a host with ``$PDK`` set).

    Materialising the variant here (rather than letting the resolver
    consult ``$PDK`` itself) is what makes the recorded name a *fallback*:
    the engine runners skip PDK resolution entirely when both selection
    args are ``None``, so a report that records a PDK must pass a concrete
    variant down for the fresh run to resolve anything at all. The root is
    left to the resolver's own ``--pdk-root``/``$PDK_ROOT``/search-order
    handling in every case.
    """
    recorded = committed.get("provenance", {}).get("pdk")
    recorded_name = recorded.get("name") if isinstance(recorded, dict) else None
    if pdk_variant is None and pdk_root is None and recorded_name is None:
        return None, None
    if pdk_variant is not None:
        return pdk_variant, pdk_root
    return (os.environ.get("PDK") or recorded_name), pdk_root

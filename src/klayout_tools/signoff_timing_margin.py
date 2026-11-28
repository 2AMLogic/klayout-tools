"""The full-custom digital timing-margin rule of ``klt signoff`` item 5
(issue #4314).

A ``sim``-only digital item 5 must evidence a measured per-corner timing
margin: a ``timing_margin: true`` spec row is satisfied only by a cited
``klt sim`` envelope whose rollup ``measurements[]`` carries a passing,
``limits``-bearing entry tagged with the row and whose every ``corners[]``
entry has a passing measurement of that name.

Moved verbatim from :mod:`.signoff` (issue #7807) to pay the size-ratchet
offset for the per-citation ``requires`` hooks (the same reason the joint-grid
helpers moved out for #6969); ``signoff._grade_timing_margin`` and
``signoff._row_margin_lacking_corners`` keep resolving through the re-import
there, so no caller changes.
"""

from __future__ import annotations

from typing import Any

from .signoff_derived_rows import _recognised_bound
from .signoff_unmet_reasons import _REASON_TIMING_MARGIN_MISSING


def _grade_timing_margin(
    required_kind_pair: tuple[str, ...] | None,
    graded: list[tuple[Any, Any, Any]],
    spec_rows: list[dict[str, Any]] | None,
    met_envelopes: dict[int, dict[str, Any]] | None,
    row_info: dict[str, Any],
    extra: dict[str, Any],
) -> tuple[str, str | None, None, dict[str, Any], dict[str, Any]] | None:
    """The ``unmet``/``timing_margin_missing`` outcome for the full-custom
    digital sub-case (``sim`` evidence only, issue #4314); ``None`` when it
    does not apply or the timing margin is satisfied."""
    if required_kind_pair is None or not graded:
        return None
    kinds = {g[2]["kind"] for _, _, g in graded if g[2] is not None}
    # Issue #4859: a `model-run` is sim-like here -- it alone neither
    # satisfies the STA+functional pair nor escapes the timing-margin rule.
    if not kinds or not kinds <= {"sim", "model-run"}:
        return None
    missing_margin = _missing_timing_margin(
        spec_rows,
        [(met_envelopes or {}).get(id(spec)) for spec, _, _ in graded],
    )
    if not missing_margin:
        return None
    return (
        "unmet",
        _REASON_TIMING_MARGIN_MISSING,
        None,
        {**row_info, "missing_timing_margin": missing_margin},
        extra,
    )


def _missing_timing_margin(
    spec_rows: list[dict[str, Any]] | None,
    envelopes: list[dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """``[{"row": id, "corners": [...]}]`` for the timing-margin evidence a
    ``sim``-only digital item 5 lacks (issue #4314); empty when satisfied.

    A ``timing_margin: true`` row is satisfied by a cited ``klt sim``
    envelope whose rollup ``measurements[]`` has a passing, ``limits``-bearing
    entry tagged ``spec_row: <row>`` and whose every ``corners[]`` entry has a
    passing measurement of that name. No such row declared -> one entry with
    ``row: None``. ``corners`` names the corners lacking the measurement
    (``[]`` when no corner list exists or no tagged measurement was found).
    """
    rows = [r["id"] for r in spec_rows or [] if r.get("timing_margin")]
    if not rows:
        return [{"row": None, "corners": []}]
    out: list[dict[str, Any]] = []
    for row in rows:
        best: list[str] | None = None
        for envelope in envelopes:
            lacking = _row_margin_lacking_corners(envelope, row)
            if best is None or len(lacking) < len(best):
                best = lacking
        if best is None or best:
            out.append({"row": row, "corners": best or []})
    return out


def _row_margin_lacking_corners(envelope: dict[str, Any] | None, row: str) -> list[str]:
    """Corner ids lacking a passing measurement for ``row`` in ``envelope``;
    ``["*"]`` when the envelope has no passing, limited, ``row``-tagged
    measurement at all."""
    if envelope is None:
        return ["*"]
    raw = envelope.get("measurements")
    names = [
        m.get("name")
        for m in (raw if isinstance(raw, list) else [])
        if isinstance(m, dict)
        and m.get("spec_row") == row
        and m.get("status") == "pass"
        and _recognised_bound(m)
    ]
    if not names:
        return ["*"]
    corners = envelope.get("corners")
    corners = (
        [c for c in corners if isinstance(c, dict)] if isinstance(corners, list) else []
    )
    best: list[str] | None = None
    for name in names:
        lacking = [
            str(c.get("corner_id"))
            for c in corners
            if not any(
                isinstance(m, dict)
                and m.get("name") == name
                and m.get("status") == "pass"
                for m in c.get("measurements") or []
            )
        ]
        if best is None or len(lacking) < len(best):
            best = lacking
    return best or []

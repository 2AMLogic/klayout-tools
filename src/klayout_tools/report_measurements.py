"""``measurements``-kind section builder for :mod:`klayout_tools.report`.

Split out of ``report.py`` because that module sits against its 500-line
hard limit (``scripts/check_file_size_baseline.py``) with no baseline debt
entry allowed (issue #5839): only the ``_classify`` branch and the
``_build_section`` dispatch entry live there; this module owns everything
the section renders.

One structural kind covers **both** measurement producers -- ``klt sim``
(``schema_version=3``; ``measurements[]`` + ``corner_count``) and
``klt yield`` (``schema_version=1``; ``measurements[]`` + ``campaign``) --
because a top-level ``measurements`` list is the single structural marker
they share, the rendered table is the same shape, and keying on
``corner_count`` vs ``campaign`` would be a per-verb field by another name.
Detection itself lives in ``report.py``'s ``_classify``, keeping the
"structural, never per-verb" rule in one place.

This module is a pure **consumer**: every cell quotes the producer
envelope's own rollup fields verbatim -- ``worst_case.{value, corner_id,
margin}`` and ``yield.empirical.{estimate, confidence_interval,
confidence}`` -- and never re-derives an extreme or a yield, mirroring how
``signoff.py`` quotes ``klt yield``'s own rollups instead of recomputing
them (``signoff.py``'s ``_yield_campaign_disclosure``). Number formatting
follows the producers' own text conventions: ``repr`` for worst-case
values/margins (exactly what ``klt sim``'s text summary prints,
``cli/sim_measurement_text.py``), ``.6g`` for limits bounds (``klt
yield``'s ``_fmt``) each labelled with its operator -- ``>=``/``<=``, or
``>``/``<`` when ``exclusive_min``/``exclusive_max`` is set, as ``klt
yield``'s own ``limits:`` line does -- and ``.6f`` for yield estimates and
confidence-interval bounds (``klt yield``'s empirical line). The numeric
formatting never raises: a malformed non-numeric value is quoted
verbatim (``repr``), so the ``klt report`` no-traceback guarantee holds.

Absent cells render as ``-`` -- the same absent marker both producers'
text output uses -- never as a literal ``null``/``0``: a measurement with
``limits: null`` has no declared bounds and its ``worst_case`` is *not an
extreme* (every margin is ``null``, see ``docs/cli/sim.md``'s
``measurements`` rollup row), so its Limits/Margin cells must read as
absent, not as zero; a ``worst_case.value`` of ``null`` (an errored or
absent measurement, issues #2925/#2492) is likewise absent, not zero.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .env_provenance import render_path_field

__all__ = ["is_measurements_rollup", "section_measurements"]

#: The absent-cell marker, matching the ``-`` both producers' own text
#: output renders for a missing number (``klt sim``'s ``worst=-``;
#: ``klt yield``'s ``_fmt``).
_ABSENT = "-"


def is_measurements_rollup(measurements: list[Any]) -> bool:
    """Whether a top-level ``measurements`` list is a *graded rollup* this
    module can render, as opposed to some other verb's list that happens to
    share the key.

    The structural marker is the per-entry graded verdict: at least one
    entry carries a string ``status`` -- the very field the table's final
    column quotes. Both rollup producers always emit it (``klt sim``:
    ``measurements[].status``; ``klt yield``: ``measurements[].status``,
    ``docs/cli/yield.md``), while ``klt yield-sensitivity``'s
    ``measurements[]`` (a per-measurement parameter ranking, not graded
    against any limits) carries none -- so it is refused as an unrecognized
    shape rather than rendered as a table whose every evidence cell reads
    ``-``, which would look like "measured, found nothing".

    Deliberately *not* ``worst_case``: that is a ``klt sim`` field only
    (``klt yield`` entries carry ``yield.empirical`` instead, and a sim
    entry's ``worst_case`` is ``null`` for an all-absent measurement), so
    keying on it would refuse the yield reports this kind exists for. Like
    the rest of detection it is structural -- it never names
    ``corner_count`` or ``campaign``.

    An empty list classifies as a rollup: there is nothing to misrender, and
    it renders the honest "No measurements found." message.
    """
    if not measurements:
        return True
    return any(
        isinstance(m, dict) and isinstance(m.get("status"), str) for m in measurements
    )


_BASE_HEADERS = (
    "Measurement",
    "Unit",
    "Limits",
    "Worst",
    "Margin",
    "Corner",
    "Status",
)


def section_measurements(envelope: dict[str, Any], source: str) -> dict[str, Any]:
    """Build the one ``measurements``-kind section for ``envelope``.

    Same normalized-section shape every ``report.py`` builder returns
    (``kind``/``source``/``title``/``status``/``item_count``/``info_lines``/
    ``table_headers``/``table_rows``/``empty_message``); the shared
    ``_render`` in ``report.py`` turns it into the text/markdown renderings.
    """
    raw = envelope.get("measurements") or []
    # Same per-entry tolerance signoff.py's campaign disclosure applies:
    # entries are dicts by contract; anything else in the list is skipped
    # rather than crashing the render mid-table.
    measurements = [m for m in raw if isinstance(m, dict)]

    title = "Measurements"
    netlist = _rendered_netlist(envelope.get("netlist"))
    if netlist:
        title += f": {netlist}"

    with_yield = any(_empirical(m) is not None for m in measurements)
    rows = [_row(m, with_yield) for m in measurements]
    headers = _BASE_HEADERS + (("Yield", "Yield CI") if with_yield else ())

    return {
        "kind": "measurements",
        "source": source,
        "title": title,
        "status": envelope.get("status"),
        "item_count": len(measurements),
        "info_lines": ([f"Netlist: {netlist}"] if netlist else []),
        "table_headers": headers,
        "table_rows": rows,
        "empty_message": "No measurements found.",
    }


def _row(m: dict[str, Any], with_yield: bool) -> tuple[str, ...]:
    worst = m.get("worst_case")
    if not isinstance(worst, dict):
        worst = {}
    row = (
        _text(m.get("name")),
        _text(m.get("unit")),
        _limits(m.get("limits")),
        _number(worst.get("value")),
        _number(worst.get("margin")),
        _text(worst.get("corner_id")),
        _text(m.get("status")),
    )
    if not with_yield:
        return row
    emp = _empirical(m)
    if emp is None:
        return row + (_ABSENT, _ABSENT)
    ci = emp.get("confidence_interval")
    ci_text = _ABSENT
    if (
        isinstance(ci, dict)
        and ci.get("low") is not None
        and ci.get("high") is not None
    ):
        ci_text = f"[{_fixed(ci['low'], '.6f')}, {_fixed(ci['high'], '.6f')}]"
        confidence = emp.get("confidence")
        if confidence is not None:
            ci_text += f" @ {_fixed(confidence, '.0%')}"
    return row + (_fixed(emp.get("estimate"), ".6f"), ci_text)


def _empirical(m: dict[str, Any]) -> dict[str, Any] | None:
    """The entry's ``yield.empirical`` block, or ``None`` when this entry
    carries no empirical yield figure (a sim rollup never does; a yield
    rollup does unless it errored) -- the marker deciding whether the two
    yield columns exist for the whole table."""
    y = m.get("yield")
    if isinstance(y, dict):
        emp = y.get("empirical")
        if isinstance(emp, dict):
            return emp
    return None


def _fixed(value: Any, spec: str) -> str:
    """``format(value, spec)`` for one producer number, never raising.

    ``None`` is the absent marker. A value the format spec cannot render (a
    producer-contract violation, e.g. a string where a number belongs) is
    quoted verbatim via ``repr`` -- visibly wrong in the table, rather than
    a traceback (``docs/cli/report.md``'s no-traceback guarantee) and rather
    than ``-``, which would read as "no value declared"."""
    if value is None:
        return _ABSENT
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return repr(value)


#: ``limits`` keys in render order, with the operator each bound carries:
#: ``(key, inclusive operator, exclusive operator)`` -- the same labelling
#: ``klt yield``'s own text line prints (``cli/yield_cmd.py``:
#: ``min(>=)=...`` / ``max(<=)=...``, ``>``/``<`` when ``exclusive_min`` /
#: ``exclusive_max`` is set).
_BOUNDS = (("min", ">=", ">"), ("max", "<=", "<"))


def _limits(limits: Any) -> str:
    """A ``limits`` block rendered with each bound's identity and
    inclusivity -- ``>=30`` (floor), ``<=10`` (ceiling), ``>=1e+06,
    <=2e+09`` (window), ``>-3, <3`` (strict bounds) -- numbers ``.6g``
    (``klt yield``'s own formatting). ``-`` when absent, never ``0``:
    ``limits: null`` means the producer declared no bounds at all.

    The operator is what makes the number checkable: a bare ``10`` cannot
    say whether ``iq_ua`` has a 10 uA ceiling or floor, and one-sided limits
    are the common shape."""
    if not isinstance(limits, dict):
        return _ABSENT
    bounds = []
    for key, inclusive, exclusive in _BOUNDS:
        if limits.get(key) is None:
            continue
        op = exclusive if limits.get(f"exclusive_{key}") else inclusive
        bounds.append(op + _fixed(limits[key], ".6g"))
    return ", ".join(bounds) if bounds else _ABSENT


def _number(value: Any) -> str:
    """One quoted rollup number: ``repr`` -- the same verbatim rendering
    ``klt sim``'s text summary prints for ``worst_case`` values and margins
    -- or ``-`` when absent (``null``: errored/absent measurement, or every
    margin of a no-limits measurement)."""
    return _ABSENT if value is None else repr(value)


def _text(value: Any) -> str:
    return _ABSENT if value is None else str(value)


def _rendered_netlist(value: Any) -> str | None:
    """A cited netlist input path, rendered for a human report -- ``None``
    when there is nothing worth naming. Same tolerance
    ``report.py``'s ``_rendered_input_path`` defines (a ``{path, scope}``
    object through the shared :func:`~klayout_tools.env_provenance.render_path_field`,
    a bare pre-#1261 path string verbatim); duplicated here rather than
    imported because ``report.py`` imports this module, so importing back
    would be a cycle."""
    if isinstance(value, Mapping):
        return render_path_field(value)
    if isinstance(value, str) and value:
        return value
    return None

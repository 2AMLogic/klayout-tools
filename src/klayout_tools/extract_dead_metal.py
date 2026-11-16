"""``klt extract``'s dead-metal reporting surface (issue #5545).

``_detect_dead_metal`` (in :mod:`klayout_tools.extract_parasitics`) does the
geometry: it returns one entry per routing-stack cluster that joins no
surviving net. This module owns everything the *report* then does with that
list -- the per-layer roll-up (:func:`summarize_dead_metal`), the single
aggregate ``warnings[]`` line it earns (:func:`dead_metal_warning`), and the
opt-in ``--omit-dead-metal`` compaction that drops the per-cluster rows
while keeping both (:func:`drop_dead_metal_rows`).

Kept out of ``extract.py``/``extract_parasitics.py`` deliberately: both are
already far over this repo's per-file size ratchet (see
``scripts/check_file_size_baseline.py``), and this is a self-contained piece
of pure data shaping with no KLayout dependency at all -- it only ever reads
``_detect_dead_metal``'s plain-dict output, which makes it directly unit
testable without a layout.

See ``docs/cli/extract.md``'s "Dead metal" section for the user-facing
contract, and ``run_extract``'s docstring for the response fields.
"""

from __future__ import annotations

from typing import Any

#: Decimal places every area in this module is rounded to -- the same
#: ``_PARAM_PRECISION_UM`` ``extract.py`` rounds ``dead_metal[].area_um2``
#: itself to, duplicated as a literal rather than imported so this module
#: stays free of the circular-import dance ``extract_parasitics.py`` needs
#: (``extract.py`` imports this module at its own top level).
_AREA_PRECISION_UM = 6


def summarize_dead_metal(dead_metal: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-layer totals over ``_detect_dead_metal``'s per-cluster list -- the
    always-present ``dead_metal_summary`` response field, and the only
    dead-metal detail left in an ``--omit-dead-metal`` report.

    Returns ``{"clusters": int, "shapes": int, "area_um2": number,
    "by_layer": [{"role", "layer", "datatype", "clusters", "shapes",
    "area_um2"}, ...]}``. ``by_layer`` is sorted by ``(layer, datatype,
    role)`` -- one entry per distinct ``(role, layer, datatype)`` the input
    mentions, so a deck that registered two roles on one stream layer reports
    each separately rather than silently merging them.

    The totals are plain sums over the input's own already-rounded
    ``shapes``/``area_um2`` values, re-rounded to
    :data:`_AREA_PRECISION_UM` so the field is a stable, diffable number
    rather than a float-summation artifact.

    An empty ``dead_metal`` gives the all-zero summary (``clusters``,
    ``shapes`` and ``area_um2`` all zero, ``by_layer`` empty) rather than
    ``None``: the field is present on every report, so a reader never has to
    branch on its absence.
    """
    by_key: dict[tuple[int, int, str], dict[str, Any]] = {}
    for entry in dead_metal:
        key = (entry["layer"], entry["datatype"], entry["role"])
        row = by_key.get(key)
        if row is None:
            row = by_key[key] = {
                "role": entry["role"],
                "layer": entry["layer"],
                "datatype": entry["datatype"],
                "clusters": 0,
                "shapes": 0,
                "area_um2": 0.0,
            }
        row["clusters"] += 1
        row["shapes"] += entry["shapes"]
        row["area_um2"] += entry["area_um2"]

    by_layer = [by_key[key] for key in sorted(by_key)]
    for row in by_layer:
        row["area_um2"] = round(row["area_um2"], _AREA_PRECISION_UM)
    return {
        "clusters": sum(row["clusters"] for row in by_layer),
        "shapes": sum(row["shapes"] for row in by_layer),
        "area_um2": round(sum(row["area_um2"] for row in by_layer), _AREA_PRECISION_UM),
        "by_layer": by_layer,
    }


def dead_metal_warning(dead_metal: list[dict[str, Any]], *, omitted: bool) -> str:
    """The single aggregate ``warnings[]`` entry a non-empty ``dead_metal``
    list earns (issue #599) -- one prose line carrying the cluster count,
    shape count and layer set, never one line per cluster.

    Built by a function rather than inlined at its one append site because
    ``--omit-dead-metal`` has to *rewrite* it after the fact:
    ``_extract_netlist`` appends the ``omitted=False`` form while it still
    holds the live ``LayoutToNetlist``, and ``run_extract`` -- the only place
    the flag is known -- swaps that exact string for the ``omitted=True``
    form. Regenerating both forms from the same list makes that an exact
    equality swap rather than a substring match on prose.

    ``omitted=True`` keeps the counts and layers byte-identical and changes
    only the pointer at the end: the per-cluster rows are gone from the
    response, so it names ``dead_metal_summary`` and says how to get the full
    list back.
    """
    cluster_word = "cluster" if len(dead_metal) == 1 else "clusters"
    total_shapes = sum(entry["shapes"] for entry in dead_metal)
    shape_word = "shape" if total_shapes == 1 else "shapes"
    layers_str = ", ".join(
        sorted({f"{entry['layer']}/{entry['datatype']}" for entry in dead_metal})
    )
    tail = (
        "per-cluster detail was omitted by --omit-dead-metal; see "
        "dead_metal_summary for the per-layer totals, and re-run without "
        "--omit-dead-metal for the per-cluster list."
        if omitted
        else "see dead_metal[] for the per-cluster layer/bbox/shape count."
    )
    return (
        f"{len(dead_metal)} routing-stack {cluster_word} ({total_shapes} "
        f"{shape_word} on {layers_str}) join no extracted net -- no via "
        "lands on this geometry and no same-layer wiring touches it, so "
        "it is invisible to the extracted netlist and to every downstream "
        "`klt lvs`/`klt sim` view; deliberate dead metal (artwork, fill) "
        "is expected here, unexplained dead metal usually means the "
        f"connection you intended is missing -- {tail}"
    )


def drop_dead_metal_rows(
    dead_metal: list[dict[str, Any]], warnings: list[str], omit: bool
) -> list[dict[str, Any]]:
    """Resolve ``--omit-dead-metal`` for one report: return the ``dead_metal``
    list it should emit, rewriting ``warnings`` **in place** when the flag
    drops the rows.

    Call :func:`summarize_dead_metal` on the *full* list first -- the whole
    point of the flag is that the counts a reader acts on survive -- then
    this, which replaces the rows with ``[]``. ``omit=False`` returns the
    list untouched and leaves ``warnings`` alone, so a run without the flag
    is byte-identical to one from before the flag existed.

    The warning swap is an exact-equality replacement against the string
    :func:`dead_metal_warning` itself produced inside ``_extract_netlist``,
    never a substring match on prose. An empty ``dead_metal`` appended no
    warning in the first place, so there is nothing to rewrite.
    """
    if not omit or not dead_metal:
        return dead_metal
    full = dead_metal_warning(dead_metal, omitted=False)
    rewritten = dead_metal_warning(dead_metal, omitted=True)
    warnings[:] = [rewritten if entry == full else entry for entry in warnings]
    return []

"""Run a DRC deck headless against a GDSII/OASIS stream.

Pure library: :func:`run_drc` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``layers.py``.

Headless invariant: uses only the pip ``klayout`` package's batch database
API (``klayout.db``) — specifically ``Region``'s native check primitives
(``width_check``, ``space_check``, ``separation_check``, ``enclosing_check``,
``enclosed_check``, ``notch_check``, ``overlap_check``) — no GUI, no Qt, and
no dependency on the standalone ``klayout`` application binary or its
DRC-DSL script runner. See ``docs/cli/drc.md`` for the engine-choice
rationale (native ``Region`` checks vs. running the official ``.lydrc``
deck through the full KLayout application).

Limitation: each rule is checked against the *whole layout*, flattened per
top cell (via ``Cell.begin_shapes_rec``, the same flattening idiom used
elsewhere in this repo) — there is no ``--top <cell>`` filter in this
version.
"""

from __future__ import annotations

import os
from typing import Any

from ._layout import load_layout
from .decks import DrcRule, UnknownDeckError, get_deck, get_layer_names

# Check kinds that operate on a single region (no other_layer).
_SINGLE_LAYER_CHECKS = {"width", "space", "notch"}
# Check kinds that compare a region against another region (other_layer required).
_TWO_LAYER_CHECKS = {"separation", "enclosing", "enclosed", "overlap"}


class DrcError(Exception):
    """Raised when a layout cannot be checked: bad file, unknown deck, or a
    malformed rule.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def run_drc(path: str, deck_name: str) -> dict[str, Any]:
    """Run ``deck_name``'s rules against the layout at ``path``.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/drc.md``)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "deck": <deck name>,
            "dbu_um": <database unit in micrometres, float>,
            "status": "clean" | "violations",
            "violation_count": <int>,
            "rule_counts": {<rule id>: <int>, ...},
            "violations": [
                {
                    "rule": str, "description": str, "check": str,
                    "layer": str, "cell": str,
                    "bbox": {"left": int, "bottom": int, "right": int, "top": int},
                    "polygon": [[x, y], ...] | None,
                },
                ...
            ],
        }

    ``schema_version`` is versioned independently per command (see
    ``docs/json-contract.md``); it starts at ``1`` and only increments when
    this command's JSON shape changes in a way that isn't purely additive.

    ``violations`` is sorted by ``(rule, cell, bbox.left, bbox.bottom)`` for
    deterministic, diff-clean output across repeated runs on the same input.

    Raises :class:`DrcError` if the file is missing/unreadable, the deck
    name is unknown, or a rule is malformed (e.g. a two-layer check missing
    ``other_layer``).
    """
    # Checked here (ahead of deck lookup) so a missing/bad path is reported
    # before an unknown deck name, matching this command's historical error
    # precedence; load_layout() repeats this cheap check before the read.
    if not os.path.exists(path):
        raise DrcError(f"file not found: {path}")
    if os.path.isdir(path):
        raise DrcError(f"not a file: {path}")

    try:
        deck = get_deck(deck_name)
    except UnknownDeckError as exc:
        raise DrcError(str(exc)) from exc
    layer_names = get_layer_names(deck_name)

    layout = load_layout(path, DrcError)

    # Imported lazily (after load_layout, which already paid this cost) for
    # kdb.Region() below.
    import klayout.db as kdb

    top_cells = list(layout.top_cells())

    violations: list[dict[str, Any]] = []
    rule_counts: dict[str, int] = {}

    for rule in deck:
        layer_index = layout.find_layer(*rule.layer)
        if layer_index is None:
            continue  # rule's layer is absent from this stream -> no violations
        other_index = None
        if rule.other_layer is not None:
            other_index = layout.find_layer(*rule.other_layer)
            if other_index is None:
                continue

        layer_label = layer_names.get(rule.layer, f"{rule.layer[0]}/{rule.layer[1]}")

        for cell in top_cells:
            region = kdb.Region(cell.begin_shapes_rec(layer_index))
            other_region = (
                kdb.Region(cell.begin_shapes_rec(other_index))
                if other_index is not None
                else None
            )

            edge_pairs = _run_check(region, other_region, rule)

            for edge_pair in edge_pairs:
                bbox = edge_pair.bbox()
                try:
                    polygon = edge_pair.polygon(0)
                    points = [[pt.x, pt.y] for pt in polygon.each_point_hull()]
                except Exception:
                    points = None

                violations.append(
                    {
                        "rule": rule.id,
                        "description": rule.description,
                        "check": rule.check,
                        "layer": layer_label,
                        "cell": cell.name,
                        "bbox": {
                            "left": bbox.left,
                            "bottom": bbox.bottom,
                            "right": bbox.right,
                            "top": bbox.top,
                        },
                        "polygon": points,
                    }
                )
                rule_counts[rule.id] = rule_counts.get(rule.id, 0) + 1

    violations.sort(
        key=lambda v: (v["rule"], v["cell"], v["bbox"]["left"], v["bbox"]["bottom"])
    )

    return {
        "schema_version": 1,
        "file": path,
        "deck": deck_name,
        "dbu_um": layout.dbu,
        "status": "violations" if violations else "clean",
        "violation_count": len(violations),
        "rule_counts": dict(sorted(rule_counts.items())),
        "violations": violations,
    }


def _run_check(region: Any, other_region: Any | None, rule: DrcRule) -> Any:
    """Dispatch to the matching ``klayout.db.Region.*_check`` primitive.

    Returns an ``EdgePairs`` collection, one entry per violation.
    """
    check = rule.check
    d = rule.threshold_dbu

    if check in _SINGLE_LAYER_CHECKS:
        return getattr(region, f"{check}_check")(d)

    if check in _TWO_LAYER_CHECKS:
        if other_region is None:
            raise DrcError(f"rule '{rule.id}': check '{check}' requires other_layer")
        return getattr(region, f"{check}_check")(other_region, d)

    raise DrcError(f"rule '{rule.id}': unsupported check kind '{check}'")

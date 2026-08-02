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
from ._provenance import build_provenance
from .decks import (
    DrcRule,
    UnknownDeckError,
    deck_source_path,
    get_deck,
    get_layer_names,
    get_nominal_dbu,
)
from .layers import layers_report

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
            "coverage": {
                "deck_layers": ["<layer>/<datatype>", ...],
                "layers_checked": ["<layer>/<datatype>", ...],
                "layers_in_stream_without_rules": ["<layer>/<datatype>", ...],
                "rules_skipped": [<rule id>, ...],
            },
            "provenance": {  # shared reproducibility block, see _provenance.py
                "klt_version": <str | None>,
                "klayout_version": <str | None>,
                "pdk": None,  # klt drc resolves no PDK
                "deck": {"name": <deck name>, "content_hash": "sha256:..."},
            },
        }

    ``schema_version`` is versioned independently per command (see
    ``docs/json-contract.md``); it starts at ``1`` and only increments when
    this command's JSON shape changes in a way that isn't purely additive.

    ``violations`` is sorted by
    ``(rule, cell, bbox.left, bbox.bottom, bbox.right, bbox.top)`` for
    deterministic, diff-clean output. The full bbox is included in the key so
    violations that share a corner are still totally ordered, keeping output
    canonical across platforms/KLayout builds regardless of the engine's
    internal shape-enumeration order.

    ``coverage`` reports what was actually checked, purely additive to the
    JSON contract (see ``docs/json-contract.md``; no ``schema_version``
    bump). A rule whose ``layer``/``other_layer`` is absent from ``path``
    is silently skipped by the engine (``layout.find_layer(...)`` returns
    ``None``) -- correct behaviour, but by itself indistinguishable from a
    genuinely-checked pass. ``coverage.layers_in_stream_without_rules`` is
    the load-bearing field: ``(layer, datatype)`` pairs present in ``path``
    that no active rule in ``deck_name`` references at all, formatted
    ``"<layer>/<datatype>"``. ``coverage.deck_layers`` is every layer the
    deck's rules reference (a static property of the deck, independent of
    ``path``); ``coverage.layers_checked`` is the subset of those layers
    actually present in this stream; ``coverage.rules_skipped`` lists the
    rule ids skipped because their layer(s) were absent. A ``"clean"``
    ``status`` with a non-empty ``layers_in_stream_without_rules`` means
    "clean, and here is exactly what was not looked at" rather than a
    fully-verified pass.

    Every ``DrcRule.threshold_dbu`` in ``deck_name`` is authored against that
    deck's nominal dbu (see :func:`klayout_tools.decks.get_nominal_dbu`), not
    against whatever dbu ``path`` happens to use. Before any
    ``Region.*_check()`` runs, thresholds are rescaled by
    ``nominal_dbu_um / layout.dbu`` so the same physical geometry produces
    identical violations regardless of the input stream's database unit.

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
        nominal_dbu_um = get_nominal_dbu(deck_name)
    except UnknownDeckError as exc:
        raise DrcError(str(exc)) from exc
    layer_names = get_layer_names(deck_name)

    layout = load_layout(path, DrcError)

    # `deck`'s threshold_dbu values are authored against nominal_dbu_um, not
    # necessarily this layout's own dbu (see DrcRule's docstring / #172) —
    # rescale so the same physical distance is checked regardless of the
    # stream's database unit.
    dbu_scale = nominal_dbu_um / layout.dbu

    # Imported lazily (after load_layout, which already paid this cost) for
    # kdb.Region() below.
    import klayout.db as kdb

    top_cells = list(layout.top_cells())

    # Deck-static: every (layer, datatype) any rule in this deck references,
    # regardless of what's actually present in `path`.
    deck_layer_tuples: set[tuple[int, int]] = set()
    for rule in deck:
        deck_layer_tuples.add(rule.layer)
        if rule.other_layer is not None:
            deck_layer_tuples.add(rule.other_layer)

    # Reuse layers.py's existing per-layer enumeration (used today by
    # `klt layers`) for stream-layer enumeration, rather than a second
    # `kdb.Layout` layer walk -- see #189.
    stream_layer_tuples = {
        (entry["layer"], entry["datatype"]) for entry in layers_report(path)["layers"]
    }

    violations: list[dict[str, Any]] = []
    rule_counts: dict[str, int] = {}
    rules_skipped: list[str] = []

    for rule in deck:
        layer_index = layout.find_layer(*rule.layer)
        if layer_index is None:
            # rule's layer is absent from this stream -> no violations, but
            # record it so `coverage.rules_skipped` can surface the skip.
            rules_skipped.append(rule.id)
            continue
        other_index = None
        if rule.other_layer is not None:
            other_index = layout.find_layer(*rule.other_layer)
            if other_index is None:
                rules_skipped.append(rule.id)
                continue

        layer_label = layer_names.get(rule.layer, f"{rule.layer[0]}/{rule.layer[1]}")

        for cell in top_cells:
            region = kdb.Region(cell.begin_shapes_rec(layer_index))
            other_region = (
                kdb.Region(cell.begin_shapes_rec(other_index))
                if other_index is not None
                else None
            )

            edge_pairs = _run_check(region, other_region, rule, dbu_scale)

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
        key=lambda v: (
            v["rule"],
            v["cell"],
            v["bbox"]["left"],
            v["bbox"]["bottom"],
            v["bbox"]["right"],
            v["bbox"]["top"],
        )
    )

    def _fmt(layer_tuple: tuple[int, int]) -> str:
        return f"{layer_tuple[0]}/{layer_tuple[1]}"

    layers_checked = deck_layer_tuples & stream_layer_tuples
    layers_in_stream_without_rules = stream_layer_tuples - deck_layer_tuples

    coverage = {
        "deck_layers": [_fmt(t) for t in sorted(deck_layer_tuples)],
        "layers_checked": [_fmt(t) for t in sorted(layers_checked)],
        "layers_in_stream_without_rules": [
            _fmt(t) for t in sorted(layers_in_stream_without_rules)
        ],
        "rules_skipped": sorted(rules_skipped),
    }

    return {
        "schema_version": 1,
        "file": path,
        "deck": deck_name,
        "dbu_um": layout.dbu,
        "status": "violations" if violations else "clean",
        "violation_count": len(violations),
        "rule_counts": dict(sorted(rule_counts.items())),
        "violations": violations,
        "coverage": coverage,
        "provenance": build_provenance(
            deck_name=deck_name, deck_path=deck_source_path(deck_name)
        ),
    }


def _run_check(
    region: Any, other_region: Any | None, rule: DrcRule, dbu_scale: float
) -> Any:
    """Dispatch to the matching ``klayout.db.Region.*_check`` primitive.

    ``dbu_scale`` (``deck's nominal_dbu_um / layout.dbu``, see
    :func:`run_drc`) rescales ``rule.threshold_dbu`` from the deck's
    authored, nominal database unit to the layout's actual one, rounding to
    the nearest whole dbu since ``Region.*_check()`` thresholds are integer
    database units.

    Returns an ``EdgePairs`` collection, one entry per violation.
    """
    check = rule.check
    d = round(rule.threshold_dbu * dbu_scale)

    if check in _SINGLE_LAYER_CHECKS:
        return getattr(region, f"{check}_check")(d)

    if check in _TWO_LAYER_CHECKS:
        if other_region is None:
            raise DrcError(f"rule '{rule.id}': check '{check}' requires other_layer")
        return getattr(region, f"{check}_check")(other_region, d)

    raise DrcError(f"rule '{rule.id}': unsupported check kind '{check}'")

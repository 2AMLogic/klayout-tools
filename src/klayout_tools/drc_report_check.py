"""Hash checks behind ``klt drc --check`` (issues #1106, #4986).

Split out of :mod:`klayout_tools.drc` to keep that module under its per-file
size ceiling; :func:`klayout_tools.drc.check_drc_report` is the public entry.
"""

from __future__ import annotations

from typing import Any

from ._provenance import content_hash
from ._report_verify import get_path, hash_check, resolve_committed_path
from .decks import deck_source_path


def _deck_path_for_report(committed: dict[str, Any]) -> str | None:
    """The on-disk deck path a committed ``klt drc`` report's own ``deck``
    field names, for re-hashing (issue #1106).

    ``--engine curated`` reports store the deck *name* (``"sky130"``, ...)
    under ``deck`` -- resolved to a source-module path via
    :func:`deck_source_path`, exactly as :func:`run_drc` itself does when it
    first builds ``provenance.deck.content_hash``. ``--engine klayout``
    reports (``committed["engine"] == "klayout"``) instead store the deck
    *script's own path* directly under ``deck`` (see
    :func:`run_drc_klayout_engine`'s docstring -- there is no separate short
    name for an arbitrary PDK-native script), so it is used as-is.
    """
    deck = committed.get("deck")
    if deck is None:
        return None
    if committed.get("engine") == "klayout":
        return deck
    return deck_source_path(deck)


def _report_hash_checks(
    committed: dict[str, Any],
    *,
    report_path: str,
    input_path: str | None,
    prefix: str,
) -> list[dict[str, Any]]:
    """The per-report hash checks behind :func:`check_drc_report`, for one
    single-engine report (``prefix`` is prepended to each ``field``; empty for
    a top-level report, ``reports.<engine>.`` for a crosscheck side)."""
    checks = [
        hash_check(
            f"{prefix}provenance.input.content_hash",
            get_path(committed, ("provenance", "input", "content_hash")),
            content_hash(
                resolve_committed_path(
                    committed.get("file"),
                    report_path=report_path,
                    override=input_path,
                )
            ),
        ),
        hash_check(
            f"{prefix}provenance.deck.content_hash",
            get_path(committed, ("provenance", "deck", "content_hash")),
            content_hash(_deck_path_for_report(committed)),
        ),
    ]
    # Issue #3483: a waived report also pins its waivers file.
    waivers_file = get_path(committed, ("provenance", "waivers", "file"))
    if waivers_file is not None:
        checks.append(
            hash_check(
                f"{prefix}provenance.waivers.content_hash",
                get_path(committed, ("provenance", "waivers", "content_hash")),
                content_hash(waivers_file),
            )
        )
    recorded_tree = get_path(committed, ("provenance", "deck", "tree", "content_hash"))
    if recorded_tree is not None:
        deck_path = _deck_path_for_report(committed)
        from .drc_klayout_engine import _deck_tree_identity

        current_tree = _deck_tree_identity(deck_path) if deck_path else None
        checks.append(
            hash_check(
                f"{prefix}provenance.deck.tree.content_hash",
                recorded_tree,
                current_tree["content_hash"] if current_tree else None,
            )
        )
    return checks

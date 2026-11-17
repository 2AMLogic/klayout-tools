"""Hash checks behind ``klt drc --check`` (issues #1106, #4986).

Split out of :mod:`klayout_tools.drc` to keep that module under its per-file
size ceiling; :func:`klayout_tools.drc.check_drc_report` is the public entry.
"""

from __future__ import annotations

import os
from typing import Any

from ._provenance import content_hash
from ._report_verify import get_path, hash_check, resolve_committed_path
from ._report_verify_hints import deck_not_found_entry
from .decks import deck_source_path


def _packaged_deck_path(name: Any) -> str | None:
    """The packaged ``klayout_tools.decks`` script a recorded deck *basename*
    names (issue #5709), or ``None``.

    Accepts only a plain ``.drc``/``.lydrc`` basename -- anything with a path
    separator or ``..`` is refused -- that exists as a file next to the deck
    modules. Resolution never decides a verdict: the caller still compares the
    resolved bytes against the recorded hash.
    """
    if not isinstance(name, str) or name in ("", ".", ".."):
        return None
    if os.path.basename(name) != name or "\\" in name or ".." in name:
        return None
    if not name.endswith((".drc", ".lydrc")):
        return None
    candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "decks", name)
    return candidate if os.path.isfile(candidate) else None


def resolve_report_deck(
    committed: dict[str, Any], deck_override: str | None = None
) -> tuple[str | None, dict[str, Any] | None]:
    """The deck path a committed ``klt drc`` report is verified against, plus
    a ``deck_not_found`` diagnostic when nothing resolves (issues #1106,
    #5709). One resolver for ``--check``, ``--check --rerun`` and both sides of
    an ``--engine both`` report.

    Order: (1) ``deck_override`` (``--deck-file``) wins -- for a
    ``klayout``-engine report only, so an ``--engine both`` envelope's
    curated side keeps its name-resolved deck and a curated single report
    ignores the flag, exactly as ``--rerun`` already rejects it -- and only
    when it names an existing file; a missing override is itself the
    ``deck_not_found`` diagnostic, never a silent ``None`` hash; (2) the
    report's own ``deck`` -- for ``--engine curated`` a deck *name*
    resolved to its module source via :func:`deck_source_path`, for
    ``--engine klayout`` the producing host's script path -- when it
    exists; (3) ``provenance.deck.name`` against the packaged
    ``klayout_tools.decks`` scripts (``klayout`` engine only). Returns
    ``(None, diagnostic)`` when none resolves; the check then fails
    (never skips).
    """
    if deck_override is not None and committed.get("engine") == "klayout":
        if os.path.isfile(deck_override):
            return deck_override, None
        return None, deck_not_found_entry(deck_override, None, override=True)
    deck = committed.get("deck")
    if deck is None:
        return None, None
    if committed.get("engine") != "klayout":
        return deck_source_path(deck), None
    if os.path.exists(deck):
        return deck, None
    name = get_path(committed, ("provenance", "deck", "name"))
    packaged = _packaged_deck_path(name)
    if packaged is not None:
        return packaged, None
    return None, deck_not_found_entry(deck, name)


def _report_hash_checks(
    committed: dict[str, Any],
    *,
    report_path: str,
    input_path: str | None,
    prefix: str,
    deck_path: str | None = None,
) -> list[dict[str, Any]]:
    """The per-report hash checks behind :func:`check_drc_report`, for one
    single-engine report (``prefix`` is prepended to each ``field``; empty for
    a top-level report, ``reports.<engine>.`` for a crosscheck side)."""
    resolved_deck, deck_missing = resolve_report_deck(committed, deck_path)
    deck_check = hash_check(
        f"{prefix}provenance.deck.content_hash",
        get_path(committed, ("provenance", "deck", "content_hash")),
        content_hash(resolved_deck),
    )
    if deck_missing is not None:
        deck_check["deck_not_found"] = deck_missing
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
        deck_check,
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
        from .drc_klayout_engine import _deck_tree_identity

        current_tree = _deck_tree_identity(resolved_deck) if resolved_deck else None
        checks.append(
            hash_check(
                f"{prefix}provenance.deck.tree.content_hash",
                recorded_tree,
                current_tree["content_hash"] if current_tree else None,
            )
        )
    return checks


def report_checks(
    committed: dict[str, Any],
    *,
    report_path: str,
    input_path: str | None,
    deck_path: str | None,
) -> list[dict[str, Any]]:
    """Every hash check behind ``klt drc --check`` for ``committed``.

    Issue #4986: an ``--engine both`` envelope nests each engine's own report
    under ``reports.<engine>``; each is checked with the same logic, fields
    prefixed with the engine. A null native side (engine failed / unavailable)
    is skipped, never reported as drift. Returns ``[]`` for a crosscheck
    envelope with no embedded report (the caller raises).
    """
    if committed.get("mode") != "crosscheck":
        return _report_hash_checks(
            committed,
            report_path=report_path,
            input_path=input_path,
            deck_path=deck_path,
            prefix="",
        )
    reports = committed.get("reports")
    reports = reports if isinstance(reports, dict) else {}
    checks: list[dict[str, Any]] = []
    for engine in ("curated", "native"):
        sub = reports.get(engine)
        if isinstance(sub, dict):
            checks.extend(
                _report_hash_checks(
                    sub,
                    report_path=report_path,
                    input_path=input_path,
                    deck_path=deck_path,
                    prefix=f"reports.{engine}.",
                )
            )
    return checks

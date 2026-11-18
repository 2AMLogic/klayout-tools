"""Hash checks behind ``klt drc --check`` (issues #1106, #4986), and the
``--check --rerun`` diff exclusions (issue #5660).

Split out of :mod:`klayout_tools.drc` to keep that module under its per-file
size ceiling; :func:`klayout_tools.drc.check_drc_report` is the public entry.
"""

from __future__ import annotations

import os
from typing import Any

from ._provenance import content_hash
from ._report_verify import (
    VOLATILE_PROVENANCE_PATHS,
    get_path,
    hash_check,
    strip_path_patterns,
)
from ._report_verify_hints import deck_not_found_entry, input_hash_check
from .decks import deck_source_path
from .evidence_canonicalize import rerun_result_for


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
    curated side keeps its name-resolved deck; a curated *single* report
    never reaches here with an override, because :func:`report_checks`
    rejects the combination exactly as ``--rerun`` does (issue #6070) -- and
    only when it names an existing file; a missing override is itself the
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
        # Issue #6466: an echoed `file` naming no existing file carries the
        # same additive `input_not_found` block `klt lvs --check` attaches.
        input_hash_check(
            f"{prefix}provenance.input.content_hash",
            get_path(committed, ("provenance", "input", "content_hash")),
            committed.get("file"),
            report_path,
            override=input_path,
            hasher=content_hash,
            override_hint="an absolute path as the positional <file> argument",
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
    prefixed with the engine. ``deck_path`` (``--deck-file``) on a curated
    single-engine report raises :class:`klayout_tools.drc.DrcError` (issue
    #6070). A null native side (engine failed / unavailable)
    is skipped, never reported as drift. Returns ``[]`` for a crosscheck
    envelope with no embedded report (the caller raises).
    """
    if committed.get("mode") != "crosscheck":
        if deck_path is not None and committed.get("engine") != "klayout":
            # Issue #6070: same contract as ``--check --rerun`` -- never a
            # silently ignored flag. (Function-local: drc imports this module.)
            from .drc import DrcError

            raise DrcError(
                "--deck-file cannot be combined with --check on a curated report"
            )
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


#: ``klt drc --check --rerun`` exclusions (issue #5660): the volatile
#: provenance fields plus the two surfaces an ``--engine klayout`` run now
#: always populates. The same rule as ``lvs._LVS_RERUN_EXCLUDE_PATHS`` -- "a
#: field a report never carried cannot itself have drifted".
#:
#: * ``environment.klayout_binary`` -- a host-local path (#2849 precedent): the
#:   same KLayout installed under another prefix is not a change in evidence.
#: * ``provenance.klayout_module_version`` -- the pip-wheel axis, volatile like
#:   its sibling ``provenance.klayout_version``.
DRC_RERUN_EXCLUDE_PATHS: frozenset[tuple[str, ...]] = VOLATILE_PROVENANCE_PATHS | {
    ("environment", "klayout_binary"),
    ("provenance", "klayout_module_version"),
}


def _rerun_diff_view(report: dict[str, Any]) -> dict[str, Any]:
    """``report`` minus :data:`DRC_RERUN_EXCLUDE_PATHS`, and minus an
    ``environment`` block those exclusions left empty.

    The second step is what makes a report committed before #5660 (no
    ``environment`` key at all) re-run as ``match``: path exclusion alone
    leaves ``{}`` on the fresh side against a *missing* key on the committed
    side, which the key-union diff reports as drift on ``environment`` itself.
    A block that still holds any other key is kept, so a future
    ``environment`` field stays evidence until someone excludes it.
    """
    view = strip_path_patterns(report, DRC_RERUN_EXCLUDE_PATHS)
    if view.get("environment") == {}:
        del view["environment"]
    return view


#: ``provenance.klayout_version_mismatch`` is computed from a different input on
#: each side of #5660: the pip wheel before it, the standalone binary after it
#: (``provenance.klayout_module_version`` is written exactly when the
#: binary-derived value is in force, so its presence marks the new definition).
_MISMATCH_PATH: frozenset[tuple[str, ...]] = frozenset(
    {("provenance", "klayout_version_mismatch")}
)


def _carries(report: dict[str, Any], key: str) -> bool:
    provenance = report.get("provenance")
    return isinstance(provenance, dict) and key in provenance


def _mismatch_redefined(committed: dict[str, Any], fresh: dict[str, Any]) -> bool:
    """Whether ``klayout_version_mismatch`` crosses the #5660 definition
    boundary: the committed report carries the flag but predates the change
    (no ``klayout_module_version``) while the fresh one is binary-derived.
    ``--engine curated`` reports never carry ``klayout_module_version`` on
    either side, so their wheel-derived flag keeps being compared; a committed
    report that never carried the flag has nothing to mark not-compared and
    keeps drifting on a fresh one that grew it, exactly as before."""
    return (
        _carries(fresh, "klayout_module_version")
        and not _carries(committed, "klayout_module_version")
        and _carries(committed, "klayout_version_mismatch")
    )


def drc_rerun_result(
    *, report_path: str, committed: dict[str, Any], fresh: dict[str, Any]
) -> dict[str, Any]:
    """``klt drc``'s ``--rerun`` payload: the ``drc`` entry of
    :data:`~klayout_tools.evidence_canonicalize._KIND_SOURCES` (the
    DRC-specific exclusions of :data:`DRC_RERUN_EXCLUDE_PATHS` and
    :func:`_rerun_diff_view`), so ``klt evidence canonicalize --kind drc``
    sees the same view (#5541). ``fresh`` is still embedded verbatim.

    Across the #5660 definition boundary the two reports' mismatch flags are
    not comparable (a divergent host flips ``true`` -> ``false`` with nothing
    in the evidence changed), so the fresh *view* drops the flag. The
    committed value then lands in ``not_compared`` through the #5866 rule
    (a provenance scalar the fresh side does not carry) -- auditable, and
    neither ``status`` nor the exit code reflects it. Between two post-#5660
    reports (or two curated ones) the flag is compared as before."""
    return rerun_result_for(
        "drc",
        report_path,
        committed=committed,
        fresh=fresh,
        fresh_view_exclude=(
            _MISMATCH_PATH if _mismatch_redefined(committed, fresh) else ()
        ),
    )

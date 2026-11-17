"""``klt drc --engine klayout --list-rule-categories`` (issue #5444).

The *assertion* side of native-deck coverage (``--expect-rule-categories``
/ ``--expect-rule-categories-file``, issues #2697/#2806) needs a category
list the caller established **independently of the layout being signed
off** -- "copying the accepted run's own categories is circular". Until this
module there was no tool-side way to establish one: a consumer had to read
the deck's own source and hand-enumerate every ``.output(<category>, ...)``
its control flow reaches under the chosen ``-rd`` switches, across the whole
``%include`` closure, with the names routinely built by loops over per-layer
tables rather than written as literals.

**How the set is derived: run the real deck on a tool-generated empty
layout.** KLayout's DRC DSL opens a report ``<category>`` the moment a rule
calls ``.output(...)``, whether or not the layer holds shapes -- so running
the deck itself, with the caller's ``--deck-var`` set, against a
single-cell geometry-free layout yields every category whose *declaration*
does not depend on measured geometry, with the ``%include`` closure, the
``-rd`` gates and the loop-generated names all resolved by KLayout rather
than by a re-implementation of the deck language here.

This is deliberately **not** the circular derivation the docs warn about:
the empty layout is tool-generated and has nothing to do with the layout
under sign-off, and the exact-set assertion then still fails closed on that
layout, naming each ``missing``/``unexpected`` category.

What it does *not* establish, and says so in every payload's
``limitations``:

- **Geometry-dependent declarations.** A deck that only declares a category
  inside a branch taken on a measured quantity (``if layer.area > ...``)
  never reaches that ``.output(...)`` on an empty layout, so the listing
  omits it. Verified empirically (KLayout 0.30.12, 2026-11-13) on a deck
  whose ``*.dens`` categories sat behind ``if lay.area > 100.0``: absent
  from the listing, while a ``-rd``-gated rule, a ``# %include``d file and
  loop-generated ``"M#{n}.a"`` names all listed correctly.
- **Switch-set dependence.** The listing is only valid for the
  ``--deck-var`` set it was made with -- the same way the assertion is.
- **Deck revision.** A listing pins no deck revision by itself; the
  payload's ``provenance.deck`` block (``content_hash`` plus the multi-file
  ``tree``) is what ties it to the deck it came from.

Everything here **fails closed**: a deck that errors, crashes or times out
on the empty layout, and a report that declares no categories at all, both
raise :class:`~klayout_tools.drc.DrcError` with nothing written to stdout --
never a short or empty list that would silently satisfy a weaker assertion
than the caller intended.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from ._provenance import build_provenance
from .drc import SCHEMA_VERSION, DrcError
from .drc_klayout_app import _preflight_klayout_version, _resolve_klayout_executable
from .drc_klayout_engine import (
    KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S,
    _check_klayout_run,
    _cleanup_klayout_drc_work_dir,
    _missing_deck_globals_warning,
    _parse_klayout_rdb_report,
    _preflight_deck_host_tools,
    _resolve_provenance_pdk,
    _with_deck_tree,
)
from .drc_klayout_launch import launch_klayout_deck

#: ``mode`` discriminator of the listing payload (``docs/json-contract.md``):
#: a listing, never a verdict -- it carries no ``status``/``violations``/
#: ``coverage``, so ``klt signoff`` refuses it as an unrecognised envelope
#: exactly the way it refuses a ``--list-rules`` listing.
LIST_RULE_CATEGORIES_MODE = "list_rule_categories"

#: ``basis``: how the set was derived. A stable token rather than prose, so a
#: consumer can tell this listing apart from any future derivation (a static
#: evaluator, a deck-declared manifest) without parsing ``limitations``.
LIST_RULE_CATEGORIES_BASIS = "empty_layout_run"

#: Database unit of the generated empty layout. Only used to satisfy
#: :func:`_parse_klayout_rdb_report`'s coordinate conversion -- a listing
#: reads category *names*, and an empty layout yields no items to convert.
_EMPTY_LAYOUT_DBU = 0.001

#: Top cell of the generated empty layout.
_EMPTY_LAYOUT_TOP_CELL = "KLT_EMPTY"

#: The fixed caveats every listing carries (never derived from the deck, so
#: a consumer can rely on them being present). Shaped like ``warnings[]``:
#: ``{category, text}``.
_LIMITATIONS: tuple[dict[str, str], ...] = (
    {
        "category": "geometry_dependent_declarations",
        "text": (
            "Categories a deck declares only inside a branch taken on "
            "measured geometry (e.g. `if layer.area > ...`) are not "
            "determinable from this listing: the deck ran against an empty "
            "layout, so such a branch was never taken and its category is "
            "absent. Feeding this listing to "
            "--expect-rule-categories-file still fails closed on the real "
            "layout, naming each such category as unexpected."
        ),
    },
    {
        "category": "deck_var_dependent",
        "text": (
            "This listing is valid only for the --deck-var set recorded in "
            "`deck_vars`. A variable that selects rule groups, a metal "
            "stack or feature switches changes the declared set."
        ),
    },
    {
        "category": "deck_revision_scoped",
        "text": (
            "This listing is valid only for the deck revision pinned in "
            "`provenance.deck` (content_hash, plus `tree` for a multi-file "
            "deck). Re-derive it when the deck changes."
        ),
    },
)


def _write_empty_layout(path: str) -> str:
    """Write a single-cell, geometry-free layout to ``path``.

    Tool-generated on purpose: it carries no shape from -- and so no
    dependence on -- the layout a caller is signing off, which is what makes
    the derived set non-circular evidence for the assertion flags.
    """
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = _EMPTY_LAYOUT_DBU
    layout.create_cell(_EMPTY_LAYOUT_TOP_CELL)
    layout.write(path)
    return path


def _not_listable(deck_file: str, exc: DrcError) -> DrcError:
    """Re-frame a run failure as "this deck could not be listed".

    Fail-closed path: the caller asked what a deck declares, and the honest
    answer when the deck did not complete is "unknown", never the partial
    report's shorter list. KLayout's own diagnostic output is carried
    through unchanged, and the original ``code`` is preserved so
    ``error.code`` still classifies the failure.
    """
    return DrcError(
        f"could not list rule categories for deck '{deck_file}': the deck "
        f"did not complete against a tool-generated empty layout. A deck "
        f"that cannot run on an empty layout cannot be listed this way -- "
        f"check the --deck-var set, or derive the list another way. "
        f"Underlying failure: {exc}",
        code=exc.code,
    )


def list_klayout_deck_rule_categories(
    deck_file: str,
    *,
    timeout_s: float = KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S,
    deck_vars: dict[str, str] | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    deck_name: str | None = None,
    allow_missing_host_tools: bool = False,
    min_klayout_version: str | None = None,
    klayout_binary: str | None = None,
) -> dict[str, Any]:
    """The rule categories ``deck_file`` declares under ``deck_vars``.

    Runs the deck against a tool-generated empty layout and reports the
    ``<category>`` names its report declares -- see this module's docstring
    for why that is the derivation, and what it does and does not establish.

    Returns a **listing**, not a verdict: ``mode``, the sorted
    ``rule_categories`` and their ``rule_category_count``, the resolved
    ``deck_file``, the effective ``deck_vars``, ``basis``, a non-empty
    ``limitations``, the same ``provenance`` block a run records, and --
    when the deck reads caller-supplied ``-rd`` globals and none were
    given -- the same ``warnings`` entry a run emits. No ``status``,
    ``violations`` or ``coverage``.

    Raises :class:`DrcError` -- so the CLI emits an error envelope and
    writes nothing to stdout -- when the deck file is missing, the host
    preflight or KLayout version floor fails, the deck errors, crashes or
    times out on the empty layout, or the report declares no category at
    all.
    """
    pdk_info = _resolve_provenance_pdk(pdk_variant, pdk_root, deck_name)

    if not os.path.isfile(deck_file):
        raise DrcError(f"deck file not found: {deck_file}")

    # Same two preflights a run does, in the same order: a vendor driver's
    # host assumptions and the application-version floor abort the deck
    # just as surely when it is being listed (issues #2333, #2689).
    _preflight_deck_host_tools(deck_file, allow_missing_host_tools)
    klayout_exe, klayout_override = _resolve_klayout_executable(klayout_binary)
    _preflight_klayout_version(klayout_exe, min_klayout_version)

    work_dir = tempfile.mkdtemp(prefix="klt-drc-list-categories-")
    try:
        report_path = os.path.join(work_dir, "report.lyrdb")
        input_path = _write_empty_layout(os.path.join(work_dir, "empty.gds"))
        try:
            completed = launch_klayout_deck(
                klayout_exe,
                deck_file,
                input_path=input_path,
                report_path=report_path,
                deck_vars=deck_vars,
                timeout_s=timeout_s,
            )
            # `allow_deck_errors=False`, always: a partial report's shorter
            # category list is exactly the silently-wrong manifest this
            # whole mode exists to stop a consumer hand-writing.
            _check_klayout_run(completed, report_path, allow_deck_errors=False)
        except DrcError as exc:
            raise _not_listable(deck_file, exc) from exc

        _, _, rule_categories = _parse_klayout_rdb_report(
            report_path, _EMPTY_LAYOUT_DBU
        )
    finally:
        _cleanup_klayout_drc_work_dir(work_dir)

    if not rule_categories:
        raise DrcError(
            f"could not list rule categories for deck '{deck_file}': the "
            "deck ran against a tool-generated empty layout but declared no "
            "rule category at all. Either every rule is gated behind a "
            "--deck-var this invocation did not set, or the deck declares "
            "its categories only when a violation exists -- neither can be "
            "listed this way. Run `klt pdk decks` to see the deck's globals."
        )

    effective_deck_vars = dict(deck_vars or {})
    deck_global_warnings = _missing_deck_globals_warning(deck_file, deck_vars)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": LIST_RULE_CATEGORIES_MODE,
        "engine": "klayout",
        "deck_file": deck_file,
        "deck_vars": effective_deck_vars,
        "basis": LIST_RULE_CATEGORIES_BASIS,
        # Additive (issue #4950), same shape a run records: present only
        # when --klayout-binary/$KLT_KLAYOUT_BINARY was in force.
        **(
            {"environment": {"klayout_binary": klayout_override}}
            if klayout_override is not None
            else {}
        ),
        "rule_category_count": len(rule_categories),
        "rule_categories": rule_categories,
        "limitations": [dict(entry) for entry in _LIMITATIONS],
        # Issue #3816: a wrong switch set is the one way this listing is
        # silently wrong in the same direction a run is, so the run's own
        # missing-globals warning is surfaced here too.
        **({"warnings": deck_global_warnings} if deck_global_warnings else {}),
        "provenance": _with_deck_tree(
            build_provenance(
                deck_name=os.path.basename(deck_file),
                deck_path=deck_file,
                pdk=pdk_info,
                deck_options=effective_deck_vars,
                include_klayout_version_mismatch=True,
            ),
            deck_file,
        ),
    }

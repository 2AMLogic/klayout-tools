"""The engine-free prefix of :func:`klayout_tools.extract.extract_netlist_from_layout`
(issue #5464), split out so ``klt pex --dry-run`` can run exactly the same
checks the real extraction runs without ever starting the extraction engine.

Everything here is cheap and side-effect-free: file existence, deck
resolution, the layout read, top-cell resolution, the FinFET option refusal
and the declared-pin-set requirement. Nothing writes, and no device or
connectivity extraction happens -- reading the stream to resolve ``--top`` is
a file read, not an extraction.

One helper, two callers: :func:`klayout_tools.extract.extract_netlist_from_layout`
(which then goes on to extract) and
:func:`klayout_tools.pex_dry_run.run_pex_dry_run` (which stops here). The
split exists so the two can never drift -- a dry run that re-implemented
these checks would report the same errors only by coincidence.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NamedTuple

from ._layout import resolve_top_cell
from .decks import (
    InvalidDeckOptionError,
    InvalidLabelLayerError,
    UnknownExtractionDeckError,
)
from .extract_progress import stage
from .extract_spef import ExtractError

if TYPE_CHECKING:  # pragma: no cover -- typing only
    import klayout.db as kdb

    from .decks import ExtractionDeck


class LayoutPreflight(NamedTuple):
    """What :func:`preflight_layout` resolved: the extraction ``deck``, the
    read-in ``layout``, and the resolved ``top_cell``."""

    deck: ExtractionDeck
    layout: kdb.Layout
    top_cell: kdb.Cell


def require_declared_set_for_rename(
    rename_declared_pins: bool,
    declared_pins: frozenset[str] | None,
    def_pins: frozenset[str] | None,
    flag: str = "--rename-declared-pins",
) -> None:
    """Issue #2854: ``--rename-declared-pins`` with neither ``--pins`` nor
    ``--def-pins`` is a clean :class:`ExtractError`, not a silent no-op --
    there is no declared set to rename *to*, and silently extracting an
    unrenameable netlist would defeat the flag's whole purpose (canonical
    pin names for ``klt lvs``'s ``options.anchor_top_level_pins``)."""
    if rename_declared_pins and declared_pins is None and def_pins is None:
        raise ExtractError(
            f"{flag} requires a declared pin set to rename "
            "to -- pass --pins <A,B,...> (or --def-pins <def>), the same "
            "set whose reconciliation survives the pins it names"
        )


def preflight_layout(
    path: str,
    deck_name: str,
    *,
    top: str | None = None,
    deck_options: Mapping[str, str] | None = None,
    label_layers: Mapping[str, tuple[int, int] | None] | None = None,
    refuse_options: Mapping[str, Any] | None = None,
) -> LayoutPreflight:
    """Resolve ``deck_name`` and ``path``'s top cell without extracting
    anything.

    ``refuse_options`` is the ``{flag: requested}`` mapping
    :func:`klayout_tools.extract_finfet.refuse_layout_options` grades -- a
    FinFET deck runs its own recogniser and implements none of the
    planar-only options, so they are refused by name instead of being
    half-applied (issue #2761). A caller that can set none of them passes
    ``None``.

    Raises :class:`ExtractError` for a missing/unreadable file, an unknown
    deck, an invalid ``deck_options``/``label_layers`` entry, or a
    missing/ambiguous top cell.
    """
    if not os.path.exists(path):
        raise ExtractError(f"file not found: {path}")
    if os.path.isdir(path):
        raise ExtractError(f"not a file: {path}")

    # Resolved through :mod:`klayout_tools.extract`'s own module attribute,
    # not a direct ``decks`` import: ``extract.get_extraction_deck`` is the
    # single seam the deck factory is reached through (the same late-bound
    # lookup ``extract_validate`` uses), so a caller that substitutes a deck
    # there sees it here too. Imported inside the function because `extract`
    # imports this module at load time.
    from . import extract as _extract

    try:
        deck = _extract.get_extraction_deck(deck_name, deck_options, label_layers)
    except (
        UnknownExtractionDeckError,
        InvalidDeckOptionError,
        InvalidLabelLayerError,
    ) as exc:
        raise ExtractError(str(exc)) from exc

    # Imported lazily (after the cheap checks above) so `klt --version` and
    # argument parsing never pay the cost of loading the KLayout database
    # module -- same discipline as `_layout.load_layout`.
    import klayout.db as kdb

    layout = kdb.Layout()
    try:
        with stage("read_layout"):
            layout.read(path)
    except Exception as exc:  # klayout raises RuntimeError for bad/unknown streams
        raise ExtractError(f"could not read layout '{path}': {exc}") from exc

    top_cell = resolve_top_cell(layout, top, ExtractError, path=path)

    # Issue #2761: a FinFET deck runs its own recogniser (see
    # `extract_finfet`), which implements none of the planar-only options
    # below -- refuse them by name instead of half-applying them.
    from .extract_finfet import refuse_layout_options

    refuse_layout_options(deck, deck_name, **dict(refuse_options or {}))

    return LayoutPreflight(deck, layout, top_cell)

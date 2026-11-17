"""The dbu a GDS/OASIS **stream** for a resolved PDK must be written at
(issue #5721), split out of :mod:`klayout_tools.pdk` for that module's
file-size ratchet (issue #2766).

This and :func:`klayout_tools.pdk.resolve_pdk_dbu` answer **different
questions**, and both are needed:

- :func:`klayout_tools.pdk.resolve_pdk_dbu` is the tech LEF's
  ``DATABASE MICRONS`` truth -- what a DEF *reader* config must use (a DEF's
  coordinates are in 1/2000-um units for gf180mcu; re-interpreting them at
  0.001 would move geometry), and the finest-grid answer for every family
  with no declared stream requirement.
- :func:`resolve_output_dbu` (here) is "what dbu a stream **for** this PDK
  must be written at": the family's entry in :data:`_REQUIRED_STREAM_DBU_UM`
  when one is declared, falling through to :func:`klayout_tools.pdk.
  resolve_pdk_dbu` otherwise. ``klt gen``/``klt gen-compose`` write at this
  value (issue #5721); ``klt place_and_route``'s DEF reading keeps
  ``resolve_pdk_dbu``.
"""

from __future__ import annotations

from typing import Any

from .pdk import resolve_pdk_dbu
from .pdk_families import pdk_variant_family

#: Per-PDK-family required **stream** dbu (um) -- the database unit a GDS/OASIS
#: stream *for that family* must be written at, not the dbu its tech LEF's
#: ``DATABASE MICRONS`` declares. The two genuinely disagree on gf180mcu: its
#: tech LEFs declare ``DATABASE MICRONS 2000`` (=> 0.0005, what
#: :func:`klayout_tools.pdk.resolve_pdk_dbu` reports), while the family's
#: native DRC runset enforces an exact-equality DBU rule --
#: ``libs.tech/klayout/tech/drc/rule_decks/geom.rb``'s
#: ``extent.output('DBU', ...) if dbu != 0.001`` -- so a stream at 0.0005 is
#: rejected whole-cell by the PDK's own deck (and by every downstream tool
#: that enforces it). Every GDS the gf180mcu install itself ships, and its
#: runset's rule, sit at 0.001; the LEF's 2000 is headroom nothing in the
#: install uses. Families with no declared requirement are absent from this
#: table and fall through to :func:`klayout_tools.pdk.resolve_pdk_dbu`.
#:
#: This is deliberately **not** :func:`~klayout_tools.decks.get_nominal_dbu`:
#: that answers "the dbu klt's *curated* deck transcribed its thresholds
#: against", not "the dbu the PDK's stream must be at" -- and it raises for
#: families whose packaged deck legitimately wants a finer dbu (asap7).
#: Keeping the two answers separate leaves the next PDK's requirement
#: expressible here without touching deck transcription.
_REQUIRED_STREAM_DBU_UM: dict[str, float] = {
    "gf180mcu": 0.001,
}


def resolve_output_dbu(pdk_info: dict[str, Any]) -> float | None:
    """Resolve the database unit (dbu, in micrometres) a GDS/OASIS stream
    **written for this PDK** must be at -- e.g. ``0.001`` for gf180mcu,
    whose native DRC runset rejects any other dbu with a whole-cell ``DBU``
    marker, even though its tech LEF's ``DATABASE MICRONS 2000`` resolves
    (via :func:`klayout_tools.pdk.resolve_pdk_dbu`) to ``0.0005``.

    Returns ``None`` -- never raises -- exactly when
    :func:`klayout_tools.pdk.resolve_pdk_dbu` itself would (no ``libs_ref``,
    no tech LEF, no parsable ``DATABASE MICRONS``) and the family declares no
    requirement; callers fall back to their own default, as they already do
    for ``resolve_pdk_dbu``.
    """
    family = pdk_variant_family(pdk_info.get("variant") or "")
    required = _REQUIRED_STREAM_DBU_UM.get(family)
    if required is not None:
        return required
    return resolve_pdk_dbu(pdk_info)

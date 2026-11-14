"""Self-diagnosing disclosure for a ``cell_library`` key that misses the
verified tie-cell table by a near miss (issue #5074).

Kept out of :mod:`synthesize` so the table itself
(:data:`synthesize._TIE_CELLS`) stays where every doc reference points,
while the hint is a pure function of ``(key, verified keys)`` -- no import
back into :mod:`synthesize`, and testable without a synthesis run.
"""

from __future__ import annotations

import difflib
from collections.abc import Collection, Container, Iterable

#: Similarity floor for :func:`tie_cell_keys_hint`'s "closest verified key"
#: lead. ``difflib``'s own default is 0.6; the near miss this exists for --
#: ``asap7sc7p5t_28_RVT`` vs ``asap7sc7p5t_28_R``, which scores 0.94 -- is
#: far above it, while an unrelated library name stays below.
TIE_CELL_HINT_CUTOFF = 0.6


def tie_cell_keys_hint(cell_library: str, verified_keys: Iterable[str]) -> str:
    """Suffix naming the ``cell_library`` keys that carry a verified
    tie-cell mapping, leading with the closest one to ``cell_library``.

    :data:`synthesize._TIE_CELLS` is looked up by **exact** string match,
    and a ``klt-pdk.json`` descriptor may map any name to a Liberty file
    (:func:`pdk_cells.resolve_liberty_for_cell_library`), so a key one
    character off a verified one -- ``asap7sc7p5t_28_RVT`` for
    ``asap7sc7p5t_28_R`` -- silently loses the ``setundef -zero``/``hilomap``
    pass and lets bare x constants reach a netlist ``klt place-and-route``
    then refuses. No table can be complete when keys are caller-chosen, so
    this discloses the accepted keys instead: a near miss then reads as a
    near miss rather than as a genuinely unsupported library.

    Advisory only -- it never substitutes a library or folds a flavour
    suffix (``TIEHIx1_ASAP7_75t_R`` *is* the RVT cell, so folding ``_LVT``
    onto ``_L`` would emit the wrong tie cell silently). Follows
    :func:`pdk_cells._other_variants_hint`'s convention: a suffix appended
    so a headless agent can self-correct, and best-effort -- any failure
    returns ``""`` rather than masking the original warning.
    """
    try:
        keys = sorted(verified_keys)
        if not keys:
            return ""
        closest = difflib.get_close_matches(
            cell_library, keys, n=1, cutoff=TIE_CELL_HINT_CUTOFF
        )
        lead = (
            " The closest cell_library with a verified tie-cell mapping is "
            f"'{closest[0]}'"
            if closest
            else " No verified tie-cell mapping has a name close to this one"
        )
        return (
            f"{lead}; the keys that do have one are: {', '.join(keys)} -- "
            "pass one of these as pdk.cell_library if it is the library you "
            "meant."
        )
    except Exception:
        return ""


def library_capability_warnings(
    cell_library: str,
    has_constr: bool | None = None,
    *,
    tie_cells: Collection[str],
    constr_inputs: Container[str],
) -> dict[str, str]:
    """Disclose skipped synthesis capabilities without guessing library data.

    Combine missing capabilities into one representative so neither is
    hidden by warning aggregation. An absent ``dont_use`` entry is not a
    missing capability: e.g. gf180 intentionally allows minimum-drive cells.

    ``has_constr`` says whether this run passed ``-constr`` at all (a request
    ``constraints.abc_constr`` supplies it for a library with no built-in
    entry, issue #4662); ``None`` falls back to ``constr_inputs``.

    ``tie_cells``/``constr_inputs`` are ``synthesize._TIE_CELLS`` and
    ``synthesize._ABC_CONSTR_INPUTS``, passed in rather than imported so this
    module stays a leaf (and so a test can substitute either table). When
    ``tie_cells`` has no entry for ``cell_library``,
    :func:`tie_cell_keys_hint` appends the keys that do -- issue #5074.
    """
    if has_constr is None:
        has_constr = cell_library in constr_inputs
    skipped = []
    if not has_constr:
        skipped.append(
            "ABC -constr (driving cell/output load), load-driven sizing/buffering "
            "and ABC timing (timing is null)"
        )
    tie_hint = ""
    if cell_library not in tie_cells:
        skipped.append(
            "setundef -zero/hilomap (bare 0/1/x constants may remain unroutable "
            "in place-and-route)"
        )
        tie_hint = tie_cell_keys_hint(cell_library, tie_cells)
    if not skipped:
        return {}
    return {
        "unsupported_cell_library": (
            f"cell_library '{cell_library}' has no verified mapping for: "
            + "; ".join(skipped)
            + ". These capabilities were skipped."
            + tie_hint
        )
    }

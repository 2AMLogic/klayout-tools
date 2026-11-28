"""Tech-LEF electromigration limits and database-unit resolution for a PDK.

Split out of :mod:`klayout_tools.pdk` (file-size ratchet, issue #8093) and
re-exported from it, so ``pdk.em_limits`` / ``pdk.resolve_pdk_dbu`` keep
working for every existing caller (``klt pdk em-limits``, ``klt gen``,
``klt place_and_route``'s DEF merge, :mod:`klayout_tools.pdk_output_dbu`,
:mod:`klayout_tools.pdk_pcell`).

Both queries **live-parse** the tech LEFs a resolved install ships rather than
owning a curated table -- the install's own files are the only place this data
is stated, and a curated copy would silently drift on a PDK upgrade:

- :func:`em_limits`` backs ``klt pdk em-limits`` (issue #1215): the
  ``DCCURRENTDENSITY``/``ACCURRENTDENSITY`` limits every ``ROUTING``/``CUT``
  layer declares, across every tech LEF in the family, flagging cross-file
  disagreement.
- :func:`resolve_pdk_dbu` (issue #1496) reads the tech LEF's ``DATABASE
  MICRONS`` value so ``klt gen`` agrees with ``klt place_and_route``'s own
  dbu resolution (#1032).

Both are built on :func:`discover_tech_lefs`, the family-wide tech-LEF
enumeration :mod:`klayout_tools.pdk_stackup` shares.

Pure library, like the rest of ``pdk``: nothing here prints.
:func:`klayout_tools.pdk.find_pdk` is deliberately **not** imported at module
scope -- :mod:`klayout_tools.pdk` imports this module for its re-exports --
so :func:`em_limits` resolves it at call time, which also keeps a
``monkeypatch.setattr(pdk, "find_pdk", ...)`` in a caller's test effective
here exactly as it was before the split.
"""

from __future__ import annotations

import math
import os
from typing import Any

from ._text import read_text as _read_text
from .lef_header import parse_lef_header

# --------------------------------------------------------------------------- #
# `klt pdk em-limits` -- electromigration current-density limits declared
# across every tech LEF an install ships (issue #1215) -- and the tech-LEF
# `DATABASE MICRONS` resolution `klt gen` sizes its output grid by (#1496).
# --------------------------------------------------------------------------- #

#: Layer ``TYPE``s a current-carrying design actually sizes an EM budget
#: against -- routing metal and cut/via (including the diffusion/poly
#: contact, ``CON``). Excludes non-current-carrying declarative layers a
#: tech LEF also declares (``MASTERSLICE`` wells/poly, ``OVERLAP``,
#: ``PR_bndry``, ...), keeping the report scoped to what issue #1215 asks
#: for -- "the layers a current-carrying design must size" -- rather than
#: echoing every ``LAYER`` block a tech LEF happens to declare.
_CURRENT_CARRYING_LAYER_TYPES = frozenset({"ROUTING", "CUT"})

#: Tolerance for deciding two tech LEFs' EM-adjacent values "agree" --
#: generous enough to absorb float round-trip noise, tight enough that the
#: issue's own 1.19 vs. 0.99 µm / 1.5 vs. 1.21 mA/µm disagreements are never
#: mistaken for agreement.
_EM_VALUE_ABS_TOL = 1e-6


def _em_values_agree(values: list[float]) -> bool:
    """``True`` when every value in non-empty ``values`` matches the first,
    within :data:`_EM_VALUE_ABS_TOL`."""
    first = values[0]
    return all(
        math.isclose(value, first, abs_tol=_EM_VALUE_ABS_TOL) for value in values
    )


def _summarize_em_field(
    entries: list[dict[str, Any]], field: str, *, conservative: bool
) -> dict[str, Any]:
    """Summarize one EM-adjacent field (``dc_current_density``,
    ``ac_current_density``, ``thickness_um``, ``resistance_rpersq``) across
    every source ``entries`` that parsed a value for it.

    Returns::

        {
            "shipped": bool,               # any source declared this field?
            "agrees": bool | None,         # None when "shipped" is False
            "values": [
                {"cell_library", "corner", "tech_lef", "value"}, ...
            ],
            "conservative": float | None,  # only present when conservative=True
        }

    ``shipped: False`` (with ``values: []``) is itself the answer for a
    layer/field with nothing declared anywhere -- e.g. ``CON``'s DC/AC
    current density -- an explicit "no limit shipped" result rather than a
    silently omitted field (issue #1215's own suggested handling).

    ``conservative`` is the minimum (more restrictive) value across sources
    when they disagree, per issue #1215's "returns the conservative value by
    default" -- only meaningful for a current-density budget (lower is
    always safer), so it is only computed when the caller asks for it.
    """
    present = [entry for entry in entries if entry[field] is not None]
    result: dict[str, Any] = {
        "shipped": bool(present),
        "agrees": _em_values_agree([e[field] for e in present]) if present else None,
        "values": [
            {
                "cell_library": e["cell_library"],
                "corner": e["corner"],
                "tech_lef": e["tech_lef"],
                "value": e[field],
            }
            for e in present
        ],
    }
    if conservative:
        result["conservative"] = min((e[field] for e in present), default=None)
    return result


def discover_tech_lefs(libs_ref: str) -> list[dict[str, str]]:
    """Enumerate every tech LEF under ``libs_ref``, naming-convention-
    independently: every ``<libs_ref>/<lib>/techlef/<lib>__<corner>.tlef``
    file, for every immediate ``libs_ref`` entry that ships a ``techlef/``
    subdirectory at all.

    Deliberately does **not** reuse
    :func:`klayout_tools.pdk_cells._scan_cell_libraries`'s
    name filter (:data:`klayout_tools.pdk_cells._STD_CELL_LIB_MARKERS`):
    issue #1215's own
    reported disagreement is precisely *between* an `_fd_sc_`-named family
    (`gf180mcu_fd_sc_mcu9t5v0`/`mcu7t5v0`) and an `_osu_sc_`-named one
    (`gf180mcu_osu_sc_gp9t3v3`/`gp12t3v3`) -- a name filter tuned for `klt
    pdk cells`'s different purpose (only digital timing-view libraries)
    would silently drop half of exactly the disagreement this query exists
    to surface. A `libs_ref` entry with no `techlef/` subdirectory
    (primitive-device, I/O-pad, hard-macro IP libraries -- verified against
    real sky130/gf180mcu installs) is naturally excluded without a name
    filter at all: it ships no tech LEF to parse.

    Returns ``[{"cell_library", "corner", "tech_lef"}, ...]``, sorted by
    ``(cell_library, corner)``. ``corner`` is derived from the filename
    (stripping the ``<cell_library>__`` prefix and ``.tlef`` suffix) rather
    than assumed to be one of `min`/`nom`/`max` -- an install shipping only
    `nom` (issue #1215's own `osu_sc_gp*t3v3` example), or a corner name
    this reader does not otherwise recognise, is still reported verbatim.
    """
    sources: list[dict[str, str]] = []
    if not os.path.isdir(libs_ref):
        return sources
    for name in sorted(os.listdir(libs_ref)):
        techlef_dir = os.path.join(libs_ref, name, "techlef")
        if not os.path.isdir(techlef_dir):
            continue
        prefix = f"{name}__"
        for filename in sorted(os.listdir(techlef_dir)):
            if not filename.endswith(".tlef"):
                continue
            if filename.startswith(prefix):
                corner = filename[len(prefix) : -len(".tlef")]
            else:
                corner = filename[: -len(".tlef")]
            sources.append(
                {
                    "cell_library": name,
                    "corner": corner,
                    "tech_lef": os.path.join(techlef_dir, filename),
                }
            )
    return sources


def resolve_pdk_dbu(pdk_info: dict[str, Any]) -> float | None:
    """Resolve a resolved PDK's own database unit (dbu, in micrometres) from
    its tech LEF's declared ``DATABASE MICRONS`` value -- e.g. ``0.0005`` for
    a tech LEF declaring ``DATABASE MICRONS 2000`` (gf180mcu), ``0.001`` for
    one declaring ``DATABASE MICRONS 1000`` (sky130).

    Mirrors :func:`~klayout_tools.place_and_route._merge_def_to_gds`'s own
    ``database_microns is not None: lefdef_config.dbu = 1.0 / database_microns``
    pattern (issue #1032) so a caller with no single ``cell_library`` in hand
    (unlike ``place_and_route``, which always resolves one) can still agree
    with it by construction (issue #1496) -- built on
    :func:`discover_tech_lefs`'s existing family-wide tech LEF enumeration
    (already used by :func:`em_limits`) rather than
    :func:`klayout_tools.pdk.lef_files`, which requires a ``cell_library``
    argument this call site does not have.

    Enumerates every tech LEF under ``pdk_info["assets"]["libs_ref"]`` (the
    same family-wide set :func:`em_limits` reports) and returns the **finest**
    dbu any of them declares -- ``1.0 / max(database_microns)``. Every real
    open PDK's tech LEFs agree on one ``DATABASE MICRONS`` value across the
    whole family, so "the finest" is normally just "the one value"; picking
    the finest rather than whichever library happens to sort first makes the
    answer independent of library naming, and keeps every library's own grid
    exactly representable whenever the declared values divide each other (as
    LEF's own permitted 100/200/1000/2000/10000/20000 set does).

    Returns ``None`` -- never raises -- when ``libs_ref`` is unset, no tech
    LEF is found, or no discovered tech LEF declares (or can have parsed) a
    ``DATABASE MICRONS`` value; the caller is expected to fall back to its
    own existing default in that case, exactly as
    :func:`~klayout_tools.place_and_route._merge_def_to_gds` already does
    when ``read_lef_header``'s own ``database_microns`` comes back ``None``.
    """
    libs_ref = (pdk_info.get("assets") or {}).get("libs_ref")
    if not libs_ref:
        return None
    declared: list[int] = []
    for source in discover_tech_lefs(libs_ref):
        text = _read_text(source["tech_lef"])
        if text is None:
            continue
        database_microns = parse_lef_header(text)["database_microns"]
        if database_microns:
            declared.append(database_microns)
    if not declared:
        return None
    return 1.0 / max(declared)


def em_limits(variant: str | None = None, root: str | None = None) -> dict[str, Any]:
    """Report electromigration current-density limits declared across every
    tech LEF a resolved PDK variant ships (issue #1215).

    Resolves one PDK install/variant exactly as
    :func:`klayout_tools.pdk.find_pdk` does (same ``variant``/``root`` args,
    same :class:`klayout_tools.pdk.PdkNotFoundError` on no match), then
    discovers every tech LEF under its ``libs_ref`` asset via
    :func:`discover_tech_lefs` -- every `libs_ref/<lib>/techlef/*.tlef`
    file, independent of the library's naming convention (**not** filtered
    to `_fd_sc_`-named standard-cell libraries the way
    :func:`klayout_tools.pdk_cells.list_cell_libraries` is -- see
    :func:`discover_tech_lefs`'s own docstring for why). Each resolved tech
    LEF is parsed with
    :func:`klayout_tools.lef_header.parse_lef_header`.

    For every ``ROUTING``/``CUT`` layer (:data:`_CURRENT_CARRYING_LAYER_TYPES`
    -- the layers a current-carrying design actually sizes against, per the
    issue's own framing) seen in **any** parsed tech LEF, reports the
    per-source ``dc_current_density``/``ac_current_density`` values plus
    ``thickness_um``/``resistance_rpersq`` for context, and whether they
    agree across every source that declared them. A layer present in one
    tech LEF but not another (e.g. an osu library shipping only ``nom``) is
    still reported using whichever sources did declare it -- a smaller
    source set is not itself a disagreement.

    Design choice -- **live-parses** the shipped tech LEFs at call time
    rather than owning a curated table, matching
    :func:`klayout_tools.pdk_cells.list_cell_libraries`'s own rationale
    (docs/cli/pdk.md): the install's own files are the only
    place this data is stated, and a curated copy would silently drift on a
    PDK upgrade instead of reflecting what is actually installed.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/pdk.md``)::

        {
            "schema_version": 1,
            "pdk": <variant name>,
            "root": <absolute install root>,
            "sources": [
                {"cell_library": str, "corner": str, "tech_lef": <abs path>},
                ...
            ],
            "layers": [
                {
                    "name": str,
                    "type": "ROUTING" | "CUT",
                    "dc_current_density": {
                        "shipped": bool, "agrees": bool | None,
                        "conservative": float | None,
                        "values": [
                            {"cell_library", "corner", "tech_lef", "value"}, ...
                        ],
                    },
                    "ac_current_density": {... same shape ...},
                    "thickness_um": {
                        "shipped": bool, "agrees": bool | None,
                        "values": [...],
                    },
                    "resistance_rpersq": {... same shape as thickness_um ...},
                },
                ...
            ],
            "disagreements": [<layer name>, ...],  # dc/ac current density only
        }

    ``layers`` is name-sorted; a variant shipping no `libs_ref` asset (or one
    whose libraries ship no `techlef/` directory at all) reports
    ``"sources": []``, ``"layers": []`` -- a successful, empty result, not an
    error. ``disagreements`` lists every layer name where
    ``dc_current_density`` or ``ac_current_density`` is ``shipped`` but not
    ``agrees`` (``thickness_um``/``resistance_rpersq`` disagreement is
    visible per-layer but does not, by itself, add a layer to this list -- it
    is context for *why* a current-density disagreement might exist, per the
    issue's own "same resistance, different thickness" observation, not a
    second disagreement axis this query adjudicates).

    Raises :class:`klayout_tools.pdk.PdkNotFoundError` when no PDK install
    resolves at all.
    """
    from . import pdk  # lazy: pdk imports this module for its re-exports

    info = pdk.find_pdk(variant=variant, root=root)
    libs_ref = info["assets"]["libs_ref"]
    sources = discover_tech_lefs(libs_ref) if libs_ref is not None else []

    layer_entries: dict[str, list[dict[str, Any]]] = {}
    layer_types: dict[str, str | None] = {}

    for source in sources:
        text = _read_text(source["tech_lef"])
        if text is None:
            continue
        header = parse_lef_header(text)
        for layer in header["layers"]:
            if layer["type"] not in _CURRENT_CARRYING_LAYER_TYPES:
                continue
            layer_entries.setdefault(layer["name"], []).append(
                {
                    "cell_library": source["cell_library"],
                    "corner": source["corner"],
                    "tech_lef": source["tech_lef"],
                    "thickness_um": layer["thickness_um"],
                    "resistance_rpersq": layer["resistance_rpersq"],
                    "dc_current_density": layer["dc_current_density"],
                    "ac_current_density": layer["ac_current_density"],
                }
            )
            layer_types.setdefault(layer["name"], layer["type"])

    layers: list[dict[str, Any]] = []
    disagreements: list[str] = []
    for name in sorted(layer_entries):
        entries = layer_entries[name]
        dc = _summarize_em_field(entries, "dc_current_density", conservative=True)
        ac = _summarize_em_field(entries, "ac_current_density", conservative=True)
        thickness = _summarize_em_field(entries, "thickness_um", conservative=False)
        resistance = _summarize_em_field(
            entries, "resistance_rpersq", conservative=False
        )

        if dc["shipped"] and not dc["agrees"]:
            disagreements.append(name)
        elif ac["shipped"] and not ac["agrees"]:
            disagreements.append(name)

        layers.append(
            {
                "name": name,
                "type": layer_types[name],
                "dc_current_density": dc,
                "ac_current_density": ac,
                "thickness_um": thickness,
                "resistance_rpersq": resistance,
            }
        )

    return {
        "schema_version": 1,
        "pdk": info["variant"],
        "root": info["root"],
        "sources": sources,
        "layers": layers,
        "disagreements": disagreements,
    }

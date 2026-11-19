"""Per-device-flavour corner-skew classification for gf180mcu model decks.

Extracted from :mod:`klayout_tools.pdk` (issue #5663) so the per-flavour
classifier could gain consumption-based ``.param`` attribution without
growing ``pdk.py`` past its ``file-size-baseline.json`` debt ceiling.
``pdk.py`` imports these helpers back -- the package's blessed
shared-utility carve-out, not a verb-to-verb private import.

What lives here:

* :func:`_param_lines` -- the normalised ``.param``-line set comparison both
  the family-level classifier and the per-flavour one are built on;
* :func:`_gf180mcu_flavour_tokens` -- groups a section's
  ``.lib '<file>' <name>`` include tokens by device flavour;
* :func:`_flavour_devices` -- the per-flavour ``{flavour, section, skew}``
  classification behind ``klt pdk corners``' ``devices`` list (issue #3794),
  extended by issue #5663.

The ``devices[].skew`` values a flavour can take:

* ``"skewed"`` -- the flavour's include tokens differ from its typical
  corner's. Token evidence always wins: a flavour that switched to a
  different sub-library is ``"skewed"`` no matter what its ``.param`` table
  says.
* ``"param"`` -- tokens identical, but at least one ``.param`` value that
  differs from the typical section's is **consumed** by the flavour's own
  sub-library. Attribution is by consumption: each differing ``.param`` name
  is word-matched against the sub-library bodies the flavour's include
  tokens point at (``_parse_named_lib_blocks`` already parsed every
  bare-name block in the deck, sub-libraries included -- no new parsing).
  When a flavour's sub-library body is not resolvable in the deck (e.g.
  pulled in by ``.include`` from another file), a **name-prefix fallback**
  attributes instead: a differing ``nfet_06v0_vth0``-shaped param maps to
  the ``06v0`` flavour by the same shape rule
  :func:`_gf180mcu_flavour_tokens` uses. The fallback is deliberately not
  consulted when the body *is* resolvable -- direct consumption evidence
  outranks a spelling guess -- and a param whose prefix maps to no flavour
  (gf180mcuD's real ``rsh_nplus_u_m`` -> phantom ``nplus_u``) mints no
  entry.
* ``"typical"`` -- provably unmoved: identical tokens, and either no
  ``.param`` differs from typical at all, or the flavour's resolvable
  sub-library bodies consume none of the differing params. With no
  ``.param`` evidence passed at all (the issue #3794 call shape), every
  unchanged-token flavour is ``"typical"``, exactly as before #5663.
* ``"unknown"`` -- undecidable (issue #5663): identical tokens, ``.param``
  values do differ from typical, but the flavour's sub-library body cannot
  be resolved in the deck and no differing param's name-prefix maps to it.
  Reported as its own value -- never silently folded into ``"typical"``
  (the false "nothing moved" answer this issue fixes) -- and surfaced by
  ``klt sim`` under ``environment.corner_coverage``'s ``unknown_devices``
  key rather than ``unmoved_devices``.
* ``None`` -- the typical corner ships the flavour but this corner's
  section does not (unlisted); unchanged from issue #3794.
"""

from __future__ import annotations

import re
from typing import Any

#: Matches both a `.param` statement line and its `+`-prefixed ngspice
#: line-continuations -- gf180mcu's `bjt_*`/`diode_*`/`res_*`/`moscap_*`
#: sections write their assignments as a bare `.param` line followed by
#: several `+name=value ...` continuation lines (see :func:`_param_lines`),
#: not `.param name=value` on one line the way `mimcap_*` does.
_PARAM_LINE_RE = re.compile(r"^\s*(?:\.param\b|\+)", re.IGNORECASE)

#: Every ``name=`` assignment on one normalised ``.param`` line (a line may
#: carry several). A quoted value expression containing ``=`` can smuggle in
#: a stray match; that only ever *enlarges* the differing-name set, i.e. it
#: errs toward reporting param evidence rather than suppressing it.
_PARAM_ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _param_lines(body: str) -> frozenset[str]:
    """The normalised set of `.param` statement lines (plus their `+`
    line-continuations, concatenated as separate set members) in ``body``,
    for comparing whether a section's parameter overrides genuinely differ
    from its family's typical section.

    A **set** comparison of the *whole* parameter area (not "does this
    section contain any `.param`/`+` line at all") -- both PDKs' decks carry
    `.param` lines that are unconditional boilerplate, unrelated to
    process-corner skew, in *every* section (gf180mcu's per-corner
    `rsh_*_u_m` sheet-resistance restatements that happen to equal the
    typical value at some corners; sky130's `mc_mm_switch`/`mc_pr_switch`
    Monte-Carlo toggle, which is `0` in every non-mismatch,
    non-statistical corner including `tt` itself). Presence alone would
    misclassify those as ``"param"``-skewed even when every value is
    unchanged from typical; comparing the actual line set against the
    family's typical section correctly resolves them to ``"typical"``.
    Internal whitespace is collapsed (``" ".join(line.split())``) so
    incidental spacing differences between corners don't register as a
    content difference.
    """
    return frozenset(
        " ".join(line.split())
        for line in body.splitlines()
        if _PARAM_LINE_RE.match(line)
    )


def _differing_param_names(
    params: frozenset[str] | None, typical_params: frozenset[str] | None
) -> frozenset[str]:
    """The ``.param`` *names* whose normalised line differs between a
    corner section and its family's typical section: the symmetric
    difference of the two :func:`_param_lines` sets, reduced to the names
    assigned on each differing line. ``None`` on either side (the
    issue #3794 call shape -- no ``.param`` evidence) yields nothing."""
    if params is None or typical_params is None:
        return frozenset()
    names: set[str] = set()
    for line in params ^ typical_params:
        names.update(_PARAM_ASSIGN_RE.findall(line))
    return frozenset(names)


def _prefix_flavour(param_name: str) -> str:
    """The device flavour a ``.param`` name's own spelling points at, by the
    same shape rule :func:`_gf180mcu_flavour_tokens` applies to include
    tokens: drop the leading device token and the trailing leaf name
    (``nfet_06v0_vth0`` -> ``06v0``). A name with fewer than three ``_``
    parts has no separable flavour and maps to itself -- which matches no
    real flavour key, so the prefix fallback safely attributes nothing."""
    parts = param_name.split("_")
    return "_".join(parts[1:-1]) if len(parts) >= 3 else param_name


def _unchanged_token_skew(
    flavour: str,
    tokens: set[tuple[str, str]],
    blocks: dict[str, str],
    differing: frozenset[str],
    lines_differ: bool,
) -> str:
    """Classify a flavour whose include tokens are identical to its typical
    corner's -- the case issue #5663 fixes. (A flavour whose tokens *moved*
    is ``"skewed"``; the caller never reaches here.)

    ``tokens`` are the ``(path, name)`` include tokens this corner's section
    carries for the flavour; ``blocks`` is the whole deck's
    ``{block_name: body}`` map (:func:`klayout_tools.pdk._parse_named_lib_blocks`).
    """
    if not lines_differ:
        # No `.param` differs from typical: token-identical is provably
        # unmoved, with nothing param-shaped to decide (issue #3794
        # behaviour, preserved verbatim so token-only input classifies
        # exactly as before).
        return "typical"
    bodies = [blocks[name] for _, name in tokens if name in blocks]
    if bodies:
        consumed = {
            name
            for name in differing
            if any(re.search(rf"\b{re.escape(name)}\b", body) for body in bodies)
        }
        # Resolvable sub-library bodies are direct consumption evidence:
        # consuming a differing param is a real skew; consuming none is
        # proof of unmoved (a differing param this deck's other blocks
        # reference cannot reach a card that never names it).
        return "param" if consumed else "typical"
    # Sub-library bodies unresolvable in this deck (e.g. pulled in by
    # `.include` from another file): the documented name-prefix fallback,
    # then the undecidable answer.
    if any(_prefix_flavour(name) == flavour for name in differing):
        return "param"
    return "unknown"


def _gf180mcu_flavour_tokens(
    tokens: set[tuple[str, str]],
) -> dict[str, set[tuple[str, str]]]:
    """Group a gf180mcu section's ``.lib '<file>' <token>`` includes by device
    flavour: ``nfet_06v0_nvt_t`` -> ``06v0_nvt`` (drop the leading device
    token and the trailing per-corner suffix).

    A token with fewer than three ``_`` parts has no separable flavour.
    Issue #3794 keyed such tokens by their whole name, which minted
    "flavours" that are not device flavours at all -- on gf180mcuD,
    ``fets_mm`` (the mismatch-model lib) and ``noise_corner`` (the noise
    lib), neither process-skewed, were reported in ``devices[]`` and
    therefore in ``klt sim``'s ``unmoved_devices``, which read as "the
    devices you care about did not move". Issue #5663 drops them: a
    fallback-keyed token describes no device flavour and is excluded from
    the grouping (the family-level token comparison around this call still
    sees the token itself)."""
    grouped: dict[str, set[tuple[str, str]]] = {}
    for path, name in tokens:
        parts = name.split("_")
        if len(parts) < 3:
            continue
        grouped.setdefault("_".join(parts[1:-1]), set()).add((path, name))
    return grouped


def _flavour_devices(
    section: str,
    flavours: dict[str, set[tuple[str, str]]],
    typical: dict[str, set[tuple[str, str]]],
    is_typical_corner: bool,
    *,
    blocks: dict[str, str] | None = None,
    params: frozenset[str] | None = None,
    typical_params: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-flavour ``{flavour, section, skew}`` list, each flavour classified
    against its own typical-corner evidence (issue #3794's token comparison,
    extended by issue #5663 with ``.param`` attribution -- this module's
    docstring documents the full ``skew`` value set).

    ``blocks`` is the deck's ``{block_name: body}`` map;
    ``params`` / ``typical_params`` are the corner section's and its family's
    typical section's :func:`_param_lines` sets. Passing only the token
    groupings (the issue #3794 call shape) reproduces the old token-only
    classification: with no ``.param`` evidence nothing can differ, so every
    unchanged-token flavour stays ``"typical"`` and nothing is
    ``"unknown"``.
    """
    differing = _differing_param_names(params, typical_params)
    lines_differ = (
        params is not None
        and typical_params is not None
        and bool(params ^ typical_params)
    )
    devices: list[dict[str, Any]] = []
    for flavour in sorted(set(flavours) | set(typical)):
        if flavour not in flavours:
            skew = None
        elif is_typical_corner:
            skew = "typical"
        elif flavours[flavour] != typical.get(flavour):
            skew = "skewed"
        else:
            skew = _unchanged_token_skew(
                flavour, flavours[flavour], blocks or {}, differing, lines_differ
            )
        devices.append(
            {
                "flavour": flavour,
                "section": section if skew is not None else None,
                "skew": skew,
            }
        )
    return devices

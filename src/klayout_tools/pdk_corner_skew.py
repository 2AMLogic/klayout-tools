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
* ``"param"`` -- with identical tokens, any referenced sub-library body
  that visibly consumes a differing parameter establishes skew. A body
  belongs to a token when its file resolves to the scanned deck and the
  named section is present. If some bodies are unresolved and none of the
  visible bodies consumes a differing parameter, a parameter name such as
  ``nfet_06v0_vth0`` can still establish the ``06v0`` flavour by prefix.
* ``"typical"`` -- identical tokens and either no differing parameters or a
  complete set of referenced bodies that consume none of those parameters.
* ``"unknown"`` -- identical tokens, differing parameters, and incomplete
  bodies with no evidence of consumption or matching parameter prefix.
  Ambiguous parameter assignments also give this result. ``klt sim`` lists
  these devices under ``environment.corner_coverage.unknown_devices``.
* ``None`` -- the typical corner ships the flavour but this corner's
  section does not (unlisted); unchanged from issue #3794.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ._spice_tokens import split_spice_tokens

#: Matches both a `.param` statement line and its `+`-prefixed ngspice
#: line-continuations -- gf180mcu's `bjt_*`/`diode_*`/`res_*`/`moscap_*`
#: sections write their assignments as a bare `.param` line followed by
#: several `+name=value ...` continuation lines (see :func:`_param_lines`),
#: not `.param name=value` on one line the way `mimcap_*` does.
_PARAM_LINE_RE = re.compile(r"^\s*(?:\.param\b|\+)", re.IGNORECASE)

#: The shared SPICE tokenizer keeps grouped expressions together. Accept only
#: complete assignments; ambiguous syntax has no provable per-flavour skew.
_PARAM_ASSIGNMENT = re.compile(
    r"""([a-zA-Z_][a-zA-Z0-9_]*)=([^\s='";{}()]+|'[^']+'|"[^"]+"|\{[^{}]+\}|\([^()]+\))"""
)


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


def _parameter_values(lines: frozenset[str]) -> dict[str, str] | None:
    """Read assignments from unordered lines, rejecting conflicting reassignments."""
    values: dict[str, str] = {}
    for line in lines:
        for token in split_spice_tokens(_PARAM_LINE_RE.sub("", line, count=1)):
            if token.startswith(";") or token == "$":
                break
            assignment = _PARAM_ASSIGNMENT.fullmatch(token)
            if assignment is None:
                return None
            name, value = assignment.groups()
            if values.setdefault(name.lower(), value) != value:
                return None
    return values


def _differing_param_names(
    params: frozenset[str] | None, typical_params: frozenset[str] | None
) -> frozenset[str] | None:
    """Compare individual assignments. Return None when a value cannot be resolved."""
    if params is None or typical_params is None or params == typical_params:
        return frozenset()
    current = _parameter_values(params)
    typical = _parameter_values(typical_params)
    if current is None or typical is None:
        return None
    return frozenset(
        name
        for name in current.keys() | typical.keys()
        if current.get(name) != typical.get(name)
    )


def _prefix_flavour(param_name: str) -> str:
    """The device flavour a ``.param`` name's own spelling points at, by the
    same shape rule :func:`_gf180mcu_flavour_tokens` applies to include
    tokens: drop the leading device token and the trailing leaf name
    (``nfet_06v0_vth0`` -> ``06v0``). A name with fewer than three ``_``
    parts has no separable flavour and maps to itself -- which matches no
    real flavour key, so the prefix fallback safely attributes nothing."""
    parts = param_name.split("_")
    return "_".join(parts[1:-1]) if len(parts) >= 3 else param_name


def _local_sub_library_bodies(
    model_path: str, blocks: dict[str, str], tokens: set[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """Return bodies for includes whose file and section identify the scanned deck."""
    bodies = {}
    for path, name in tokens:
        if name in blocks:
            try:
                if os.path.samefile(
                    os.path.join(os.path.dirname(model_path), path), model_path
                ):
                    bodies[path, name] = blocks[name]
            except OSError:
                # An unavailable include leaves the token unresolved.
                continue
    return bodies


def _unchanged_token_skew(
    flavour: str,
    tokens: set[tuple[str, str]],
    blocks: dict[tuple[str, str], str],
    differing: frozenset[str] | None,
) -> str:
    """Classify a flavour whose include tokens are identical to its typical
    corner's -- the case issue #5663 fixes. (A flavour whose tokens *moved*
    is ``"skewed"``; the caller never reaches here.)

    ``tokens`` are the ``(path, name)`` include tokens this corner's section
    carries for the flavour. ``blocks`` maps resolved tokens to their bodies.
    """
    if differing is None:
        return "unknown"
    if not differing:
        # No `.param` differs from typical: token-identical is provably
        # unmoved, with nothing param-shaped to decide (issue #3794
        # behaviour, preserved verbatim so token-only input classifies
        # exactly as before).
        return "typical"
    bodies = [blocks[token] for token in tokens if token in blocks]
    if any(
        re.search(rf"\b{re.escape(name)}\b", body, re.IGNORECASE)
        for body in bodies
        for name in differing
    ):
        return "param"
    # An omitted sibling can still consume a changed parameter. Only a
    # complete set of bodies can establish the absence of consumption.
    if tokens and len(bodies) == len(tokens):
        return "typical"
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
    blocks: dict[tuple[str, str], str] | None = None,
    params: frozenset[str] | None = None,
    typical_params: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-flavour ``{flavour, section, skew}`` list, each flavour classified
    against its own typical-corner evidence (issue #3794's token comparison,
    extended by issue #5663 with ``.param`` attribution -- this module's
    docstring documents the full ``skew`` value set).

    ``blocks`` maps resolved (file, section) pairs to their bodies;
    ``params`` / ``typical_params`` are the corner section's and its family's
    typical section's :func:`_param_lines` sets. Passing only the token
    groupings (the issue #3794 call shape) reproduces the old token-only
    classification: with no ``.param`` evidence nothing can differ, so every
    unchanged-token flavour stays ``"typical"`` and nothing is
    ``"unknown"``.
    """
    differing = _differing_param_names(params, typical_params)
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
                flavour, flavours[flavour], blocks or {}, differing
            )
        devices.append(
            {
                "flavour": flavour,
                "section": section if skew is not None else None,
                "skew": skew,
            }
        )
    return devices

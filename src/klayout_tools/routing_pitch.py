"""Conservative routing-pitch lower bound from curated deck width/space rules.

Library-only (no CLI verb): :func:`routing_pitch_bound` reads a layer's
ordinary minimum ``width`` and ``space`` :class:`~klayout_tools.decks.DrcRule`
entries and returns ``min_width + min_space`` in micrometres as a *necessary*
lower bound on the centre-to-centre pitch of two parallel tracks. It is not a
legality claim: no deck's :class:`~klayout_tools.decks.DrcRule` can express
wide-metal spacing or end-of-line predicates, so a successful lookup is always
``status="partial"`` and lists the deck's declared-unmodelled rule ids for the
layer. A caller must read the result as "pitch must not be smaller than", never
"DRC-clean at"; run ``klt drc`` on the actual layout for legality.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .decks import (
    DrcRule,
    get_deck,
    get_layer_names,
    get_nominal_dbu,
    get_unmodeled_rules,
)

STATUS_PARTIAL = "partial"
STATUS_UNSUPPORTED = "unsupported"

SUPPORTED_RULE_CLASSES = ("width", "space")


class RoutingPitchError(ValueError):
    """Unknown deck/layer, or a requested width the deck contradicts."""


@dataclass(frozen=True)
class RoutingPitchResult:
    """Outcome of :func:`routing_pitch_bound`.

    ``min_pitch_um`` is a lower bound only and is ``None`` when ``status`` is
    ``"unsupported"``.
    """

    status: str
    deck: str
    layer: str
    min_pitch_um: float | None = None
    min_width_um: float | None = None
    min_space_um: float | None = None
    rule_ids: list[str] = field(default_factory=list)
    provenance: list[dict[str, str]] = field(default_factory=list)
    unmodeled: list[str] = field(default_factory=list)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_layer(
    deck: str, layer: str | tuple[int, int]
) -> tuple[tuple[int, int], str]:
    names = get_layer_names(deck)
    if isinstance(layer, tuple):
        if layer not in names:
            raise RoutingPitchError(f"layer {layer} is not in deck '{deck}'")
        return layer, names[layer]
    for key, name in names.items():
        if layer in (name, name.split(".")[0]) and (
            name == layer or name.endswith(".drawing") or "." not in name
        ):
            return key, name
    raise RoutingPitchError(f"unknown layer '{layer}' for deck '{deck}'")


def _plain_rules(
    rules: list[DrcRule], layer: tuple[int, int], check: str
) -> list[DrcRule]:
    return [
        r
        for r in rules
        if r.layer == layer
        and r.check == check
        and r.other_layer is None
        and r.derived_layer is None
        and r.layer_selection is None
        and r.region_spec is None
        and r.net_spec is None
        and r.threshold_dbu is not None
    ]


def _relevant_unmodeled(deck: str, layer_name: str) -> list[str]:
    """Declared-unmodelled ids belonging to the metal ``layer_name`` names.

    Matching is by the deck's own id convention: sky130 ``m<N>.*`` for
    ``met<N>``; gf180mcu ``M<N>.*``/``V<N-1>.*``/``V<N>.*`` (and ``CO.*`` for
    Metal1) for ``Metal<N>``. Other layers match nothing.
    """
    ids = get_unmodeled_rules(deck)
    m = re.fullmatch(r"met(\d+)(?:\..*)?", layer_name) if deck == "sky130" else None
    if m:
        return [i for i in ids if i.startswith(f"m{m.group(1)}.")]
    m = re.fullmatch(r"Metal(\d+)", layer_name) if deck == "gf180mcu" else None
    if m:
        n = int(m.group(1))
        prefixes = [f"M{n}.", f"V{n}.", f"V{n - 1}."] + (["CO."] if n == 1 else [])
        return [i for i in ids if i.startswith(tuple(prefixes))]
    return []


def routing_pitch_bound(
    deck: str,
    layer: str | tuple[int, int],
    *,
    requested_width_um: float | None = None,
) -> RoutingPitchResult:
    """Return the ordinary ``min_width + min_space`` pitch bound for ``layer``.

    ``layer`` is a deck layer name (``"met1"``/``"met1.drawing"``,
    ``"Metal1"``) or a ``(layer, datatype)`` tuple. When ``requested_width_um``
    is given it must be at least the deck's minimum width (compared in deck
    dbu) and the bound uses that width: ``requested_width_um + min_space``.

    Raises :class:`RoutingPitchError` for an unknown deck/layer or a requested
    width below the deck minimum. Returns ``status="unsupported"`` when the
    layer lacks a single unconditional width or space rule, and
    ``status="partial"`` otherwise (never complete; see module docstring).
    """
    try:
        rules = get_deck(deck)
        dbu = get_nominal_dbu(deck)
    except Exception as exc:  # UnknownDeckError
        raise RoutingPitchError(str(exc)) from exc
    key, name = _resolve_layer(deck, layer)

    widths = _plain_rules(rules, key, "width")
    spaces = _plain_rules(rules, key, "space")
    if len(widths) != 1 or len(spaces) != 1:
        missing = "width" if len(widths) != 1 else "space"
        found = len(widths) if missing == "width" else len(spaces)
        why = "no" if found == 0 else f"{found} conditional"
        return RoutingPitchResult(
            status=STATUS_UNSUPPORTED,
            deck=deck,
            layer=name,
            reason=f"layer has {why} unconditional {missing} rule(s) in the deck",
        )
    w_rule, s_rule = widths[0], spaces[0]
    min_w = w_rule.threshold_dbu * dbu
    min_s = s_rule.threshold_dbu * dbu
    width_um = min_w
    if requested_width_um is not None:
        if round(requested_width_um / dbu) < w_rule.threshold_dbu:
            raise RoutingPitchError(
                f"requested width {requested_width_um} um is below the {deck} "
                f"minimum {min_w:.6g} um for {name} ({w_rule.id})"
            )
        width_um = requested_width_um
    return RoutingPitchResult(
        status=STATUS_PARTIAL,
        deck=deck,
        layer=name,
        min_pitch_um=round(width_um + min_s, 6),
        min_width_um=round(min_w, 6),
        min_space_um=round(min_s, 6),
        rule_ids=[w_rule.id, s_rule.id],
        provenance=[
            asdict(r.provenance) for r in (w_rule, s_rule) if r.provenance is not None
        ],
        unmodeled=_relevant_unmodeled(deck, name),
        reason=(
            "ordinary width+space only; wide-metal and end-of-line rules are not "
            "representable, so this is a lower bound, not a DRC-clean pitch"
        ),
    )

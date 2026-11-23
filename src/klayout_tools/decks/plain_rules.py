"""Physical thresholds for plain single-layer rules in curated PDK decks."""

from .. import decks


def largest_plain_rule_um(
    family: str, layer: tuple[int, int] | None, check: str
) -> tuple[float, str] | None:
    """Return the largest plain-rule threshold on ``layer`` for ``check``.

    Considers only plain single-layer rules (``other_layer`` and
    ``derived_layer`` unset) whose layer matches. Returns
    ``(threshold_um, rule_id)`` for the largest matching threshold; ties
    keep deck order. Returns ``None`` when ``layer`` is ``None``, when
    ``family`` has no resolvable deck (``decks.UnknownDeckError``), or when
    the deck holds no matching plain rule for the layer.
    """
    if layer is None:
        return None
    try:
        deck_rules = decks.get_deck(family)
        nominal_dbu_um = decks.get_nominal_dbu(family)
    except decks.UnknownDeckError:
        return None
    best: tuple[float, str] | None = None
    for rule in deck_rules:
        if (
            rule.check == check
            and rule.layer == layer
            and rule.other_layer is None
            and rule.derived_layer is None
        ):
            threshold_um = rule.threshold_dbu * nominal_dbu_um
            if best is None or threshold_um > best[0]:
                best = (threshold_um, rule.id)
    return best

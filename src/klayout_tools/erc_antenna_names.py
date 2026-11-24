"""Match a PDK antenna rule table to the spec's conductor names."""

from __future__ import annotations

from . import _warn


def match_antenna_layers(
    limits: dict[str, tuple[float, str]], names: list[str], pdk: str
) -> dict[str, tuple[float, str]]:
    """Add case-insensitive aliases for the non-gate layers in this stackup."""
    matched = dict(limits)
    folded = {layer.casefold(): rule for layer, rule in limits.items()}
    for name in names:
        if name.casefold() in folded:
            matched.setdefault(name, folded[name.casefold()])
    if names and matched.keys().isdisjoint(names):
        _warn.warn(
            f"klt erc: warning: no non-gate stackup layer ({', '.join(names)}) "
            f"matches the {pdk} antenna table ({', '.join(sorted(limits))})"
        )
    return matched

"""Per-landing cut-resistance resolution for ``--resistance-graph``
(issue #6917).

``klt extract`` stamps each landing with the per-cut resistance its own
landing type resolves to, in this order:

1. ``--resistance-graph-r-cut`` when given -- one uniform value for every
   landing, ``0.0`` included (an ideal via; the flag's existing semantics);
2. otherwise the deck's *nominal* per-cut value for that landing type --
   ``poly`` -> ``metal0`` from :attr:`ParasiticsDeck.poly_contact` (sky130's
   licon), ``metal{i}`` -> ``metal{i + 1}`` from :attr:`ParasiticsDeck.cuts`
   (``--parasitics`` has no corner axis, so nominal only);
3. ``None`` -- the deck curates nothing for that landing type, and the graph
   reports ``via_resistance_unknown`` / ``unsupported`` rather than assuming
   an ideal via.

A leaf module by design (issue #6917): it imports only from ``decks``, adds
no internal import edge, and keeps the resolution table out of the
ratchet-capped ``extract_parasitics`` module.
"""

from __future__ import annotations

from collections.abc import Callable

from .decks import ParasiticsDeck

#: Maps a landing's ``(lower_role, upper_role)`` to its per-cut resistance
#: in ohms, or ``None`` when no source curates that landing type.
CutResistanceResolver = Callable[[str, str], float | None]


def cut_resistance_resolver(
    override_ohm: float | None, deck: ParasiticsDeck | None
) -> CutResistanceResolver:
    """The resolver for one ``--resistance-graph`` request: the explicit
    ``override_ohm`` (``--resistance-graph-r-cut``) uniformly when given,
    otherwise ``deck``'s nominal value per landing type. The landing roles
    are the join keys ``_graph_landings_and_ports`` stamps: ``diffusion`` /
    ``poly`` / ``metal{i}`` below, ``metal{j}`` above."""
    if override_ohm is not None:
        return lambda _lower, _upper: override_ohm
    inter_metal = (
        {cut.lower_metal_index: cut.nominal_ohm for cut in deck.cuts} if deck else {}
    )
    poly_contact = deck.poly_contact.nominal_ohm if deck and deck.poly_contact else None

    def resolve(lower_role: str, upper_role: str) -> float | None:
        if not upper_role.startswith("metal"):
            return None
        if lower_role == "poly":
            return poly_contact if upper_role == "metal0" else None
        if lower_role.startswith("metal"):
            i = int(lower_role[len("metal") :])
            if int(upper_role[len("metal") :]) == i + 1:
                return inter_metal.get(i)
        return None

    return resolve

"""Select per-net runset geometry that needs a curated PEX probe."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def net_probe_regions(
    lvs: Any,
    net: Any,
    metals: Mapping[int, str],
    uncertain: Mapping[int, Sequence[str]],
    landings: Mapping[int, Any],
) -> dict[int, Any]:
    """Collect the regions in layer order, including contact-to-metal landings.

    A derived device-port strip may be inside a larger metal island. Its
    shape adds no coverage if its only possible metal role already covers
    the entire strip on this same net. Contact landings cannot prove that
    coverage. An ambiguous role or uncovered portion still needs a probe.
    """
    regions = {}
    for layer in sorted(metals.keys() | uncertain.keys() | landings.keys()):
        region = lvs.polygons_of_net(net, layer, True)
        if region is not None and not region.is_empty():
            regions[layer] = region & landings[layer] if layer in landings else region

    for layer, candidates in uncertain.items():
        if layer not in regions or len(candidates) != 1:
            continue
        if any(
            role == candidates[0]
            and metal in regions
            and (regions[layer] - regions[metal]).is_empty()
            for metal, role in metals.items()
        ):
            del regions[layer]
    return regions

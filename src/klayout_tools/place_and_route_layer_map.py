"""Layer-map diagnostics for the route report and its SPEF extraction step."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .place_and_route_request import PlaceAndRouteError


def layer_map_warnings(layer_map_info: dict[str, Any] | None) -> list[str]:
    """Warn when the DEF->GDS merge resolved no KLayout LEF/DEF layer map."""
    if layer_map_info is None or layer_map_info.get("resolution") != "none":
        return []
    return [
        "no KLayout LEF/DEF layer map was resolved: routing layers and vias "
        "were written with KLayout's sequential default layer numbers and may "
        "overlap the PDK's device layers, so DRC results on the GDS are "
        "unreliable; supply one at <assets.klayout>/tech/<variant>.map (or "
        "family-level <family>.map) for the PDK's klt-pdk.json"
    ]


@contextmanager
def explain_spef_layer_map(layer_map_info: dict[str, Any] | None) -> Iterator[None]:
    """Include the merge warning when a post-route SPEF operation fails."""
    try:
        yield
    except PlaceAndRouteError as error:
        warnings = layer_map_warnings(layer_map_info)
        if not warnings:
            raise
        raise PlaceAndRouteError(
            "\n".join([str(error), *warnings]),
            code=error.code,
            job_id=error.job_id,
            details=error.details,
        ) from error

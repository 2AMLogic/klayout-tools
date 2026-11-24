"""Multicycle request validation and engine diagnostics for place-and-route."""

from __future__ import annotations

from typing import Any

from ._sta_multicycle import validate_multicycle_paths
from .place_and_route_request import PlaceAndRouteError
from .post_route_sta_errors import PostRouteStaError


class MulticycleConstraintError(PlaceAndRouteError):
    """A request error shared by every rebuild corner."""


def validated_entries(
    constraints: dict[str, Any], clock_period_ns: float | None
) -> list[dict[str, Any]] | None:
    try:
        entries = validate_multicycle_paths(constraints)
    except PostRouteStaError as exc:
        raise MulticycleConstraintError(str(exc)) from exc
    if entries and clock_period_ns is None:
        raise MulticycleConstraintError(
            "constraints.multicycle_paths requires constraints.clock_period_ns"
        )
    return entries


def response_fields(entries: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {"multicycle_paths": entries} if entries else {}

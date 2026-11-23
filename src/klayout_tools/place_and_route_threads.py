"""OpenROAD worker capacity and observed thread counts for place-and-route."""

from __future__ import annotations

import os
import re
from typing import Any

from .place_and_route_request import PlaceAndRouteError


def validate_threads(request: dict[str, Any], override: int | str | None) -> None:
    if override is not None:
        request["threads"] = override
    value = request.get("threads", 1)
    if value != "max" and (type(value) is not int or value < 1):
        raise PlaceAndRouteError("request.threads must be a positive integer or 'max'")


def resolve_threads(value: int | str) -> int:
    """Resolve 'max' on the machine performing the run."""
    if isinstance(value, int):
        return value
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        try:
            return len(affinity(0))
        except OSError:
            pass
    return os.cpu_count() or 1


def observed_threads(stdout: str, requested: int) -> int:
    match = re.search(r"^\[INFO ORD-0030\] Using (\d+) thread\(s\)\.", stdout, re.M)
    if match:
        return int(match[1])
    if requested == 1:
        return 1
    raise PlaceAndRouteError(
        "OpenROAD did not report the effective thread count (ORD-0030)"
    )

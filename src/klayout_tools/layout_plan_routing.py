"""Per-net routing hints for ``klt layout-plan-execute`` (issue #5094).

Split out of :mod:`klayout_tools.layout_plan_execute` to keep that module
under its file-size ratchet. ``request.routing.nets`` maps a net name to
``{layer_role?, width_um?, waypoints_um?, legs?}``; :func:`apply_net_routing`
copies each entry onto the matching derived ``connectivity[]`` entry
(``gen_compose``'s per-net override, issue #1655). Validation errors are
raised as the ``error`` class the caller passes in, so the caller keeps its
own exception type.
"""

from __future__ import annotations

from typing import Any

from .gen_compose import GenComposeError, _parse_legs, parse_waypoints_um

#: Allowed keys in one ``request.routing.nets[<net>]`` entry.
ALLOWED_NET_ROUTING_KEYS = {"layer_role", "width_um", "waypoints_um", "legs"}


def require_positive_number(value: Any, name: str, error: type[Exception]) -> float:
    """Return ``value`` as a float, or raise ``error`` unless it is a
    positive (non-bool) number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise error(f"{name} must be a positive number when given")
    return float(value)


def _resolve_net_entry(
    entry: Any, where: str, error: type[Exception]
) -> dict[str, Any]:
    allowed = ", ".join(sorted(ALLOWED_NET_ROUTING_KEYS))
    if not isinstance(entry, dict):
        raise error(f"{where} must be a JSON object")
    unknown = set(entry) - ALLOWED_NET_ROUTING_KEYS
    if unknown:
        raise error(
            f"{where} has unknown field(s): {', '.join(sorted(unknown))}"
            f" -- allowed: {allowed}"
        )
    if not entry:
        raise error(f"{where} must set at least one of: {allowed}")
    resolved: dict[str, Any] = {}
    if "layer_role" in entry:
        layer_role = entry["layer_role"]
        if not isinstance(layer_role, str) or not layer_role:
            raise error(f"{where}.layer_role must be a non-empty string when given")
        resolved["layer_role"] = layer_role
    if "width_um" in entry:
        resolved["width_um"] = require_positive_number(
            entry["width_um"], f"{where}.width_um", error
        )
    if "waypoints_um" in entry:
        resolved["waypoints_um"] = _resolve_waypoints(
            entry["waypoints_um"], where, error
        )
    if "legs" in entry:
        if "waypoints_um" in entry:
            raise error(
                f"{where} cannot set both 'waypoints_um' and 'legs' -- "
                "'waypoints_um' steers a 2-pin net's single path, 'legs' "
                "steers individual legs of a net by pin name"
            )
        legs = entry["legs"]
        if not isinstance(legs, list) or not legs:
            raise error(f"{where}.legs must be a non-empty array of leg objects")
        resolved["legs"] = legs
    return resolved


def _resolve_waypoints(raw: Any, where: str, error: type[Exception]) -> Any:
    """Validate with ``gen_compose``'s own parser (plane-bearing triples
    included) and return the raw value so it is forwarded unchanged."""
    if raw is None:
        raise error(f"{where}.waypoints_um must be a non-empty array when given")
    try:
        parse_waypoints_um(raw, net="", index=0, roles_out=[])
    except GenComposeError as exc:
        prefix = "request.connectivity[0] (net '').waypoints_um"
        message = str(exc)
        if message.startswith(prefix):
            message = f"{where}.waypoints_um" + message[len(prefix) :]
        raise error(message) from exc
    return raw


def resolve_net_routing(raw: Any, error: type[Exception]) -> dict[str, dict[str, Any]]:
    """Validate ``request.routing.nets`` and return it normalised."""
    if not isinstance(raw, dict):
        raise error(
            "request.routing.nets must be a JSON object mapping net name to "
            "{layer_role?, width_um?, waypoints_um?, legs?}"
        )
    return {
        net: _resolve_net_entry(entry, f"request.routing.nets[{net!r}]", error)
        for net, entry in raw.items()
    }


def _validate_legs(
    legs: Any,
    entry: dict[str, Any],
    net: str,
    index: int,
    error: type[Exception],
) -> None:
    """Validate ``legs`` against the derived entry's own pins with
    ``gen_compose``'s ``_parse_legs`` (unknown pin, same pin twice, unknown
    key, bad waypoints) and re-point its messages at the request field."""
    pins = [pin for pin in entry.get("pins", []) if isinstance(pin, dict)]
    try:
        _parse_legs(legs, net=net, index=index, parsed_pins=pins)
    except GenComposeError as exc:
        prefix = f"request.connectivity[{index}] (net '{net}').legs"
        message = str(exc)
        if message.startswith(prefix):
            message = f"request.routing.nets[{net!r}].legs" + message[len(prefix) :]
        raise error(message) from exc


def apply_net_routing(
    connectivity: list[dict[str, Any]],
    nets: dict[str, dict[str, Any]],
    error: type[Exception] = ValueError,
) -> list[str]:
    """Copy per-net overrides onto the matching derived ``connectivity[]``
    entries in place; one ``warnings[]`` entry per net name that matched
    none. ``legs`` is validated here, not in :func:`resolve_net_routing`,
    because it can only be checked against the derived entry's own pins."""
    by_net = {entry["net"]: entry for entry in connectivity}
    index_of = {entry["net"]: i for i, entry in enumerate(connectivity)}
    warnings: list[str] = []
    for net, override in nets.items():
        entry = by_net.get(net)
        if entry is None:
            warnings.append(
                f"request.routing.nets[{net!r}] matches no derived "
                "connectivity entry (net is unmapped, has fewer than 2 pins, "
                "or does not exist); override ignored"
            )
            continue
        if "legs" in override:
            _validate_legs(override["legs"], entry, net, index_of[net], error)
        entry.update(override)
    return warnings

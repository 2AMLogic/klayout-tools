"""Per-net routing hints for ``klt layout-plan-execute`` (issue #5094).

Split out of :mod:`klayout_tools.layout_plan_execute` to keep that module
under its file-size ratchet. ``request.routing.nets`` maps a net name to
``{layer_role?, width_um?}``; :func:`apply_net_routing` copies each entry onto
the matching derived ``connectivity[]`` entry (``gen_compose``'s per-net
override, issue #1655). Validation errors are raised as the ``error`` class the
caller passes in, so the caller keeps its own exception type.
"""

from __future__ import annotations

from typing import Any

#: Allowed keys in one ``request.routing.nets[<net>]`` entry.
ALLOWED_NET_ROUTING_KEYS = {"layer_role", "width_um"}


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
    return resolved


def resolve_net_routing(raw: Any, error: type[Exception]) -> dict[str, dict[str, Any]]:
    """Validate ``request.routing.nets`` and return it normalised."""
    if not isinstance(raw, dict):
        raise error(
            "request.routing.nets must be a JSON object mapping net name to "
            "{layer_role?, width_um?}"
        )
    return {
        net: _resolve_net_entry(entry, f"request.routing.nets[{net!r}]", error)
        for net, entry in raw.items()
    }


def apply_net_routing(
    connectivity: list[dict[str, Any]], nets: dict[str, dict[str, Any]]
) -> list[str]:
    """Copy per-net overrides onto the matching derived ``connectivity[]``
    entries in place; one ``warnings[]`` entry per net name that matched
    none."""
    by_net = {entry["net"]: entry for entry in connectivity}
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
        entry.update(override)
    return warnings

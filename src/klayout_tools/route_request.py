"""Request parsing and the error type for ``klt route`` (#5205).

See :mod:`klayout_tools.route_labelled` for the verb's design and
``docs/cli/route.md`` for the request/report contract.
"""

from __future__ import annotations

from typing import Any, TypeGuard

from ._paths import _resolve_relative
from ._request_fields import check_known_fields

#: Top-level request fields (``schema`` is accepted by ``check_known_fields``).
_KNOWN_FIELDS = ("layout", "pdk", "routing", "nets", "options")

_OVERLAP_CHECKS = ("report",)


class RouteError(Exception):
    """A ``klt route`` request that cannot be fulfilled (exit code 1).

    ``code`` / ``details`` are the optional machine-readable classification
    emitted as ``error.code`` and extra ``error`` fields in the JSON envelope.
    """

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_object(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RouteError(f"{where} must be a JSON object")
    return raw


def _require_str(raw: dict[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise RouteError(f"{where}.{key} is required (a non-empty string)")
    return value


def _check_keys(raw: dict[str, Any], known: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise RouteError(
            f"{where} has unknown field(s): {', '.join(unknown)} -- "
            f"known: {', '.join(known)}"
        )


# --- request parsing ----------------------------------------------------------


def _parse_pin(raw: Any, where: str) -> dict[str, Any]:
    from .gen_compose import GenComposeError
    from .gen_compose_port_access import parse_ports_from_labels

    pin = _require_object(raw, where)
    _check_keys(pin, ("cell", "label", "layers", "access", "direction_deg"), where)
    cell = _require_str(pin, "cell", where)
    label = _require_str(pin, "label", where)
    name: Any = label
    if pin.get("access") is not None:
        name = {"name": label, "access": pin["access"]}
    try:
        spec = parse_ports_from_labels(
            {"layers": pin.get("layers"), "names": [name]}, where
        )
    except GenComposeError as exc:
        raise RouteError(str(exc)) from exc
    assert spec is not None
    direction = pin.get("direction_deg")
    if direction is not None and (
        not _is_number(direction) or float(direction) not in (0.0, 90.0, 180.0, 270.0)
    ):
        raise RouteError(f"{where}.direction_deg must be one of 0, 90, 180, 270")
    return {
        "cell": cell,
        "label": label,
        "spec": spec,
        "direction_deg": None if direction is None else int(direction),
    }


def parse_request(request: Any, request_dir: str) -> dict[str, Any]:
    request = _require_object(request, "request")
    check_known_fields(request, _KNOWN_FIELDS, verb="route", error_cls=RouteError)

    layout = _require_object(request.get("layout"), "request.layout")
    _check_keys(layout, ("gds_path", "top_cell"), "request.layout")
    gds_path = _resolve_relative(
        _require_str(layout, "gds_path", "request.layout"), request_dir
    )
    top_cell = layout.get("top_cell")
    if top_cell is not None and (not isinstance(top_cell, str) or not top_cell):
        raise RouteError("request.layout.top_cell must be a non-empty string")

    pdk = _require_object(request.get("pdk"), "request.pdk")
    variant = _require_str(pdk, "variant", "request.pdk")

    routing = _require_object(request.get("routing"), "request.routing")
    _check_keys(routing, ("layer_role", "width_um"), "request.routing")
    layer_role = _require_str(routing, "layer_role", "request.routing")
    width_um = routing.get("width_um")
    if not _is_number(width_um) or float(width_um) <= 0:
        raise RouteError("request.routing.width_um is required (a number > 0)")

    raw_nets = request.get("nets")
    if not isinstance(raw_nets, list):
        raise RouteError("request.nets must be an array")
    nets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_net in enumerate(raw_nets):
        where = f"request.nets[{index}]"
        net = _require_object(raw_net, where)
        _check_keys(net, ("net", "pins"), where)
        name = _require_str(net, "net", where)
        if name in seen:
            raise RouteError(f"request.nets contains duplicate net '{name}'")
        seen.add(name)
        raw_pins = net.get("pins")
        if not isinstance(raw_pins, list) or len(raw_pins) != 2:
            raise RouteError(
                f"{where}.pins must have exactly two pins (slice 1 routes "
                "two-pin nets only)"
            )
        nets.append(
            {
                "net": name,
                "pins": [
                    _parse_pin(pin, f"{where}.pins[{i}]")
                    for i, pin in enumerate(raw_pins)
                ],
            }
        )

    options = _require_object(request.get("options", {}), "request.options")
    _check_keys(options, ("overlap_check", "output"), "request.options")
    overlap_check = options.get("overlap_check", "report")
    if overlap_check not in _OVERLAP_CHECKS:
        raise RouteError(
            f"request.options.overlap_check '{overlap_check}' is not supported "
            f"-- allowed: {', '.join(_OVERLAP_CHECKS)}"
        )
    output = options.get("output")
    if output is not None and (not isinstance(output, str) or not output):
        raise RouteError("request.options.output must be a non-empty string")

    return {
        "gds_path": gds_path,
        "top_cell": top_cell,
        "variant": variant,
        "layer_role": layer_role,
        "width_um": float(width_um),
        "nets": nets,
        "output": None if output is None else _resolve_relative(output, request_dir),
    }

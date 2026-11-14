"""Request-field helpers split out of ``layout_plan.py`` to keep it under its
file-size ceiling: ``options`` parsing and the group-level tap/ring ``net``
for ``device_groups[]`` (#5095) -- shape check, reference check against the
netlist digest, and plan echo.
"""

from __future__ import annotations

from typing import Any


def parse_group_net(raw_group: dict[str, Any], entry_where: str) -> dict[str, str]:
    """Shape-check the optional ``net``; ``{}`` when absent, else ``{"net": n}``."""
    net = raw_group.get("net")
    if net is None:
        return {}
    if not isinstance(net, str) or not net:
        from .layout_plan import LayoutPlanUsageError

        raise LayoutPlanUsageError(
            f"{entry_where}.net must be a non-empty string when given"
        )
    return {"net": net}


def net_echo(group: dict[str, Any]) -> dict[str, str]:
    """The plan-echo fragment: ``net`` only when declared (additive)."""
    return {"net": group["net"]} if "net" in group else {}


def validate_group_nets(groups: list[dict[str, Any]], digest_nets: Any) -> None:
    """Application-error pass: ``net`` only on a ``guard_ring`` group, and it
    must name a net of the ingested netlist digest."""
    from .layout_plan import LayoutPlanError

    names = {
        n["name"]
        for n in (digest_nets if isinstance(digest_nets, list) else [])
        if isinstance(n, dict) and isinstance(n.get("name"), str)
    }
    for group in groups:
        if "net" not in group:
            continue
        where = f"request.device_groups (id '{group['id']}')"
        if group["generator"] != "guard_ring":
            raise LayoutPlanError(
                f"{where}.net is only supported on a 'guard_ring' group, "
                f"not generator '{group['generator']}'"
            )
        if group["net"] not in names:
            raise LayoutPlanError(
                f"{where}.net '{group['net']}' does not resolve against the "
                "ingested netlist digest's nets"
            )


def parse_options(raw: Any) -> dict[str, Any]:
    from .layout_plan import LayoutPlanUsageError

    if raw is None:
        return {"cell_name": None, "output": None}
    if not isinstance(raw, dict):
        raise LayoutPlanUsageError("request.options must be a JSON object")

    cell_name = raw.get("cell_name")
    if cell_name is not None and (not isinstance(cell_name, str) or not cell_name):
        raise LayoutPlanUsageError(
            "request.options.cell_name must be a non-empty string when given"
        )
    output = raw.get("output")
    if output is not None and (not isinstance(output, str) or not output):
        raise LayoutPlanUsageError(
            "request.options.output must be a non-empty string when given"
        )

    return {"cell_name": cell_name, "output": output}


def ingest_netlist(netlist_spec: dict[str, Any], resolved_path: str) -> dict[str, Any]:
    """Build the netlist digest; an ingest failure is a ``LayoutPlanError``.

    The shared reader's hints name ``klt lvs``'s ``reference.form``/
    ``reference.deck`` keys; a layout-plan author spells the same options
    ``netlist.*``, so the message is rewritten to match (#5135).
    """
    from .layout_plan import LayoutPlanError
    from .lvs import LvsError
    from .netlist_digest import build_netlist_digest

    try:
        return build_netlist_digest(
            resolved_path,
            top=netlist_spec["top"],
            form=netlist_spec["form"],
            deck=netlist_spec["deck"],
            device_map=netlist_spec["device_map"],
        )
    except LvsError as exc:
        detail = str(exc).replace("reference.form", "netlist.form")
        detail = detail.replace("reference.deck", "netlist.deck")
        raise LayoutPlanError(
            f"request.netlist could not be ingested: {detail}"
        ) from exc

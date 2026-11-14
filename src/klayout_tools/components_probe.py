"""``klt components``' point-to-component probes (``--probe``, issue #5552).

Split out of ``components.py``, which is over the repository's per-file line
limit and may not grow. Three pieces, in the order the report uses them:
validate the caller's probe specs, resolve each point against the live
connectivity graph, and name the hit by the component id the report assigns.

The lookup deliberately runs on the very ``LayoutToNetlist``
``components.run_components_report`` builds its components from -- the same
registered region objects, the same clusters -- so a hit can only ever name a
component that report also lists. Nothing here opens a layout or builds a
graph of its own.
"""

from __future__ import annotations

from typing import Any


def _require_probe_name(entry: Any, error_cls: type[Exception]) -> str:
    """The ``name`` of one probe entry, or raise -- the probe-side twin of
    ``components_specs``' ``_require_name`` (kept here so this module needs
    no import back into the spec validators)."""
    if not isinstance(entry, dict):
        raise error_cls(f"each probes entry must be an object, got {entry!r}")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise error_cls("each probes entry requires a non-empty 'name' string")
    return name


def validate_probes(
    probes: list[dict[str, Any]],
    conductor_specs: list[dict[str, Any]],
    error_cls: type[Exception],
) -> list[dict[str, Any]]:
    """Validate the ``probes`` point lookups (issue #5552).

    ``layer`` must name one of the declared ``conductors`` -- the piece
    ``cli/_parsing.parse_probes`` deliberately leaves to the engine, since
    only the engine knows the caller's conductor names. The error quotes the
    declared names the way an unknown via/label-layer conductor does.

    ``error_cls`` is the exception to raise (``ComponentsError`` for every
    caller today) -- the same ``ErrorFactory`` convention
    ``cli/_parsing.parse_probes`` itself follows, which is what keeps this
    module free of an import back into ``components``.
    """
    conductor_names = [spec["name"] for spec in conductor_specs]

    specs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in probes:
        name = _require_probe_name(entry, error_cls)
        if name in seen_names:
            raise error_cls(f"duplicate probe name: {name!r}")
        seen_names.add(name)

        layer = entry.get("layer")
        if not isinstance(layer, str) or layer not in conductor_names:
            raise error_cls(
                f"probes entry {name!r} 'layer' must name one of the declared "
                f"conductors ({', '.join(conductor_names)}), got {layer!r}"
            )

        point: dict[str, float] = {}
        for axis in ("x", "y"):
            value = entry.get(axis)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise error_cls(
                    f"probes entry {name!r} {axis!r} must be a number "
                    f"(micrometres), got {value!r}"
                )
            point[axis] = float(value)

        specs.append({"name": name, "layer": layer, "x": point["x"], "y": point["y"]})
    return specs


def probe_hits(
    kdb: Any,
    l2n: Any,
    conductor_regions: dict[str, Any],
    probe_specs: list[dict[str, Any]],
    cell_name: str,
    dbu: float,
    *,
    graph_empty: bool,
) -> dict[str, tuple[str, int] | None]:
    """Resolve each probe point to ``(cell name, cluster id)``, or ``None``
    where the named conductor carries no net there (issue #5552).

    ``probe_net`` is given the *registered* region object out of
    ``conductor_regions`` -- the post-qualifier, post-``--region``-clip region
    that was ``register()``ed and ``connect()``ed -- which is what makes a hit
    one of the clusters the reported components are built from. A point on
    empty space, on a conductor other than the one named, or outside the clip
    window simply has no net: that is ``None``, not an error.

    ``graph_empty`` short-circuits every probe to ``None`` for a cell whose
    circuit the engine never produced (nothing could be reported there
    either, so no probe can resolve to a component).
    """
    hits: dict[str, tuple[str, int] | None] = {}
    for spec in probe_specs:
        net = None
        if not graph_empty:
            point = kdb.Point(round(spec["x"] / dbu), round(spec["y"] / dbu))
            net = l2n.probe_net(conductor_regions[spec["layer"]], point)
        hits[spec["name"]] = None if net is None else (cell_name, net.cluster_id)
    return hits


def probe_entry(
    spec: dict[str, Any],
    hit: tuple[str, int] | None,
    component_ids: dict[tuple[str, int], str],
) -> dict[str, Any]:
    """One ``probes[]`` entry: the spec echoed back plus the id of the
    component that owns the named conductor's shape under the point.

    ``found`` is ``True`` only when the probed cluster is one the report
    itself lists, so ``component_id`` can never name a component the caller
    cannot find in ``components[]``.
    """
    component_id = None if hit is None else component_ids.get(hit)
    return {
        "name": spec["name"],
        "layer": spec["layer"],
        "x_um": spec["x"],
        "y_um": spec["y"],
        "found": component_id is not None,
        "component_id": component_id,
    }

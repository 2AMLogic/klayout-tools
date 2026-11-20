"""Composed-frame mirrors for ``blocks[].derived_ports[]`` (issue #5609).

``blocks[].derived_ports[]`` is built by
:func:`gen_compose_port_access.parse_ports_from_labels` in the **source
cell's own** frame -- un-oriented, un-translated -- while every other
per-block coordinate the ``gen-compose`` response reports (``bbox_um``, the
promoted ``ports[]``) is oriented by the block's ``orientation`` (#1166) and
then translated by its ``offset_um`` into the composed frame. Nothing marked
that difference, so a caller driving its own router or via-drop from
``landing.x_um`` silently placed geometry at the wrong coordinates for any
block not placed at the origin. :func:`add_composed_frames` adds the
composed-frame mirror fields at the response's single emission site; the
source-frame fields keep their exact values (additive -- no
``schema_version`` bump), and the parse-time data is never mutated.
"""

from typing import Any


def add_composed_frames(
    block: dict[str, Any], offset_um: dict[str, float]
) -> list[dict[str, Any]]:
    """One block's ``derived_ports`` reports, plus composed-frame mirrors.

    Applies the identical transform every other per-block coordinate gets:
    orient each source-frame point about the block's own origin via
    :func:`gen_compose._apply_orientation_um` (imported function-local --
    ``gen_compose`` imports this module, so a module-level import would be
    circular), then add ``offset_um``. Each entry gains
    ``label_composed_um: {x, y}``; a non-null ``landing`` additionally gains
    ``x_composed_um``/``y_composed_um`` beside its unchanged ``x_um``/
    ``y_um``. Returns new dicts -- the parse-time
    ``blocks[id]["derived_ports"]`` stays source-frame for any other reader.
    """
    from .gen_compose import _apply_orientation_um

    orientation = block.get("orientation", "none")
    offset_x, offset_y = offset_um["x"], offset_um["y"]
    framed: list[dict[str, Any]] = []
    for report in block["derived_ports"]:
        entry = dict(report)
        label = report["label_um"]
        lx, ly = _apply_orientation_um(
            float(label["x"]), float(label["y"]), orientation
        )
        entry["label_composed_um"] = {"x": lx + offset_x, "y": ly + offset_y}
        landing = report.get("landing")
        if landing is not None:
            x, y = _apply_orientation_um(
                float(landing["x_um"]), float(landing["y_um"]), orientation
            )
            entry["landing"] = {
                **landing,
                "x_composed_um": x + offset_x,
                "y_composed_um": y + offset_y,
            }
        framed.append(entry)
    return framed

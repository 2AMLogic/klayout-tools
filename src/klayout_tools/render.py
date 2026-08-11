"""Render per-layer PNG images from a GDSII/OASIS stream.

Pure library: :func:`render_report` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``layers.py``.
Serialisation and human-readable formatting live in the CLI command module so
this function stays reusable (e.g. by a future MCP server or the gallery
site's content pipeline, #62).

Headless invariant: uses the pip ``klayout`` package's ``klayout.lay``
module, whose ``LayoutView`` renders offscreen without a GUI, Qt display, or
X server -- no ``xvfb`` required. Runnable in CI.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .layers import LayersError, layers_report

#: Default render dimensions in pixels, chosen for gallery-thumbnail use.
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 768

#: Filename pattern this module owns inside an output directory -- used to
#: clear stale renders (e.g. from a layer set that shrank on re-render)
#: without touching files this command didn't write.
_OWNED_FILENAME_RE = re.compile(r"^(-?\d+_-?\d+|overview)\.png$")

#: Filename of the all-layers composite image.
OVERVIEW_FILENAME = "overview.png"

#: Hex ``#rrggbb`` (or ``#rgb``) color accepted by ``--background``.
_BACKGROUND_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class RenderError(Exception):
    """Raised when a layout cannot be rendered.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def _layer_image_filename(layer: int, datatype: int) -> str:
    """The deterministic, downstream-parseable filename for one layer's PNG."""
    return f"{layer}_{datatype}.png"


def render_report(
    path: str,
    output_dir: str | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    background: str = "#ffffff",
    top: str | None = None,
    layers: list[tuple[int, int]] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Render one PNG per non-empty layer of a GDSII or OASIS stream.

    KLayout auto-detects the stream format on read, so both ``.gds`` and
    ``.oas`` inputs are handled by the same code path.

    Args:
        path: path to a GDSII or OASIS layout file.
        output_dir: directory to write PNGs into. Defaults to a ``renders/``
            subdirectory next to the input file (i.e. ``<file-dir>/renders/``)
            -- so a layout at ``<block>/output/<name>.gds`` (the convention
            the gallery content pipeline, #62, is expected to follow) renders
            to ``<block>/output/renders/`` with no block-specific logic.
        width: image width in pixels (must be positive).
        height: image height in pixels (must be positive).
        background: canvas color as a ``#rrggbb``/``#rgb`` hex string.
        layers: optional set of ``(layer, datatype)`` pairs to restrict
            rendering to (issue #673). ``None`` (the default) renders every
            non-empty layer, unchanged from before this option existed. When
            given, only layers whose pair is in this set are written to a
            PNG (and included in the all-layers ``overview.png`` composite)
            -- every other layer still appears in ``layers[]`` with
            ``rendered: false, path: null``, exactly like a declared-but-
            empty layer. An empty list is rejected (see below); a pair
            absent from the stream simply matches nothing.
        bbox: optional ``(left, bottom, right, top)`` physical crop window in
            micrometres (issue #673). ``None`` (the default) fits the whole
            layout, unchanged from before this option existed. When given,
            the viewport is set via ``LayoutView.zoom_box()`` instead of
            ``zoom_fit()`` -- which already expands the requested box to the
            output image's pixel aspect ratio rather than stretching it (see
            ``actual_extent`` below), so no manual aspect-ratio math is
            needed here.

    Besides the per-layer PNGs, an all-layers composite is written as
    ``overview.png`` -- the "what does this block look like" image (gallery
    thumbnails, agent quick-looks). When ``layers`` is given, the overview
    only shows the selected layers too, so it stays consistent with what was
    actually asked for rather than leaking unrequested geometry.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/render.md``)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "output_dir": <resolved output directory, absolute or as derived>,
            "width": <int>,
            "height": <int>,
            "background": <hex color, str>,
            "overview": <path to the all-layers composite PNG>,
            "layer_count": <number of layers in the stream, int>,
            "rendered_count": <number of per-layer PNGs written, int>,
            "requested_layers": [[layer, datatype], ...] | None,
            "requested_bbox": {"left": float, "bottom": float,
                                "right": float, "top": float} | None,
            "actual_extent": {"left": float, "bottom": float,
                               "right": float, "top": float},
            "layers": [
                {
                    "layer": int,
                    "datatype": int,
                    "name": str | None,
                    "shapes": int,
                    "annotation": bool,
                    "path": <path to the PNG, or None if not rendered>,
                    "rendered": bool,
                },
                ...
            ],
        }

    ``schema_version`` is versioned independently per command (see
    ``docs/json-contract.md``); it starts at ``1``. ``requested_layers``,
    ``requested_bbox``, and ``actual_extent`` are additive fields (issue
    #673) -- no ``schema_version`` bump, per ``docs/json-contract.md``.

    Layers reporting ``shapes: 0`` (declared but empty -- see
    ``layers_report()``), or excluded by a caller-supplied ``layers`` set,
    are listed but not rendered (``rendered: false``, ``path: null``): an
    isolated render of an empty or unselected layer is either blank or not
    what was asked for, neither of which is worth the render cost.

    Each ``layers[]`` entry's ``annotation`` field is passed straight through
    from ``layers_report()`` -- see its docstring -- so a layer in the
    reserved 990-999 range is still named as such here, even if it happens
    to carry shapes and gets rendered.

    ``top`` (issue #554) restricts the render to one named top cell's own
    hierarchy instead of the whole stream (today's default, unchanged when
    omitted): the per-layer ``shapes``/render-or-skip decision comes from
    ``layers_report(path, top=top)`` (already scoped, see its docstring),
    and the offscreen ``LayoutView``'s active cellview is switched to
    ``top`` before rendering (``CellView.set_cell_name``) so
    ``max_hier``/``zoom_fit`` -- and therefore both the overview and every
    per-layer PNG -- only draw geometry reachable from that cell. A named
    cell absent from the stream is a :class:`RenderError`, matching
    ``klt ring-check --top``.

    Raises :class:`RenderError` if the file is missing, unreadable, not a
    recognisable layout stream, ``top`` names a cell absent from the
    stream, ``width``/``height`` is not positive, ``layers`` is an empty
    (but non-``None``) list, or ``bbox`` does not satisfy
    ``right > left and top > bottom``.
    """
    if width <= 0 or height <= 0:
        raise RenderError(f"invalid image size: {width}x{height}")
    if not _BACKGROUND_RE.match(background):
        raise RenderError(
            f"invalid background color '{background}' (expected #rrggbb or #rgb)"
        )
    if layers is not None and not layers:
        raise RenderError("--layers must contain at least one layer")
    if bbox is not None:
        bbox_left, bbox_bottom, bbox_right, bbox_top = bbox
        if bbox_right <= bbox_left or bbox_top <= bbox_bottom:
            raise RenderError(
                "invalid --bbox: right/top must exceed left/bottom, got "
                f"({bbox_left}, {bbox_bottom}, {bbox_right}, {bbox_top})"
            )

    try:
        report = layers_report(path, top=top)
    except LayersError as exc:
        raise RenderError(str(exc)) from exc

    resolved_output_dir = output_dir or os.path.join(
        os.path.dirname(os.path.abspath(path)), "renders"
    )
    os.makedirs(resolved_output_dir, exist_ok=True)
    _clear_owned_files(resolved_output_dir)

    # Imported lazily, mirroring layers.py's lazy `klayout.db` import, so
    # `klt --version` and argument parsing don't pay the module's load cost.
    import klayout.db as kdb
    import klayout.lay as lay

    selected_pairs = set(layers) if layers is not None else None

    view = lay.LayoutView()
    try:
        view.set_config("background-color", background)
        view.load_layout(path, 0)
    except Exception as exc:  # klayout raises RuntimeError for bad/unknown streams
        view.destroy()
        raise RenderError(f"could not read layout '{path}': {exc}") from exc

    try:
        if top is not None:
            # `layers_report(path, top=top)` above already validated `top`
            # exists in this same stream, so this is not expected to fail --
            # checked anyway (`cellview.cell is None` on an unknown name)
            # rather than trusting that invariant silently, since a render
            # against every cell in the stream instead of a nonexistent
            # `--top` would be a silent wrong-answer, not a loud failure.
            cellview = view.cellview(0)
            cellview.set_cell_name(top)
            if cellview.cell is None:
                raise RenderError(f"top cell not found in stream: {top}")

        view.add_missing_layers()
        view.max_hier()
        view.resize(width, height)
        if bbox is not None:
            bbox_left, bbox_bottom, bbox_right, bbox_top = bbox
            # `zoom_box()` already expands the requested box to the canvas's
            # pixel aspect ratio (padding the shorter axis, centered on the
            # request) rather than stretching the image to fit -- verified
            # against the installed klayout package, 2026-08-11 -- so no
            # manual aspect-ratio math is needed here, matching `zoom_fit()`'s
            # existing whole-layout behaviour.
            view.zoom_box(kdb.DBox(bbox_left, bbox_bottom, bbox_right, bbox_top))
        else:
            view.zoom_fit()

        actual_box = view.box()
        actual_extent = {
            "left": actual_box.left,
            "bottom": actual_box.bottom,
            "right": actual_box.right,
            "top": actual_box.top,
        }

        nodes = []
        it = view.begin_layers()
        while not it.at_end():
            nodes.append(it.current())
            it.next()

        if selected_pairs is not None:
            # Restrict visibility to the requested layer set before the
            # overview capture below, so the composite reflects the
            # selection instead of leaking unrequested geometry (issue
            # #673). With no selection, visibility is left exactly as
            # `add_missing_layers()` set it up -- today's unchanged default
            # (every layer visible).
            for node in nodes:
                node.visible = (
                    node.source_layer,
                    node.source_datatype,
                ) in selected_pairs

        # Capture the composite overview before switching to per-layer
        # isolation below.
        overview_path = os.path.join(resolved_output_dir, OVERVIEW_FILENAME)
        try:
            view.save_image(overview_path, width, height)
        except Exception as exc:
            raise RenderError(f"could not render overview: {exc}") from exc

        for node in nodes:
            node.visible = False

        rendered: list[dict[str, Any]] = []
        for entry in report["layers"]:
            layer, datatype = entry["layer"], entry["datatype"]
            is_selected = selected_pairs is None or (layer, datatype) in selected_pairs
            if entry["shapes"] == 0 or not is_selected:
                rendered.append({**entry, "path": None, "rendered": False})
                continue

            matches = [
                node
                for node in nodes
                if node.source_layer == layer and node.source_datatype == datatype
            ]
            for node in matches:
                node.visible = True

            out_path = os.path.join(
                resolved_output_dir, _layer_image_filename(layer, datatype)
            )
            try:
                view.save_image(out_path, width, height)
            except Exception as exc:
                raise RenderError(
                    f"could not render layer {layer}/{datatype}: {exc}"
                ) from exc

            for node in matches:
                node.visible = False

            rendered.append({**entry, "path": out_path, "rendered": True})
    finally:
        view.destroy()

    return {
        "schema_version": 1,
        "file": path,
        "output_dir": resolved_output_dir,
        "width": width,
        "height": height,
        "background": background,
        "overview": overview_path,
        "layer_count": report["layer_count"],
        "rendered_count": sum(1 for entry in rendered if entry["rendered"]),
        "requested_layers": (
            [[layer, datatype] for layer, datatype in layers]
            if layers is not None
            else None
        ),
        "requested_bbox": (
            {
                "left": bbox[0],
                "bottom": bbox[1],
                "right": bbox[2],
                "top": bbox[3],
            }
            if bbox is not None
            else None
        ),
        "actual_extent": actual_extent,
        "layers": rendered,
    }


def _clear_owned_files(output_dir: str) -> None:
    """Remove PNGs this command previously wrote to *output_dir*.

    Only removes files matching this module's own ``<layer>_<datatype>.png``
    naming pattern, so a re-render (e.g. after a layer was removed from the
    design) doesn't leave a stale image behind, without touching any other
    file a caller may have placed in the same directory.
    """
    try:
        entries = os.listdir(output_dir)
    except OSError:
        return
    for name in entries:
        if _OWNED_FILENAME_RE.match(name):
            try:
                os.remove(os.path.join(output_dir, name))
            except OSError:
                pass

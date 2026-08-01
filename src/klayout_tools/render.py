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

    Besides the per-layer PNGs, an all-layers composite is written as
    ``overview.png`` -- the "what does this block look like" image (gallery
    thumbnails, agent quick-looks).

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
            "layers": [
                {
                    "layer": int,
                    "datatype": int,
                    "name": str | None,
                    "shapes": int,
                    "path": <path to the PNG, or None if not rendered>,
                    "rendered": bool,
                },
                ...
            ],
        }

    ``schema_version`` is versioned independently per command (see
    ``docs/json-contract.md``); it starts at ``1``.

    Layers reporting ``shapes: 0`` (declared but empty -- see
    ``layers_report()``) are listed but not rendered (``rendered: false``,
    ``path: null``): an isolated render of an empty layer is a blank image,
    which carries no information worth the render cost.

    Raises :class:`RenderError` if the file is missing, unreadable, not a
    recognisable layout stream, or ``width``/``height`` is not positive.
    """
    if width <= 0 or height <= 0:
        raise RenderError(f"invalid image size: {width}x{height}")
    if not _BACKGROUND_RE.match(background):
        raise RenderError(
            f"invalid background color '{background}' (expected #rrggbb or #rgb)"
        )

    try:
        report = layers_report(path)
    except LayersError as exc:
        raise RenderError(str(exc)) from exc

    resolved_output_dir = output_dir or os.path.join(
        os.path.dirname(os.path.abspath(path)), "renders"
    )
    os.makedirs(resolved_output_dir, exist_ok=True)
    _clear_owned_files(resolved_output_dir)

    # Imported lazily, mirroring layers.py's lazy `klayout.db` import, so
    # `klt --version` and argument parsing don't pay the module's load cost.
    import klayout.lay as lay

    view = lay.LayoutView()
    try:
        view.set_config("background-color", background)
        view.load_layout(path, 0)
    except Exception as exc:  # klayout raises RuntimeError for bad/unknown streams
        view.destroy()
        raise RenderError(f"could not read layout '{path}': {exc}") from exc

    try:
        view.add_missing_layers()
        view.max_hier()
        view.resize(width, height)
        view.zoom_fit()

        # All layers are still visible here: capture the composite overview
        # before switching to per-layer isolation.
        overview_path = os.path.join(resolved_output_dir, OVERVIEW_FILENAME)
        try:
            view.save_image(overview_path, width, height)
        except Exception as exc:
            raise RenderError(f"could not render overview: {exc}") from exc

        nodes = []
        it = view.begin_layers()
        while not it.at_end():
            nodes.append(it.current())
            it.next()
        for node in nodes:
            node.visible = False

        rendered: list[dict[str, Any]] = []
        for entry in report["layers"]:
            layer, datatype = entry["layer"], entry["datatype"]
            if entry["shapes"] == 0:
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

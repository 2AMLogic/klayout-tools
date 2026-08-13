"""Shared helpers used by both gallery content-pipeline scripts
(``scripts/bootstrap-gallery-blocks.py`` and ``scripts/ingest-canary.py``).

Renders every non-empty per-layer PNG `render_report` writes (plus the
all-layers `overview.png` composite) into ``layout["renders"]``, keyed by a
human-readable label -- issue #942, "Option B" from #651's deferred child
#651 (Option A only ever recorded the fixed ``{"overview": ...}`` entry,
discarding the per-layer PNGs the same call wrote alongside it). Optionally
also renders one zoomed "center crop" composite (via `render_report`'s
`bbox` parameter, issue #673) so a detail page can show more than the full-
die postage stamp.

Extracted in issue #670 after the two scripts' near-identical
``_attach_overview_render`` copies (both added in the same commit, #658)
started drifting in wording; issue #942 folded the per-layer/crop rendering
in without re-forking the two callers.

The two callers keep printing their own skip message (stdout vs stderr,
different prefixes -- see each script's ``_attach_overview_render``
wrapper) around this shared render-and-attach step, so this module has no
opinion on where/how a failure gets reported.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from klayout_tools.render import RenderError

#: Fraction of the full layout's width/height kept by the automatic "center
#: crop" zoomed render (issue #942's `center_crop` option) -- e.g. `0.5`
#: keeps the middle half of each axis, discarding the outer quarter on
#: every side. Applied uniformly rather than per-cell/PDK-aware, so it works
#: for any block with a layout, not just the two canaries the issue's
#: acceptance criteria names by example.
CENTER_CROP_FRACTION = 0.5

#: Relative-to-`output/` directory a center-crop render is written into --
#: kept separate from the base `renders/` directory so the crop's own
#: per-layer PNGs (written by the same `render_report` call, only the
#: composite is recorded here) never collide with -- or get cleared
#: alongside -- the full-layout renders' filenames.
CENTER_CROP_SUBDIR = "center_crop"


def _center_crop_bbox(
    extent: dict[str, float], fraction: float
) -> tuple[float, float, float, float]:
    """A `(left, bottom, right, top)` box keeping the middle `fraction` of
    each axis of `extent` (as returned by `render_report`'s
    `actual_extent`), centered on the same midpoint."""
    left, bottom, right, top = (
        extent["left"],
        extent["bottom"],
        extent["right"],
        extent["top"],
    )
    center_x, center_y = (left + right) / 2, (bottom + top) / 2
    half_width = (right - left) * fraction / 2
    half_height = (top - bottom) * fraction / 2
    return (
        center_x - half_width,
        center_y - half_height,
        center_x + half_width,
        center_y + half_height,
    )


def _render_label(
    layer: int, datatype: int, layer_names: dict[tuple[int, int], str] | None
) -> str:
    """Human-readable `renders` key for one rendered `(layer, datatype)`
    pair: the PDK's own `name.purpose`-style label when known (from
    `klayout_tools.decks.get_layer_names`), else a `layer_<n>_<n>` fallback
    that at least reads as "layer N/M" once `DetailPage.tsx` formats it,
    never the bare `67_20`-style filename stem this issue's acceptance
    criteria calls out."""
    if layer_names is not None:
        name = layer_names.get((layer, datatype))
        if name:
            return name
    return f"layer_{layer}_{datatype}"


def attach_overview_render(
    gds_path: Path,
    block_dir: Path,
    layout: dict,
    *,
    render_fn: Callable[..., dict],
    layer_names: dict[tuple[int, int], str] | None = None,
    center_crop: bool = False,
) -> RenderError | None:
    """Render `gds_path` into `output/renders/` (via `render_fn`, normally
    `klayout_tools.render.render_report`) and attach the result to
    `layout["renders"]`:

    - `"overview"` -> the all-layers composite, always present on success.
    - one entry per non-empty rendered layer, keyed by `_render_label()`
      (a human-readable PDK layer name when `layer_names` resolves one,
      else a `layer_<n>_<n>` fallback) -- issue #942; previously only the
      fixed `"overview"` entry was recorded ("Option A" from #651),
      discarding these per-layer PNGs even though `render_fn` always wrote
      them to disk.
    - `"center_crop"` -> an additional zoomed composite over the middle
      `CENTER_CROP_FRACTION` of the layout (via a second `render_fn` call
      with `bbox` set), when `center_crop=True`. Best-effort: a crop
      failure is swallowed (the base renders above already succeeded, and
      losing the extra zoom is not worth failing the whole attach step
      over) rather than returned as an error.

    Best-effort overall: on a `RenderError` from the *base* (non-cropped)
    render, `layout` is left untouched and the exception is returned (not
    raised) for the caller to turn into its own skip warning -- callers
    must not fail the whole bootstrap/ingest run over a render failure,
    same treatment as the DRC/signals fields. Returns `None` on success.
    """
    try:
        report = render_fn(
            str(gds_path), output_dir=str(block_dir / "output" / "renders")
        )
    except RenderError as exc:
        return exc

    renders: dict[str, str] = {"overview": "renders/overview.png"}
    for entry in report.get("layers", []):
        if not entry.get("rendered"):
            continue
        layer, datatype = entry["layer"], entry["datatype"]
        label = _render_label(layer, datatype, layer_names)
        renders[label] = f"renders/{layer}_{datatype}.png"

    if center_crop:
        extent = report.get("actual_extent")
        if extent:
            bbox = _center_crop_bbox(extent, CENTER_CROP_FRACTION)
            crop_dir = block_dir / "output" / "renders" / CENTER_CROP_SUBDIR
            try:
                render_fn(str(gds_path), output_dir=str(crop_dir), bbox=bbox)
            except RenderError:
                pass  # best-effort -- keep the base renders already attached
            else:
                renders["center_crop"] = f"renders/{CENTER_CROP_SUBDIR}/overview.png"

    layout["renders"] = renders
    return None


def infer_layer_names(slug: str) -> dict[tuple[int, int], str]:
    """Best-effort PDK guess from a block's `slug`, resolved to that PDK's
    curated `(layer, datatype) -> name` table (`klayout_tools.decks
    .get_layer_names`) -- used only to label canary blocks' `renders` keys,
    since canary repos carry no explicit `pdk` field (unlike the #4 corpus
    pipeline, which already knows its PDK from the `tests/corpus/<pdk>/`
    directory it is walking).

    Every current and documented-candidate canary slug is `<pdk>-<name>`
    (e.g. `gf180-bandgap`, `sky130-bandgap`, `gf180-trng`) -- see
    `blocks/README.md`'s "still-private canaries" list -- so a simple
    prefix match is enough; returns `{}` (no labels -- callers fall back to
    the `layer_<n>_<n>` label) for any slug that doesn't start with a
    recognised PDK prefix, rather than guessing wrong.
    """
    from klayout_tools.decks import get_layer_names

    if slug.startswith("gf180"):
        return get_layer_names("gf180mcu")
    if slug.startswith("sky130"):
        return get_layer_names("sky130")
    return {}


# Re-exported so callers that only need the type hint don't need to reach
# into klayout_tools.render themselves.
__all__ = [
    "CENTER_CROP_FRACTION",
    "attach_overview_render",
    "infer_layer_names",
]

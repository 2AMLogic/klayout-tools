"""Shared helpers used by both gallery content-pipeline scripts
(``scripts/bootstrap-gallery-blocks.py`` and ``scripts/ingest-canary.py``).

Currently just the overview-render attachment step (issue #651, Option A --
only the all-layers composite thumbnail is tracked in git, not the per-layer
PNGs the same `klt render` call also writes alongside it). Extracted in
issue #670 after the two scripts' near-identical ``_attach_overview_render``
copies (both added in the same commit, #658) started drifting in wording.

The two callers keep printing their own skip message (stdout vs stderr,
different prefixes -- see each script's ``_attach_overview_render`` wrapper)
around this shared render-and-attach step, so this module has no opinion on
where/how a failure gets reported.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from klayout_tools.render import RenderError


def attach_overview_render(
    gds_path: Path,
    block_dir: Path,
    layout: dict,
    *,
    render_fn: Callable[..., dict],
) -> RenderError | None:
    """Render `gds_path`'s all-layers composite into `output/renders/` (via
    `render_fn`, normally `klayout_tools.render.render_report`) and attach it
    to `layout["renders"]` as `{"overview": "renders/overview.png"}`.

    Best-effort: on a `RenderError`, `layout` is left untouched and the
    exception is returned (not raised) for the caller to turn into its own
    skip warning -- callers must not fail the whole bootstrap/ingest run over
    a render failure, same treatment as the DRC/signals fields.  Returns
    `None` on success.
    """
    try:
        render_fn(str(gds_path), output_dir=str(block_dir / "output" / "renders"))
    except RenderError as exc:
        return exc
    layout["renders"] = {"overview": "renders/overview.png"}
    return None

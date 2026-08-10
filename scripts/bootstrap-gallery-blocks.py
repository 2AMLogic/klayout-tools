#!/usr/bin/env python3
"""Bootstrap ``blocks/<slug>/output/layout.json`` from the #4 test corpus.

Provisional bootstrap for the gallery layout data loader (issue #59) —
issue #61 ("Gallery: per-layout metrics extractor") had not landed when this
loader was built, so this script stands in for `klt`'s own metrics
extractor described there. Once #61 lands, regenerate `blocks/` with that
command instead and delete this script.

Usage::

    python scripts/bootstrap-gallery-blocks.py
    python scripts/bootstrap-gallery-blocks.py --skip-signals

Writes one ``blocks/<slug>/output/layout.json`` per
``tests/corpus/<pdk>/*.gds`` file, using ``klt layers`` / ``klt cells``
(invoked via subprocess, so this script has no dependency beyond an
installed ``klt``). One block (``gf180mcu_fd_sc_mcu9t5v0__clkinv_1``) is
deliberately left without a ``layout.json`` so the checked-in ``blocks/``
tree demonstrates the gallery loader's ``no_artifacts`` handling on a real
(non-synthetic) directory — see ``blocks/README.md``.

Downloads / viewer link (issue #652)
-------------------------------------

Every block written here also gets its source GDS copied to
``output/<slug>.gds`` and recorded as ``layout.json``'s ``layout_file``,
with ``downloadable: true``. This is safe for every corpus file (unlike the
canary blocks ingested by ``scripts/ingest-canary.py``): each one is a
standard-cell layout redistributed verbatim from an Apache-2.0-licensed
upstream PDK repo (``tests/corpus/README.md``), so there is no #62
public-repo gate to check. Setting both fields is what makes
``site/scripts/copy-renders.mjs`` stage the file into the built site and
``DetailPage.tsx``'s Downloads section (raw download + the Tiny Tapeout
hosted viewer link, issue #249) actually render instead of staying dead
branches.

Signals (issue #99, epic #90 phase 2)
--------------------------------------

For the 7 blocks with a real ``klt sim``-compatible transistor-level
netlist vendored by ``scripts/fetch-cell-netlists.sh`` (see
``scripts/gallery_signals.py`` for the full PVT-sweep pipeline), this
script also runs the sweep and writes ``output/sim/signals.json`` +
attaches it as ``layout.json``'s ``signals`` field (skipped, with a
warning, when the vendored netlist data hasn't been fetched -- this
bootstrap step is opt-in/best-effort, same as the DRC/renders fields
``klt layout-metrics`` itself treats as optional). ``--skip-signals``
regenerates only the base layers/cells metrics, unchanged from before
issue #99, for a fast local iteration loop.

Renders (issue #651, epic #650 "gallery visuals" phase 1)
------------------------------------------------------------

For every block that gets a ``layout.json`` (i.e. not one of
``NO_ARTIFACTS_SLUGS``), this script also calls ``klt render``'s library
function (:func:`klayout_tools.render.render_report`) against the corpus
GDS and attaches ``layout.json``'s ``renders`` field as
``{"overview": "renders/overview.png"}`` -- **Option A** from #651: only
the all-layers composite thumbnail is tracked in git (``.gitignore``
narrowed accordingly), not the per-layer PNGs the same call also writes
alongside it, so the gallery gets a real thumbnail without putting KLayout
on the deploy path. Best-effort: a render failure is printed as a warning
and does not fail the bootstrap run or affect ``layout["status"]``, same
treatment as the DRC/signals fields.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gallery_signals  # noqa: E402

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# _gallery_common imports from klayout_tools.render, so SRC_DIR must already
# be on sys.path (above) before this import.
from _gallery_common import attach_overview_render  # noqa: E402

# RenderError is not used directly in this module anymore (the shared
# _gallery_common.attach_overview_render helper catches it) -- kept as
# `bg.RenderError` for tests/test_bootstrap_gallery_blocks.py, which raises
# it via a monkeypatched render_report to exercise the best-effort path.
from klayout_tools.render import RenderError, render_report  # noqa: E402,F401

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_ROOT = REPO_ROOT / "tests" / "corpus"
BLOCKS_ROOT = REPO_ROOT / "blocks"
SCHEMA_VERSION = 1

# Intentionally left without a layout.json to demonstrate the gallery
# loader's no_artifacts handling on a real (non-synthetic) block directory
# -- still gets a real signals.json artifact (see bootstrap_block below),
# just never wired into a layout.json.
NO_ARTIFACTS_SLUGS = {"gf180mcu_fd_sc_mcu9t5v0__clkinv_1"}


def run_klt(*args: str) -> dict:
    """Invoke the `klt` CLI and parse its JSON output."""
    result = subprocess.run(
        ["klt", *args, "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _attach_signals(slug: str, block_dir: Path, layout: dict | None) -> None:
    """Run the `klt sim` PVT sweep for ``slug`` (if a vendored netlist is
    available) and attach the result to ``layout`` (when not ``None``).

    Always writes ``output/sim/signals.json`` when a sweep ran (even for
    ``NO_ARTIFACTS_SLUGS`` blocks, where ``layout`` is ``None``) so the
    vendored-netlist-backed artifact exists independent of whether this
    particular block gets a `layout.json` -- see the module docstring.

    The nominal corners' waveform artifacts are staged under
    ``output/signals/`` (see ``gallery_signals._stage_waveforms``); that
    directory is wiped first so a renamed/removed corner never leaves a
    stale, no-longer-referenced artifact behind. Staging is skipped for
    ``NO_ARTIFACTS_SLUGS`` blocks (``layout is None``): with no
    ``layout.json`` to reference them, nothing on the site could ever fetch
    those files, so committing them would be dead weight.
    """
    import shutil
    import tempfile

    if not gallery_signals.netlists_available():
        print(
            "  (skipping signals: pdks/cell-netlists/ not populated -- "
            "run scripts/fetch-cell-netlists.sh first)"
        )
        return

    waveform_dir = block_dir / "output" / gallery_signals.WAVEFORM_SUBDIR
    if waveform_dir.is_dir():
        shutil.rmtree(waveform_dir)

    with tempfile.TemporaryDirectory() as work_dir:
        signals = gallery_signals.run_signals_for_block(
            slug, work_dir, block_dir=str(block_dir) if layout is not None else None
        )
    if signals is None:
        return  # no CellConfig for this slug -- not one of the 7 sim'd cells

    signals_path = gallery_signals.write_signals_json(str(block_dir), signals)
    print(
        f"  {slug}: signals status={signals.get('status')} "
        f"corners={signals.get('corner_count')} -> "
        f"{Path(signals_path).relative_to(REPO_ROOT)}"
    )
    if layout is not None:
        layout["signals"] = signals


def _attach_overview_render(
    slug: str, block_dir: Path, gds_path: Path, layout: dict
) -> None:
    """Render ``gds_path``'s all-layers composite into ``output/renders/``
    and attach it to ``layout["renders"]`` as ``{"overview": ...}``.

    Thin wrapper around the shared ``_gallery_common.attach_overview_render``
    helper (issue #670 -- deduped from a near-identical copy of this function
    in ``scripts/ingest-canary.py``): best-effort, mirroring
    ``_attach_signals``, a render failure prints a warning here and leaves
    ``layout`` untouched rather than aborting the whole bootstrap run. Only
    the fixed ``renders/overview.png`` relative path is recorded (Option A,
    issue #651) -- the per-layer PNGs `render_report` also writes alongside
    it are real files on disk (useful for local inspection) but deliberately
    not listed in `renders`, since `.gitignore` only tracks the composite.
    """
    exc = attach_overview_render(gds_path, block_dir, layout, render_fn=render_report)
    if exc is not None:
        print(f"  {slug}: (skipping render: {exc})")


def bootstrap_block(gds_path: Path, pdk: str, *, skip_signals: bool) -> None:
    slug = gds_path.stem
    rel = gds_path.relative_to(REPO_ROOT)

    block_dir = BLOCKS_ROOT / slug
    output_dir = block_dir / "output"

    if slug in NO_ARTIFACTS_SLUGS:
        output_dir.mkdir(parents=True, exist_ok=True)
        # No layout.json written -- the directory exists, but the artifact
        # does not, exercising the loader's no_artifacts path for real.
        print(f"  {slug}: skipped (intentional no_artifacts demo)")
        if not skip_signals:
            _attach_signals(slug, block_dir, layout=None)
        return

    layers = run_klt("layers", str(rel))
    cells = run_klt("cells", str(rel))
    instance_count = sum(c.get("instances", 0) for c in cells.get("cells", []))

    output_dir.mkdir(parents=True, exist_ok=True)

    # Stage the source GDS into the block's own output/ directory and record
    # it as `layout_file` (issue #652): every corpus file is a standard-cell
    # layout downloaded verbatim from an Apache-2.0-licensed upstream PDK
    # repo (see tests/corpus/README.md "Provenance" / "License note"), so
    # redistributing it here is clear -- unlike the two canary blocks
    # (`gf180-bandgap`/`sky130-bandgap`), which stay gated behind #62's
    # public-repo check in `scripts/ingest-canary.py` and are not touched by
    # this script. `downloadable: true` + `layout_file` together are what
    # `site/scripts/copy-renders.mjs` stages into the built site and
    # `DetailPage.tsx`'s `canDownload` gate checks for.
    staged_gds = output_dir / gds_path.name
    shutil.copyfile(gds_path, staged_gds)

    # Field shape mirrors `klt layout-metrics` (issue #61) exactly — see
    # src/klayout_tools/layout_metrics.py and docs/cli/layout-metrics.md. In
    # particular: no `$schema` (the envelope key is `schema_version`), no
    # `pdk` field, and `name` is always present. `drc` is omitted here
    # because this bootstrap doesn't run a DRC deck against the corpus GDS.
    layout = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "name": slug,
        "status": "ok",
        "description": f"{pdk} standard-cell layout `{slug}` from the #4 test corpus.",
        "layer_count": layers["layer_count"],
        "cell_count": cells["cell_count"],
        "instance_count": instance_count,
        "layout_file": staged_gds.name,
        "downloadable": True,
    }

    _attach_overview_render(slug, block_dir, gds_path, layout)

    if not skip_signals:
        _attach_signals(slug, block_dir, layout)

    layout_path = output_dir / "layout.json"
    layout_path.write_text(json.dumps(layout, indent=2) + "\n")
    print(f"  {slug}: wrote {layout_path.relative_to(REPO_ROOT)}")


def main() -> int:
    skip_signals = "--skip-signals" in sys.argv[1:]

    if not CORPUS_ROOT.exists():
        print(f"Corpus root not found: {CORPUS_ROOT}", file=sys.stderr)
        return 1

    for pdk_dir in sorted(CORPUS_ROOT.iterdir()):
        if not pdk_dir.is_dir() or pdk_dir.name == "golden":
            continue
        pdk = pdk_dir.name
        print(f"{pdk}:")
        for gds_path in sorted(pdk_dir.glob("*.gds")):
            bootstrap_block(gds_path, pdk, skip_signals=skip_signals)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

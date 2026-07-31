#!/usr/bin/env python3
"""Regenerate ``blocks/`` from the #4 test corpus with the real extractor.

Successor to ``bootstrap-gallery-blocks.py`` (deleted), per the follow-up in
``blocks/README.md``: now that ``klt render`` (#60) and ``klt layout-metrics``
(#61) exist, every gallery block is built the same way a real block will be —
a materialized layout file inside the block directory, per-layer PNG renders
under ``output/renders/``, and a ``layout.json`` emitted by the extractor
itself (never hand-assembled).

Usage::

    uv run python scripts/regen-gallery-blocks.py

For each ``tests/corpus/<pdk>/*.gds``:

1. copy the GDS to ``blocks/<slug>/<slug>.gds`` (the extractor's
   ``layout_file`` discovery expects it inside the block dir);
2. ``klt render`` it into ``blocks/<slug>/output/renders/``;
3. ``klt layout-metrics blocks/<slug>`` to write ``output/layout.json``.

Idempotent: renders and layout.json are regenerated from scratch each run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_ROOT = REPO_ROOT / "tests" / "corpus"
BLOCKS_ROOT = REPO_ROOT / "blocks"


def run_klt(*args: str) -> None:
    subprocess.run(["klt", *args], cwd=REPO_ROOT, check=True)


def regen_block(gds_path: Path) -> None:
    slug = gds_path.stem
    block_dir = BLOCKS_ROOT / slug
    output_dir = block_dir / "output"
    renders_dir = output_dir / "renders"

    block_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(gds_path, block_dir / gds_path.name)

    if renders_dir.exists():
        shutil.rmtree(renders_dir)
    # --background matches the site's card canvas (site/src css --night).
    run_klt(
        "render",
        str(block_dir / gds_path.name),
        "--output",
        str(renders_dir),
        "--background",
        "#0b0e13",
    )
    run_klt("layout-metrics", str(block_dir))
    print(f"  {slug}: regenerated ({len(list(renders_dir.glob('*.png')))} renders)")


def main() -> int:
    gds_files = sorted(CORPUS_ROOT.glob("*/*.gds"))
    if not gds_files:
        print(f"no corpus GDS files found under {CORPUS_ROOT}", file=sys.stderr)
        return 1
    print(f"Regenerating {len(gds_files)} gallery blocks from the test corpus:")
    for gds in gds_files:
        regen_block(gds)
    return 0


if __name__ == "__main__":
    sys.exit(main())

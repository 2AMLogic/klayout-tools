#!/usr/bin/env python3
"""Bootstrap ``blocks/<slug>/output/layout.json`` from the #4 test corpus.

Provisional bootstrap for the gallery layout data loader (issue #59) —
issue #61 ("Gallery: per-layout metrics extractor") had not landed when this
loader was built, so this script stands in for `klt`'s own metrics
extractor described there. Once #61 lands, regenerate `blocks/` with that
command instead and delete this script.

Usage::

    python scripts/bootstrap-gallery-blocks.py

Writes one ``blocks/<slug>/output/layout.json`` per
``tests/corpus/<pdk>/*.gds`` file, using ``klt layers`` / ``klt cells``
(invoked via subprocess, so this script has no dependency beyond an
installed ``klt``). One block (``gf180mcu_fd_sc_mcu9t5v0__clkinv_1``) is
deliberately left without a ``layout.json`` so the checked-in ``blocks/``
tree demonstrates the gallery loader's ``no_artifacts`` handling on a real
(non-synthetic) directory — see ``blocks/README.md``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_ROOT = REPO_ROOT / "tests" / "corpus"
BLOCKS_ROOT = REPO_ROOT / "blocks"
SCHEMA_VERSION = 1

# Intentionally left without a layout.json to demonstrate the gallery
# loader's no_artifacts handling on a real (non-synthetic) block directory.
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


def bootstrap_block(gds_path: Path, pdk: str) -> None:
    slug = gds_path.stem
    rel = gds_path.relative_to(REPO_ROOT)

    block_dir = BLOCKS_ROOT / slug
    output_dir = block_dir / "output"

    if slug in NO_ARTIFACTS_SLUGS:
        output_dir.mkdir(parents=True, exist_ok=True)
        # No layout.json written -- the directory exists, but the artifact
        # does not, exercising the loader's no_artifacts path for real.
        print(f"  {slug}: skipped (intentional no_artifacts demo)")
        return

    layers = run_klt("layers", str(rel))
    cells = run_klt("cells", str(rel))
    instance_count = sum(c.get("instances", 0) for c in cells.get("cells", []))

    layout = {
        "$schema": "https://klayout-tools.org/schemas/layout/v1.json",
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "status": "ok",
        "name": slug,
        "description": f"{pdk} standard-cell layout `{slug}` from the #4 test corpus.",
        "pdk": pdk,
        "layer_count": layers["layer_count"],
        "cell_count": cells["cell_count"],
        "instance_count": instance_count,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    layout_path = output_dir / "layout.json"
    layout_path.write_text(json.dumps(layout, indent=2) + "\n")
    print(f"  {slug}: wrote {layout_path.relative_to(REPO_ROOT)}")


def main() -> int:
    if not CORPUS_ROOT.exists():
        print(f"Corpus root not found: {CORPUS_ROOT}", file=sys.stderr)
        return 1

    for pdk_dir in sorted(CORPUS_ROOT.iterdir()):
        if not pdk_dir.is_dir() or pdk_dir.name == "golden":
            continue
        pdk = pdk_dir.name
        print(f"{pdk}:")
        for gds_path in sorted(pdk_dir.glob("*.gds")):
            bootstrap_block(gds_path, pdk)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

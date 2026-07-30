"""Regenerate the golden `klt layers` fixtures for the corpus.

Run this whenever the corpus gains/loses a file, or whenever a deliberate
change to `layers_report`'s output schema needs its goldens refreshed:

    python tests/corpus/generate_golden.py

For each `*.gds`/`*.oas` file directly under `tests/corpus/<pdk>/`, writes
`tests/corpus/golden/<pdk>/<basename>.layers.json` containing the
`layers_report()` output, with the `file` field recorded as the path
relative to the repo root (for readability/reproducibility — the
corpus-backed tests re-key this field to whatever path they actually pass in,
since it is an echo of the caller's argument, not derived content).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parent
REPO_ROOT = CORPUS_DIR.parent.parent
GOLDEN_DIR = CORPUS_DIR / "golden"

sys.path.insert(0, str(REPO_ROOT / "src"))

from klayout_tools.layers import layers_report  # noqa: E402

PDK_DIRS = ["sky130", "gf180mcu"]


def main() -> int:
    for pdk in PDK_DIRS:
        pdk_dir = CORPUS_DIR / pdk
        golden_pdk_dir = GOLDEN_DIR / pdk
        golden_pdk_dir.mkdir(parents=True, exist_ok=True)

        layout_files = sorted(
            p for p in pdk_dir.iterdir() if p.suffix in (".gds", ".oas")
        )
        for layout_path in layout_files:
            rel_path = layout_path.relative_to(REPO_ROOT).as_posix()
            report = layers_report(str(layout_path))
            report["file"] = rel_path  # canonical, cwd-independent record

            golden_path = golden_pdk_dir / f"{layout_path.stem}.layers.json"
            golden_path.write_text(json.dumps(report, indent=2) + "\n")
            print(f"wrote {golden_path.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

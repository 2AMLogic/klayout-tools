"""Regenerate the sky130 5T OTA golden-metrics fixture (issue #248).

Run this only as a **deliberate, reviewed** update -- e.g. after a change
that intentionally moves a device count, a composed bbox extent, or a DRC
violation count for the 5T OTA `gen-compose` worked example
(`docs/cli/gen-compose.md`). It regenerates
`sky130_5t_ota_gen_compose.json` from a real run of the same
`klt gen-compose` -> `klt extract` -> `klt drc` pipeline
`tests/test_metrics_regression.py::test_5t_ota_metrics_match_golden`
exercises (imports `build_5t_ota_metrics` from that module directly, so the
regenerator can never silently drift from what the regression test itself
runs):

    python tests/golden_metrics/generate_golden_metrics.py

This preserves any existing `tol_pct` band declared per-metric in the
current golden file (only each metric's `value` is refreshed) -- a
tolerance band is a deliberate authoring choice, not something a
regeneration run should silently reset to exact-match. Review the diff
before committing, exactly like `tests/corpus/generate_golden.py`.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

GOLDEN_METRICS_DIR = Path(__file__).resolve().parent
TESTS_DIR = GOLDEN_METRICS_DIR.parent
REPO_ROOT = TESTS_DIR.parent

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(TESTS_DIR))

from klayout_tools import pdk  # noqa: E402

# Mirror test_metrics_regression.py's `_isolate` autouse fixture -- this
# script runs outside pytest, so the same host-search-space scrubbing has to
# be applied by hand.
pdk.STORE_DIRS = []
pdk.CONVENTIONAL_PREFIXES = []

from test_metrics_regression import build_5t_ota_metrics  # noqa: E402

GOLDEN_PATH = GOLDEN_METRICS_DIR / "sky130_5t_ota_gen_compose.json"


def main() -> int:
    existing: dict[str, object] = {}
    if GOLDEN_PATH.is_file():
        existing = json.loads(GOLDEN_PATH.read_text()).get("metrics", {})

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        pdk_root = tmp_path / "pdk_install"
        (pdk_root / "sky130A" / "libs.tech").mkdir(parents=True)
        actual = build_5t_ota_metrics(tmp_path, pdk_root)

    metrics: dict[str, object] = {}
    for key in sorted(actual):
        value = actual[key]
        prior = existing.get(key)
        if isinstance(prior, dict) and "tol_pct" in prior and prior["tol_pct"]:
            metrics[key] = {"value": value, "tol_pct": prior["tol_pct"]}
        else:
            metrics[key] = value

    doc = {
        "$comment": (
            "Golden metrics for the sky130 5T OTA klt gen-compose -> "
            "klt extract -> klt drc pipeline (issue #248). Regenerate with "
            "`python tests/golden_metrics/generate_golden_metrics.py` -- "
            "see tests/golden_metrics/README.md for the update policy."
        ),
        "metrics": metrics,
    }
    GOLDEN_PATH.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {GOLDEN_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

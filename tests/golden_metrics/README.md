# Golden-metrics fixtures

Tolerance-banded, last-known-good **metric** snapshots for `klt` pipelines --
device counts, cell/instance counts, DRC violation counts, composed-cell
extents, and similar scalar metrics -- distinct from `tests/corpus/golden/`,
which pins a `klt` verb's **full** JSON response byte-for-byte.

Prior art / rationale: issue #248 (OpenROAD-flow-scripts' `metadata-base-ok.json`
+ `rules.json` pattern). The geometry-level regression tests
(`tests/test_corpus.py`, `tests/test_render_corpus.py`) catch a verb producing
*different* output; this net catches a verb producing *plausible but drifted*
output -- e.g. a change that quietly grows a generated cell 20% or drops a
device from extraction, while the geometry it does produce stays internally
self-consistent (so a byte/structure comparison alone would not flag it).

## File format

```json
{
  "metrics": {
    "extract.device_count": 11,
    "gen_compose.bbox_um.x1": { "value": 13.44, "tol_pct": 2 },
    "drc.violation_count": 0
  }
}
```

- Each key is a **flattened, dot-separated** path through a merged set of
  `klt` verb JSON responses (see `tests/helpers/metrics_regression.py`'s
  `flatten_metrics` docstring for the exact flattening rules -- nested dicts
  join with `.`, a list's *length* becomes `<key>_count`, non-numeric fields
  such as paths/hashes/status strings are dropped).
- A **bare number** (e.g. `"extract.device_count": 11`) means exact match
  (`tol_pct: 0`).
- An **object** `{"value": <number>, "tol_pct": <number>}` allows the actual
  metric to differ from `value` by up to `tol_pct` percent (inclusive) before
  the test fails -- for a metric expected to move slightly run-to-run (e.g. a
  continuous geometric extent) without being a meaningful regression.
- A metric declared in the golden file but absent from the live output, or
  present in the live output but *not* declared in the golden file, is a
  **schema-drift failure** -- reported loudly, never silently skipped.

## Regenerating a golden file

Golden-file updates are a **reviewed, deliberate act** -- that is the whole
point of this regression net. Never hand-edit a value to make a real
regression pass; instead investigate why the metric moved, and if the move is
intentional, regenerate and review the diff:

```bash
python tests/golden_metrics/generate_golden_metrics.py
git diff tests/golden_metrics/
```

The regenerator preserves each metric's existing `tol_pct` band (only
`value` is refreshed) -- declaring a tolerance is a deliberate authoring
choice, not something a regeneration run should silently reset to
exact-match.

## Fixtures

- `sky130_5t_ota_gen_compose.json` -- the sky130 5T OTA `klt gen-compose` ->
  `klt extract` -> `klt drc` pipeline, the same worked example
  [`docs/cli/gen-compose.md`](../../docs/cli/gen-compose.md) documents (a
  differential pair, a current-mirror load, and a tail current source, wired
  and routed) and `tests/test_gen_compose.py`'s
  `test_compose_labeled_net_survives_extraction_as_named_pin` already
  exercises. Built and verified by
  `tests/test_metrics_regression.py::test_5t_ota_metrics_match_golden`.

## Adding a new golden-metrics fixture

1. Add a `build_<fixture>_metrics(...)` function to a `tests/test_*.py`
   module that runs the relevant `klt` verb(s) and merges their flattened
   metrics (see `build_5t_ota_metrics` in `tests/test_metrics_regression.py`
   for the pattern).
2. Add a small regeneration script under `tests/golden_metrics/` that
   imports that function and writes `tests/golden_metrics/<fixture>.json`
   (mirrors `generate_golden_metrics.py`).
3. Run the regeneration script once to produce the initial golden file, and
   review its contents before committing -- a golden file nobody reviewed at
   creation time is not a meaningful regression net.
4. Add a test asserting the live metrics match the golden file via
   `helpers.metrics_regression.assert_metrics_match`.

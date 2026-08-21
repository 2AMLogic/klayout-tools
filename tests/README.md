# `tests/`

The `klt` CLI's `pytest` suite — one `test_<verb>.py` per `src/klayout_tools/<verb>.py`
module (plus a handful of cross-cutting files: `test_cli.py`, `test_output.py`,
`test_error_paths.py`, `test_provenance.py`). Run it with:

```bash
uv sync --locked --extra dev    # once, to create/refresh .venv
uv run --extra dev pytest       # the whole suite
uv run --extra dev pytest tests/test_drc.py -k some_case   # one file/case
```

`.github/workflows/ci.yml` runs `ruff check` plus this suite on Python
3.10–3.13 for every PR and every push to `main` — see the root
[`README.md`](../README.md#development) for the full local dev-loop
(`npm run check:ci` runs the same lint+test gate CI does).

## Layout

```
tests/
  test_<verb>.py       # one file per src/klayout_tools/<verb>.py module
  helpers/              # shared test-only doubles/fixtures (not shipped) -- see its docstring
  corpus/                # checked-in open-PDK GDS + golden JSON fixtures. See its README.
  golden_deck/            # golden DRC/LVS deck-derived fixtures (per-PDK). See its README.
  golden_metrics/         # golden scalar-metric baselines (device/cell/violation counts). See its README.
```

- **`helpers/`** — shared test-only doubles (`subprocess_fakes.fake_completed`,
  `cocotb_fakes.FakeCocotbRunner`, `fake_aws._FakeAws`,
  `metrics_regression`'s tolerance-based scalar comparator), extracted from
  near-identical definitions that had drifted across multiple test files.
  Nothing here is importable from the installed `klt` distribution.
- **`corpus/`, `golden_deck/`, `golden_metrics/`** each hold checked-in
  fixture data plus a `generate_golden*.py` regeneration script; see each
  directory's own README for provenance, licensing, and how to regenerate
  its fixtures after a legitimate behavior change. `corpus/` also nests
  per-crate benchmark corpora (`legalize/`, `statime/`, `techmap/`,
  `synth_e2e_validation/`, `place_and_route/`), each with its own
  `regenerate.sh` and (except `place_and_route/`) its own `README.md` — see
  [`native/README.md`](../native/README.md) for which crate each one backs.

## Conventions

- **Golden-fixture regression** (`test_corpus.py`, `test_render_corpus.py`,
  `test_golden_deck.py`, …) compares a verb's full JSON/GDS output
  byte-for-byte against a checked-in fixture — the right bar for output that
  must not drift at all. Regenerate the fixture only when the change is a
  deliberate, understood behavior change (each of `corpus/`, `golden_deck/`,
  `golden_metrics/` documents its own regeneration command).
- **Scalar-metric regression** (`helpers/metrics_regression.py`) is the
  narrower, tolerance-based alternative for a single numeric metric (device
  count, cell count, violation count) where the interesting regression is
  "did this number move outside an acceptable band", not "is the entire
  response identical" — see `golden_metrics/README.md`.
- Everything else is an ordinary `pytest` unit/integration test against its
  matching `src/klayout_tools/` module, using the shared `helpers/` doubles
  where a real subprocess/simulator/AWS call would otherwise be needed.

## Where to look next

- [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) and
  [`docs/json-contract.md`](../docs/json-contract.md) — what a `klt` verb's
  JSON contract must satisfy; most tests assert against this shape.
- [`src/README.md`](../src/README.md) — the package under test.

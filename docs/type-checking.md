# Type checking (mypy baseline ratchet)

CI runs `mypy` over `src/` as a **ratchet**, not a ceiling (issue #3221),
modeled on the complexity ratchet (`scripts/check_complexity_baseline.py`).

- `[tool.mypy]` in `pyproject.toml` configures the run (not `strict`;
  `mypy` is pinned in the `dev` extra so diagnostics are stable between
  releases).
- `type-baseline.json` records the diagnostics present when the ratchet
  landed, as a count per `(file, error code, message)`. Line numbers are
  deliberately not part of the key, so unrelated edits never trip it.
- `scripts/check_type_baseline.py` fails on a diagnostic whose key is not in
  the baseline, or whose count rose. Fewer diagnostics never fail.

## Running it

```bash
uv run --frozen --extra dev python scripts/check_type_baseline.py
```

Always run through `uv run --extra dev` so project dependencies are
installed; the script refuses to run when it sees `import-not-found` /
`import-untyped` diagnostics, since those are environment noise and must
never be baselined. Genuinely absent or untyped third parties (the optional
`cocotb` extra, `jsonschema`) are handled by `[[tool.mypy.overrides]]`.

## Refreshing the baseline

After fixing diagnostics (or, with reviewer sign-off, accepting new ones):

```bash
uv run --frozen --extra dev python scripts/check_type_baseline.py --update-baseline
```

Commit the regenerated `type-baseline.json`. Baseline against the locked
environment (including the pinned `klayout` and `mypy`), since upgrading
either changes the diagnostics.

## `py.typed` and the envelope modules

`src/klayout_tools/py.typed` ships in the wheel. It is justified by the
stricter per-module override (`disallow_untyped_defs`) on the JSON-contract
envelope modules `klayout_tools.cli.output` and
`klayout_tools.signoff_envelopes`, which have zero baselined entries and
must stay that way. See also [`json-contract.md`](json-contract.md).

# `klt yield-csv`

Derive a [`klt yield`](yield.md) **sample-set document** from a long-format
per-draw Monte Carlo CSV — one row per draw
([#2931](https://github.com/2AMLogic/klayout-tools/issues/2931)).

```
klt yield-csv <csv> --group-by <col> --value <col>
              [--status-col <col> --pass-value <v>] [--draw-col <col>]
              [--unit <u>] [--min <x>] [--max <x>] [--target-yield <y>]
              [--format text|json]
```

```bash
klt yield-csv trials.csv --group-by metric --value result \
    --status-col status --pass-value ok --format json > samples.json
klt yield samples.json --format json > yield.json
```

The `--format json` output is the sample-set document itself (plus
`schema_version` and a `derivation.csv` block with the CSV path and `sha256:`
hash), emitted through the shared envelope in
[`docs/json-contract.md`](../json-contract.md). It runs no simulations, grades
no yield and needs no native extension. The full contract — column mapping,
failed-draw rules and what `klt signoff` hashes — is in
[`docs/cli/yield.md` → "Deriving a sample set from a per-draw CSV"](yield.md#deriving-a-sample-set-from-a-per-draw-csv-klt-yield-csv).

## Exit codes

| Exit code | Meaning |
| --- | --- |
| `0` | Derived; the document is on stdout. |
| `1` | Could not derive: missing/unreadable/empty CSV, missing column, duplicate draw, or a group with no usable draw. JSON error envelope on stderr under `--format json`; stdout empty. |
| `2` | Usage error (argparse) — e.g. `--group-by` missing. |

# `klt yield-samples`

Derive a [`klt yield`](yield.md) **sample-set document** from two unedited
`klt sim --format json` Monte Carlo reports: a nominal campaign and a seeded
known-bad variant whose draw becomes each matched measurement's
`negative_control`
([#2563](https://github.com/2AMLogic/klayout-tools/issues/2563)).

```
klt yield-samples <nominal> --negative-control <report>
                  [--description <text>] [--measurement <name>]...
                  [--format text|json]
```

```bash
klt yield-samples nominal.json --negative-control known-bad.json \
    --description 'seeded defect' --format json > samples.json
klt yield samples.json --format json > yield.json
```

The `--format json` output is the sample-set document itself — a flat
payload (`schema_version`, `measurements`, `derivation`, and `provenance` when the nominal report carries a well-typed `environment.monte_carlo` seed) emitted through the
shared envelope in [`docs/json-contract.md`](../json-contract.md), which
`klt yield` reads unchanged. The command does not run simulations, grade
yield, or need the native extension.

The full contract — how both populations are screened, how measurements are
matched, the `inconclusive`-into-`errored` mapping for the control, the
`derivation` audit block with both source paths and SHA-256 hashes, and
which document `klt signoff` then hashes (including the freshness
limitation) — lives in one place:
[`docs/cli/yield.md` → "Deriving a sample set from two `klt sim` reports"](yield.md#deriving-a-sample-set-from-two-klt-sim-reports-klt-yield-samples).

## Measurements outside `--measurement`

A derived sample set carries a control only for the selected measurements. For
the others, hand-edit the set to declare
`"negative_control": {"not_applicable": true, "reason": "..."}` when the seeded
defect cannot degrade them by construction (see
[yield.md](yield.md#negative-control)).

## Exit codes

| Exit code | Meaning |
| --- | --- |
| `0` | Derived; the document is on stdout. |
| `1` | Could not derive: a missing/unreadable/malformed input, a report with no Monte Carlo corners, a measurement that is missing, duplicated, unit-mismatched or has no draws, or a nominal measurement that already declares a `negative_control`. JSON error envelope on stderr under `--format json`; stdout empty. |
| `2` | Usage error (argparse) — e.g. `--negative-control` missing. |

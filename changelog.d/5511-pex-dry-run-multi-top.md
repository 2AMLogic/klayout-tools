- **`klt pex --dry-run` validates multi-top requests instead of refusing two or
  more `--top` values** (#5511, additive — `schema_version` unchanged):
  `--dry-run` is removed from `pex_multi_top_flags.reject_unsupported_flags`
  and now runs extraction preflight once per `--top` in `--top` order (naming
  a missing or failing top cell), enforces the shared multi-top flag refusals
  and duplicate `--top` check, and verifies through the real run's
  `pex_multi_top._require_subckt_per_top` helper that the schematic DUT
  declares a `.SUBCKT` for every `--top` before reporting `status: "valid"`.
  The dry-run report gains an additive `tops` field (`null` on a zero- or
  one-`--top` dry run, or `[{"top": "<resolved cell name>"}, ...]` in `--top`
  order on a multi-top dry run), while `top` stays the string name of the
  first resolved top. See [`docs/cli/pex.md`](docs/cli/pex.md) and
  [`docs/json-contract.md`](docs/json-contract.md).

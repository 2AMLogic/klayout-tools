- **`klt artifact` docs list `key_mismatch` among the exit-`1` causes** (#7369,
  2026-11-23): `docs/cli/artifact.md`'s `## Exit codes` table read as an
  exhaustive list of what `1` means but never named `key_mismatch`, which
  `put`, `get` and `delete` all emit when the broker answers a key naming
  another digest (for `get` and `delete`, also a key other than the one in
  the `artifact:<key>` URI or `MANIFEST[#SELECTOR]` entry). The `1` row now
  says so. Docs only; no behaviour change.

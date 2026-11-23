- **`klt artifact` docs list `key_mismatch` among the exit-`1` causes** (#7369,
  2026-11-23): `docs/cli/artifact.md`'s `## Exit codes` table read as an
  exhaustive list of what `1` means but never named `key_mismatch`, which
  `put`, `get` and `delete` all emit when the broker answers a key naming
  another digest (for `get` and `delete`, also a key other than the one in
  the `artifact:<key>` URI or `MANIFEST[#SELECTOR]` entry). `put` also emits
  it, before sending a byte, when the broker binds the upload to an
  `x-amz-checksum-sha256` that is not the file's digest. The `1` row now
  says so. Docs only; no behaviour change.

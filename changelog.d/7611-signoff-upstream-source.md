- **`klt signoff` records an optional `upstream_source` pin for a block
  consumed from another repository** (issue #6972, PR #7065, 2026-11-23): a
  block manifest of any `kind` may declare
  `"upstream_source": {"repository": "...", "commit": "..."}` — where the
  block's design sources live, at a full hexadecimal Git commit ID (40 hex
  digits, or 64 in a SHA-256 repository; case-insensitive, reported
  lowercase). The pin is echoed once at the top level of the tier report
  (`upstream_source`) and on each fleet roll-up row (`blocks[].upstream_source`,
  beside the row's `source`); an undeclared pin is omitted from both, never
  `null`. `--format text` prints `upstream source: <repository> @ <commit>`
  under the `block:`/`kind:` header of a tier report and
  `upstream: <repository> @ <commit>` on a fleet row. `--check` flags a
  changed pin as `"drifted"` (exit `3`) even with the evidence held fixed,
  naming `upstream_source.commit` (or `upstream_source.repository`) on a tier
  report and `blocks.0.upstream_source.commit` on a fleet roll-up. **Reported,
  never graded** — the pin records provenance (`klt signoff` never contacts
  the remote or attests the evidence was generated from that commit) and moves
  no item's `status` and no block's `tier`; a manifest that declares nothing
  renders a byte-identical report, so `--check` sees no drift on an upgrade
  alone. A branch, tag, shortened SHA, incomplete pair or unknown member is
  refused by name (exit `1`) rather than silently dropped. **Additive —
  `schema_version` unchanged.** See
  [`docs/cli/signoff.md`](docs/cli/signoff.md) → "The declared upstream
  source".

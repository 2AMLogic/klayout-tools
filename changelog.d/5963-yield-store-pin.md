- **`klt signoff --manifest`: a `yield` evidence entry can pin its samples
  companion through a committed artifact-store manifest, offline
  (#5963, 2026-11-18)**: a file-backed entry may now carry
  `artifact_manifest` (repo-relative path to a committed
  `*.manifest.json`) plus optional `artifact_member`. When the samples
  document the `klt yield` report names is not on disk, the grader reads
  the named member's recorded `sha256` from the committed manifest — the
  same manifest reader `klt artifact verify` uses — and compares it
  against the entry's `content_hash` pin: `verified` or `stale_evidence`
  exactly as a disk re-hash would, with no broker, no environment and no
  network at grade time. The citation discloses
  `content_hash_basis: "artifact_manifest"` and `input_verified: null`
  (the bytes were not re-hashed; `klt artifact get`/`verify` remain the
  byte-level check). A store-resolved hash never supplies a verdict: the
  envelope itself stays committed and file-backed (operator ruling
  2026-11-17, option `a`). An unreadable/invalid manifest, or a missing,
  duplicate-named or basename-ambiguous member, leaves the hash unresolved
  with a named `content_hash_unresolved` reason (`manifest_unreadable`,
  `manifest_invalid`, `member_missing`, `duplicate_member`,
  `ambiguous_member`); a non-string key value renders that one item
  `invalid_evidence`. Entries without the new keys, and samples files
  present on disk, grade byte-identically to before.

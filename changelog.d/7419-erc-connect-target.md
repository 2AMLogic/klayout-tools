- **`klt erc` skips a tie whose `connect_to` role never touches its taps**
  (#7419, 2026-11-23): a `ties[]` entry with no `through` whose `connect_to` stackup
  role has zero plan-view interaction with any of its own tap sites is
  recorded in `erc_coverage.skipped` under the new reason
  `degenerate_connect_target` — the direct overlap join is the only way
  such a tie can pass, so zero interaction means it cannot pass by
  construction, yet the per-well `erc.missing_tie` findings it emits are
  byte-identical to a genuinely untied layout. The canonical case is ASAP7
  without LISD (a tap reaches M1 only through SDT -> LISD -> V0, so
  `connect_to: "M1"` against a stackup without LISD never overlaps the
  tap). The findings are deliberately unchanged — whether the stackup
  omits the conductor path or the block really is untied is a fact about
  the stream the envelope cannot decide, so a genuinely untied block can
  legitimately carry both the skip and the findings. The existing
  well/tap degeneracy reasons keep precedence when more than one holds,
  and a `through` tie (contact-qualified join) is never graded by this
  test. `erc_status` rolls up as for the other reasons (`violations`
  still wins when findings exist). Additive; `schema_version` unchanged.

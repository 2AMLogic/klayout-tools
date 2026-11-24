- **`klt sim` refuses a one-argument `.lib <token>` inside the selected
  `mismatch_scale` section** (#7671, 2026-11-24): the sg13g2 sigma scan
  treated every single-argument `.lib` line as a section opener, so inside an
  already-selected section a `.lib <file>` include form silently ended the
  scan — the run was accepted with a partial card set, a `mismatch_scale: 0`
  run that was not a sigma=0 control, with nothing downstream flagging it
  (the same hazard class #5504 closed for non-literal sigma values). Such a
  line is genuinely ambiguous (a section name or an include) and SPICE
  sections do not nest, so `klt` now raises a `SimError` naming the file and
  the offending line instead of partially scaling; the fix is to write the
  include as `.include <file>`, or a two-argument `.lib <file> <section>`
  (both already followed). Top-level `.lib <section>` openers outside the
  selected section are skipped exactly as before, and no vendored sg13g2
  corner file uses the form inside a section (verified), so existing
  resolutions are unchanged.

- **Failed report writes no longer name the `.klt-<uuid>.tmp` staging file**:
  the error keeps the destination and the errno text, the `klt erc/drc/lvs/pex`
  and `klt sim` docs state the writable-parent-directory requirement of the
  atomic replace, and ERC's rejected-flag lists include `--output`
  (#7658, 2026-11-24).

- `klt ring-check` and `klt keepout-check` now validate `--cells` through one
  shared pair of helpers instead of two identical private copies. Each verb
  still raises its own error class with the same message text (#8065).

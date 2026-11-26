- `klt pex` now attributes an ngspice measurement window miss to the row’s own
  measurement when its diagnostic is available. A completed analysis can report
  a window miss after issuing singular-matrix or stepping warnings. An aborted
  analysis retains its first error diagnostic (#7904).

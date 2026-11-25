- **`klt sim` keeps a noise run's per-frequency spectrum as
  `artifacts.waveform_spectrum`** (#7866): `options.waveforms` on an
  `analysis.kind: "noise"` request exported only ngspice's current plot, the
  one-point `Integrated Noise` totals, so the full spectrum (`frequency`,
  `inoise_spectrum`, `onoise_spectrum`, one point per swept frequency) could not
  be kept as evidence, including from a batch run. A `noise` corner with
  `options.waveforms` + `options.keep_artifacts` now also writes the spectrum
  plot to `waveform_spectrum.raw` and converts it to
  `waveform_spectrum.raw.json` (same shape, `waveform_max_bytes` guard and
  `waveform_decimate` stride as the main artifact), reported as
  `corners[].artifacts.waveform_spectrum`. Additive: `waveform.raw`,
  `waveform.raw.json` and `noise_representation` are unchanged, decks for other
  analyses and for `noise` without `options.waveforms` are byte-identical, and
  the current plot is restored so `expr`/`noise`/`noise.mechanism` measurements
  resolve as before. Not a runner-gated batch feature (a gate would refuse
  requests that only need the integrated plot); an older runner image writes no
  spectrum, so check that `artifacts.waveform_spectrum` is a path.

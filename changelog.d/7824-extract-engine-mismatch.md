- **`klt extract` flags KLayout engine drift at run time**
  (#7824, additive — `klt extract` `schema_version` unchanged). The default
  in-process engine now opts into the same `provenance.klayout_version_mismatch`
  comparison `klt drc`/`klt lvs` carry (issue #1490): a run whose resolved
  `klayout` engine differs from the version this `klayout-tools` build/commit
  was tested against records `true` and prints the shared stderr warning
  (naming both versions and the `--with klayout==<expected>` install remedy,
  issue #5601); a match or an unresolvable expected version records `false`
  with no warning, and the report always retains the actual
  `provenance.klayout_version`. The warning's consequence sentence now names
  extraction alongside DRC/LVS — an extracted netlist's measured device
  parameters and parasitic R/C are first-class engine outputs, which is why a
  `klayout-tools==X.Y.Z` pin used as an evidence standard has the same
  engine-drift hole DRC/LVS had. `--engine klayout-runset` reports are
  unchanged (standalone application; out of scope). Replay guidance — the
  paired-pin `uvx --from ... --with klayout==<recorded version>` example,
  distinct from `klayout_version_expected` for new evidence — is in
  `docs/cli/extract.md` → "Engine-version drift".

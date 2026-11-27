- **`klt sim` batch deck docs: a staged include does not make `stamp_scope`
  report `image`** (#7985, 2026-11-26, docs only -- no report field, exit
  code or `schema_version` changes): `environment.remote.worker_environment.pdk.stamp_scope`
  can still read `executed` with `models_staged: false` for a `decks[]` job
  whose relative `.include`/`.lib` stages a file beside the deck without a
  `decks[].inputs[]` declaration, even when that file is the model the engine
  ran. `docs/cli/sim.md` "Worker-observed environment" now shows that shape
  beside the field reference, states that the stamp identifies the worker
  image's PDK tree and cannot establish the origin of every model the
  simulator read, and gives the check for the staged bytes: the local file's
  SHA-256 against `key.includes[<name>]` in `<outdir>/<id>/unit.json`.

- **`klt version --require` gates on the commands an install provides**
  (#7331, 2026-11-23): `--require <command>[,<command>…]` (repeatable) checks
  the `commands` list #7324 added and exits `3` when a required top-level
  verb is missing — the normal payload is still emitted, with an additive
  `required_commands` field (`{required, missing}`, input order,
  de-duplicated) present only when the flag is passed. An unknown name is the
  signal, not a usage error; a `--require` that names nothing is a usage
  error (exit `2`). On a miss, stderr names the missing commands and, when
  `reinstall_spec` is not `null`, the command that re-obtains the running
  build. A consumer can now gate a pin with two existing verbs — `klt version
  --require drc,lvs && klt pdk check --expect-version <rev>` — instead of
  scraping `klt --help` or hand-rolling a preflight. No new verb, no pin-file
  reader and no PDK flags; without the flag nothing changes and
  `schema_version` stays `1`. See `docs/cli/version.md` "Requiring commands".

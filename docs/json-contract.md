# JSON output contract

Every `klt <verb>` command supports `--format text|json` (default: `text`).
**JSON is the API; text is a courtesy rendering of the same data** (CLAUDE.md).
This document defines the shared envelope every command emits through, so
verbs conform from day one rather than being harmonized after the fact.

All commands emit through `src/klayout_tools/cli/output.py`'s
`emit_success()` / `emit_error()` helpers — no `*_cmd.py` module hand-rolls
`json.dump`/`json.dumps`.

## Design: additive envelope, not a wrapping envelope

Each command's JSON payload stays **flat** at the top level (e.g. `klt
layers`'s `file`, `dbu_um`, `layer_count`, `layers` fields), rather than being
nested under a new key like `{"result": {...}}`. The only envelope-level
addition is a `schema_version` field alongside the command's existing fields.
This keeps the contract additive: an already-shipped command's documented
fields are never renamed, removed, or nested as part of adopting this
contract.

## Success shape

```json
{
  "schema_version": 1,
  "...": "the command's own top-level fields, unchanged"
}
```

- `schema_version` (integer) — starts at `1`. **Versioned per command**, not
  globally: `klt layers` and `klt cells` evolve independently, so a
  breaking change to one command's JSON shape does not force a version bump
  on another. A command bumps its own `schema_version` only when it makes a
  non-additive (breaking) change to its own payload; adding new fields does
  not require a bump.
- All other top-level fields are defined by the individual command's own
  documentation (e.g. `docs/cli/layers.md`).
- Written to **stdout only**, as indented JSON with a trailing newline.

## Error shape

Under `--format json`, errors are also JSON — not a plain-text line — written
to **stderr**, with **stdout left empty**. This means a caller never needs to
inspect stdout content to tell success from failure under `--format json`;
the exit code alone is authoritative.

```json
{
  "schema_version": 1,
  "error": {
    "command": "layers",
    "message": "file not found: missing.gds"
  }
}
```

- `error.command` — the subcommand name (e.g. `"layers"`).
- `error.message` — a concise, human-readable description of what went wrong.
  No Python traceback is ever emitted.

Under `--format text` (the default), errors remain the pre-existing
plain-text stderr line: `klt <command>: <message>`. Text is a courtesy
rendering, not the contract, so this shape is not versioned.

## Exit codes

| Exit code | Meaning                                                                                     |
| --------- | -------------------------------------------------------------------------------------------- |
| `0`       | Success. The documented success payload was written to stdout.                               |
| `1`       | Application-level error (e.g. missing/unreadable file). Documented error shape on stderr.     |
| `2`       | Usage error (missing required argument, invalid `--format` choice, etc.) — raised by argparse before a command's handler runs. |

**Carve-out:** exit code `2` and its accompanying stderr output are argparse's
own behavior, produced before any subcommand's `run()` executes. They are
deliberately out of scope for the shared `output.py` helper — argparse always
writes plain text for usage errors, in both `--format text` and `--format
json` modes, since format-specific handling would require parsing the
arguments before the parser itself has rejected them.

## `--format text` vs `--format json`

- `text` (default) — a human-readable rendering. Its exact layout is **not**
  part of the contract and may change between releases without notice.
- `json` — the stable API. Breaking a JSON field (renaming, removing, or
  retyping it) is a breaking change per CLAUDE.md; adding a field is not.

## Adding a new command

1. The library function backing the command returns a plain dict payload
   including `schema_version` (see `layers_report()` in
   `src/klayout_tools/layers.py` for the pattern) — not the CLI layer, so the
   version travels with the payload wherever it's reused (e.g. a future MCP
   server, per `docs/ARCHITECTURE.md`).
2. The `*_cmd.py` module's `run()` calls `output.emit_success(payload,
   args.format, text_renderer)` on success and `return output.emit_error(name,
   message, args.format)` on the documented error path.
3. Document the command's fields in `docs/cli/<verb>.md`, including its
   `schema_version`.

See `src/klayout_tools/cli/layers_cmd.py` and `docs/cli/layers.md` for a
worked example.

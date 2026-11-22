- **`klt formal` now rejects an unreadable RTL source at request validation**
  (#5718, 2026-11-18): `klt synthesize`, `klt equiv` and `klt
  functional-verification` probed each `request.sources` entry with
  `open(path, "rb")` and raised a named request error, but `klt formal`'s own
  copy of that loop only checked that the file existed. An existing-but-unreadable
  source (for example a `chmod 000` file) therefore passed `klt formal`
  validation and failed later, outside request validation, instead of getting
  the clean error its siblings give. All four verbs now resolve each source
  through one shared `resolve_readable_files` helper in `request_paths.py`, so
  they cannot drift apart again. **New request-error path, no JSON field
  changes:** `klt formal` raises `FormalError: could not read source
  '<entry>': <OSError>`. Every existing message is byte-identical (`RTL source
  not found: …`, `source not found: …`, `<label> source not found: …`), and
  `klt formal`'s optional `sources` (`null` / `[]` with `netlist_path`) is
  unchanged.

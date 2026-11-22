# `klt cell-hash`

Run `klt cell-hash --cell NAME LAYOUT` to print a named cell’s canonical
geometry digest from a GDSII or OASIS stream. The hash covers the cell and
its descendants. A cell retains its digest when another cell places it in
a larger layout. Changing geometry in its subtree changes the digest.

```sh
klt cell-hash --cell PAD bare-pad.gds
klt cell-hash --cell PAD block.oas --format json
```

The default output is a single `sha256:`-prefixed digest followed by a
newline. JSON output includes `schema_version` and `cell_hash`. Use the
digest in a foundry-cell exception. [Signoff documentation](signoff.md)

The command exits 0 on success. If the stream cannot be read or the cell is
absent, it writes the diagnostic to stderr and exits 1. Missing arguments
produce a usage error and exit 2.

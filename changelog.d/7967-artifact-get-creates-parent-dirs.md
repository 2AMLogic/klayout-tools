- **`klt artifact get -o PATH` creates the missing parent directories of
  `PATH`** (#7967). A clean checkout can now restore an object at its
  manifest entry name with `klt artifact get MANIFEST#NAME -o NAME` run from
  the repo root; previously the fetch failed with a raw `FileNotFoundError`
  traceback when any parent directory did not exist. The verified bytes still
  land in a temporary file beside the destination and are renamed into place
  only once their sha256 matches.

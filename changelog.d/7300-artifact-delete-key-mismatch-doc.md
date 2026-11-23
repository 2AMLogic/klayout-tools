- **`klt artifact delete` docs state the `key_mismatch` pre-flight** (#7300,
  2026-11-22): `docs/cli/artifact.md`'s `## delete` section described only
  the `still_referenced` refusal that stops a batch before the first delete
  call. It now says the same of `key_mismatch`: every `REF` is `stat`ed first,
  the key `stat` returns must name the requested digest and, for an
  `artifact:<key>` URI or a `MANIFEST[#SELECTOR]` entry, be the `REF`'s key,
  and a disagreement exits `1` with `error.code: "key_mismatch"` having
  deleted nothing, with or without `--yes`. Docs only; the behaviour dates
  from #6725.

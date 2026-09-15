# Upstream bug report: `fst-writer`/`fst-reader` zlib-compressed time-table size-tie

**Status: drafted, not yet filed.** This document is the complete text of a
bug report against `ekiwi/fst-writer` (cross-linking `ekiwi/fst-reader`,
since the defect spans both crates), ready to file verbatim. Filing it is a
public action on a third-party GitHub repository under a human maintainer's
own identity -- an agent cannot do this. **This is a pending operator
action** (see issue #1606). Once filed, replace this status line with the
issue URL and, per issue #1606's acceptance criteria, link it back into
#1606's body/comments.

No code in this repository depends on the upstream fix landing; see
`native/wave/src/verify.rs` for the mitigation already in place, and this
issue's own body for why bumping `native/wave/Cargo.toml`'s `fst-writer`/
`fst-reader` pins is explicitly out of scope until then.

---

## Where to file

- Primary: https://github.com/ekiwi/fst-writer/issues/new
- Cross-link: https://github.com/ekiwi/fst-reader (the disambiguation bug is
  really on the reader side; both crates are by the same maintainer,
  Kevin Laeufer / `ekiwi`, both BSD-3-Clause)

## Suggested title

> zlib-compressed time-table section: a compressed/uncompressed size tie is
> misread as "stored raw" (`fst-writer` writes compressed, `fst-reader`
> assumes raw)

## Report body

### What

`fst-writer`'s `write_time_table` (`src/io.rs`) chooses whether to
zlib-compress the time-table section with:

```rust
if compressed.len() > time_table.len() { store raw } else { store compressed }
```

`fst-reader`'s `read_zlib_compressed_bytes` (`src/io.rs`) decides whether a
section *was* stored raw purely by comparing the recorded
`uncompressed_length == compressed_length`.

When zlib compression happens to produce a result **exactly** the same size
as the input -- a real, reproducible occurrence for small-to-medium time
tables with mostly-uniform delta values (e.g. a free-running clock's own
edge spacing) -- the writer takes the "store compressed" branch while the
reader's own length-equality heuristic takes the "was stored raw" branch,
silently decoding the raw zlib-compressed bytes as if they were the
uncompressed varint time deltas. The result is a store that **opens without
error** but reports a corrupted time table (an implausibly large last
entry, since the misread zlib bytes decode as a run of large varint
values).

### Reproduction

A minimal from-scratch `fst-writer`/`fst-reader` round-trip confirmed this
independently of any downstream consumer's own code: write a single 1-bit
signal through a full `fst-writer` session, advancing time with the delta
sequence

```
[5, 10, 15, 20, 25, 30, 35, 37, 40, 45, 50, 55, 60, 65, 70]
```

(i.e. mostly-uniform steps of 5, with two short steps breaking the run --
empirically confirmed to hit the exact compressed/uncompressed size tie at
zlib level 3, `fst-writer`'s own hardcoded `ZLIB_LEVEL`). Toggle the
signal's value at each time step (arbitrary; only the time-table encoding
matters here). Finish the writer, then re-open the resulting file with
`fst-reader` and read back the time table: the decoded table's last entry
does not match the last time value written (`70`), and is instead an
implausibly large number -- the reader has decoded the raw
zlib-compressed bytes as if they were uncompressed varints.

A minimal repro crate reproducing this end-to-end is available on request;
this project's own regression test pinning the exact delta sequence above
lives at
[`native/wave/src/verify.rs`](https://github.com/2AMLogic/klayout-tools/blob/main/native/wave/src/verify.rs)
in `2AMLogic/klayout-tools` (`verify_store_rejects_the_known_fst_writer_corruption`,
in the `#[cfg(test)] mod tests` block), which exercises the real crate pair
(not a mock) and asserts the corruption is detected by an independent
read-after-write check.

### Impact

Any consumer of `fst-writer` to produce a store later read by `fst-reader`
(or any other reader implementing the same length-equality heuristic --
this is `fst-reader`'s own documented decoding strategy, so likely shared
by other implementations following it) is exposed to silent, undetected
time-table corruption whenever the input time-table's zlib-compressed
encoding happens to be exactly as long as its raw encoding. The corrupted
store opens and is queryable -- it does not error -- so a consumer without
its own independent post-write verification would ship a corrupted
artifact undetected.

### Suggested fix

The fix belongs in the reader's raw-vs-compressed disambiguation: rather
than inferring "was this section stored raw?" from a length comparison
that both the raw and compressed encoding paths can independently produce
(a tie is not distinguishing), carry an explicit flag bit (or a reserved
sentinel value for one of the two length fields) recording which branch
the writer took. This requires a coordinated change to both crates (the
on-disk format `fst-writer` emits and `fst-reader` decodes) since it is
this pair's own binary section layout, not a third-party format constraint
-- unless the on-disk FST time-table section layout is itself specified by
an external format document neither crate controls, in which case the fix
may instead need to live entirely on the reader side (e.g. attempting a
zlib decode first and falling back to raw only if that fails, rather than
using the length-equality heuristic at all).

### Environment

- `fst-writer` = 0.3.1
- `fst-reader` = 0.17.0
- Reproduced on Linux (x86_64), stable Rust toolchain; nothing about the
  defect appears platform- or toolchain-dependent (it is a pure data/format
  issue, not a memory-safety or platform bug).

---

## Operator checklist

- [ ] File the report above at https://github.com/ekiwi/fst-writer/issues/new
      (verbatim body above, or adapted as the maintainer's issue template
      requires).
- [ ] Cross-link from an issue/comment on `ekiwi/fst-reader` per the "Where
      to file" note above.
- [ ] Update this document's status line with the filed issue URL.
- [ ] Link the filed issue back into klayout-tools issue #1606's body or
      comments (its own acceptance criteria call for this).
- [ ] Once the upstream fix lands and releases, file a fresh low-priority
      klayout-tools issue to bump `native/wave/Cargo.toml`'s
      `fst-writer`/`fst-reader` pins and confirm
      `verify_store_rejects_the_known_fst_writer_corruption`
      (`native/wave/src/verify.rs`) stops reproducing the corruption --
      explicitly out of scope for #1606 itself per its acceptance criteria.

# pdks/ — open PDK data

Local, gitignored home for the open PDK data klayout-tools works
against. Populate it with:

```bash
scripts/fetch-pdks.sh
```

The fetched data is **not** committed — it is hundreds of MB of
LEF/GDS/tech files that are already versioned upstream. Only this
README and the fetch script are tracked; the script pins an exact
upstream release so every checkout gets identical data.

## Contents after fetching

| Path | What it is |
| ---- | ---------- |
| `lambdapdk/` | [siliconcompiler/lambdapdk](https://github.com/siliconcompiler/lambdapdk) (Apache-2.0) at the version pinned in `scripts/fetch-pdks.sh` |
| `cell-netlists/` | Real, transistor-level SPICE netlists + primitive device models for the 7 klayout-tools.org gallery standard cells, pinned per-file (not a whole-release tarball) and checksum-verified by `scripts/fetch-cell-netlists.sh` — see that script and `scripts/gallery_signals.py`'s module docstring |
| `sky130-liberty/` | A minimal, open_pdks-layout `sky130A` variant holding the real `sky130_fd_sc_hd__tt_025C_1v80.lib` Yosys/ABC need for `klt synthesize`'s GCD worked example, fetched by `scripts/fetch-sky130-liberty.sh` — see that script's header comment for why this can't come from `lambdapdk/` (issue #417) |

lambdapdk bundles, per process: KLayout layer properties and tech
files, DRC/PEX decks, and standard-cell library data (LEF/GDS/liberty)
for **sky130**, **gf180**, **asap7**, **freepdk45**, **ihp130**,
**gt2n**, and **interposer**. PDK trees live under
`lambdapdk/lambdapdk/<process>/`.

**Not discovered by `klt pdk find`/`list`/`env`.** The lambdapdk tree
fetched here uses its own layout (`lambdapdk/lambdapdk/<process>/{libs,base}`),
not the open_pdks layout (`<variant>/libs.tech/`, `<variant>/libs.ref/`)
that `klt pdk` resolves — see [`docs/cli/pdk.md`](../docs/cli/pdk.md#scope-v1)
for the resolver's scope. Point tools at paths under `pdks/lambdapdk/`
directly instead, e.g.
`klt stats pdks/lambdapdk/.../sky130_sram_1rw1r_64x256_8.gds`.

**`sky130-liberty/` is the exception**: unlike `lambdapdk/`, it *is* laid
out as an open_pdks variant (`sky130A/libs.tech/`, `sky130A/libs.ref/`) so
`klt pdk`/`klt synthesize` discover it the normal way once
`PDK_ROOT=pdks/sky130-liberty PDK=sky130A` is set — it just ships only the
one liberty file a synthesis run needs, not a full standard-cell library
(GDS/LEF/spice views), which is why it is fetched by its own script rather
than folded into `fetch-pdks.sh`.

All content here must be open-source PDK data — see the "Open PDKs
only" rule in `CLAUDE.md`. Never place proprietary or NDA'd PDK
material in this directory.

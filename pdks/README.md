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
| `ihp-open-pdk/` | [IHP-GmbH/IHP-Open-PDK](https://github.com/IHP-GmbH/IHP-Open-PDK) (Apache-2.0) at the release tag pinned in `scripts/fetch-ihp-sg13g2.sh` — the **real SG13G2** PDK, a distinct project from lambdapdk's `ihp130` (see "`ihp130` vs. SG13G2" below); fetched by `scripts/fetch-ihp-sg13g2.sh` (issue #522) |

lambdapdk bundles, per process: KLayout layer properties and tech
files, DRC/PEX decks, and standard-cell library data (LEF/GDS/liberty)
for **sky130**, **gf180**, **asap7**, **freepdk45**, **ihp130**,
**gt2n**, and **interposer**. PDK trees live under
`lambdapdk/lambdapdk/<process>/`.

**Not discovered by `klt pdk find`/`list`/`env`.** The lambdapdk tree
fetched here uses its own layout (`lambdapdk/lambdapdk/<process>/{libs,base}`),
not a layout `klt pdk` resolves — see [`docs/cli/pdk.md`](../docs/cli/pdk.md#scope)
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

**`ihp-open-pdk/` is also discovered** — its own tree already ships
`ihp-sg13g2/libs.tech/`/`ihp-sg13g2/libs.ref/`, so
`PDK_ROOT=pdks/ihp-open-pdk PDK=ihp-sg13g2` resolves the normal way (issue
#522 taught the resolver this shape and its flat, `PDK_ROOT` pointed
directly at `ihp-sg13g2/` variant too — see `docs/cli/pdk.md`'s "Scope").

### `ihp130` (lambdapdk) vs. SG13G2 (IHP-Open-PDK) — do not conflate these

**`lambdapdk/lambdapdk/ihp130/` is not SG13G2.** They share the "IHP"/"ihp"
name and both target IHP's process line, but they are two different
upstream projects with different, non-interchangeable data:

- `lambdapdk`'s `ihp130` tree is siliconcompiler's own third-party PDK
  package for an IHP process, maintained independently of IHP.
- `ihp-open-pdk/` (this directory, fetched by `scripts/fetch-ihp-sg13g2.sh`)
  is IHP's **own** open-source PDK release, targeting **SG13G2**
  specifically — the tech file, DRC/LVS decks, device models, and
  standard-cell library any SG13G2 design (e.g.
  [`2AMLogic/sg13g2-bandgap`](https://github.com/2AMLogic/sg13g2-bandgap))
  actually needs.

Using `lambdapdk/lambdapdk/ihp130/` where SG13G2 data is required will not
produce a design that matches IHP's SG13G2 process — fetch and point tools
at `pdks/ihp-open-pdk/` instead.

All content here must be open-source PDK data — see the "Open PDKs
only" rule in `CLAUDE.md`. Never place proprietary or NDA'd PDK
material in this directory.

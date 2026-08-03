# Scripts

Repository automation scripts. These are operator tools run by hand (or from
CI), not part of the `klt` CLI.

## Layout

```
scripts/
  README.md                    # this file
  deploy-site.sh                # build site/ (Vite + React) and deploy site/dist/ to Cloudflare Pages
  fetch-pdks.sh                  # pinned fetch of lambdapdk open PDK data into pdks/
  fetch-cell-netlists.sh         # pinned, checksum-verified fetch of real gallery-cell SPICE netlists/models
  fetch-sky130-liberty.sh        # pinned, checksum-verified fetch of the real sky130_fd_sc_hd liberty klt synthesize needs
  install-yosys.sh               # build + install a pinned, checksum-verified Yosys from source (CI provisioning)
  bootstrap-gallery-blocks.py   # regenerate blocks/*/output/layout.json (incl. `signals`) from the #4 corpus
  gallery_signals.py            # `klt sim` PVT-sweep pipeline for the 7 gallery cells (imported by the above)
  ingest-canary.py               # ingest a public canary block repo (issue #62) into blocks/<slug>/output/layout.json
  aws/build-remote-sim-ami.sh    # build/publish the remote-sim AMI (see docs/cli/sim.md)
```

## `deploy-site.sh`

Builds the Vite + React project in [`site/`](../site) (`npm --prefix site ci
&& npm --prefix site run build`, producing `site/dist/`) and deploys `site/dist/` to
Cloudflare Pages (project `klayout-tools`, custom domain klayout-tools.org).
Run it after changing the site content. Auth uses a scoped API token rather
than wrangler OAuth — source `~/.cloudflare/rjwalters/pages-rjwalters.env`
first; see the script's header comment for details.

```
source ~/.cloudflare/rjwalters/pages-rjwalters.env
scripts/deploy-site.sh
```

Pass `--no-deploy` to build only (`site/dist/`) and skip the Cloudflare
deploy — useful for local verification and does not require Cloudflare
credentials:

```
scripts/deploy-site.sh --no-deploy
```

Out of scope: regenerating `blocks/*/output/layout.json` or renders — that's
the content pipeline (`bootstrap-gallery-blocks.py` for the #4 corpus,
`ingest-canary.py` for canary blocks, #62); `deploy-site.sh` only builds and
deploys whatever is already checked into `blocks/` and `site/`.

## `fetch-pdks.sh`

Downloads a pinned release of [lambdapdk](https://github.com/siliconcompiler/lambdapdk)
(Apache-2.0) into `pdks/` — gitignored except for `pdks/README.md`. Run it to
populate local open PDK data; see [`pdks/README.md`](../pdks/README.md) for
what lands where.

```
scripts/fetch-pdks.sh
```

## `fetch-cell-netlists.sh`

Fetches real, transistor-level SPICE netlists and primitive device models
for the 7 gallery standard cells — pinned to an exact upstream commit SHA
per file and checksum-verified (fails closed on mismatch), into
`pdks/cell-netlists/` (gitignored, same as `pdks/lambdapdk/`). Unlike
`fetch-pdks.sh`'s whole-release-tarball pin, this pins individual files:
the full upstream primitive-model repos are 100+ MB of corners/devices the
7 cells never instantiate. See `scripts/gallery_signals.py`'s module
docstring for what these files are used for and the one documented device
substitution (gf180mcu only).

```
scripts/fetch-cell-netlists.sh
```

## `fetch-sky130-liberty.sh` / `install-yosys.sh`

CI provisioning for `klt synthesize` (issue #417, Phase 2 of
[Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391)):
`.github/workflows/ci.yml`'s `test` job runs both so
`tests/test_synthesize.py`'s real-Yosys GCD integration test — previously
always skipped in CI — actually runs there.

`install-yosys.sh` builds and installs a pinned, checksum-verified Yosys
release from source (`apt-get install yosys` resolves Ubuntu 24.04's
`0.33-5build2`, ~30 releases stale relative to
[`docs/design/yosys-synthesis-spike.md`](../docs/design/yosys-synthesis-spike.md)'s
worked example — see that survey's section 3.4). Installs into
`~/.cache/yosys-<version>` by default (`$YOSYS_INSTALL_PREFIX` to override);
idempotent, same `--force` convention as the other fetch scripts here.

`fetch-sky130-liberty.sh` fetches the one real `sky130_fd_sc_hd` Liberty
timing view (`tt_025C_1v80`) that worked example needs, extracted from a
pinned volare/open_pdks release asset (not the whole ~166 MB tarball) into
`pdks/sky130-liberty/` — see [`pdks/README.md`](../pdks/README.md) and the
script's own header comment for why this can't come from `fetch-pdks.sh`'s
lambdapdk payload.

```
scripts/install-yosys.sh
scripts/fetch-sky130-liberty.sh
PDK_ROOT="$PWD/pdks/sky130-liberty" PDK=sky130A uv run pytest tests/test_synthesize.py -k integration
```

## `gallery_signals.py` / `bootstrap-gallery-blocks.py`

`bootstrap-gallery-blocks.py` regenerates `blocks/*/output/layout.json`
from the [#4 test corpus](../tests/corpus/README.md); `gallery_signals.py`
(imported by it, not run standalone) runs a `klt sim` PVT sweep per block
against the netlists `fetch-cell-netlists.sh` vendors and attaches the
result as `layout.json`'s `signals` field (15 PVT corners per cell), plus
each block's 3 nominal-corner waveform artifacts under
`blocks/<slug>/output/signals/` for the site's waveform viewer. Requires
`fetch-cell-netlists.sh` to have been run first — otherwise the signals
step is skipped with a warning (base layer/cell metrics still regenerate
normally). See both modules' own docstrings for the full pipeline.

```
scripts/fetch-cell-netlists.sh
python scripts/bootstrap-gallery-blocks.py
```

## `ingest-canary.py`

Ingests a **public** canary block repo (issue #62 — a real 2AM Logic block
designed end-to-end by AI agents, e.g. `2AMLogic/gf180-bandgap`) into
`blocks/<slug>/output/layout.json`, alongside (not instead of) the #4-corpus
blocks the two scripts above bootstrap. Gated fail-closed on the target
repo's GitHub visibility (`gh api repos/<repo> --jq .visibility`) — a
private/inaccessible repo, or `gh` itself being unavailable, refuses and
writes nothing. Pre-layout blocks (no GDS yet) get a `status: "in design —
simulation evidence"` sim-evidence card instead of `"ok"`/`"partial"`/
`"no_artifacts"` — see [`../blocks/README.md`](../blocks/README.md#canary-blocks-issue-62)
for the full field-level documentation.

```
python scripts/ingest-canary.py --repo 2AMLogic/gf180-bandgap
python scripts/ingest-canary.py --repo 2AMLogic/sky130-bandgap
```

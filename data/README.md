# `data/`

Small, checked-in, machine-consumed data files that ship with the repo (as
opposed to `pdks/`, which is fetched/gitignored PDK corpus data — see
`pdks/README.md`).

## `remote-sim-ami-manifest.json`

The published-AMI registry `src/klayout_tools/remote_launcher.py`'s
`resolve_ami()` reads to turn a `(pdk, region)` pair into the versioned,
ngspice-and-PDK-decks-baked AMI the `remote` `klt sim` backend's launcher
provisions from — see
[`docs/design/remote-sim-backend-spike.md`](../docs/design/remote-sim-backend-spike.md)
decision 4 and
[`docs/schemas/remote-sim-ami-manifest.schema.json`](../docs/schemas/remote-sim-ami-manifest.schema.json)
for the entry shape.

**Ships empty (`"images": []`) in this repo.** Populating it requires
actually running `scripts/aws/build-remote-sim-ami.sh` against a real AWS
account (billable — it launches a build instance, bakes ngspice + the PDK
decks, and calls `create-image`) — that is an operator action against a real
account, not something this repo's CI or a checked-in fixture can produce.
An operator who wants to use the `remote` backend runs that script once per
`(pdk, region)` combination they need and commits the resulting manifest
entries (or keeps a private copy — nothing requires publishing your own
account's AMI ids upstream). `resolve_ami()` fails loudly
(`RemoteLaunchError`) rather than silently substituting a different region
or a stale build when the manifest has no matching entry — see
`src/klayout_tools/remote_launcher.py`'s "no silent defaults for
cost-relevant fields" discipline.

**Pointing at a private/operator-built manifest (issue #370):** this repo's
copy is only ever the *final fallback* of a 4-step "first hit wins"
resolution order (`remote_launcher.load_ami_manifest`/
`_candidate_manifest_paths`, mirroring `pdk.find_pdk`'s search-order
convention) — `request.remote.ami_manifest` (a request field, not a CLI
flag: it travels with the sim request the same way `models.pdk_root` does),
else `$KLT_AMI_MANIFEST`, else the user-scope
`~/.config/klt/remote-sim-ami-manifest.json`, else this file.
`scripts/aws/build-remote-sim-ami.sh` writes **both** this repo-checkout
copy **and** the user-scope copy on every successful build, so a freshly
built AMI is immediately usable from any `klt` install on that machine
(`uv tool install`/`pipx`/`pip`) — the "future `--ami-manifest` flag" this
README previously anticipated turned out unnecessary: a request field
composes better with the CLI's `--request`-file-driven contract (`docs/cli/sim.md`)
than a one-off flag would, and the user-scope write-through means most
operators never need to set anything at all.

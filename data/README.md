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
entries (or points `RemoteLauncher(manifest_path=...)` / a future
`--ami-manifest` flag at a private copy — nothing requires publishing your
own account's AMI ids upstream). `resolve_ami()` fails loudly
(`RemoteLaunchError`) rather than silently substituting a different region
or a stale build when the manifest has no matching entry — see
`src/klayout_tools/remote_launcher.py`'s "no silent defaults for
cost-relevant fields" discipline.

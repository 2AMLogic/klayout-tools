# `examples/signoff/`

A minimal, runnable **block manifest** for `klt signoff --manifest` — the
copy-paste starting point a block author adapts, and the committed
counterpart to the inline manifests
[`docs/cli/signoff.md`](../../docs/cli/signoff.md) quotes as prose
(issue [#1954](https://github.com/2AMLogic/klayout-tools/issues/1954)).

| File | What it is |
| --- | --- |
| `manifest.json` | **The thing to copy.** A `kind: "analog"` block manifest citing T1 items 3 (DRC) and 4 (LVS) and nothing else. |
| `fleet.json` | A one-block fleet manifest, for `klt signoff --fleet`. |
| `block.gds` | The block's layout — a hand-drawn, DRC-clean sky130 sketch (see "Why synthetic" below). |
| `drc.json` | The **real** `klt drc --format json` envelope for `block.gds` (`status: "clean"`), captured verbatim. Item 3's evidence. |
| `schematic.spice` / `layout.spice` | The two sides of the LVS compare, in the schematic-equivalent plain-element form [`klt lvs`](../../docs/cli/lvs.md) requires. |
| `lvs.request.json` | The `klt lvs` request binding that pair. |
| `lvs.json` | The **real** `klt lvs --format json` envelope for that request (`status: "match"`), captured verbatim. Item 4's evidence. |
| `generate.py` | Regenerates every file above, byte-identically. |

```bash
klt signoff --manifest examples/signoff/manifest.json
klt signoff --manifest examples/signoff/manifest.json --format json
klt signoff --fleet examples/signoff/fleet.json
```

**Run these from the repo root.** A manifest's file-backed evidence paths
resolve against the *invoking process's* working directory — there is no
manifest-relative anchoring — so `manifest.json` names its evidence as
`examples/signoff/drc.json`, not `drc.json`. Adapt those paths when you copy
the manifest into your own block repo.

## What it grades to

```
block: example-current-mirror  kind: analog
tier: none
T1: 2/10 items met

[UNMET] T1 #1 Design sources
        reason: no_evidence
[UNMET] T1 #2 Layout
        reason: no_evidence
[MET  ] T1 #3 DRC clean
        cite: examples/signoff/drc.json (kind=drc, status=clean, content_hash=sha256:..., exit_status=0)
[MET  ] T1 #4 LVS clean
        cite: examples/signoff/lvs.json (kind=lvs, status=match, content_hash=None, exit_status=0)
[UNMET] T1 #5 Full corner verification vs a ratified spec
        reason: no_evidence
...
```

Two `MET` rows, eight `UNMET`/`no_evidence` rows, and the whole
`T2`-`T4` ladder `unmet`/`tier_not_supported`. The command exits `3`, and
**that is the point**: `tier` is `null` because the block does not yet clear
T1, and `klt signoff` says so rather than rounding two genuine passes up
into a tier claim. It would exit `0` only once every T1 item resolved to a
passing, fresh check.

The eight uncited items are the example's most important half. Each one
names *why* it is unmet (`no_evidence` — the manifest never cited a check
for it), so a skipped check is visibly distinguishable from a check that ran
and failed (`check_failed`). Nothing is ever silently assumed met.
`tests/test_signoff.py::test_committed_example_manifest_grades_items_3_and_4_met`
pins exactly this shape so the example cannot rot.

## Reading the manifest

```json
{
  "block": "example-current-mirror",
  "kind": "analog",
  "evidence": {
    "3": {
      "file": "examples/signoff/drc.json",
      "content_hash": "sha256:..."
    },
    "4": "examples/signoff/lvs.json"
  }
}
```

- **`kind`** is required (`analog` / `digital` / `mixed-signal`) — it selects
  which column of the per-kind T1 items applies.
- **Item 3 pins `content_hash`.** That is the staleness gate: the value is
  `block.gds`'s content hash as recorded in `drc.json`'s own
  `provenance.input.content_hash`. Re-run DRC against a *different* layout
  revision and the pin no longer matches, so the item renders `unmet`
  (`stale_evidence`) instead of quietly claiming a pass against the wrong
  layout. Pin it whenever you can.
- **Item 4 does not pin one**, deliberately: `klt lvs` populates no
  `provenance.input` block at all (its two netlist inputs are hashed into
  `environment.layout_sha256`/`reference_sha256` instead), so a pinned hash
  there could only ever render `unmet`.
- **Everything else is simply absent.** Adding an item means adding its
  evidence entry — a bare path string, `{"file": ..., "content_hash": ...}`,
  or a command-backed `{"command": [...], "cwd": ...}` entry that `klt
  signoff` actually runs. See
  [`docs/cli/signoff.md`](../../docs/cli/signoff.md)'s "Tier-verdict report
  (`--manifest`)" for the full evidence-entry grammar.

## Do not pad items 1, 2, 9, and 10

Those four T1 items have no `klt` verb behind them, so `klt signoff` grades
them on whether *some* recognised, passing envelope was cited — not on
whether that evidence has anything to do with the claim. Citing this
example's `drc.json` again for item 10 ("Repo hygiene") would produce a
third `MET` row, and the tool would have no basis on which to object.

This example deliberately does not do that. An honest `UNMET`/`no_evidence`
row is worth more than a mechanically-green one, and the tool checks
envelope validity, freshness, and kind-restriction — never topical
relevance. See [`docs/cli/signoff.md`](../../docs/cli/signoff.md)'s "Items 1,
2, 9, and 10: `klt signoff` cannot check topical relevance".

## Why synthetic

The evidence here is **real** — `drc.json` and `lvs.json` are captured `klt`
output, never hand-written — but the *block* it describes is not. `block.gds`
is a schematic-looking sketch (one `diff.drawing` region crossed by two
`poly.drawing` gates, plus `li1`/`met1` routing bars): enough geometry for a
genuine, clean run against the built-in `sky130` deck, not a manufacturable
current mirror. `layout.spice` is likewise hand-written, standing in for
`klt extract` output.

That is the same trade `examples/drc/` and `examples/size/` make. A worked
example only needs to exercise the contract shape — here, the manifest
schema, the evidence-entry forms, the staleness pin, and the `MET`/`UNMET`
split. `examples/design-pipeline/` is the place to look for a full,
coherent, PDK-backed artifact set.

The payoff of staying synthetic: **this example needs no PDK.** `klt drc
--deck sky130` uses the built-in rule deck and `klt lvs` compares two
netlists in-process, so `generate.py` runs from a bare `pip install klayout`
checkout — unlike `examples/design-pipeline/generate.py`, which needs a
resolvable sky130A install.

## Regenerating

```bash
uv run python3 examples/signoff/generate.py
```

Byte-identical for a fixed `klt`/KLayout build: the GDS is written with
timestamps suppressed (a default GDS write stamps modification times, which
would move `block.gds`'s content hash — and therefore item 3's pin — on
every run), and the script rewrites `manifest.json`'s pinned hash from the
freshly captured `drc.json`, so the two can never drift apart. The captured
envelopes do embed `provenance.klt_version`/`provenance.klayout_version`, so
a version bump legitimately changes them — the graded verdicts do not.

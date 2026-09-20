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
| `drc.json` | The **real** `klt drc --format json` envelope for `block.gds` (`status: "clean"`), captured as emitted apart from [two normalized fields](#two-normalized-provenance-fields). Item 3's evidence. |
| `schematic.spice` / `layout.spice` | The two sides of the LVS compare, in the schematic-equivalent plain-element form [`klt lvs`](../../docs/cli/lvs.md) requires. |
| `lvs.request.json` | The `klt lvs` request binding that pair. |
| `lvs.json` | The **real** `klt lvs --format json` envelope for that request (`status: "match"`), captured as emitted apart from [two normalized fields](#two-normalized-provenance-fields). Item 4's evidence. |
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
T1: 2/11 items met

[UNMET] T1 #1 Design sources
        reason: no_evidence
[UNMET] T1 #2 Layout
        reason: no_evidence
[MET  ] T1 #3 DRC clean
        cite: examples/signoff/drc.json (kind=drc, status=clean, content_hash=sha256:..., exit_status=0)
        coverage: layers_in_stream_without_rules=0, rules_skipped=49 (capm.enclosing.via3.1, ...), deck_scope=17 (cap2m, capm, ct, difftap, +13 more)
[MET  ] T1 #4 LVS clean
        cite: examples/signoff/lvs.json (kind=lvs, status=match, content_hash=sha256:..., exit_status=0)
[UNMET] T1 #5 Full corner verification vs a ratified spec
        reason: no_evidence
...
```

Two `MET` rows, nine `UNMET`/`no_evidence` rows, and the whole
`T2`-`T4` ladder `unmet`/`tier_not_supported`. The command exits `3`, and
**that is the point**: `tier` is `null` because the block does not yet clear
T1, and `klt signoff` says so rather than rounding two genuine passes up
into a tier claim. It would exit `0` only once every T1 item resolved to a
passing, fresh check.

The nine uncited items are the example's most important half. Each one
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
- **Item 4 does not pin one**, deliberately — but not because it *can't*.
  `klt lvs` used to populate no `provenance.input` block at all, and this
  file used to say a pinned hash there could only ever render `unmet`. That
  stopped being true in
  [#1969](https://github.com/2AMLogic/klayout-tools/issues/1969): `lvs.json`
  now carries `provenance.input: {"content_hash": ..., "role": "netlist"}`,
  with the `role` discriminator added in
  [#2027](https://github.com/2AMLogic/klayout-tools/issues/2027). Two
  reasons the example still cites it as a bare path string:
  - **It is the other evidence-entry form.** A block author needs to see
    both, and item 3 already shows the pinned one.
  - **It would pin a different artifact.** In this example's pre-extracted
    `request.layout.netlist` shape, that hash is of `layout.spice` — the
    layout-side *netlist* — not of `block.gds`. Pinning it gates staleness
    of the netlist you compared, which is a genuinely useful claim, just not
    the same claim item 3 makes. `klt signoff` compares input hashes only
    within a `role` for exactly this reason (#2027), so a `netlist` digest is
    never checked against item 3's `layout` digest.

  Pin it if your flow regenerates `layout.spice` from `klt extract` and you
  want that revision nailed down; leave it off if the bare-path form is
  enough.
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

## Two normalized provenance fields

`drc.json` and `lvs.json` are captured `klt` output, with exactly two
`provenance` fields overwritten with `null` before the file is written:

| Field | Why it is normalized |
| --- | --- |
| `provenance.klt_version` | The running build's identity suffix ([#2090](https://github.com/2AMLogic/klayout-tools/issues/2090)): `0.5.0` from a clean release tag, `0.5.0+g<sha>` or `0.5.0+g<sha>.dirty` from anything else. It is not stable even *within* one regeneration: `drc.json` is captured first, and writing it dirties the tree, so `lvs.json` would record a `.dirty` suffix its sibling does not. |
| `provenance.deck.released` | A lookup of the deck's content hash in the running build's release-history table. It says whether a *released* `klayout-tools` ships this exact deck — which flips to `false` the moment anyone edits a deck after a tag, and flips back when the next release regenerates the table, for unchanged fixture bytes. |

Both describe the machine and checkout that *regenerated the fixture*, not
the block being signed off. Committing what a dev checkout happens to
produce would bake a local artifact into a worked example — and would make
the round-trip check below impossible to keep green.

`null`, not a substituted value: `null` is what the JSON contract already
reserves for "no answer recorded here"
([`docs/json-contract.md`](../../docs/json-contract.md)), and
`is_deck_hash_released`'s `None` leg is already defined as "cannot confirm",
which consumers must never read as a claim. Writing `released: true` from a
checkout where it is genuinely `false` would be a fabricated release claim;
writing the dev checkout's own `false` would publish a fact about that
machine instead of about the block.

**Nothing else is touched.** `generate.py` re-serializes the captured
stdout with the CLI's own settings and fails the run unless it reproduces
the verb's bytes exactly, so "everything but these two fields is real
output" is enforced, not just claimed. Every verdict, count, content hash,
and coverage entry below them is whatever `klt` emitted.

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

Byte-identical from any checkout for a fixed `klt`/KLayout build: the GDS is
written with timestamps suppressed (a default GDS write stamps modification
times, which would move `block.gds`'s content hash — and therefore item 3's
pin — on every run), the two checkout-dependent `provenance` fields are
normalized (above), and the script rewrites `manifest.json`'s pinned hash
from the freshly captured `drc.json`, so the two can never drift apart. The
captured envelopes do still embed `provenance.klayout_version`, which
`uv.lock` pins — a KLayout bump legitimately changes them, and belongs in
the bumping PR. The graded verdicts do not change.

**CI enforces this.** `scripts/check-signoff-example.sh` (the
`examples/signoff round-trip` job in
[`ci.yml`](../../.github/workflows/ci.yml)) regenerates this directory on
every PR and fails on any diff. A red check there is not a broken
generator — it means a `klt drc`/`klt lvs` change moved the committed
evidence, and the fix is to regenerate and commit the result *in the same
PR*, updating this README and `generate.py`'s docstring if the diff changed
anything they describe in prose. Before that check existed, these fixtures
drifted a whole coverage-schema version behind reality while the README went
on documenting the stale shape as current
([#2028](https://github.com/2AMLogic/klayout-tools/issues/2028)).

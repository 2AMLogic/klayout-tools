# Live run: `klt erc` vs `klt lvs` substrate agreement on `2AMLogic/sg13g2-ldo`

Issue #5346, piece E of #5336. This is the record of the **out-of-band live
run** that `docs/design/erc-substrate-continuity.md` §9 fixture 5 requires and
that this repository's CI cannot perform: it needs the `2AMLogic/sg13g2-ldo`
chip-repo checkout, which is not available to a CI job here. It was run on a
fleet host that has that checkout, against the committed LDO core GDS.

Nothing in this document is asserted from a synthetic fixture. The §9 fixtures
1 and 2 synthetic agreement tests live in
`tests/test_erc_substrate_agreement.py`; the item 11 grade tests live in
`tests/test_signoff_substrate_item11.py`. This record is the separate,
real-layout half.

## 1. Inputs (cited by content hash)

Chip repo `2AMLogic/sg13g2-ldo` @ `41a8294b1c3ef406a6affec4dbae202585580fd2`:

| Input | Path (chip-repo relative) | sha256 |
|---|---|---|
| Layout | `layout/sg13cmos5l-ldo_core_cmos5l/sg13cmos5l-ldo_core_cmos5l.gds` | `06d92e1aadb8b51614016f2f5d953c8bd0674380267f8ab90c2b0cca59576629` |
| ERC spec | `layout/sg13cmos5l-ldo_core_cmos5l/erc-supply-spec.json` | `65be24907bb66af7c0752716477ec85f77495e800dc82dcf6d0ac1acb4075171` |
| LVS request | `layout/sg13cmos5l-ldo_core_cmos5l/lvs_request.json` | `f5b7f838b902473f7eee4f9d6a7332b4bd29e2772fa406bef63d783204547409` |

Tool side: `klt 0.9.2.dev979+g01232fdb83b0`
(`01232fdb83b0746dfe88f913727191a8132a2535`), KLayout `0.30.12`, extraction
deck `sg13cmos5l` `content_hash: sha256:8b005441745c8f9500d04e0fece081fe5162bb1d562cc8d6e09b60f1f24d1446`.
The spec declares three `kind: "supply"` nets (`VIN`, `VOUT`, `VSS`) and three
ties (`nwell_tie_VIN`, `nwell_tie_TAIL`, `substrate_tie_VSS`); the substrate
tie is the native-substrate form (`well_layer: null` + `well_boxes`).

Reproduce (from the chip-repo root, with this repo's `klt` on `PATH`):

```sh
klt erc layout/sg13cmos5l-ldo_core_cmos5l/sg13cmos5l-ldo_core_cmos5l.gds \
  layout/sg13cmos5l-ldo_core_cmos5l/erc-supply-spec.json --format json
klt erc layout/sg13cmos5l-ldo_core_cmos5l/sg13cmos5l-ldo_core_cmos5l.gds \
  layout/sg13cmos5l-ldo_core_cmos5l/erc-supply-spec.json \
  --deck sg13cmos5l --format json
cd layout/sg13cmos5l-ldo_core_cmos5l && klt lvs lvs_request.json --format json
klt extract layout/sg13cmos5l-ldo_core_cmos5l/sg13cmos5l-ldo_core_cmos5l.gds \
  --deck sg13cmos5l --top sg13cmos5l_ldo_core_cmos5l --format json
```

## 2. The two ERC runs, side by side

The same layout and the same spec, differing only in whether `--deck` is
passed. This is the live form of the §4 decidable-or-disclosed rule.

| Observable | **no `--deck`** | **`--deck sg13cmos5l`** |
|---|---|---|
| `erc_status` | `clean` | `clean` |
| `erc_finding_count` | `0` | `0` |
| `erc_coverage.inapplicable` | one record: `{id: erc.supply_short, reason: substrate_isolation_undecidable, missing: ["--deck naming the PDK family, so the isolation layers can be measured", "or assert ties[].isolation_boxes"]}` | `[]` |
| `erc.supply_short` in `checked` | no | yes |
| `erc.supply_short.substrate` in `checked` | no | yes |
| `provenance.substrate_bodies` | absent | `vsubs` (ties: `substrate_tie_VSS`) + 4 well bodies |

`status: "not_checked"` and exit `4` on both runs are the documented
**antenna** outcome of passing no `--pdk` (`docs/cli/erc.md`: zero graded
antenna levels ⇒ `not_checked`/exit 4), not an ERC verdict. Gate on
`erc_status`, as that document says.

With the deck, the substrate pass **evaluates**: the whole p-substrate is one
body `vsubs`, tied by `substrate_tie_VSS` only. `VIN` and `VOUT` reach no
substrate tie, so no two supply nets share a substrate body, and there is no
substrate-path `erc.supply_short`. Without the deck there is no isolation
layer to measure and no `ties[].isolation_boxes` asserted, so the question is
**disclosed as undecidable** rather than answered.

Both verdicts are correct and they do not contradict each other: one is an
answer, the other is a refusal to answer.

## 3. The LVS half

`klt lvs lvs_request.json` (committed request, `combine_devices: true`):

- `status: "match"`, `error_count: 0`.
- `counts.nets`: `{layout: 13, reference: 13, matched: 13}`;
  `counts.devices` `18/18/18`; `counts.pins` `6/6/6`.
- `net_correspondence` pairs `VIN↔VIN`, `VOUT↔VOUT`, `VSS↔VSS`, each
  `pin: true`, and all 13 nets 1:1. **No two supply nets are merged on the
  layout side.**
- `mismatch_count: 1`, all of it `device.geometry_not_compared`
  (severity `warning`): KLayout declares the `rhigh` resistor's `A`/`L`/`P`/`W`
  secondary, so only `R` was compared. Pre-existing, unrelated to the
  substrate path.
- `hints: null`, `hints_applied: null` — the request supplies no
  `hints.short_nets`.
- `power_connectivity.status: "unchecked"`, because
  `reference.form` is `plain-element`: the reference netlist carries its own
  supply pins and nets, so they take part in the ordinary compare and the
  signal-only gate-level check does not apply.
- `body_verification.status: "verified"`.

`klt extract --deck sg13cmos5l` independently: `net_count: 13`, with `VIN`,
`VOUT` and `VSS` on distinct net ids (`11`, `12`, `14`), `merged_net_labels:
[]`, `split_net_labels: []`.

## 4. Verdict: ERC and LVS agree, with no disagreement to explain

- ERC with the deck: substrate-path `erc.supply_short` **evaluated, zero
  findings** — `VIN`/`VOUT`/`VSS` do not share a substrate body.
- LVS and extract: `VIN`/`VOUT`/`VSS` are **three distinct nets**, 13↔13
  matched, nothing merged.

The two engines report the same thing about the same layout, so §9 fixture 5's
"any disagreement carries a stated reason" clause has nothing to discharge on
this layout. The ERC run without `--deck` is not a disagreement either — it
reports that it cannot decide, and the `missing` list says exactly what would
make it decidable.

### On substituting `klt extract` net identity for `hints.short_nets`

PR #5366 noted that `tests/test_erc_substrate_agreement.py` compares ERC
against `klt extract` net identity rather than `klt lvs` `hints.short_nets`,
and asked the live run to rule on whether that substitution is acceptable.

**It is acceptable, and it is the more correct choice.** The live run shows
why: `hints.short_nets` is a caller-supplied **input** hint on the request
document, not an engine observation. This chip's committed request supplies
none, so the live report carries `hints: null` / `hints_applied: null` — there
is no `hints.short_nets` for an agreement test to read. An agreement assertion
built on it would be comparing ERC against the test's own input. The
engine-side observables that actually answer "does LVS see these supplies as
one net" are `net_correspondence` / `counts.nets` (LVS) and net identity
(`klt extract`), which is what the test uses.

A worthwhile follow-up, not required by this issue: have the synthetic
agreement test additionally assert LVS `net_correspondence` for the §9
fixtures, so the test exercises the same field this live run was read on.

## 5. Edge case: `nbulay` undrawn vs drawn

§9 fixture 5 and issue #5346's test plan ask for "sg13g2 with no `nbulay`
drawn (evaluated) vs drawn (disclosed)". The committed LDO core draws **no**
`nbulay`: `32/0` does not appear in the stream (the no-`--deck` run's
`erc_coverage.layers_in_stream_without_declaration` is `["28/0", "31/2",
"44/0", "63/0", "99/39", "111/0"]`), which is the undrawn/evaluated case in
§2 above.

**The AC's "drawn ⇒ disclosed" expectation is now out of date, and the live
run is the evidence.** It was written before piece D landed. Since #5345 the
`sg13cmos5l` and `sg13g2` decks declare `substrate_isolation=(32, 0)`
(`nBuLay.drawing`) with `substrate_isolation_needs_nwell_ring=True` (#5359),
so under `--deck` a drawn `nbulay` is **evaluated**, not disclosed. Disclosure
on this layout is what happens when **no deck is named**, which is the
left-hand column of §2. The dead fallback strings for `sg13g2`/`sg13cmos5l` in
`erc_substrate.py`'s `_FAMILY_MISSING` are now unreachable through `--deck`
for exactly that reason.

To confirm the drawn branch on this real layout rather than on a fixture, two
**derived variants** of the committed GDS were built — the committed stream
plus a `32/0` island at `(200, -10, 220, 15)` enclosing the second
`substrate_tie_VSS` tap bar, once without and once with a closing 2 µm `31/0`
ring. These are derived probes, clearly not the committed layout, and nothing
in §2-§4 rests on them:

| Variant | `inapplicable` | `provenance.substrate_bodies` (substrate kind) |
|---|---|---|
| `nbulay` drawn, **no** closing NWell ring | `[]` (evaluated) | `vsubs` only — the island does not count as isolation |
| `nbulay` drawn, **with** closing NWell ring | `[]` (evaluated) | `vsubs` **and** `vsubs_iso0`, both tied by `substrate_tie_VSS` |

Both stay `erc_status: clean` with zero findings, which is right: `VSS` is the
only substrate-tied supply, so partitioning the substrate cannot create a
supply short. The second row is the live confirmation that #5359's
closing-ring rule fires on real geometry and that `vsubs_iso<n>` is minted and
named in provenance, per §8. `klt extract` on the ring variant keeps
`net_count: 13` with `VIN`/`VOUT`/`VSS` distinct; its device count differs
from the committed layout by one (136 vs 137), an artifact of the hand-drawn
ring overlapping real geometry in a derived file, not a finding.

## 6. Consequence for the chip repo (action lives there, not here)

`layout/sg13cmos5l-ldo_core_cmos5l/erc-supply-spec.md` documents the stage-8
invocation **without** `--deck`, and the committed
`erc_supply_report.json` predates the substrate pass. Re-running that exact
documented command on current `klt` now yields
`inapplicable[erc.supply_short] = substrate_isolation_undecidable`, and item
11 reads that as **unmet**. Verified directly against both live envelopes:

```text
no --deck            substrate_undecidable_gate -> {"power_delivery":
                       {"substrate_isolation_missing": [
                         "--deck naming the PDK family, so the isolation
                          layers can be measured",
                         "or assert ties[].isolation_boxes"]}}
                     substrate_continuity       -> null

--deck sg13cmos5l    substrate_undecidable_gate -> null
                     substrate_continuity       -> {"checked": true,
                                                    "by_assertion": false}
```

So the chip repo needs to add `--deck sg13cmos5l` to its stage-8 `klt erc`
invocation (it already passes `--deck` to `klt extract` and `klt lvs`) and
refresh the committed report. That makes item 11 `met` **by evaluation** of
the substrate path rather than by the field not existing yet.

This is a change in `2AMLogic/sg13g2-ldo`, outside this issue's scope — it is
recorded here and on #5346 for the chip repo to pick up. It is not a
relaxation of any chip-repo assertion (§10 out-of-scope): it names the deck
the chip already uses elsewhere, so the substrate question is answered instead
of disclosed.

# Runset–curated net correspondence for `klt pex` (issue #6606)

**Status: decided and landed** (`klt pex --engine klayout-runset --deck
<curated>`, 2026-11-20). This document records the measured decision issue
#6606 asked for: how the runset-engine leg's nets and a curated deck's
parasitic nets are put in correspondence, which alternatives were **measured**
rather than argued, and what `klt signoff` does with a parasitic-free pex leg
(AC 4).

Measurements used KLayout 0.30.12 with the IHP SG13CMOS5L `pnpMPA` LVS
testcase and the inverter fixture in `tests/test_extract_body_nets_sg13cmos5l.py`.
The table identifies runs using the PDK runset and runs using controlled
connectivity changes. Separate pad layouts test split detection and stacked metals.

## The gap

The runset engine (`klt extract --engine klayout-runset`, #5481) and a
curated deck extract the same layout independently and number their
unlabelled nets independently (`$1`, `…`; `docs/cli/extract.md`'s "Anonymous
net numbering" — deliberately *not* a contract even within one engine). A
name-based join is therefore impossible by construction, and `klt pex
--engine klayout-runset` (#6559) could only disclose `parasitics_carried:
false`.

## Measured: what each side can give the join

- **M1 — the runset `.lvsdb` carries per-net geometry.** Read back with
  `kdb.LayoutVsSchematic.read()`, `polygons_of_net(net, layer, True)`
  returns real per-net shapes over the database's *internal* layout
  (`pnpMPA`: net `$1` holds 62 polygons on one internal layer; the inverter
  fixture: each net 3–4 polygons across cont/metal/sd-like layers). The
  IHP runset stores anonymous layers. A runset that registers an original
  stream layer can preserve its GDS layer and datatype in the same database
  format. KLayout's `layer_info(index)` retrieves that provenance after reload.
- **M2 — the curated side already has a point→net oracle.** `klt extract
  --probe` (#2986) resolves a point on a conductor role (`metal0`…,
  `poly`, `well`) to the *final* net name and `cluster_id`, post-purge and
  post-rename, on the live `LayoutToNetlist`. The join reuses this machinery
  wholesale; no new extraction-side API was added for it.
- **M3 — coordinates agree.** Both engines' per-net polygons land in the
  original stream's coordinate space (the inverter fixture's shapes compare
  equal, dbu 0.001 both sides), so a geometric join needs no transforms.

## Measured refutations (why the rule is what it is)

- **R1 — probing every point on every role is unsound.** The naive join
  (sample any point of a runset net, probe every curated conductor role)
  matched *every* net to the gate net on the inverter fixture: a
  source/drain polygon's hull vertex lies exactly on the poly bar's edge
  (the sd region is `activ − poly`, cut at the bar), and the channel zone
  (`activ ∧ poly`) is *inside* both the gate and the S/D clusters of either
  engine by construction. Device layers overlap; wells *contain* the
  diffusion they host. **Rule: the join runs on the metal routing stack
  only, and only on points strictly interior to the runset polygon.**
- **R2 — boundary points produce false matches even on metal.** The
  substrate cluster's bbox centre (a whole-die polygon) landed exactly on
  the gate metal's top edge and probed the gate net. **Rule: sample points
  are strictly interior** (bbox centre when strictly inside, otherwise an
  inward bisector step from a convex hull vertex); a probe hit then implies
  genuine area overlap, not edge-touching.
- **R3 — layer index and one-sided area overlap can both misidentify a
  metal.** In the inverter fixture, a Metal2 pad on 10/0 placed inside the
  VGND Metal1 pad on 8/0 has 100 % area overlap with both masks. With no
  via, the curated probe at (0.2, 0.5) µm returns VGND on Metal1 and UPPER
  on Metal2. The saved stream layer/datatype identifies both correctly.
  Removing provenance from two coincident metal masks leaves both layer
  assignments ambiguous.

## The landed rule

Use the runset layer's original GDS layer and datatype when available. For
anonymous layers, compare the selected circuit's per-layer union with the
complete overlapping islands of each original mask. This comparison allows
the circuit to omit whole islands. Containment of at least 99 % of the
runset area identifies *candidate* metal roles. A match requires exactly one
candidate, equal island geometry, and no equally good non-metal source.
This distinguishes a via inside a pad from the metal that surrounds it.
The deck also connects its contact layer directly to the first metal.
For a runset contact with known stream provenance, probe the intersection
of the contact and that metal landing. This exposes a missing contact
connection on the runset side, including a fragment with only a contact.

Probe one strictly interior grid point on **every** polygon of each identified
metal layer. Both the hull and hole edges are excluded. A 16-pad runset net
whose last pad belongs to a different curated net gives a split even when
the first 15 belong to the same net. The 17- and 65-pad regressions exercise
the same case beyond the former sampling limits.

Confirmed hits on several curated clusters establish a **split**. Confirmed
hits from several runset nets on one curated cluster establish a **merge**.
A unique pairing with complete probe coverage earns a **match**. A polygon
without a usable interior point, an unresolved probe, or an ambiguous metal
assignment leaves the affected runset net unmatched. Probes of candidate
roles on uncertain layers can rule out exclusive ownership by another net.
Only confirmed geometry contributes to split and merge reports.

The merged leg is the runset leg's own netlist rewritten in place: matched
nodes renamed (a named runset net keeps its name — device-side truth; an
anonymous one adopts its curated counterpart's label, never onto a
colliding name), plus one lumped series `R` (node→hub), one ground `C`,
matched coupling capacitors, and the deck's substrate DC tie. This is
deliberately **coarser than the curated engine's own star** — the curated
netlist distributes a net's R across its *curated* device terminals,
terminals that do not exist in a runset leg; the merged writer collapses to
the single-lumped form `parasitics.nets[]` already publishes
(`rc_model: "lumped"`).

The selected subcircuit owns these cards. A cluster ID joins each probe match
to the curated R/C row even when disconnected islands share a label.
Coupling rows identify their endpoints by label alone. Ambiguous pairs appear
in `omitted_couplings`. Ground capacitors use the runset substrate match when
one exists. Otherwise they use a separate node with the deck's DC tie, and
`substrate_return.runset` is null. The tie resistance and the actual return
node accompany the item-7 citation so a reader can assess the return path.

### Measured outcomes

| Layout | Result |
|---|---|
| inverter fixture, controlled runset with Metal1 contacts | 5/8 runset nets matched (VGND, Y, VPWR, Y, A — five distinct curated cluster IDs), 3 body clusters unmatched, 0 disagreements, 11 cards (5 R/C pairs and the substrate tie). Both Y islands carry their own pair. The substrate return has no runset match |
| inverter fixture, IHP PDK runset | 3 of 4 runset nets matched (VGND, Y and A), 0 disagreements, 7 cards |
| inverter fixture, runset contact-to-metal joins omitted | every labelled curated net reported as a merge over exactly its 2 fragments; 0 matches; no cards — and no stray ground tie |
| inverter fixture, diffusion bridged to gate (deliberate wrong join) | 2 split disagreements naming the curated nets each bridged cluster spans; the one cluster left whole still matches |
| `pnpMPA` PDK testcase (the flagship gap) | curated leg extracts **nothing but `vsubs`** — its labels sit on `10/25` (the deck reads `.pin` `10/2`) and its vias are KLayout PCell instances the pip `klayout.db` curated engine cannot resolve — so all four runset nets are reported (PAD/VDD unmatched, `$1`+`VSS` a merge onto `vsubs`), 0 cards, `parasitics_carried: false` |

The `pnpMPA` row is the honest boundary of this route, not a failure of
the correspondence: the join is sound wherever the curated leg has coverage
of the layout's conventions (labels on the deck's `.pin` layers —
`klt`-generated blocks and `klt place-and-route` output, per #1385 — and
explicit via geometry). On a layout the curated deck cannot read, the
merged run degrades to exactly the disclosed device-only leg.

## AC 4: the item-7 grading policy

**A parasitic-free pex leg is refused for item 7** (`unmet`, reason
`no_parasitics`), decided in `signoff_pex_disclosure.parasitic_free_refusal`
and gated in `signoff._kind_gated_check`. Until #6606 the disclosure was
reported-only; with a route that *can* carry parasitics, a leg that does
not never establishes "post-layout parasitic verification" — its `delta[]`
rows are device-representation deltas. The remedy is re-running *this*
artifact with `--deck <curated>` (the same artifact-kind discipline as
`not_post_layout`); a merged leg that attached cards discloses
`parasitics_carried: true` and grades exactly as a curated one. The
citation identifies an unmatched substrate return when one was used.

## What is deliberately still not carried

- Measured device parameters on runset `X` cards (#5513, `--device-param`)
  — orthogonal; declare them as on the parasitic-free route.
- The richer curated parasitic models (`--critical-net`, `--distributed-rc`,
  `--resistance-graph*`, `--mom-rlc-*`, `--parasitic-variant`,
  `--substrate-tie-ohms`, `--deck-option`, `--pins`) are refused by name
  under the merged route too: the merged writer serves the default lumped
  model only.
- `klt extract` itself still extracts devices-and-connectivity only under
  the runset engine; the merged leg is a `klt pex` concern (the leg `klt
  pex` re-simulates), and `klt extract --engine klayout-runset --deck`
  remains refused.

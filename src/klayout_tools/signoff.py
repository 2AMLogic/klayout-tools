"""Aggregate ``klt`` verb JSON envelopes into one signoff verdict.

The gap this closes (issue #309): nothing combines ``klt drc``/``klt
lvs``/``klt extract``/``klt sim``'s independent per-verb JSON reports into a
single pass/fail artifact. ``.claude/skills/design-signoff/SKILL.md``
hand-assembles that artifact today by walking the T1 checklist in
``docs/design-evidence-tiers.md``; this module is the first mechanical
increment underneath it -- the two *always-checkable* halves of that walk
that need no external contract to implement:

1. **Combine each input envelope's own pass/fail verdict** into one overall
   verdict (`klt drc`'s ``status``, `klt lvs`'s ``status``, `klt sim``'s
   ``status``; `klt extract`'s ``status`` is informational -- see
   :func:`_check_passed`).
2. **Refuse to combine mismatched provenance** (issue #251's ``provenance``
   block, shipped in all four verbs): a "clean" DRC report against last
   week's layout and a "match" LVS report against today's is not a signoff,
   it is two unrelated facts. See :func:`_provenance_consistency`.

What this module does **not** yet do: diff the aggregated result against a
block's declared spec (S3 in ``docs/design/design-pipeline.md`` has no
machine-readable schema yet -- see that doc's §4 gap map).

## Tier-verdict mode (issue #722, Phase 0 of epic #706)

:func:`build_tier_report` is a second, additive entry point alongside
:func:`build_signoff`: instead of aggregating a fixed set of envelope files,
it renders the full T1-T4 item skeleton -- mechanically parsed from
``docs/design-evidence-tiers.md`` by :mod:`.design_evidence_tiers`, never
duplicated here -- and grades each item against a caller-supplied **block
manifest** (kind + per-item evidence locations). An item is ``"met"`` only
when its evidence resolves to a *passing* ``klt`` JSON envelope with fresh
provenance; anything else (no evidence given, an unreadable/malformed
evidence file, an envelope whose own check did not pass, or a stale
input-hash pairing) renders ``"unmet"`` -- this phase never fabricates a
``"met"`` verdict for an item with no runnable check behind it.

## Gate binding (issue #825, Phase 1 of epic #706)

Phase 0 above only *read* pre-existing envelope files a human (or another
process) had already produced. A manifest ``evidence`` entry may now name a
**command** instead of a file -- ``{"command": [<argv>, ...]}`` -- and
:func:`build_tier_report` actually runs it (``klt drc``/``klt
lvs``/``klt extract`` for netlist regeneration/``klt sim`` for corner sim,
whichever gate the manifest points at) as a subprocess, grading the item
against *that run's own* exit status and stdout, never a pre-existing
file's say-so. A nonzero exit, a timeout, a launch failure, or stdout that
doesn't parse as a recognized ``klt`` envelope all render ``"unmet"``,
exactly like every other ungrounded case Phase 0 already refused to
fabricate a ``"met"`` for -- see :func:`_grade_evidence`. A ``"met"``
citation now always carries a ``"command"`` field (the executed argv,
joined for display; ``None`` for a file-backed entry, since no command was
run to produce it) alongside the ``exit_status`` that command actually
returned -- Phase 0's ``exit_status: 0`` for a file-backed entry remains
*inferred* (a readable, passing envelope implies its producing command
must have exited zero); a command-backed entry's ``exit_status`` is
*observed*.

## Statistical-evidence binding (issue #870, Phase 2a of epic #706)

Phase 1 bound the four *deterministic* gates (DRC/LVS/netlist regeneration/
corner sim). This phase extends the same evidence model to the T1 checklist's
statistical item (item 6, "Statistical claims carry Monte Carlo evidence"):
:func:`_classify`/:func:`_check_passed`/:func:`_detail` now also recognise a
``klt yield`` (issue #816, Phase 1a of epic #710) JSON report -- reachable
via a file-backed *or* command-backed ``evidence`` entry exactly like every
other kind, no new evidence shape. A ``"met"`` verdict passes on ``status ==
"pass"`` (every declared ``target_yield`` met) or ``status == "reported"``
(no measurement declared one, so nothing could fail); ``status == "fail"``
(a declared ``target_yield`` was not supported at the stated confidence)
renders ``"unmet"``, same as every other kind's failing check.

`klt yield`'s current JSON shape (as of issue #816) carries no `provenance`
block of its own -- unlike drc/lvs/extract/sim, its envelope names no content
hash for the Monte Carlo sample document it analysed. Rather than leave a
`"met"` yield citation with no input hash at all (see :func:`_grade_evidence`
below), this module hashes that referenced samples document directly, the
same ``sha256_file`` helper every other kind's own `provenance` block already
uses -- see :func:`_yield_samples_content_hash`. This is a Phase-2a
reconciliation against #710's *current* report shape, exactly as the issue
anticipated ("this issue's binding logic and interface can be built and
tested against #710's current report shape now, with a follow-up
reconciliation if that shape changes"): if a later #710 phase adds its own
`provenance.input.content_hash` to `klt yield`'s JSON, that value takes
precedence automatically and this fallback stops firing, no code change
needed here.

An item with no backing Monte Carlo campaign evidence renders `"unmet"` via
the same `_REASON_NO_EVIDENCE`/`_REASON_UNREADABLE_EVIDENCE`/etc. machinery
every other item already uses -- there is no separate "statistical" code
path to fabricate a `"met"` for, by construction.

## Post-layout binding: item 7 <- `klt pex` (issue #871, Phase 2b of epic #706)

Item 7 ("Post-layout verification") is the T1 checklist's schematic-vs-
extracted-netlist re-simulation delta. Before this phase, `_build_tier_item`
was kind-agnostic per item -- any recognised envelope (even a bare `klt drc`
report) could satisfy *any* item, including item 7, which let a manifest
render item 7 `"met"` on evidence that never actually re-simulated an
extracted netlist. This phase adds a per-item ``allowed_kinds`` restriction
(:func:`_build_tier_item`'s new parameter, wired only at item 7's call site
in :func:`build_tier_report`) so item 7 only accepts evidence that classifies
as kind ``"pex"`` -- a citation of any other recognised kind now renders
``"unmet"`` (:data:`_REASON_WRONG_KIND`), never a fallback pass. Items 1-6
and 8-10 are unaffected (``allowed_kinds=None`` there, preserving the
original unrestricted behaviour).

**Envelope shape, now ratified by issue #801.** At the time this phase
(#871) landed, `klt pex` (Epic #709) did not exist yet, so :func:`_classify`
recognised a **Curator-proposed, provisional** `pex` shape ahead of the real
command: a top-level ``delta`` list plus a ``reference_netlist`` field,
mirroring how `klt sim`'s shape is detected by
``measurements``/``corner_count`` and `klt extract`'s by
``device_count``/``nets``. Issue #801 ("Define `klt pex`",
``src/klayout_tools/pex.py``) shipped the real command matching that
provisional shape exactly (`klt pex`'s own JSON schema is documented in
``docs/cli/pex.md``), so this recognition rule needed no change -- it now
recognises `klt pex`'s real, ratified output, not a stand-in for it.

## Fleet roll-up (issue #827, Phase 1c of epic #706)

:func:`build_fleet_report` is a third, additive entry point: instead of
grading one block manifest, it grades a **fleet manifest** -- a list of
per-block manifests (inline, or file paths to them) -- by calling
:func:`build_tier_report` once per block and reducing each block's full
item list down to two facts: its current tier, and, for any block not yet
at T1, the single T1 item still blocking it (the first unmet T1 item, in
the same doc order :func:`build_tier_report` renders). It never re-parses
or re-grades evidence itself -- every citation/reason a block's row reflects
was computed by :func:`build_tier_report`, so the two can never disagree
about *why* a block is or isn't T1. This turns "which canaries are at which
tier, and what's blocking each not-yet-T1 block" from a survey (open N
tier reports) into one query.

**Statistical/post-layout items flow through automatically (issue #872, Phase
2c of epic #706).** Because :func:`build_fleet_report` reduces whatever
``items[]`` :func:`build_tier_report` renders, and :func:`build_tier_report`
has always rendered *every* T1 item the doc lists (item 6 and item 7
included, since Phase 0 -- and item 11, since issue #2025, for free),
this needed no roll-up-side code change once #870/#871 taught
:func:`_classify`/:func:`_check_passed` to recognise ``klt yield``/``klt
pex`` evidence: a block's blocking item now resolves against those two
items' *real* verdicts, not just against whether they have any evidence at
all. Before #870/#871, a manifest citing genuine statistical/post-layout
evidence for items 6/7 rendered ``"unrecognized_envelope"`` -- so a canary
with that evidence already assembled was reported blocked "on statistical/
post-layout evidence" by the roll-up, indistinguishable from a canary with
no evidence for those items at all. See the fleet-roll-up regression tests
in ``tests/test_signoff.py`` (issue #872) for the walk-through, including one
that runs a real ``klt yield`` subprocess and shows a block's ``tier``
change from ``null`` to ``"T1"`` once that evidence is bound.

## Generic evidence ingestion (issue #1152)

Every kind through Phase 2c above is a `klt` verb's own output -- `_classify`
detects each one from a structural marker no other verb's shape carries
(``violations``/``mismatches``/``delta``+``reference_netlist``/etc.). That
closed set has no ingestion path for evidence that is not a `klt` JSON
envelope at all: a project's own hand-rolled Markdown characterization
record, or any other non-`klt`-native artifact. T1 item 8
("Characterization report", ``docs/design-evidence-tiers.md``) names no
specific `klt` verb -- unlike items 3-7, there is no command whose output
could ever satisfy it structurally -- so before this phase it could never be
machine-graded ``"met"`` by `klt signoff` at all.

This phase adds a seventh, deliberately **opt-in** kind, ``"generic"``: a
minimal envelope carrying ``schema_version``, an explicit, literal
``"kind": "generic"`` self-declaration (checked in :func:`_classify` ahead
of every structural check, so an incidental field collision with a native
shape can never misclassify it), and ``status: "pass"|"fail"`` -- graded by
:func:`_check_passed`/:func:`_detail` exactly like every other kind in
envelope-aggregation mode. A ``provenance`` block is optional, not required:
when a caller includes one (the same shared shape every native kind uses),
:func:`_provenance_consistency` and the ``--manifest`` staleness gate
(:func:`_grade_evidence`'s ``content_hash`` comparison) pick it up for free,
with no generic-specific code path -- exactly like `klt yield`'s envelope
would if it populated ``provenance.input.content_hash`` itself (see
"Statistical-evidence binding" above). Omitting it is a documented,
caller-visible caveat (see ``docs/cli/signoff.md``): an unprovenanced
generic citation can never satisfy a ``content_hash``-pinned evidence entry
(its ``actual_hash`` is always ``None``, so a pinned ``expected_hash`` never
matches -- renders :data:`_REASON_UNVERIFIABLE_PROVENANCE`, not
:data:`_REASON_STALE_EVIDENCE`, since nothing was ever recorded to compare
against -- issue #2182), and its freshness is otherwise un-checked, same as
it would be with no evidence at all.

**Item 8 only, not a global unlock.** Naively adding ``"generic"`` to
:func:`_classify`'s recognised set, with no further change, would let a
generic citation satisfy *any* unrestricted T1 item (1-6, 8-10 today) the
same way any other recognised, passing kind already can -- exactly the
false-positive risk this issue's own "Non-goals" section warns against:
`klt signoff` would then let a hand-rolled "yep, it's fine" JSON record
stand in for DRC/LVS/corner/Monte-Carlo/post-layout evidence it never
actually proved. :data:`_ITEMS_ACCEPTING_GENERIC_EVIDENCE` closes that gap
independently of the existing :data:`_ITEM_ALLOWED_KINDS` mechanism (item
7's ``pex``-only restriction): :func:`_build_tier_item` downgrades a
``"met"`` grading on a ``"generic"``-kind citation to ``"unmet"``/
``"wrong_kind"`` for every item *not* in that set, regardless of whether
``allowed_kinds`` would otherwise have accepted it. Today the set is
``{8}`` -- the one T1 item whose own checklist text names no `klt` verb.
Items 3-7 (DRC, LVS, corner verification, Monte Carlo, post-layout) reject a
``"generic"`` citation unconditionally, the same ``"wrong_kind"`` outcome
item 7 already renders for any non-``pex`` citation; this phase did not
touch items 3-6's separate, pre-existing permissiveness toward *other*
recognised native kinds (when it shipped, :data:`_ITEM_ALLOWED_KINDS` named
only item 7) -- closing that wider gap was out of *that* issue's scope, and
was done for items 3 and 4 by issue #1987 (they now accept only ``"drc"``
and ``"lvs"`` respectively -- see :data:`_ITEM_ALLOWED_KINDS`) and for
items 5, 6 and 8 by issue #2044 (see "Kind-restricting items 5, 6 and 8"
below -- item 8 is now ``"generic"``-only, so the two mechanisms agree on it
instead of one being a superset of the other).

## `klt power` (IR-drop/EM) evidence ingestion (issue #1321, Phase 2 of epic #712)

`klt power` (Epic #712 Phase 1, issues #844/#845/#846) emits a routed
design's resistive power-grid extraction, static IR-drop solve
(``worst_case_droop_mv``), and a per-net electromigration (EM)
current-density verdict (``em_verdict``) -- see ``docs/cli/power.md``.
Before this phase, `klt signoff`'s envelope-aggregation mode had no path
to consume that output at all: :func:`_classify` recognised the six native
`klt` verb shapes above plus the opt-in ``"generic"`` kind, but not `klt
power`'s -- so epic #712's own acceptance criterion 3 ("consumable by the
signoff aggregator as a signoff-completeness item") had no first-class
implementation.

This phase adds an eighth kind, ``"power"``, detected structurally like
every other native kind (a top-level ``power_nets`` list plus a
``networks`` list -- unique to this shape; it cannot collide with ``drc``'s
``violations``, ``lvs``'s ``mismatches``, ``sim``'s
``measurements``+``corner_count``, ``yield``'s
``measurements``+``measurement_count``+``source``, ``extract``'s
``device_count``+``nets``, or ``pex``'s ``delta``+``reference_netlist``).
Unlike every other kind, a `klt power` envelope carries no top-level
``status`` field and no ``provenance`` block at all (``docs/cli/power.md``'s
JSON schema) -- so :func:`_check_passed` derives a pass/fail verdict from
``em_verdict`` instead: ``"met"`` only when a static IR-drop solve actually
ran (``em_verdict`` is not ``None`` -- a spec declaring neither ``pads`` nor
a ``current_model`` produces no solve at all, per ``docs/cli/power.md``, and
proves nothing) *and* that solve's own EM verdict rolled up to exactly
``em_verdict["status"] == "pass"``. A rolled-up ``"fail"`` (a checked edge
exceeded its declared current-density limit), ``"not_checked"`` (nothing
in the whole spec had both a declared current limit and a solved current,
so nothing was actually verified), or ``"pass_partial"`` (issue #1997 --
at least one edge checked clean, but some other edge in the design was
never checked at all) does not pass, exactly like a missing
``em_verdict`` -- this module never infers a passing verdict from
``worst_case_droop_mv`` alone, since the envelope declares no droop
*limit* to compare it against (that binding is left to a future phase, per
issue #1321's own "Dependencies" note on the still-open activity-weighted-
current-model work). ``worst_case_droop_mv`` is still surfaced, verbatim,
in :func:`_detail` -- informational, not itself a pass/fail input.

**Still not a T1 checklist item.** ``docs/design-evidence-tiers.md``'s T1
checklist has no item for power-grid IR-drop/EM evidence -- unlike
``"generic"`` (which item 8 alone accepts), no T1 item names `klt power` at
all. Item 11 ("Power delivery (structural)", issue #2025 -- see below) did
*not* change this: the operator ruling that added it deliberately kept the
*analysis* question (how far does the supply droop, does any segment exceed
its EM limit) out of T1 and graded only the *structural* one. So a
``"power"``-kind citation is recognised by
:func:`build_signoff` (envelope-aggregation mode) but never satisfies any
:func:`build_tier_report`/:func:`build_fleet_report` item --
:data:`_ITEMS_ACCEPTING_POWER_EVIDENCE` is deliberately empty, mirroring
how :data:`_ITEMS_ACCEPTING_GENERIC_EVIDENCE` scopes ``"generic"`` to item
8 alone, just with an empty accept-set instead of ``{8}`` today.

## Digital-flow evidence: `sta` + `functional-verification` (issue #1959)

Every kind above is an artifact an *analog* (or full-custom digital) block
produces. A digital RTL-flow block's own evidence -- a multi-corner `klt
sta` characterization and a `klt functional-verification` cocotb regression
-- classified as nothing at all, so ``docs/design-evidence-tiers.md``'s
Digital column for items 5 and 7 named artifacts this grader could not read:
item 5 ("Full corner verification vs a ratified spec") rendered
``unrecognized_envelope`` on the block's real evidence, and item 7
("Post-layout verification") was globally restricted to ``"pex"``, an
analog/full-custom artifact an RTL/synthesis block has no way to produce.
**No digital RTL-flow block could reach T1.**

This phase adds the two kinds the Digital column actually names, and makes
item 7's restriction per-block-kind:

- ``"sta"`` (``docs/cli/sta.md``) -- detected structurally by a top-level
  ``geometry_source`` string (unique to this verb's response) plus either
  the flat single-corner shape's ``timing_status`` or the multi-corner
  shape's ``corners`` list. Unlike every other kind, a `klt sta` envelope's
  own ``status`` is *always* ``"ok"`` (the verb has no pass/fail concept of
  its own), so :func:`_check_passed` derives a verdict from the reported
  timing instead -- see :func:`_sta_timing_passed`.
- ``"functional-verification"`` (``docs/cli/functional-verification.md``) --
  detected by a top-level ``tests`` list plus ``test_count``; passes on its
  own ``status == "pass"`` with consistent per-test counts, at least one
  passing test and no failures (skips are allowed). It carries no shared
  ``provenance`` block at all (that verb's verdict depends on no PDK and no
  deck), so a ``content_hash``-pinned citation of one always renders
  ``"unmet"``/:data:`_REASON_UNVERIFIABLE_PROVENANCE` (issue #2182), the same
  documented caveat an unprovenanced ``"generic"`` envelope already has.

**Item 7's artifact, settled against the doc's own text.** The Digital
column of item 7 asks for "the functional test suite re-run against the
post-route gate-level netlist with back-annotated SDF timing" -- an SDF
gate-level re-simulation, *not* STA-with-parasitics. So item 7's digital
branch accepts a ``"functional-verification"`` citation **only** when that
run was actually SDF-annotated (``environment.sdf`` is an object with
``annotated: true`` -- ``docs/cli/functional-verification.md``'s "SDF
back-annotation" section: "an annotated run is identifiable from the JSON
alone"). An unannotated (zero-delay, pre-layout) regression is the right
kind of artifact but not post-layout evidence, so it renders
:data:`_REASON_NOT_POST_LAYOUT` -- deliberately distinct from
:data:`_REASON_WRONG_KIND`, per issue #826's rule that the report must say
precisely which thing is missing. A `klt sta` citation for item 7 is
``"wrong_kind"``: a SPEF-annotated timing run strengthens item 5, it is not
the functional re-simulation item 7 names.

**Corner scoping (the issue's finding #2) is the cited run's own declared
corner set.** A `klt sta` check passes only when *every* corner it reported
has ``timing_status == "constrained"`` (OpenSTA's unconstrained sentinel,
``1e+39``, is a positive number -- a naive ``worst_slack_ns >= 0`` rule
would otherwise call an untimed design closed) and non-negative setup/hold
slack. Which corners those are is decided by the request that produced the
envelope (`klt sta`'s own ``pdk.corners``), so the pass rule is scoped to
the corner set the claimant declared, never to an unrestricted sweep. This
module deliberately does **not** recognise `klt place-and-route`'s
``worst_setup_slack_ns``, whose corner set is the PDK's full shipped list
rather than a declared one.

**Item 5 stayed unrestricted** (``allowed_kinds is None``) through this
phase: it widened what is *recognised*, it did not tighten what item 5
accepts -- an analog block's item 5, and a full-custom digital block's `klt
sim` corner-matrix citation for it, graded exactly as they did before.
Issue #2044 later closed that permissiveness (see the next section), while
keeping both of those citations accepted. **Direction 3 of issue #1959 --
letting a ``"generic"`` citation satisfy items 5/7 for digital blocks -- is
deliberately not implemented**: it would weaken precisely the guarantee
:data:`_ITEMS_ACCEPTING_GENERIC_EVIDENCE` exists to preserve.

## Kind-restricting items 5, 6 and 8 (issue #2044)

Issue #1987 restricted items 3 and 4 to ``"drc"``/``"lvs"`` because a `klt
extract` citation -- which :func:`_check_passed` counts as passing
unconditionally, since `klt extract` has no independent pass/fail (it
either emits a ``status: "extracted"`` envelope or raises) -- could
otherwise stand in for a DRC/LVS check it never ran. That reasoning was
never specific to items 3 and 4: **items 5, 6 and 8 each name their
evidence in ``docs/design-evidence-tiers.md`` just as explicitly**, and
each was still unrestricted, so a bare `klt extract` envelope graded all
three ``"met"``:

- **Item 5** ("Full corner verification vs a ratified spec") names a PVT
  corner-matrix simulation for the Analog column (`klt sim`), and
  multi-corner STA plus a bit-exact functional regression for the Digital
  column -- "the machine-checkable evidence for the RTL-flow artifacts is a
  `klt sta` JSON report ... and a `klt functional-verification` JSON report"
  (issue #1959). The full-custom digital sub-case satisfies it "instead by
  PVT corner-matrix SPICE simulation", so ``"sim"`` stays accepted for a
  digital partition too.
- **Item 6** ("Statistical claims carry Monte Carlo evidence") names `klt
  yield` -- "a `klt yield` JSON report ... is the machine-checkable evidence
  for this item".
- **Item 8** ("Characterization report") names no verb, but it is not
  therefore evidence-free: the doc gives it a purpose-built substitute, the
  opt-in ``"generic"`` envelope (issue #1152, "``klt signoff --manifest``
  grades it via an opt-in generic evidence envelope"). So item 8 accepts
  ``"generic"`` and nothing else -- its previous willingness to also accept
  any passing native kind (a clean `klt drc` report satisfying a
  characterization claim) was the same borrowed-pass hole, one kind wider.

This is a **narrowing** of :data:`_ITEM_ALLOWED_KINDS` only. It does not
touch :func:`_check_passed` (``extract`` still passes unconditionally
there), does not drop ``extract`` from :func:`build_signoff`'s ``checks[]``,
and does not change its participation in :func:`_provenance_consistency` or
its device/net-count reporting -- exactly as issue #1987 left them. What
changes is only whether an `extract` citation (or any other wrong-kind one)
can satisfy a *numbered tier item*.

**Items 1, 2, 9 and 10 are deliberately left unrestricted.** Unlike items
3-8 they name no evidence at all -- ``docs/design-evidence-tiers.md`` says
so outright ("items **1**, **2**, **9**, and **10** have none ... Citing
them honestly is the claimant's responsibility, not something the tool
verifies"), and ``docs/cli/signoff.md``'s "Items 1, 2, 9, and 10" section
states the grading consequence plainly: any recognised, passing native kind
satisfies them, topical relevance included. Excluding ``extract`` there
specifically would be arbitrary -- a `klt drc` citation for item 9
("Testbenches shipped") is exactly as irrelevant and still counts by
design. Binding those four to real evidence needs an artifact for them to
bind *to*, which is a separate question from this one.

## Power delivery (structural): item 11 <- `klt erc` + `klt lvs` (+ P&R) (issue #2025)

Every item above is proved by **one** artifact. T1 item 11 ("Power delivery
(structural)"), added to ``docs/design-evidence-tiers.md`` by the operator
ruling on issue #2025, is the first that is not: it asks whether the supply
is actually connected to what it powers, and no single `klt` verb answers
that. So this phase adds three things:

- **Two new recognised kinds.** ``"erc"`` (``docs/cli/erc.md`` -- detected
  by a top-level ``gates`` list plus ``gate_role``; gradeable at all only
  since issue #1968 gave that verb a ``status`` and a ``provenance`` block)
  and ``"place-and-route"`` (``docs/cli/place-and-route.md`` -- detected by
  a ``stage_reached`` string plus the always-present ``power`` object).
  Both are **opt-in per item**, scoped to item 11 alone via
  :data:`_OPT_IN_KIND_ITEMS`, for exactly the reason ``"generic"`` is scoped
  to item 8: a `klt place-and-route` response passes
  :func:`_check_passed` on ``status == "ok"`` alone, so an unrestricted
  citation of one would reopen the "cannot fail, therefore always passes"
  hole issue #1987 closed for `klt extract`.
- **A compound evidence entry.** Item 11's manifest entry may be a *list* of
  ordinary evidence entries (each file- or command-backed, each resolved
  through the same :func:`_resolve_evidence` path every other item uses).
  See :func:`_normalize_evidence_parts`.
- **A dedicated grading path**, :func:`_grade_power_delivery`: the cited set
  must contain a `klt erc` supply-spec run and the LVS report item 4 grades,
  plus -- for an RTL-flow digital block -- the `klt place-and-route` response
  that says a PDN was built at all. Its six item-specific reasons
  (:data:`_REASON_NO_PDN`, :data:`_REASON_SUPPLY_SPEC_INCOMPLETE`,
  :data:`_REASON_SUPPLY_SPEC_DISCLOSED_UNEXPRESSIBLE`,
  :data:`_REASON_SUPPLY_SPEC_DISCLOSED_TOOL_LIMITATION`,
  :data:`_REASON_SUPPLY_NOT_CONTINUOUS`, :data:`_REASON_LVS_SUPPLY_UNPROVEN`)
  keep "no grid was ever built" distinguishable from "the grid is built but
  a rail is split in two", per issue #826's invariant.

Two deliberate scoping decisions, both from the ruling itself:

- **IR-drop and EM stay out.** `klt power`'s analysis verdict remains
  accepted by no T1 item (:data:`_ITEMS_ACCEPTING_POWER_EVIDENCE` is still
  empty). Item 11 is the *structural* question only.
- **Item 11 does not grade the ERC envelope's own ``status``.** A `klt erc`
  report is ``"clean"`` only when it has zero findings of *any* rule and no
  antenna violation anywhere; item 11 grades exactly the supply-continuity
  rules its checklist text names (see :func:`_erc_supply_findings`), so an
  antenna verdict on an unrelated signal net -- or the tie-cell false
  positives issue #1994 tracks -- cannot block a power-delivery claim.
  Envelope-aggregation mode still grades an ``erc`` check on ``status``.

One consequence worth stating plainly: ``t1_item_count`` is now 11 (22 for a
``mixed-signal`` block), so every existing T1 claim means something slightly
different than it did -- which is precisely why the doc required an operator
ruling before this item could be added (#1982).

## Critical-metric consumption (issue #1850)

Before this phase, every kind above derived ``passed`` purely from the
envelope's own ``status`` (or, for ``power``, ``em_verdict``) -- this module
had no mechanical way to reason about a *specific* metric's value.
:func:`_check_passed` now also reads ``envelope["metrics"]`` (the
declared, METRICS2.1-style registry from :mod:`klayout_tools.metrics`,
issue #247, adopted so far by ``klt drc``/``klt extract``/``klt sim`` --
issues #1847/#1848/#1849): any metric registered with ``critical=True``
whose value fails its own declared ``higher_is_better`` polarity (see
:func:`_critical_metric_blockers`) forces ``passed: False``, independent
of the envelope's own ``status``. This applies to every kind uniformly
(never hard-coded per-verb knowledge), and to both entry points --
:func:`build_signoff` calls :func:`_check_passed` directly, and
:func:`build_tier_report`/:func:`build_fleet_report` inherit it for free
through :func:`_grade_evidence`, which already delegates to the same
function. The offending metric(s), when any block a check, are named in
:func:`build_signoff`'s ``checks[].detail.critical_metric_blockers``.

## DRC coverage surfacing (issue #2002)

``docs/design-evidence-tiers.md`` item 3 requires a DRC claim to enumerate
the cited deck's coverage gaps -- and, since issue #1982, names the exact
fields that disclosure must quote: ``coverage.layers_in_stream_without_rules``,
``coverage.rules_skipped``, and ``coverage.deck_scope`` (``docs/cli/drc.md``).
This module read none of them: item 3 graded on ``status == "clean"``
alone, so a deck with twenty rule-free drawn layers and sixteen skipped
rules produced exactly the artifact a fully-covering deck did.

This phase is deliberately **report, not enforce**. :func:`_check_passed`
is unchanged -- a ``drc`` envelope still passes on ``status == "clean"``,
whatever its coverage block says, so no existing claim's verdict moves --
but the three fields are now surfaced verbatim, off the envelope the claim
actually cites, in every artifact a reviewer reads:

- :func:`build_signoff`'s ``checks[].detail.coverage`` (envelope-aggregation
  mode), alongside the existing ``file``/``deck``/``violation_count``.
- A ``"met"`` item's ``citation.coverage`` (tier-report mode, which is where
  item 3's verdict is produced) -- so the disclosure a claim owes and the
  numbers it owes it about sit in the same artifact.
- :func:`build_fleet_report`'s ``blocks[].drc_coverage`` -- the same
  reduction-not-re-grading discipline the roll-up already applies to
  ``blocking_item``: it reads the per-block tier report's own item-3
  citations and never re-opens an envelope.

See :func:`_drc_coverage_disclosure`. Purely additive everywhere: the key
is present only for a ``drc``-kind envelope that actually carries a
``coverage`` block, so evidence committed before ``coverage`` existed (and
a ``--fleet`` run mixing pre- and post-``coverage`` envelopes) renders
exactly as it did before -- no crash, and no fabricated "no gaps" claim for
an envelope that never reported any.

**What this does not do.** Surfacing is not enforcement: this module still
cannot tell a *disclosed* gap from an *undisclosed* one, because a claim
states its disclosure in a block README/manifest that `klt signoff` has no
field to compare against. Whether a non-empty
``layers_in_stream_without_rules`` should change item 3's verdict at all,
where a claimant would state the disclosure for it to be compared against,
and whether `klt lvs`'s own coverage-shaped disclosures (warnings-only
mismatches, ``power_connectivity: "unchecked"``) deserve the same treatment
for item 4, are all open questions issue #2002 left open on purpose -- not
answered here.

## Device-body bias surfacing (issue #1983)

``docs/cli/extract.md`` states that a device body left on an anonymous,
deck-synthesized net has **no DC bias path at all**, which makes a
resimulation of that extracted netlist "physically wrong, not merely
imprecise" -- the run converges and produces numbers, and those numbers are
not comparable to a schematic-level netlist's. `klt pex` *is* such a
resimulation, and it is the artifact ``docs/design-evidence-tiers.md`` item
7 (post-layout verification -- the item with the strictest citation rule in
the checklist) is cited from. So an item-7 citation could be backed by
numbers that look like measurements and are not, with nothing anywhere in
the signoff artifact saying so. Symmetrically, `klt lvs` reported its own
body-tie coverage gap only as a ``device.body_unverified``
``mismatches[]`` warning -- an entry this module never read, and one that
never changed that envelope's ``status``.

Both sides now state it in a field, and this module quotes both:

- ``checks[].detail.body_verification_status`` -- `klt lvs`'s
  ``body_verification.status`` (``"verified"``/``"unverified"``/
  ``"unchecked"``), beside the existing ``power_connectivity_status``.
- ``checks[].detail.body_bias`` and a ``"met"`` item's
  ``citation.body_bias`` -- `klt pex`'s ``body_bias`` block reduced to its
  verdict and counts (see :func:`_pex_body_bias_disclosure`). The citation
  form is the load-bearing one: item 7's verdict and the property that can
  invalidate the numbers backing it now sit in the same artifact.

**Report, not enforce**, exactly like the DRC coverage surfacing above.
:func:`_check_passed` is untouched: a `pex` envelope still passes on
``status == "pass"`` whatever its ``body_bias`` says, and an `lvs` envelope
still passes on ``status == "match"`` (plus the #1965 power gate) whatever
its ``body_verification`` says -- so no existing claim's verdict moves. Two
reasons this is the right default here and hard-fail was the right default
for ``power_connectivity`` (#1965): a power-connectivity ``"mismatch"`` is
a *defect* (a real miswire, always wrong), whereas an unverified/unbiased
body is a *coverage* condition that some PDK decks produce on every layout
they extract regardless of what the designer drew -- hard-failing it would
retroactively fail whole PDKs' worth of otherwise-valid evidence on a
question this module cannot itself adjudicate. And the original friction was
specifically that the condition was *invisible*: "an item whose evidence can
be silently invalid is worse than an item with no evidence, because the
second is visible" (issue #1983). Making it visible is the fix; deciding
what it should cost a claim is a policy question left to the reader of the
evidence, and ``docs/design-evidence-tiers.md`` item 7 now states that
condition explicitly rather than implying a `pex` citation is
self-validating.

## Vacuous-verdict refusal (issue #1996)

Several verbs can reach a passing top-level verdict on a run that checked
**nothing**: a PDK-native DRC deck whose whole rule set is gated behind a
``--deck-var`` the caller never set (a well-formed, empty report --
``status: "clean"``), a `klt sim` request whose PVT corner matrix expanded
to zero corners (``status: "pass"``, every counter ``0``), a `klt pex` run
that produced no ``delta[]`` row to compare at all. Each of those is a
*vacuously* true verdict, and this module used to grade them exactly like
an earned one -- ``status == "clean"``/``"pass"`` was the whole test.

Those verbs now say so in a field: the shared
``coverage.nothing_checked``/``nothing_checked_reasons`` convention declared
in :mod:`klayout_tools.coverage`, which this module reads through that
module's :func:`~klayout_tools.coverage.coverage_nothing_checked` helper
(never by reaching into the dict, so "no ``coverage`` block at all" reads
uniformly as *makes no coverage statement* -- ``False`` -- rather than as
either a gap or a guarantee). Evidence committed before the convention
existed therefore grades byte-identically to before.

Unlike the two surfacing phases above, this one **enforces** rather than
merely reports, and the asymmetry is deliberate. A coverage *gap*
(``rules_skipped``, an unbiased device body) is a partial result whose cost
to a claim is a judgement this module cannot make; ``nothing_checked`` is
the total case -- the cited artifact contains no statement about the design
whatsoever, so there is nothing for a reviewer to weigh. Letting it back a
``"met"`` item would mean an empty report is indistinguishable from a real
one, which is precisely the gap issue #1996 exists to close. So:

- :func:`_grade_evidence` renders such a citation ``"unmet"`` with
  :data:`_REASON_NOTHING_CHECKED` -- grouped with ``wrong_kind`` /
  ``not_post_layout`` as a "no runnable check proves this item" reason
  (the cited check did not *fail* on its own terms; it simply measured
  nothing), per issue #826's invariant that the two must stay
  distinguishable.
- :func:`build_signoff`'s envelope-aggregation mode forces that check's
  ``passed`` to ``False`` and names the reasons in
  ``checks[].detail.nothing_checked_reasons``, so the two entry points
  cannot disagree about whether an empty report is evidence.

:func:`_check_passed` itself is untouched -- it still answers "what does
this envelope's own verdict say", which is what its other callers ask it --
so the refusal is applied beside it, once per entry point, by
:func:`_nothing_checked_reasons`.

## Partial-coverage qualification (issue #2109)

The case between the two above: a run that passed every check it ran **and**
skipped requested work. :mod:`klayout_tools.coverage`'s common rollup rule
calls that ``partial`` -- a real, exit-0 result that is neither the verb's
unconditional success nor complete signoff evidence -- and this module
applies it in two places, neither of which re-derives it:

- A producer that has adopted the rollup reports its partial status token
  (``"clean_partial"``, ``"pass_partial"``, :data:`_PARTIAL_STATUS_BY_KIND`).
  :func:`_check_passed` already declines it, since it is not the kind's
  success token; what :func:`_non_passing_reason` adds is that the item says
  :data:`_REASON_PARTIAL_COVERAGE` instead of :data:`_REASON_CHECK_FAILED`,
  so a reader is sent to the skipped work rather than to a violation that
  does not exist. Producer-side failure precedence is structural: a run that
  found a defect reports its *failure* token, because
  :func:`~klayout_tools.coverage.coverage_rollup` decides ``failed`` before
  it consults coverage at all. Signoff-side failure precedence is not, and
  is ordered explicitly (issue #2152): a :func:`_critical_metric_blockers`
  hit (#1850) is decided in this module, can co-occur with a partial token
  on one envelope, and outranks it.
- Until a verb's Phase 2 adapter lands (#2110/#2111/#2116/#2117/#2118 --
  ``erc``'s own adapter, #2115, now reports ``"clean_partial"``), its status
  can still be the unconditional success word on a run whose common
  coverage says ``partial``. That verdict is unchanged here -- item
  3's grading rule is ``docs/design-evidence-tiers.md``'s to set, and it
  deliberately leaves the weighing of a coverage gap to the claimant -- but
  :func:`_partial_coverage_disclosure` now states the gap on both the
  ``checks[]`` and ``"met"``-citation paths, so it is visible in the report
  rather than only in the source envelope.

Unlike ``nothing_checked``, partial is **not** a hard refusal: the run did
check something, and discarding it would throw away a real result rather
than qualify it. What no path here does is read it as complete --
:func:`~klayout_tools.coverage.coverage_refusal_reason` (no verdict reached)
and :func:`~klayout_tools.coverage.coverage_qualification_reason` (not an
unconditional success) are separate questions for exactly that reason.

## `klt erc`'s second coverage scope (issue #2179)

A `klt erc` envelope answers two independent questions -- an antenna-ratio
one that needs a PDK limit table, and a connectivity/geometry one that needs
none -- and since #2179 it carries a checked-work scope for each:
``coverage`` (antenna) and ``erc_coverage`` (connectivity), with
``erc_status`` as the latter's own roll-up (``docs/cli/erc.md``).

That matters here because `klt erc` ships an antenna-ratio table for sky130
only. On any other PDK -- or any run that omits ``--pdk`` -- *every* antenna
level is ungraded for *every* layout, so the antenna scope is known-zero,
``status`` is ``"not_checked"``, and both gates above fire: the vacuous-
verdict refusal on the ``coverage`` block, and :func:`_check_passed`
declining a non-``"clean"`` status. The result was that a connectivity-clean
run and a run that checked nothing at all were indistinguishable to this
module, so the structural supply read `klt erc` *can* produce on such a PDK
could never back a citation.

Two narrow widenings, both keyed on the connectivity scope actually being
present and having reached a verdict of its own
(:func:`_erc_connectivity_state`), so an envelope predating #2179 grades
byte-identically to before:

- :func:`_erc_passed` lets ``status == "not_checked"`` pass when
  ``erc_status == "clean"``. An antenna *violation* still reports
  ``"violations"`` and still fails; nothing here weakens the antenna claim,
  it only stops a permanently-unanswerable antenna question from voiding a
  separately-answered connectivity one.
- :func:`_coverage_refusal` reads both scopes when deciding whether the
  artifact states anything at all, and is applied at *both* entry points
  (:func:`_build_check`, :func:`_kind_gated_refusal`) so aggregation mode
  and numbered-citation grading cannot disagree about it.

T1 item 11 is untouched by all of this: it never graded the ERC envelope's
``status`` in the first place (:func:`_grade_power_delivery` reads the
supply rules directly, see :func:`_erc_supply_findings`), which is why it
already worked on a table-less PDK.

## Typed, runtime-validated evidence ingestion (issue #2033)

``CLAUDE.md`` says "JSON is the contract", but every producer/consumer
boundary in this repo was typed ``dict[str, Any]`` -- a shape no checker and
no runtime read could enforce. This module's :func:`_classify` is the pilot
boundary for changing that (issue #2033, decomposed from #2011 item 2), for
a concrete reason: it is where an envelope that "cannot fail" has already
cost real verdicts (#1987/#1988).

Two halves, deliberately separate:

1. **A typed shape per recognised kind** -- one ``TypedDict`` per
   :func:`_classify` kind (:data:`_ENVELOPE_SHAPES`), declaring both the
   fields that kind's envelope must carry and the optional ones the
   boundary reads. :func:`_build_check`/:func:`_check_passed`/:func:`_detail`
   take the resulting union (``_EvidenceEnvelope``) rather than
   ``dict[str, Any]``. Those declarations live in ``signoff_envelopes.py``
   since issue #2313 and are re-imported here by name, so every
   ``signoff.<shape>`` reference still resolves from this module.
2. **Runtime validation at the read boundary** -- a ``TypedDict`` is erased
   at runtime, so the declaration alone would check nothing about an
   envelope read off disk. :func:`_validate_envelope` therefore re-reads the
   *same* declarations (``__required_keys__`` plus the resolved
   annotations, so the two can never drift) against the actual JSON, and
   rejects an envelope that matches a kind's discriminating shape but is
   missing a required field or carries one of the wrong type.

The rejection routes exactly like every other unreadable-evidence case
already does: :func:`build_signoff` raises :class:`SignoffError` (clean
stderr, exit 1), and ``--manifest`` grading renders the citing item
``"unmet"`` with ``reason: "unrecognized_envelope"`` -- never a silent
``"met"``. The motivating case is ``extract``, the one kind
:func:`_check_passed` passes *unconditionally*: before this, a truncated
`klt extract` envelope with no ``status`` at all still produced a passing
check.

**What this does not catch**, stated plainly because the parent issue asks
for it: typed envelopes are not a false-pass cure. This catches malformed
and incomplete envelopes. It cannot establish that meaningful work happened
upstream, and it cannot catch a *semantic* mismatch between two
well-formed values -- e.g. #1999's escaped-identifier defect, where one
parser kept a leading backslash another stripped: both strings are valid
``str`` and both satisfy every shape declared here. Expansion of this
pattern to other verbs' boundaries is deliberately deferred until this
pilot's diagnostics have been seen in practice (#2011).

## Input-artifact verification (issue #2196)

``--manifest``'s freshness gate compares the manifest's pinned
``content_hash`` against the *cited envelope's own self-reported*
``provenance.input.content_hash``. Both sides of that comparison are
statements **about** a revision; neither is the revision. A manifest and an
envelope can go on agreeing with each other indefinitely while the GDS,
netlist or record they describe is rewritten underneath them::

    manifest.content_hash == envelope.provenance.input.content_hash   <- gated
    envelope.provenance.input.content_hash == sha256(<the artifact>)  <- #2196

:func:`_verify_input_artifact` closes the second line: it re-hashes the
input artifact the envelope itself names
(:data:`_INPUT_ARTIFACT_FIELDS` -- ``klt drc``'s ``file``, ``klt lvs``'s
``layout``, ``klt pex``'s ``layout``, ...) with the same
:func:`~klayout_tools._provenance.sha256_file` the producing run used, and
every ``"met"`` citation carries the answer as ``input_verified``:

- ``True`` -- the artifact was found and re-hashed, and it matches the hash
  the envelope recorded. The freshness claim is anchored to a file.
- ``False`` -- the artifact was found and re-hashed, and it **disagrees**:
  the envelope's self-report no longer describes what is on disk.
- ``None`` -- nothing was re-hashed, so the pinned hash was only ever
  compared to another claim. Either the envelope records no input hash, its
  kind names no input path this module can resolve, or the path it names
  does not resolve to a readable file from the grading context (an absolute
  scratch path, a request-file-relative path graded from elsewhere).

**Disclosure, not grading** -- deliberately, and matching the precedent set
by ``coverage`` (#2002) and ``body_bias`` (#1983): ``input_verified`` is
never consulted by any grading rule, so no item's ``met``/``unmet`` verdict
moves because of it. What changes is that the unverified case is now
*visible*: a reviewer can tell a freshness claim checked against a file
from one checked only against another claim, which was previously
indistinguishable. The field is always present on a citation (``null``
included) for exactly that reason -- an omitted key would leave the silent
case silent. A verdict-changing remedy (a distinct ``input_changed``
reason) is a deliberate follow-up, not smuggled in here.

Pure library: :func:`build_signoff`, :func:`build_tier_report`, and
:func:`build_fleet_report` all return plain Python data (a ``dict`` of
JSON-serialisable primitives) and never print, mirroring ``report.py``.
Serialisation and console printing live in the CLI command module
(``cli/signoff_cmd.py``).

This module is a **consumer** of the shared JSON envelope
(``docs/json-contract.md``), like ``report.py`` -- it never changes any
verb's own JSON output, it only reads it. An envelope's *kind* is detected
from the structural shape of its own fields (see :func:`_classify`), the
same convention ``report.py`` uses, extended here to also recognise
``klt extract``'s and ``klt sim``'s shapes (``report.py`` does not, since
neither is a findings-list/key-metrics report in its sense).
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import (
    Any,
    NoReturn,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from ._provenance import INPUT_ROLE_LAYOUT, sha256_file
from ._report_verify import build_rerun_result, load_committed_report
from .build_identity import version_report
from .coverage import (
    coverage_nothing_checked,
    coverage_nothing_checked_reasons,
    coverage_refusal_reason,
    coverage_skipped_work,
    coverage_state,
    coverage_validation_error,
)
from .design_evidence_tiers import (
    CANONICAL_DOC_LABEL,
    DEFAULT_DOC_PATH,
    DesignEvidenceTiersError,
    doc_source_label,
    parse_tier_doc,
)
from .env_provenance import find_repo_root
from .metrics import get_metric, is_registered

# The typed envelope shapes this boundary validates against (issue #2033) live
# in their own module since issue #2313 -- ~300 lines of pure `TypedDict`
# declarations with no runtime logic, following the same split as
# `extract_report.py`. Every moved name is re-imported here *by name* so
# `signoff.<name>` keeps resolving exactly as it did before the split; the
# `X as X` form marks the ones with no call site left in this module as
# deliberate re-exports rather than dead imports.
from .signoff_envelopes import (
    _ENVELOPE_SHAPES,
    _SCALAR_CHECKS,
    _UNION_ORIGINS,
    _EvidenceEnvelope,
)
from .signoff_envelopes import _DrcEnvelope as _DrcEnvelope
from .signoff_envelopes import _DrcRequired as _DrcRequired
from .signoff_envelopes import _EnvelopeCommon as _EnvelopeCommon
from .signoff_envelopes import _ErcEnvelope as _ErcEnvelope
from .signoff_envelopes import _ErcRequired as _ErcRequired
from .signoff_envelopes import _ErrorEnvelope as _ErrorEnvelope
from .signoff_envelopes import _ErrorRequired as _ErrorRequired
from .signoff_envelopes import _ExtractEnvelope as _ExtractEnvelope
from .signoff_envelopes import _ExtractRequired as _ExtractRequired
from .signoff_envelopes import (
    _FunctionalVerificationEnvelope as _FunctionalVerificationEnvelope,
)
from .signoff_envelopes import (
    _FunctionalVerificationRequired as _FunctionalVerificationRequired,
)
from .signoff_envelopes import _GenericEnvelope as _GenericEnvelope
from .signoff_envelopes import _GenericRequired as _GenericRequired
from .signoff_envelopes import _LvsEnvelope as _LvsEnvelope
from .signoff_envelopes import _LvsRequired as _LvsRequired
from .signoff_envelopes import _PexEnvelope as _PexEnvelope
from .signoff_envelopes import _PexRequired as _PexRequired
from .signoff_envelopes import _PlaceAndRouteEnvelope as _PlaceAndRouteEnvelope
from .signoff_envelopes import _PlaceAndRouteRequired as _PlaceAndRouteRequired
from .signoff_envelopes import _PowerEnvelope as _PowerEnvelope
from .signoff_envelopes import _PowerRequired as _PowerRequired
from .signoff_envelopes import _SimEnvelope as _SimEnvelope
from .signoff_envelopes import _SimRequired as _SimRequired
from .signoff_envelopes import _StaEnvelope as _StaEnvelope
from .signoff_envelopes import _StaRequired as _StaRequired
from .signoff_envelopes import _YieldEnvelope as _YieldEnvelope
from .signoff_envelopes import _YieldRequired as _YieldRequired

__all__ = [
    "SignoffError",
    "DesignEvidenceTiersError",
    "build_signoff",
    "build_tier_report",
    "build_fleet_report",
    "check_tier_report",
    "check_fleet_report",
    "describe_grader",
]

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md. This is *this* command's own
#: envelope version, independent of the schema_version of any envelope it
#: aggregates.
SCHEMA_VERSION = 1

#: Schema version for :func:`build_tier_report`'s own JSON shape -- distinct
#: from :data:`SCHEMA_VERSION` (the envelope-aggregation mode's shape)
#: because the two modes' top-level fields are unrelated; bumping one must
#: never imply the other changed.
#:
#: Still ``1`` after issue #2176 (``build``, ``items[].graded_by_build``, the
#: ``ungradeable_by_build`` reason), issue #2202
#: (``build_t1_item_count``), and issue #2216 (``build.grading_ruleset_id``),
#: deliberately: every one of those fields is a *new* key, and a new
#: ``reason`` value is a value set growing within an unchanged shape -- both
#: explicitly additive under ``docs/json-contract.md`` ("adding new fields
#: does not" bump; "an additive change can introduce a new enum-like
#: value"). Issue #2202's field has no behaviour change behind it at all: it
#: reports *beside* ``t1_item_count``, and never reinterprets it. Issue
#: #2216's field is the same shape: a new key inside the already-additive
#: ``build`` block, reporting *beside* its existing fields rather than
#: redefining any of them. The grading change that rode with #2176 refuses a
#: citation this build has no rules for, which is a *correction* of a
#: verdict that was wrong, not a redefinition of what ``status``/``reason``
#: mean -- the same shape as issue #1987's (item 3 stopped accepting `klt
#: extract` citations) and issue #2044's (items 5/6/8 gained kind
#: restrictions) grading corrections, neither of which bumped this constant.
#: Contrast :data:`FLEET_REPORT_SCHEMA_VERSION` below, which bumped for issue #2178
#: because an already-shipped field's *meaning* changed there.
TIER_REPORT_SCHEMA_VERSION = 1

#: Schema version for :func:`build_fleet_report`'s own JSON shape -- distinct
#: from both :data:`SCHEMA_VERSION` and :data:`TIER_REPORT_SCHEMA_VERSION`
#: for the same reason: the three modes' top-level fields are unrelated, so
#: bumping one must never imply either of the others changed.
#:
#: ``2`` (issue #2178): ``blocks[].blocking_item`` no longer means "the first
#: unmet T1 item in render order". It now skips the structurally-ungradeable
#: items 1, 2, 9 and 10 whenever a gradeable T1 item is also unmet (see
#: :func:`_blocking_t1_item`), so the same fleet manifest that reported
#: ``blocking_item.id == 1`` under version ``1`` reports a different item
#: under ``2``. Adding the new ``blocks[].ungraded_items`` field alongside it
#: would have been purely additive on its own, but redefining what an
#: already-shipped field's unchanged type *means* is a breaking change under
#: ``docs/json-contract.md``'s "value sets within an unchanged shape" rule
#: (the `klt precheck` ``layer_whitelist[].shapes`` precedent, issue #452),
#: so this bumps rather than landing silently.
#:
#: ``3`` (issue #2203): ``blocks[].blocking_item`` moves again, for exactly
#: the same reason and so under exactly the same rule. An unmet item this
#: build has no grading rules for (``graded_by_build: False``, issue #2176)
#: is now *preferred* as the blocker over every other unmet item, where
#: version ``2`` demoted it below any gradeable one -- it satisfied
#: :func:`_is_structurally_ungradeable_item` incidentally, so #2178's skip
#: swept it up. A fleet manifest graded against a ``--tiers-doc`` newer than
#: the running build therefore reports a different ``blocking_item.id``
#: under ``3`` than under ``2`` (see :func:`_blocking_t1_item` for the rule
#: and the rationale). That case is reachable in practice -- it is the case
#: issue #2176 exists for -- so this is a real observable move, not a
#: speculative bump. Nothing else changes shape: ``ungraded_items`` lists
#: exactly the same rows it did under ``2``, and no field is added, removed
#: or retyped.
FLEET_REPORT_SCHEMA_VERSION = 3

#: Schema version for :func:`describe_grader`'s own JSON shape (issue
#: #2216) -- distinct from the other three for the same reason they are
#: distinct from each other: this mode's top-level fields are unrelated to
#: envelope-aggregation, tier-report, and fleet-roll-up mode, so bumping one
#: must never imply another changed.
DESCRIBE_GRADER_SCHEMA_VERSION = 1

#: Block kinds recognised by ``docs/design-evidence-tiers.md``'s "Block
#: kind" subsection -- the manifest's ``kind`` field must be one of these.
_BLOCK_KINDS = ("analog", "digital", "mixed-signal")

#: The one block kind that grades more than one partition, and therefore the
#: only kind a ``partition_boundary`` declaration (issue #2278) is meaningful
#: for -- an ``analog``/``digital`` block has a single partition, so it has no
#: boundary to state.
_PARTITIONED_BLOCK_KIND = "mixed-signal"

#: The partition names a ``"mixed-signal"`` manifest may declare a boundary
#: for (issue #2278) -- the same two names :func:`build_tier_report` renders
#: rows for and :func:`_lookup_evidence` accepts as an evidence key suffix.
_MIXED_SIGNAL_PARTITIONS = ("analog", "digital")

#: Per-T1-item-id, **per-block-kind** restriction on which :func:`_classify`
#: kinds may satisfy that item (issue #871, Phase 2b of epic #706; made
#: per-block-kind by issue #1959) -- resolved by :func:`_allowed_kinds_for`
#: and passed as :func:`_build_tier_item`'s ``allowed_kinds`` parameter. An
#: item id absent from this map (items 1, 2, 9 and 10, today) is
#: unrestricted (``None``), preserving the original Phase 0/1 behaviour where
#: any recognised, passing envelope kind satisfies any item.
#:
#: **Every T1 item that names evidence is restricted here** (issue #2044):
#: items 3-8. The four absent ids are exactly the four
#: ``docs/design-evidence-tiers.md`` documents as having no tool behind them
#: at all ("items **1**, **2**, **9**, and **10** have none ... Citing them
#: honestly is the claimant's responsibility, not something the tool
#: verifies"), so leaving them unrestricted is the documented behaviour, not
#: an oversight -- there is no verb to bind them to, and singling out one
#: irrelevant kind for them would be arbitrary when every other irrelevant
#: kind still satisfies them by design.
#:
#: The inner map is keyed by the **partition kind** being graded
#: (``"analog"``/``"digital"`` -- a ``"mixed-signal"`` manifest grades both,
#: one per partition, so that value never appears here) and must name every
#: partition kind, since :func:`_allowed_kinds_for` falls back to the
#: strictest (analog) set rather than silently becoming unrestricted.
#: Items 3, 4, 6 and 8 name the same set for both partition kinds (a DRC
#: clean is a DRC clean whichever flow drew the block, and items 6 and 8 are
#: kind-independent in the doc's own checklist); items 5 and 7 are per-kind
#: checklist items whose two columns name genuinely different artifacts:
#:
#: - **item 5, analog** (and a mixed-signal block's analog partition): a
#:   ``"sim"``-kind citation only -- the doc's "PVT corner-matrix simulation
#:   results covering every spec row at its bound corners".
#: - **item 5, digital**: ``"sta"`` or ``"functional-verification"`` (the
#:   doc's Digital column, "multi-corner static timing analysis ... plus a
#:   bit-exact functional test suite", recognised as first-class evidence
#:   kinds by issue #1959) *or* ``"sim"``, which keeps the full-custom
#:   digital sub-case working: that partition declares ``kind: "digital"``
#:   but satisfies item 5 "instead by PVT corner-matrix SPICE simulation",
#:   i.e. exactly an analog partition's artifact.
#: - **item 7, analog** (and a mixed-signal block's analog partition): a
#:   ``"pex"``-kind citation only -- the `klt pex`
#:   schematic-vs-extracted-netlist delta report (see this module's
#:   "Post-layout binding" docstring section). Unchanged by issue #1959.
#: - **item 7, digital**: ``"pex"`` *or* ``"functional-verification"``.
#:   ``"pex"`` keeps the full-custom digital sub-case working unchanged (it
#:   declares ``kind: "digital"`` and produces exactly an analog block's
#:   post-layout artifact -- ``docs/design-evidence-tiers.md``'s "Full-custom
#:   digital sub-case"); ``"functional-verification"`` is the RTL-flow
#:   artifact the doc's own Digital column names, and is additionally
#:   required to be SDF-annotated (see
#:   :data:`_ITEMS_REQUIRING_POST_LAYOUT_EVIDENCE`).
_ITEM_ALLOWED_KINDS: dict[int, dict[str, set[str]]] = {
    # Issue #1987: items 3 ("DRC clean") and 4 ("LVS clean") name their
    # verb in docs/design-evidence-tiers.md, so only that verb's own
    # envelope may satisfy them -- in particular never a `klt extract`
    # report, which `_check_passed` counts as passing unconditionally.
    3: {"analog": {"drc"}, "digital": {"drc"}},
    4: {"analog": {"lvs"}, "digital": {"lvs"}},
    # Issue #2044: items 5, 6 and 8 name their evidence in the doc just as
    # explicitly as items 3/4/7 do, so they get the same treatment -- see
    # this module's "Kind-restricting items 5, 6 and 8" docstring section.
    5: {
        "analog": {"sim"},
        "digital": {"sta", "functional-verification", "sim"},
    },
    6: {"analog": {"yield"}, "digital": {"yield"}},
    7: {
        "analog": {"pex"},
        "digital": {"pex", "functional-verification"},
    },
    # Item 8's only machine-checkable evidence is the purpose-built generic
    # envelope (issue #1152). Naming it here is a *narrowing* of item 8's
    # native-kind permissiveness, not a second gate on "generic" itself --
    # `_ITEMS_ACCEPTING_GENERIC_EVIDENCE` still decides which items a
    # "generic" citation may satisfy, and the two agree on item 8 rather
    # than double-restricting it.
    8: {"analog": {"generic"}, "digital": {"generic"}},
}

#: T1 item ids whose evidence must prove a **post-layout** run, not merely a
#: passing check of an accepted kind (issue #1959). Checked in
#: :func:`_grade_evidence` via :func:`_is_post_layout_evidence`: a ``"pex"``
#: report is post-layout by construction (it re-simulates an extracted
#: netlist), but a ``"functional-verification"`` regression is only
#: post-layout evidence when it ran with back-annotated SDF timing --
#: otherwise it is the pre-layout zero-delay run item 7's own checklist text
#: explicitly excludes ("not only the pre-layout RTL/gate simulation"), and
#: renders :data:`_REASON_NOT_POST_LAYOUT`.
_ITEMS_REQUIRING_POST_LAYOUT_EVIDENCE: frozenset[int] = frozenset({7})

#: T1 item ids whose own checklist text in ``docs/design-evidence-tiers.md``
#: names no specific `klt` verb (issue #1152) -- the only items a
#: ``"generic"``-kind citation (see this module's "Generic evidence
#: ingestion" docstring section) may satisfy. Checked independently of
#: :data:`_ITEM_ALLOWED_KINDS` in :func:`_build_tier_item`: a passing
#: ``"generic"`` citation for any item *not* in this set is always
#: downgraded to ``"unmet"``/``"wrong_kind"``, even for an item that would
#: otherwise accept any recognised native kind (``allowed_kinds is None``).
#: Today this is only item 8 ("Characterization report") -- every other
#: item's checklist text names a concrete `klt` verb (`klt
#: drc`/`lvs`/sim/yield/pex) or a repo-state artifact no envelope, native or
#: generic, could ever stand in for (items 1, 2, 9, 10), so none of them
#: opt in.
_ITEMS_ACCEPTING_GENERIC_EVIDENCE: frozenset[int] = frozenset({8})

#: T1 item ids a ``"power"``-kind citation (issue #1321, see this module's
#: "`klt power` (IR-drop/EM) evidence ingestion" docstring section) may
#: satisfy in :func:`build_tier_report`/:func:`build_fleet_report`.
#: Deliberately **empty**: ``docs/design-evidence-tiers.md``'s T1 checklist
#: has no item for power-grid IR-drop/EM evidence at all today, unlike
#: ``"generic"`` (scoped to item 8 via :data:`_ITEMS_ACCEPTING_GENERIC_EVIDENCE`
#: above) -- so a ``"power"`` citation is recognised by :func:`build_signoff`
#: (envelope-aggregation mode) but is never accepted as ``"met"`` evidence
#: for any tier-report item, even one with ``allowed_kinds is None``. Checked
#: the same way :data:`_ITEMS_ACCEPTING_GENERIC_EVIDENCE` is, in
#: :func:`_build_tier_item`.
_ITEMS_ACCEPTING_POWER_EVIDENCE: frozenset[int] = frozenset()

#: T1 item ids an ``"erc"``-kind citation (issue #2025) may satisfy: item 11
#: ("Power delivery (structural)") alone -- the only item whose checklist
#: text names `klt erc` at all. Scoped exactly like ``"generic"`` above, so
#: an ERC report can never borrow a pass for one of the six otherwise-
#: unrestricted items (1, 2, 5, 6, 9, 10): it proves supply continuity and
#: antenna ratios, not design sources, corner coverage, or repo hygiene.
_ITEMS_ACCEPTING_ERC_EVIDENCE: frozenset[int] = frozenset({11})

#: T1 item ids a ``"place-and-route"``-kind citation (issue #2025) may
#: satisfy: item 11 alone, for the same reason. This one matters more than
#: most -- :func:`_check_passed` counts a `klt place-and-route` response as
#: passing on ``status == "ok"`` alone (a run that completed, negative slack
#: and all), so an unrestricted citation of one would reopen exactly the
#: "cannot fail, therefore always passes" hole issue #1987 closed for `klt
#: extract` on items 3 and 4.
_ITEMS_ACCEPTING_PLACE_AND_ROUTE_EVIDENCE: frozenset[int] = frozenset({11})

#: Every **opt-in** kind, mapped to the T1 item ids that accept it -- the
#: single table :func:`_build_tier_item` consults, so a new opt-in kind is
#: one entry here rather than a fourth near-identical branch there. A kind
#: absent from this table is *native and unrestricted*: it is governed only
#: by :data:`_ITEM_ALLOWED_KINDS` (which restricts specific items to
#: specific kinds), exactly as before this table existed. A kind present
#: here is rejected -- a ``"met"`` grading downgraded to
#: ``"unmet"``/:data:`_REASON_WRONG_KIND` -- for every item id outside its
#: own set, *regardless* of whether that item's ``allowed_kinds`` is
#: ``None``.
_OPT_IN_KIND_ITEMS: dict[str, frozenset[int]] = {
    "generic": _ITEMS_ACCEPTING_GENERIC_EVIDENCE,
    "power": _ITEMS_ACCEPTING_POWER_EVIDENCE,
    "erc": _ITEMS_ACCEPTING_ERC_EVIDENCE,
    "place-and-route": _ITEMS_ACCEPTING_PLACE_AND_ROUTE_EVIDENCE,
}

#: T1 item ids graded by the dedicated **compound** evidence path
#: (:func:`_grade_power_delivery`) instead of the one-envelope-one-item
#: :func:`_build_tier_item` path every other item uses -- item 11 ("Power
#: delivery (structural)", issue #2025) alone.
#:
#: Item 11 is the first T1 item no single artifact can prove. Its checklist
#: text names a `klt erc` supply run **plus** an LVS report, plus -- for an
#: RTL-flow digital partition -- the `klt place-and-route` response that
#: says a PDN was built at all. So its manifest entry may be a **list** of
#: ordinary evidence entries (each file- or command-backed, each resolved by
#: the same machinery every other item uses), and the item is ``"met"`` only
#: when the cited set jointly proves every condition the item names.
_ITEMS_GRADED_AS_POWER_DELIVERY: frozenset[int] = frozenset({11})


def _is_structurally_ungradeable_item(item_id: int) -> bool:
    """Is this T1 item one of the ones with **no `klt` verb behind it at
    all** -- a claim about what a repository *contains* rather than about a
    check that can be run (issue #2178)?

    Today that is exactly items 1 (Design sources), 2 (Layout), 9
    (Testbenches shipped) and 10 (Repo hygiene) -- the four
    ``docs/design-evidence-tiers.md`` documents as naming no tool, and the
    four ``docs/cli/signoff.md``'s "Items 1, 2, 9, and 10" section tells a
    manifest author to leave **uncited** by default, since any passing
    envelope satisfies them regardless of topical relevance.

    **Derived, never hard-coded.** An item is structurally ungradeable
    exactly when no grading table names it: it carries no
    :data:`_ITEM_ALLOWED_KINDS` restriction (items 3-8 do) and is not graded
    by the compound power-delivery path (:data:`_ITEMS_GRADED_AS_POWER_DELIVERY`
    -- item 11). So the day one of those four gains a verb and an entry in
    either table, it stops being ungradeable here with no second list to
    remember to update -- the same anti-drift discipline
    :mod:`.design_evidence_tiers` applies to the item list itself.

    Consumed only by the fleet roll-up's reduction
    (:func:`_blocking_t1_item`, :func:`_ungraded_t1_items`). It changes no
    item's grading: an ungradeable item is graded exactly as before, and a
    block's ``tier`` still requires every T1 item -- these four included --
    to be ``"met"``.
    """
    return (
        item_id not in _ITEM_ALLOWED_KINDS
        and item_id not in _ITEMS_GRADED_AS_POWER_DELIVERY
    )


@cache
def _t1_item_ids_of(doc_path: str) -> frozenset[int] | None:
    """The T1 checklist item ids of the ``design-evidence-tiers.md`` at
    ``doc_path``, or ``None`` when that doc cannot be read or parsed.

    Cached per path: the doc a *build* ships with is immutable for the life
    of the process (this is never used to read a caller-supplied doc --
    :func:`build_tier_report` already parses that one itself), so re-reading
    it once per block of a fleet roll-up would buy nothing.
    """
    try:
        doc = parse_tier_doc(doc_path)
    except DesignEvidenceTiersError:
        return None
    return frozenset(
        item["id"] for item in doc["t1_items"] if isinstance(item["id"], int)
    )


def _build_t1_item_ids() -> frozenset[int] | None:
    """The T1 item ids **this build** was written against -- the item list of
    the ``design-evidence-tiers.md`` copy that ships with this install
    (:data:`~.design_evidence_tiers.DEFAULT_DOC_PATH`: the packaged copy for
    a wheel, the source checkout's ``docs/`` otherwise).

    Deliberately *not*
    :func:`~.design_evidence_tiers.default_doc_path`: that one honours
    ``$KLT_TIERS_DOC``, and the override is exactly what this has to be
    measured against. ``None`` when the shipped doc cannot be read or parsed
    at all -- see :func:`_is_graded_by_build` for how that is handled.
    """
    return _t1_item_ids_of(str(DEFAULT_DOC_PATH))


def _is_graded_by_build(item_id: int, build_item_ids: frozenset[int] | None) -> bool:
    """Does this build have grading rules for T1 item ``item_id`` (issue
    #2176)?

    ``--tiers-doc``/``$KLT_TIERS_DOC`` deliberately decouples the parsed item
    list from the grading logic compiled into the running build (that is what
    the override is *for*), so a doc newer than the build can hand
    :func:`build_tier_report` an item id this build has never heard of. Such
    an item parses fine and renders a row that looks like every other row,
    but nothing in this build knows its accepted envelope kinds, its evidence
    shape, or its pass conditions.

    An item is graded by this build when either:

    1. a grading table names it (:data:`_ITEM_ALLOWED_KINDS`,
       :data:`_ITEMS_GRADED_AS_POWER_DELIVERY`) -- an explicit, per-id rule;
       or
    2. this build's **own** shipped doc lists it
       (:func:`_build_t1_item_ids`) -- the four items with no `klt` verb
       behind them (1, 2, 9 and 10 today, see
       :func:`_is_structurally_ungradeable_item`) have no table entry by
       design, and "any recognised, passing envelope satisfies it" is a
       documented rule for them, not an absence of one.

    Both halves are derived, never hard-coded: the day the shipped doc gains
    an item, this build knows it; the day a grading table gains an id, this
    build grades it. ``build_item_ids is None`` (the shipped doc is
    unreadable in this install) reports **every** item as graded, because
    divergence cannot be *proven* -- a report must never invent a refusal it
    cannot substantiate, and behaviour then matches every release before this
    check existed.

    This compares item **ids** only, not the doc's prose. A doc that
    renumbers or rewords an item this build does know (so id 3 no longer
    means "DRC clean") is a content-drift question, answered by pinning the
    doc's content in the report -- deliberately a separate mechanism.
    """
    if item_id in _ITEM_ALLOWED_KINDS or item_id in _ITEMS_GRADED_AS_POWER_DELIVERY:
        return True
    if build_item_ids is None:
        return True
    return item_id in build_item_ids


def _build_t1_row_count(
    build_item_ids: frozenset[int] | None, partition_count: int
) -> int | None:
    """How many T1 rows **this build's own shipped doc** would have rendered
    for a block graded across ``partition_count`` partitions (issue #2202),
    or ``None`` when that doc cannot be read at all.

    The counterpart of ``t1_item_count``, and the answer to the direction
    :func:`_is_graded_by_build` does *not* cover. That one reports items the
    **parsed doc** lists which this build has no rules for -- rows that are
    rendered, and must not look graded. The reverse -- a doc *older* than the
    build, listing fewer items than this build knows how to check -- produces
    no wrong verdict at all: every rendered row is correctly graded. It is a
    pure **scope** gap, and an invisible one, because the missing items are
    simply not rows. A report claiming ``tier: "T1"`` on 9/9 items is a
    strictly weaker claim than one claiming it on 11/11, and without this
    field the two are distinguishable only by a reader who already knows
    which build produced which (issue #2176's ``build`` block narrows that to
    a *reconstruction* -- which is exactly what a committed artifact must not
    require).

    Multiplied by ``partition_count`` for the same reason ``t1_item_count``
    is: a ``mixed-signal`` block renders each T1 item once per partition, so
    an unmultiplied count would read as a shortfall (11 vs. 22) on every
    mixed-signal report that has none. The two counts are therefore directly
    comparable, and equal whenever the parsed doc's item list matches the
    build's.

    ``None`` when :func:`_build_t1_item_ids` is ``None`` (an install with
    neither the packaged doc nor a source checkout): the same "never invent a
    claim you cannot substantiate" rule :func:`_is_graded_by_build` applies
    to the other direction -- a fabricated count here would be read as a
    shortfall, or as its absence, and neither would be true.

    Note that neither count bounds the other: a doc can both omit items this
    build grades *and* add items it does not, in which case
    ``t1_item_count`` exceeds this value while some rows also carry
    ``graded_by_build: false``. The two fields answer different questions and
    are deliberately not derived from each other.
    """
    if build_item_ids is None:
        return None
    return len(build_item_ids) * partition_count


def _is_ungradeable_by_build(item: dict[str, Any]) -> bool:
    """Is this **rendered** T1 item one this build has no grading rules for
    at all -- an id only a ``--tiers-doc``/``$KLT_TIERS_DOC`` copy of the doc
    lists (issue #2203)?

    Reads the ``graded_by_build`` field :func:`_is_graded_by_build` already
    put on every rendered T1 item, rather than re-deriving the answer: the
    roll-up must never be able to disagree with the per-block report about
    which items were gradeable. A T2-T4 ladder row carries no such key (its
    ``reason: "tier_not_supported"`` already says the repository cannot check
    it), so an absent field reads as "graded" -- the same direction
    :func:`_is_graded_by_build` defaults in when it cannot *prove* divergence.

    **Deliberately distinct from :func:`_is_structurally_ungradeable_item`**,
    which answers a different question: "does any `klt` verb exist for this
    id, repo-wide". Today's ``graded_by_build: False`` population happens to
    be a strict *subset* of that one -- an id no grading table names is
    structurally ungradeable by that function's own derivation -- which is
    exactly why the fleet reduction cannot key on the structural predicate
    alone and expect to tell the two apart (issue #2203). They mean opposite
    things to a reader:

    ===========================  ==========================  ======================
    ..                           structurally ungradeable    ungradeable by build
    ===========================  ==========================  ======================
    What is missing              a `klt` verb, repo-wide     *this build's* rules
    Fix                          commit/cite the artifact    a newer `klt`
    Expected?                    yes, for every manifest     no, never
    ===========================  ==========================  ======================

    See :func:`_blocking_t1_item` for what the roll-up does with each.
    """
    return item.get("graded_by_build", True) is False


#: The :func:`~.build_identity.version_report` fields echoed into a tier /
#: fleet report's ``build`` block (issue #2176), in order. ``klt version``'s
#: own ``schema_version`` is dropped (the report carries its own, and a
#: nested one would read as this block's version rather than that command's),
#: and so are its two KLayout-engine fields: the engine this `klt signoff`
#: process happens to resolve says nothing about what its grading rules can
#: check, and every cited envelope already carries the engine *its own* run
#: used in its ``provenance`` block.
#:
#: ``grading_ruleset_id`` (issue #2216) is the opposite case from the
#: KLayout fields -- it is included, not dropped: unlike the resolved engine
#: version, it *is* a statement about what this build's grading rules can
#: check, which is exactly what a report's ``build`` block exists to name.
#: `git_commit`/`git_tag` alone cannot answer "did the grading rules that
#: produced this report change" without a reader cross-referencing commit
#: history that may not even be checked out (a wheel install has no `.git`
#: directory); the content hash answers it directly.
_BUILD_IDENTITY_FIELDS = (
    "version",
    "package_version",
    "git_commit",
    "git_tag",
    "dirty",
    "is_release",
    "grading_ruleset_id",
)


@cache
def _build_identity_fields() -> tuple[tuple[str, Any], ...]:
    """:data:`_BUILD_IDENTITY_FIELDS`, resolved once per process.

    Cached because a source/editable install resolves its identity by
    shelling out to ``git`` (:func:`~.build_identity._checkout_identity`),
    and :func:`build_fleet_report` renders one tier report per block: without
    this, a 20-block roll-up would run that probe 20 times to answer a
    question whose answer cannot change while the process lives. Immutable
    (a tuple of pairs) so the cached value can never be mutated through one
    report's ``build`` block; :func:`_build_identity` mints a fresh dict per
    report from it.
    """
    report = version_report()
    return tuple((field, report[field]) for field in _BUILD_IDENTITY_FIELDS)


def _build_identity() -> dict[str, Any]:
    """The running build's identity, for a report that outlives the
    invocation that produced it (issue #2176).

    A committed tier report is read later, by someone who was not at the
    terminal and cannot see which `klt` rendered it -- so "which build
    graded this, and therefore what could it check" has to be *in* the
    artifact. Reuses :func:`~.build_identity.version_report`, the same
    payload `klt version --format json` prints, rather than re-deriving
    version/commit detection here; see :data:`_BUILD_IDENTITY_FIELDS` for
    the fields it drops and why.
    """
    return dict(_build_identity_fields())


def _graded_t1_item_ids() -> frozenset[int] | None:
    """Every T1 item id this build has grading rules for (issue #2216) --
    the full set :func:`_is_graded_by_build` tests membership against, one
    call instead of probing per id.

    The union of both halves that function's own docstring names as
    "graded": the ids named by a grading table (:data:`_ITEM_ALLOWED_KINDS`,
    :data:`_ITEMS_GRADED_AS_POWER_DELIVERY`) and this build's own shipped
    doc's item list (:func:`_build_t1_item_ids` -- covers the four items
    with no `klt` verb behind them at all, items 1, 2, 9 and 10, which carry
    no table entry by design). In practice the first set is always a subset
    of the second for a self-consistent build (every table id is also one
    the shipped doc lists), so this is normally just
    :func:`_build_t1_item_ids`'s own answer -- the union guards the
    hypothetical case of the two drifting apart without silently
    under-reporting either.

    ``None`` when :func:`_build_t1_item_ids` is, for the same "never invent
    a claim you cannot substantiate" reason: an install that cannot read its
    own shipped doc cannot *prove* which ids it grades, so it must not
    enumerate a set it cannot back up.
    """
    build_item_ids = _build_t1_item_ids()
    if build_item_ids is None:
        return None
    graded_table_ids = frozenset(_ITEM_ALLOWED_KINDS) | _ITEMS_GRADED_AS_POWER_DELIVERY
    return graded_table_ids | build_item_ids


def describe_grader() -> dict[str, Any]:
    """The ``klt signoff --describe-grader`` JSON payload (issue #2216).

    Answers, at runtime and without reading source, the two questions a
    consumer who commits a tier-verdict report as evidence cannot otherwise
    answer from a `klt` version string alone:

    - **Which grading code is this** -- ``grading_ruleset_id``, the same
      content hash :func:`~.build_identity.version_report` and every tier /
      fleet report's own ``build`` block carry (see that function's
      docstring for why a content hash rather than a derivation of
      ``git_commit``).
    - **Which T1 checklist items can it grade at all** --
      ``graded_t1_item_ids``, built on :func:`_build_t1_item_ids` and the
      same "is this item graded" test :func:`_is_graded_by_build` already
      applies per item while rendering a tier report's ``items[]`` --
      surfaced here directly rather than requiring a caller to run a whole
      tier report against a throwaway manifest just to read which ids came
      back ``graded_by_build: true``.

    Always reports **this build's own shipped** grading rules --
    deliberately not affected by ``--tiers-doc``/``$KLT_TIERS_DOC``, which
    overrides the item *list* a tier report parses, not the grading logic
    compiled into the running build (:func:`_build_t1_item_ids` reads
    :data:`~.design_evidence_tiers.DEFAULT_DOC_PATH` for exactly this
    reason). ``klt signoff --describe-grader --tiers-doc <path>`` is
    therefore refused by the CLI layer rather than silently ignoring the
    flag -- see ``docs/cli/signoff.md``.
    """
    report = version_report()
    graded_ids = _graded_t1_item_ids()
    return {
        "schema_version": DESCRIBE_GRADER_SCHEMA_VERSION,
        "version": report["version"],
        "grading_ruleset_id": report["grading_ruleset_id"],
        "source_doc": CANONICAL_DOC_LABEL,
        "graded_t1_item_ids": (sorted(graded_ids) if graded_ids is not None else None),
    }


#: Provenance sub-fields compared for consistency across every input
#: envelope that carries them -- see _provenance_consistency()'s docstring
#: for what each check means and why a mismatch is refused rather than
#: silently aggregated.
_PDK_NAME = "pdk.name"
_PDK_VERSION = "pdk.version"
_INPUT_HASH = "input.content_hash"

#: How a ``provenance.input`` block with no ``role`` key is read by
#: :func:`_check_input_hashes` (issue #2027). ``"layout"`` was the field's
#: only documented meaning before the discriminator existed
#: (``docs/json-contract.md``: "the input layout stream the run was made
#: against"), so reading an older envelope this way keeps it in the
#: layout-side comparison rather than exempting it.
_DEFAULT_INPUT_ROLE = INPUT_ROLE_LAYOUT

#: ``reason`` values a tier-report item can carry when its ``status`` is
#: ``"unmet"`` -- see :func:`_grade_evidence` and :func:`_build_tier_item`.
#: Issue #826 (Phase 1b of epic #706): the whole point of this enum is that
#: "no runnable check exists for this item" (:data:`_REASON_NO_EVIDENCE`,
#: :data:`_REASON_INVALID_EVIDENCE`, :data:`_REASON_UNREADABLE_EVIDENCE`,
#: :data:`_REASON_UNRECOGNIZED_ENVELOPE`, :data:`_REASON_TIER_NOT_SUPPORTED`)
#: must never collapse, in the JSON output, into the same shape as "a check
#: ran and did not pass" (:data:`_REASON_CHECK_ERRORED`,
#: :data:`_REASON_CHECK_FAILED`, :data:`_REASON_STALE_EVIDENCE`) -- a reader
#: (human or agent) parsing the report must be able to tell "nobody ever
#: checked this" apart from "somebody checked this and it failed" without
#: cross-referencing anything outside the item itself.
_REASON_NO_EVIDENCE = "no_evidence"
_REASON_INVALID_EVIDENCE = "invalid_evidence"
_REASON_UNREADABLE_EVIDENCE = "unreadable_evidence"
_REASON_UNRECOGNIZED_ENVELOPE = "unrecognized_envelope"
_REASON_CHECK_ERRORED = "check_errored"
_REASON_CHECK_FAILED = "check_failed"
_REASON_STALE_EVIDENCE = "stale_evidence"

#: Issue #2198: the resolved evidence *is* a `klt` envelope -- it carries a
#: kind's **primary** marker (see :data:`_NEAR_MISS_MARKERS`) -- but not the
#: shape discriminator :func:`_classify` pairs that marker with, because the
#: discriminator was added to the verb's response *after* this artifact was
#: written. Deliberately distinct from
#: :data:`_REASON_UNRECOGNIZED_ENVELOPE`, which collapses three unrelated
#: situations already ("not a JSON object", "matches no recognised shape",
#: "matches one but is malformed for it") and, without this reason, a fourth:
#: a well-formed response from an older `klt`. Committed evidence is
#: append-only by construction (``docs/design-evidence-tiers.md``,
#: "Provenance hygiene in evidence records"), so the fleet's stock of
#: envelopes written against older schemas only grows -- and every additive
#: marker field silently converts some of it from "gradeable" to "looks like
#: it isn't a `klt` file". The two remedies differ and the report must say
#: which applies: "re-run this check under a newer `klt`" (version skew) vs.
#: "this citation is wrong" (unrecognised).
_REASON_ENVELOPE_VERSION_SKEW = "envelope_version_skew"

#: Issue #2182: a manifest entry pins ``content_hash``, but the resolved
#: envelope carries **no input hash at all** -- ``resolution["content_hash"]
#: is None`` -- rather than one that mismatches the pin. Two ways this
#: happens today: a `klt functional-verification` envelope, which carries no
#: `provenance` block by design (see :data:`_REASON_STALE_EVIDENCE`'s own
#: docstring reference above and ``docs/cli/signoff.md``); or a `"generic"`
#: envelope (issue #1152) with no `provenance` block at all. Deliberately
#: distinct from :data:`_REASON_STALE_EVIDENCE`, which is reserved for a
#: genuinely *mismatched*, non-``None`` hash (the evidence ran, and provably
#: ran against a different revision than the one pinned -- the remedy is
#: "re-run the check"): here nothing was ever recorded to compare against,
#: so the remedy is different -- "re-produce this evidence with a producer
#: that records provenance" (or unpin `content_hash` for this entry), not
#: "re-run the same producer again". A `klt yield` envelope is unaffected by
#: this distinction: :func:`_yield_samples_content_hash` supplies a computed
#: fallback hash whenever its samples document can be found -- resolved
#: relative to the report file's own directory first, falling back to this
#: process's own working directory for compatibility (issue #2197) -- so its
#: ``resolution["content_hash"]`` is ``None`` for that reason alone only when
#: the named document could not be located in *either* place (see
#: ``resolution["content_hash_unresolved"]``, which then names what was
#: named and where this looked, so that case stays distinguishable from an
#: envelope that recorded no samples document at all).
_REASON_UNVERIFIABLE_PROVENANCE = "unverifiable_provenance"

#: Issue #2342: the evidence entry carried a ``"pointer"`` (an RFC 6901 JSON
#: Pointer naming where inside the cited document the envelope lives -- see
#: :func:`_resolve_json_pointer`), and that pointer did not resolve to a JSON
#: object: it is syntactically malformed, it names a path the document does
#: not contain, or it names a value that is not an object (a string, number,
#: array, or ``null`` -- none of which can ever be a `klt` envelope).
#: Deliberately distinct from :data:`_REASON_UNRECOGNIZED_ENVELOPE`, which
#: says "the thing this citation resolved to is not a shape I recognise": the
#: two are different mistakes with different remedies. An unrecognised
#: envelope means the manifest cited the wrong *item* (fix the citation);
#: an invalid pointer means the manifest cited the right document but named
#: the wrong *place inside it* (fix the pointer) -- and, critically, that the
#: cited bytes were never even classified, so the reason must not imply
#: anything about them. Grouped with the other "no runnable check exists for
#: this item" reasons per issue #826's invariant.
_REASON_INVALID_POINTER = "invalid_pointer"

_REASON_TIER_NOT_SUPPORTED = "tier_not_supported"
#: Issue #825 (Phase 1 of epic #706): a command-backed evidence entry's
#: subprocess itself did not complete usably -- it could not be launched, it
#: timed out, or it exited nonzero *without* leaving a parseable envelope on
#: stdout (per docs/json-contract.md, exit code 1/2 leaves stdout empty).
#: A nonzero exit that *does* carry a parseable envelope on stdout -- e.g.
#: `klt drc`'s `EXIT_VIOLATIONS = 3`, a successful run that found violations
#: -- is not this case; it flows into :func:`_classify`/:func:`_check_passed`
#: exactly like the file-backed path, landing on :data:`_REASON_CHECK_ERRORED`
#: or :data:`_REASON_CHECK_FAILED`. Distinct from
#: :data:`_REASON_UNREADABLE_EVIDENCE` (the command exited *zero* but its
#: stdout wasn't valid JSON) -- different ways "no runnable check proves this
#: item" can happen, kept distinguishable per issue #826's invariant.
_REASON_COMMAND_FAILED = "command_failed"

#: Issue #871 (Phase 2b of epic #706): the evidence resolved to a readable,
#: recognised, *passing* envelope -- but its classified kind is not one this
#: item accepts (see :func:`_build_tier_item`'s ``allowed_kinds`` parameter).
#: Grouped with the other "no runnable check exists for this item" reasons
#: (:data:`_REASON_NO_EVIDENCE` et al.) rather than with
#: :data:`_REASON_CHECK_FAILED`: the cited check did not fail on its own
#: terms, it simply does not prove what *this* item requires (e.g. a clean
#: DRC report cited for item 7, "post-layout verification", proves nothing
#: about a schematic-vs-extracted-netlist delta) -- so it must never render
#: `"met"` by borrowing an unrelated check's pass.
_REASON_WRONG_KIND = "wrong_kind"

#: Issue #2176: the manifest cited evidence for a T1 item **this build has
#: no grading rules for at all** -- an item id that appears in the doc the
#: report was built from (``--tiers-doc``/``$KLT_TIERS_DOC``) but not in
#: this build's own bundled copy of it, and that no grading table names
#: either (see :func:`_is_graded_by_build`). The caller is asking a question
#: this build cannot answer: it knows none of that item's accepted envelope
#: kinds, none of its evidence shape, none of its pass conditions.
#:
#: Grouped with the other "no runnable check exists for this item" reasons,
#: for the sharpest form of the reason :data:`_REASON_WRONG_KIND` is: no
#: check was even attempted. Deliberately distinct from ``wrong_kind``
#: ("cite a different artifact -- this build knows which") and from
#: :data:`_REASON_NO_EVIDENCE` ("nothing was cited"): here the citation may
#: be perfectly good and the *build* is what is missing, so the actionable
#: fix is to upgrade `klt` (or grade against the doc this one ships), not to
#: touch the manifest. Without it, such a citation fell through to the
#: unrestricted ``allowed_kinds is None`` path and could render ``"met"`` --
#: a pass produced by rules that do not exist in the running build, and
#: indistinguishable in the report from a correctly-graded row.
#:
#: Note that the evidence is *not* resolved before this refusal: an
#: ungradeable item's command-backed entry is never executed, since this
#: build could not interpret what came back anyway.
_REASON_UNGRADEABLE_BY_BUILD = "ungradeable_by_build"

#: Issue #1959: the evidence resolved to a readable, recognised, *passing*
#: envelope **of a kind this item accepts**, but that run is not the
#: post-layout run the item requires -- today, a `klt
#: functional-verification` regression cited for item 7 that ran without SDF
#: back-annotation (``environment.sdf`` is ``null``), i.e. the pre-layout
#: zero-delay simulation item 7's own checklist text excludes. Deliberately
#: distinct from :data:`_REASON_WRONG_KIND` (which says "cite a different
#: kind of artifact"): here the artifact *is* the right one, it just has to
#: be re-run against the post-route netlist with SDF timing, and per issue
#: #826's invariant the report must say which of the two is missing rather
#: than collapse them into one ambiguous reason. Grouped with the "no
#: runnable check proves this item" reasons for the same rationale as
#: ``wrong_kind``: the cited check did not fail on its own terms.
_REASON_NOT_POST_LAYOUT = "not_post_layout"

#: Issue #1996: the evidence resolved to a readable, recognised, *passing*
#: envelope of a kind this item accepts -- whose own ``coverage`` block
#: states that the run checked **nothing** (``coverage.nothing_checked`` is
#: ``true``; see :mod:`klayout_tools.coverage` and this module's
#: "Vacuous-verdict refusal" docstring section). A DRC deck whose whole rule
#: set was gated behind an unset ``--deck-var``, a `klt sim` corner matrix
#: that expanded to zero corners, a `klt pex` run with no ``delta[]`` row to
#: compare: each reports a passing ``status`` that says nothing at all about
#: the design. Grouped with the "no runnable check proves this item" reasons
#: (:data:`_REASON_WRONG_KIND`, :data:`_REASON_NOT_POST_LAYOUT`) rather than
#: with :data:`_REASON_CHECK_FAILED`, per issue #826's invariant: the cited
#: check did not fail on its own terms, it simply measured nothing -- and the
#: fix is to re-run it with something to check, not to fix a defect it found.
_REASON_NOTHING_CHECKED = "nothing_checked"

#: Issue #2109: the evidence resolved to a readable, recognised envelope of a
#: kind this item accepts, whose producer applied the common rollup rule
#: (:func:`~klayout_tools.coverage.coverage_rollup`) and reported its
#: **partial** status token -- ``"clean_partial"``, ``"pass_partial"``, the
#: per-kind spelling in :data:`_PARTIAL_STATUS_BY_KIND`. Every check it ran
#: passed, and it also skipped requested work, so its result is real but not
#: unconditional.
#:
#: It *names* the same class of problem as the "no runnable check proves
#: this item" reasons (:data:`_REASON_WRONG_KIND`,
#: :data:`_REASON_NOT_POST_LAYOUT`, :data:`_REASON_NOTHING_CHECKED`), per
#: issue #826's invariant: the cited check did not fail on its own terms,
#: and the fix is to re-run it over the work it skipped, not to fix a defect
#: it found. Reporting it as ``check_failed`` -- which is what happened
#: before this constant existed, since a partial token simply is not the
#: kind's success token -- sent a reader looking for a violation that does
#: not exist.
#:
#: It is *ordered* like :data:`_REASON_CHECK_FAILED` rather than like those
#: three, because it replaces that reason on the same code path: it is
#: decided from the envelope's own verdict, before :func:`_build_tier_item`'s
#: kind gate (which only ever downgrades a would-be ``"met"``). So a partial
#: report cited for an item that does not accept its kind reports
#: ``partial_coverage``, exactly as a *failing* report of that kind reports
#: ``check_failed`` rather than ``wrong_kind`` today. The three gated
#: refusals differ because they apply to an envelope that *passed*, where
#: "cite a different artifact" really is the more actionable answer.
_REASON_PARTIAL_COVERAGE = "partial_coverage"

#: The status token each kind reports for a partial result (issue #2109):
#: the kind's own success token from :func:`_check_passed`, suffixed
#: ``_partial``, exactly as
#: :func:`~klayout_tools.coverage.rollup_status` derives it by default. This
#: table is what lets :func:`_non_passing_reason` tell "checked everything it
#: was asked to and something failed" apart from "passed everything it ran
#: and skipped some of what it was asked to".
#:
#: Two kinds are deliberately absent. ``extract`` has no pass/fail verdict to
#: qualify (:func:`_check_passed` returns ``True`` for any present envelope),
#: and ``sta``'s verdict is derived from per-corner timing fields rather than
#: a status token, so a partial spelling would have nothing to match against;
#: their adapters must decide how a partial timing/extraction result is
#: reported before either belongs here. ``power`` matches inside
#: ``em_verdict`` rather than at the top level, mirroring
#: :func:`_check_passed`, and is handled in :func:`_reports_partial_status`.
_PARTIAL_STATUS_BY_KIND: dict[str, str] = {
    "drc": "clean_partial",
    "erc": "clean_partial",
    "lvs": "match_partial",
    "sim": "pass_partial",
    "pex": "pass_partial",
    "yield": "pass_partial",
    "functional-verification": "pass_partial",
    "place-and-route": "ok_partial",
    "generic": "pass_partial",
    "power": "pass_partial",
}

#: Issue #2025, T1 item 11 ("Power delivery (structural)") only. Six
#: reasons, not one, for the same reason :data:`_REASON_NOT_POST_LAYOUT` is
#: distinct from :data:`_REASON_WRONG_KIND`: item 11 is a *compound* claim,
#: and a report that collapsed "no grid was ever built" into the same
#: ``check_failed`` shade as "the grid is built but one rail is split in
#: two" would tell a reader nothing about which artifact to go fix. Each
#: names a distinct, independently-actionable condition of the item's own
#: checklist text:
#:
#: - :data:`_REASON_NO_PDN` -- the cited `klt place-and-route` response says
#:   no power grid was built (``power.pdn`` is not ``true``, or no
#:   ``power.tapcell_master`` was placed). Fix: re-run P&R with a
#:   ``request.power`` block.
#: - :data:`_REASON_SUPPLY_SPEC_INCOMPLETE` -- the cited `klt erc` run's own
#:   spec document does not ask the question item 11 grades: it could not be
#:   read, declares no ``"kind": "supply"`` net, declares no ``ties[]``
#:   with no disclosure of why (see :data:`_REASON_SUPPLY_SPEC_DISCLOSED_UNEXPRESSIBLE`
#:   below for the disclosed case, issue #2234) -- an uncomputed check is
#:   not a clean one -- declares a ``ties[]`` entry the ERC run itself
#:   reported as *degenerate* (issue #2199, see
#:   :func:`_erc_missing_tie_skipped` -- a check that could not tell a tap
#:   from a source/drain contact is likewise not a clean one), or its
#:   stackup does not cover every strap layer the P&R response reports.
#:   Fix: widen the spec and re-run `klt erc`.
#: - :data:`_REASON_SUPPLY_SPEC_DISCLOSED_UNEXPRESSIBLE` -- the cited `klt
#:   erc` run declares zero ``ties[]``, exactly as
#:   :data:`_REASON_SUPPLY_SPEC_INCOMPLETE`'s "no ``ties[]``" case -- but its
#:   spec explicitly disclosed why no tap can be expressed
#:   (``ties_disclosure``, issue #2234, see :func:`_erc_missing_tie_disclosed`).
#:   Still ``"unmet"`` -- a disclosure proves nothing about the tap's actual
#:   connectivity, so it can never substitute for a computed
#:   ``erc.missing_tie`` result -- but distinguishable from "nobody declared
#:   ties at all", which :data:`_REASON_SUPPLY_SPEC_INCOMPLETE` still covers.
#:   Fix: express the tap (``tap_boxes``, ``tap_requires``, or
#:   ``tap_is_dedicated``) and re-run `klt erc`, or accept this item stays
#:   unmet for this stream.
#: - :data:`_REASON_SUPPLY_SPEC_DISCLOSED_TOOL_LIMITATION` -- the same zero
#:   ``ties[]`` fact once more, disclosed once more, but naming a different
#:   obstacle (``ties_disclosure.kind: "tool_limitation"``, issue #2247):
#:   the tap *is* expressible, and the reason no tie was declared is that
#:   the `klt` build this evidence had to be produced on cannot grade a
#:   declared tie safely (issue #2169's unisolated tie extraction turning a
#:   correct ``ties[]`` into a false ``erc.supply_short`` is the reported
#:   instance). Still ``"unmet"``, for exactly the reason
#:   :data:`_REASON_SUPPLY_SPEC_DISCLOSED_UNEXPRESSIBLE` is -- a disclosure
#:   is the caller's word, never a computed ``erc.missing_tie`` result --
#:   but kept distinct from it because the *remedy* differs: that one says
#:   "this stream has no tap to name", this one says "this layout has a tap;
#:   the build could not be trusted to grade it". Fix: re-run against a
#:   `klt` build whose tie extraction is isolated (#2169) and declare the
#:   tie, not a redrawn layout.
#: - :data:`_REASON_SUPPLY_NOT_CONTINUOUS` -- the ERC run *did* ask, and the
#:   answer is no: a declared supply resolved to zero or several islands
#:   (``erc.unconnected_net``), two declared supplies resolved to the same
#:   island (``erc.supply_short``), or a well/tub has no connected tap
#:   (``erc.missing_tie``). Fix: the layout.
#: - :data:`_REASON_LVS_SUPPLY_UNPROVEN` -- the LVS half of the item is not
#:   proven: for a digital partition with a PDN citation, the same report's
#:   ``power_connectivity.status`` is not ``"match"`` (``"unchecked"`` does
#:   not satisfy item 11, unlike item 4); otherwise, its
#:   ``net_correspondence`` does not pair every declared supply net to a
#:   reference-side net, so the supplies were not part of the compare.
_REASON_NO_PDN = "no_pdn"
_REASON_SUPPLY_SPEC_INCOMPLETE = "supply_spec_incomplete"
_REASON_SUPPLY_SPEC_DISCLOSED_UNEXPRESSIBLE = "supply_spec_disclosed_unexpressible"
_REASON_SUPPLY_SPEC_DISCLOSED_TOOL_LIMITATION = "supply_spec_disclosed_tool_limitation"
_REASON_SUPPLY_NOT_CONTINUOUS = "supply_not_continuous"
_REASON_LVS_SUPPLY_UNPROVEN = "lvs_supply_unproven"

#: The three :func:`_classify` kinds T1 item 11's compound evidence list may
#: cite (issue #2025). A cited part of any other recognised kind renders
#: :data:`_REASON_WRONG_KIND` -- it proves nothing about power delivery, and
#: item 11 must never reach ``"met"`` by borrowing an unrelated check's pass.
_POWER_DELIVERY_KINDS: frozenset[str] = frozenset({"erc", "lvs", "place-and-route"})

#: Wall-clock cap on a command-backed evidence entry's subprocess (issue
#: #825) -- a hung `klt drc`/`klt lvs`/`klt sim` gate must not hang `klt
#: signoff` itself. Generous (corner-matrix sims are slow) but finite; a
#: timeout renders that item "unmet" (see :func:`_grade_evidence`), never an
#: exception that aborts the whole report.
_COMMAND_EVIDENCE_TIMEOUT_S = 1800


class SignoffError(Exception):
    """Raised when an envelope cannot even be read/parsed, or does not match
    any recognized ``klt`` envelope shape.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other ``klt`` verb's error contract.
    """


class EnvelopeVersionSkewError(SignoffError):
    """Raised by :func:`_classify` for an envelope that carries a kind's
    **primary** marker but none of the shape discriminators that marker is
    paired with (issue #2198) -- the shape a `klt` response written before
    the discriminator field existed has.

    A :class:`SignoffError` subclass on purpose: every existing caller keeps
    treating it exactly as it treated the unrecognised-shape raise (the CLI
    still exits 1 with a clean message), while a caller that *can* act on
    the distinction -- :func:`_resolve_evidence`, which renders
    :data:`_REASON_ENVELOPE_VERSION_SKEW` rather than
    :data:`_REASON_UNRECOGNIZED_ENVELOPE` -- catches it first.

    ``kind``/``primary_field``/``missing_fields`` name the near-miss, so the
    message can say which verb's response this almost is and which field it
    lacks -- something the unrecognised-shape raise structurally cannot say
    about a genuinely foreign document.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        primary_field: str,
        missing_fields: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.primary_field = primary_field
        self.missing_fields = missing_fields


def build_signoff(sources: list[str]) -> dict[str, Any]:
    """Read every entry in ``sources`` (a file path, or ``"-"`` for stdin)
    as a ``klt`` JSON envelope and aggregate them into one signoff verdict.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/signoff.md``)::

        {
            "schema_version": 1,
            "status": "pass" | "fail" | "refused",
            "check_count": <int>,
            "passed_count": <int>,
            "failed_count": <int>,
            "provenance_consistency": {
                "ok": <bool>,
                "mismatches": [
                    {
                        "field": "pdk.name" | "pdk.version" |
                                 "input.content_hash" |
                                 "deck[<deck name>].content_hash",
                        # `input.content_hash` only (issue #2027): which
                        # `provenance.input.role` this entry's checks
                        # disagreed on -- hashes are compared only within a
                        # role, never across them.
                        "role": <str>,   # optional
                        "values": [{"source": <str>, "value": <str>}, ...],
                    },
                    ...
                ],
            },
            "checks": [
                {
                    "source": <str, the entry from `sources`>,
                    "kind": "drc" | "lvs" | "extract" | "sim" | "yield" | "pex"
                            | "power" | "sta" | "functional-verification"
                            | "erc" | "place-and-route" | "generic" | "error",
                    "status": <str> | None,
                    "passed": <bool>,
                    "detail": {...},  # kind-specific summary, see _detail()
                    "provenance": {...} | None,
                },
                ...
            ],
        }

    ``status`` is ``"refused"`` whenever ``provenance_consistency.ok`` is
    ``False`` -- a mismatched-provenance input set never produces a
    ``"pass"``/``"fail"`` verdict, on the theory that a wrong-but-confident
    verdict is worse than a loud refusal (see this module's docstring,
    point 2). Otherwise ``status`` is ``"fail"`` if any check's ``passed``
    is ``False``, else ``"pass"``.

    ``passed`` (issue #1850) also factors in any registered ``critical:
    true`` metric present in the envelope's own ``metrics`` block (see
    :mod:`klayout_tools.metrics`, populated by verbs such as ``klt drc``,
    ``klt extract``, ``klt sim``): a critical metric whose value fails its
    own declared ``higher_is_better`` polarity forces ``passed: False``,
    independent of (and in addition to) the envelope's own ``status``. When
    that happens, the offending metric(s) are named in the check's
    ``detail.critical_metric_blockers`` list -- see
    :func:`_critical_metric_blockers` and :func:`_detail`. This is purely
    additive: an envelope with no ``metrics`` block, or none marked
    ``critical``, behaves exactly as before.

    ``passed`` (issue #1996) is likewise forced to ``False`` for an envelope
    whose own ``coverage`` block reports ``nothing_checked: true`` -- a run
    that checked nothing never counts as a passing check, whatever its
    ``status`` says. The reason codes are named in that check's
    ``detail.nothing_checked_reasons``. Also purely additive: an envelope
    with no ``coverage`` block, or one predating the convention, makes no
    such statement and behaves exactly as before. See this module's
    "Vacuous-verdict refusal" docstring section.

    Raises :class:`SignoffError` if ``sources`` is empty, any entry cannot
    be read/parsed as JSON, is not a JSON object, does not match a
    recognized envelope shape, or matches one but is malformed for it --
    missing a required field, or carrying one of the wrong type (issue
    #2033; see :func:`_classify` and :func:`_validate_envelope`).
    """
    if not sources:
        raise SignoffError(
            "at least one klt JSON envelope file (or '-' for stdin) is required"
        )

    checks: list[dict[str, Any]] = []
    for source in sources:
        envelope = _read_envelope(source)
        if not isinstance(envelope, dict):
            raise SignoffError(
                f"envelope '{source}' must be a JSON object, got "
                f"{type(envelope).__name__}"
            )
        # Issue #2033: `_classify` both recognises the kind *and* validates
        # the envelope against that kind's declared shape, so the cast below
        # is backed by a runtime check -- not an assertion about unvalidated
        # JSON.
        kind = _classify(envelope, source)
        checks.append(_build_check(kind, cast(_EvidenceEnvelope, envelope), source))

    provenance_consistency = _provenance_consistency(checks)

    passed_count = sum(1 for check in checks if check["passed"])
    failed_count = len(checks) - passed_count

    if not provenance_consistency["ok"]:
        status = "refused"
    elif failed_count:
        status = "fail"
    else:
        status = "pass"

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "check_count": len(checks),
        "passed_count": passed_count,
        "failed_count": failed_count,
        "provenance_consistency": provenance_consistency,
        "checks": checks,
    }


# --------------------------------------------------------------------------- #
# Reading + classifying input envelopes
# --------------------------------------------------------------------------- #


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"nonstandard numeric constant {token!r} is not permitted")


def _finite_json_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"numeric literal {token!r} is not finite")
    return value


def _read_json_source(source: str, description: str) -> Any:
    """Read and JSON-decode one JSON source: ``source == "-"`` reads stdin,
    otherwise ``source`` is a file path. Raises :class:`SignoffError` on any
    read/parse failure, including nonstandard numeric constants and literals
    that overflow to infinity -- never lets invalid JSON evidence become a
    passing verdict or leak nonfinite values into output. ``description``
    (e.g. ``"envelope"``, ``"manifest"``) only affects error-message wording,
    so each caller's failures still read naturally.

    Deliberately mirrors ``report.py``'s ``_read_envelope`` (same
    read/parse/error-message contract) rather than importing it: the two
    commands' input-reading needs are identical but incidental, not a
    shared abstraction worth coupling two independently-versioned CLI
    verbs over. Shared *within* this module by :func:`_read_envelope` and
    :func:`_read_fleet_block_manifest` (issue #827), which do belong to the
    same command and would otherwise triplicate this same read/parse logic.
    """
    if source == "-":
        try:
            return json.load(
                sys.stdin,
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
            )
        except ValueError as exc:
            raise SignoffError(f"stdin {description} is not valid JSON: {exc}") from exc

    if not os.path.exists(source):
        raise SignoffError(f"file not found: {source}")
    if os.path.isdir(source):
        raise SignoffError(f"not a file: {source}")

    try:
        with open(source, encoding="utf-8") as handle:
            return json.load(
                handle,
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
            )
    except (OSError, UnicodeDecodeError) as exc:
        raise SignoffError(
            f"could not read {description} file '{source}': {exc}"
        ) from exc
    except ValueError as exc:
        raise SignoffError(
            f"{description} file '{source}' is not valid JSON: {exc}"
        ) from exc


def _read_envelope(source: str) -> Any:
    """Read and JSON-decode one ``klt`` envelope -- see
    :func:`_read_json_source`."""
    return _read_json_source(source, "envelope")


@cache
def _shape_hints(kind: str) -> dict[str, Any]:
    """Resolved annotations for ``kind``'s :data:`_ENVELOPE_SHAPES` entry.

    Resolved rather than read raw off ``__annotations__`` because
    ``signoff_envelopes.py`` (where the shapes are declared) uses
    ``from __future__ import annotations``, so every annotation there is a
    *string* until :func:`typing.get_type_hints` evaluates it.
    Cached because the result is immutable per kind and this runs once per
    ingested envelope.
    """
    return get_type_hints(_ENVELOPE_SHAPES[kind])


def _matches_declared_type(annotation: Any, value: Any) -> bool:
    """Whether ``value`` satisfies ``annotation`` -- the runtime half of a
    declaration that is otherwise erased.

    Deliberately shallow: containers are checked for their own type only
    (``list[Any]`` means "a list", not "a list whose entries were also
    validated"). Element-level validation is each producing verb's own
    responsibility, and claiming it here would overstate what this boundary
    proves. ``Any`` means "required to be present, unconstrained in shape"
    -- used where a field's own value shape is genuinely polymorphic across
    committed evidence (see ``_PexRequired.reference_netlist``).
    """
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin in _UNION_ORIGINS:
        return any(_matches_declared_type(arg, value) for arg in get_args(annotation))
    check = _SCALAR_CHECKS.get(annotation)
    if check is not None:
        return check(value)
    return isinstance(value, origin or annotation)


def _type_label(annotation: Any) -> str:
    """A short, human-readable rendering of ``annotation`` for an error
    message (``dict[str, Any] | None`` rather than a ``typing``-qualified
    repr)."""
    return (
        str(annotation).replace("typing.", "").replace("<class '", "").replace("'>", "")
    )


def _validate_envelope(kind: str, envelope: Mapping[str, Any], source: str) -> None:
    """Validate an envelope :func:`_classify` has already matched to ``kind``
    against that kind's declared shape (:data:`_ENVELOPE_SHAPES`), raising
    :class:`SignoffError` if it does not (issue #2033).

    Checks every *required* key of the shape: present, and of the declared
    type. Optional keys are not type-checked when present -- an unexpected
    type there is already handled defensively by each reader
    (``.get(...) or {}``, ``isinstance`` guards) and hard-failing on one
    would retroactively reject committed evidence over a field this
    boundary only reports.

    Raising (rather than downgrading the check to "failed") is deliberate
    and matches how every other unreadable input is handled here: a
    malformed envelope means the verdict is unknown, not negative.
    ``--manifest`` grading converts that into an explicit ``"unmet"`` /
    ``"unrecognized_envelope"`` item -- see :func:`_grade_evidence`.
    """
    hints = _shape_hints(kind)
    for field in sorted(_ENVELOPE_SHAPES[kind].__required_keys__):
        if field not in envelope:
            raise SignoffError(
                f"envelope '{source}' matches the klt {kind} shape but is "
                f"missing required field '{field}' -- a malformed or "
                "incomplete envelope is rejected rather than graded (see "
                "docs/cli/signoff.md)"
            )
        value = envelope[field]
        if not _matches_declared_type(hints[field], value):
            raise SignoffError(
                f"envelope '{source}' matches the klt {kind} shape but its "
                f"required field '{field}' is {type(value).__name__}, expected "
                f"{_type_label(hints[field])} -- a malformed envelope is "
                "rejected rather than graded (see docs/cli/signoff.md)"
            )


#: Issue #2198: the kinds :func:`_classify` recognises by a **pair** of
#: markers -- a primary marker that is unique to one verb's response, plus a
#: shape discriminator that was added to that response later. One entry per
#: such kind: ``(kind, primary field, the discriminators any one of which
#: completes the match)``.
#:
#: An envelope carrying the primary marker but none of the discriminators is
#: still refused (it is *not* graded as evidence of that kind -- the whole
#: point of pairing the markers), but it is refused as
#: :class:`EnvelopeVersionSkewError` /
#: :data:`_REASON_ENVELOPE_VERSION_SKEW` rather than as a generic
#: unrecognised shape, because that is overwhelmingly what it is: a
#: well-formed response from a `klt` that predates the discriminator.
#:
#: ``sta`` is the only such kind today. Its primary marker,
#: ``geometry_source``, shipped with the verb (#1959); its ``timing_status``
#: discriminator is additive (#1865/#1915), so every `klt sta` response
#: committed before that -- valid output from an already-stable verb -- has
#: exactly this shape. A kind whose markers are *both* original (e.g.
#: ``place-and-route``'s ``stage_reached``+``power``) has no version-skew
#: shape and belongs nowhere in this table: it is matched by its own branch
#: in :func:`_classify` long before the near-miss check runs.
_NEAR_MISS_MARKERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("sta", "geometry_source", ("timing_status", "corners")),
)


def _raise_unclassified(envelope: Mapping[str, Any], source: str) -> NoReturn:
    """Refuse an envelope no kind's full marker set matched, as either
    version skew or an unrecognised shape (issue #2198).

    Called only from :func:`_classify`'s final ``else``, so a response that
    legitimately classifies as some *other* kind can never be reported as a
    near-miss. Separate from :func:`_classify` so that near-miss detection
    does not add a branch to that already-branchy chain.
    """
    for kind, primary_field, discriminators in _NEAR_MISS_MARKERS:
        if isinstance(envelope.get(primary_field), str):
            missing = ", ".join(f"'{field}'" for field in discriminators)
            raise EnvelopeVersionSkewError(
                f"envelope '{source}' carries klt {kind}'s "
                f"'{primary_field}' marker but none of the fields that "
                f"identify its shape ({missing}) -- it looks like a klt "
                f"{kind} response written before those fields existed, so "
                f"it is refused rather than graded as {kind} evidence. "
                "Re-run the check with the current klt to produce a "
                "gradeable envelope (see docs/cli/signoff.md).",
                kind=kind,
                primary_field=primary_field,
                missing_fields=discriminators,
            )

    raise SignoffError(
        f"envelope '{source}' has an unrecognized shape (schema_version="
        f"{envelope.get('schema_version')!r}): not a klt drc/lvs/extract/sim/"
        "yield/pex/power/sta/functional-verification/erc/place-and-route "
        "success or error envelope, and not a generic evidence envelope "
        '("kind": "generic") either -- klt signoff aggregates those eleven '
        "verbs' output plus opt-in generic evidence today (see "
        "docs/cli/signoff.md)"
    )


def _classify_failure_reason(error: SignoffError) -> str:
    """The ``_REASON_*`` value a :func:`_classify` refusal renders as in
    ``--manifest`` grading (issue #2198): version skew is reported
    distinguishably from a genuinely unrecognised envelope, since the remedy
    ("re-run under a newer klt") is not the same."""
    if isinstance(error, EnvelopeVersionSkewError):
        return _REASON_ENVELOPE_VERSION_SKEW
    return _REASON_UNRECOGNIZED_ENVELOPE


def _classify(envelope: Mapping[str, Any], source: str) -> str:
    """Detect an envelope's kind from its own structural shape. Raises
    :class:`SignoffError` for an envelope that matches none of them.

    Order matters: an ``error`` envelope (any verb's own ``--format json``
    failure output) is checked first since a failed verb run carries none
    of the success-shape markers below. ``"generic"`` (issue #1152) is
    checked next, ahead of every native structural check, for the opposite
    reason every native kind is checked *after* it: a hand-rolled, non-`klt`
    envelope could plausibly carry any field name by coincidence (e.g. a
    caller-chosen ``"violations"`` key that has nothing to do with `klt
    drc`), so it is never inferred structurally like the eleven kinds below --
    only an explicit, literal ``"kind": "generic"`` self-declaration
    classifies as ``"generic"``, checked first so no such coincidence can
    ever misroute it into a native kind instead.

    Recognition is only half the boundary (issue #2033): once a kind is
    matched, the envelope is validated against that kind's declared shape
    (:func:`_validate_envelope`) before the kind is returned, so an envelope
    that *looks* like a `klt` verb's output but is missing a required field
    -- or carries one of the wrong type -- is rejected here rather than
    graded downstream. Callers already handle the raise: ``--manifest``
    grading turns it into an explicit ``"unrecognized_envelope"`` item (see
    :func:`_grade_evidence`), and :func:`build_signoff` surfaces it as the
    CLI's ordinary clean error exit.

    Version skew is distinguished from a genuinely foreign document (issue
    #2198): an envelope carrying a kind's *primary* marker but none of the
    discriminators it is paired with -- the shape a response written before
    an additive discriminator existed has -- raises
    :class:`EnvelopeVersionSkewError` (a :class:`SignoffError`), which
    ``--manifest`` grading renders as
    ``reason: "envelope_version_skew"`` instead. It is refused either way;
    what changes is that the report can now say "re-run this under a newer
    `klt`" rather than "this is not a `klt` artifact". See
    :data:`_NEAR_MISS_MARKERS` for which kinds have such a shape.
    """
    if "schema_version" not in envelope:
        raise SignoffError(
            f"envelope '{source}' has no 'schema_version' field -- not a "
            "recognized klt JSON envelope (see docs/json-contract.md)"
        )

    error = envelope.get("error")
    if isinstance(error, dict) and "message" in error:
        kind = "error"

    # "generic" (issue #1152): an opt-in envelope for evidence that is not a
    # `klt` verb's own output at all -- e.g. hand-rolled from a Markdown
    # characterization record. See this module's "Generic evidence
    # ingestion" docstring section and docs/cli/signoff.md for this shape's
    # full contract and which --manifest items may cite it.
    elif envelope.get("kind") == "generic":
        kind = "generic"

    elif isinstance(envelope.get("violations"), list):
        kind = "drc"

    elif isinstance(envelope.get("mismatches"), list):
        kind = "lvs"

    elif isinstance(envelope.get("measurements"), list) and "corner_count" in envelope:
        kind = "sim"

    # `klt yield` (issue #816, Phase 1a of epic #710): also carries a
    # `measurements` list, but never a `corner_count` (checked above first,
    # so the two can never collide) -- `measurement_count` plus a `source`
    # object are unique to this shape (docs/cli/yield.md's JSON schema).
    elif (
        isinstance(envelope.get("measurements"), list)
        and "measurement_count" in envelope
        and isinstance(envelope.get("source"), dict)
    ):
        kind = "yield"

    elif "device_count" in envelope and isinstance(envelope.get("nets"), list):
        kind = "extract"

    # `klt pex` (issue #871, Phase 2b of epic #706; shape ratified by #801,
    # "Define `klt pex`", `src/klayout_tools/pex.py`): a top-level `delta`
    # list (per-corner/per-spec-row schematic-vs-extracted comparisons) plus
    # `reference_netlist` (the schematic netlist compared against) is unique
    # to this shape -- it cannot collide with `sim` (detected by
    # `measurements`+`corner_count` above, checked first) or `extract`
    # (`device_count`+`nets`, checked just above). This recognition rule
    # predates #801 (it recognised a Curator-proposed provisional shape
    # ahead of the real command); #801's real output matches it exactly, so
    # no change was needed here once the real command shipped.
    elif isinstance(envelope.get("delta"), list) and "reference_netlist" in envelope:
        kind = "pex"

    # `klt power` (issue #1321, Phase 2 of epic #712; shape from #844/#845/
    # #846): a top-level `power_nets` list plus a `networks` list is unique
    # to this shape -- it cannot collide with `sim` (`measurements`+
    # `corner_count`), `yield` (`measurements`+`measurement_count`+`source`),
    # `extract` (`device_count`+`nets`), or `pex` (`delta`+
    # `reference_netlist`), all checked above. See this module's "`klt
    # power` (IR-drop/EM) evidence ingestion" docstring section.
    elif isinstance(envelope.get("power_nets"), list) and isinstance(
        envelope.get("networks"), list
    ):
        kind = "power"

    # `klt sta` (issue #1959; docs/cli/sta.md): a top-level
    # `geometry_source` string ("routed"/"placement_estimate"/
    # "netlist_estimate") is unique to this verb's response -- no other klt
    # verb emits that field at the top level -- and is always present, never
    # null. It is paired with a shape discriminator so a future response
    # carrying the field alone still fails loudly rather than being graded
    # as timing evidence: `timing_status` (the flat, single-corner shape) or
    # a `corners` list (the multi-corner `pdk.corners` shape). `klt
    # place-and-route`'s response also carries `worst_slack_ns`/
    # `timing_status`/`corners`, but no `geometry_source` -- it is a
    # different verb with a different (unrestricted) corner-sweep contract
    # and stays deliberately unrecognised, see this module's "Digital-flow
    # evidence" docstring section.
    elif isinstance(envelope.get("geometry_source"), str) and (
        "timing_status" in envelope or isinstance(envelope.get("corners"), list)
    ):
        kind = "sta"

    # `klt functional-verification` (issue #1959;
    # docs/cli/functional-verification.md): a top-level `tests` list (one
    # entry per cocotb test case) plus `test_count` is unique to this shape
    # -- it cannot collide with `drc` (`violations`), `lvs` (`mismatches`),
    # `sim`/`yield` (`measurements`), `extract` (`device_count`+`nets`),
    # `pex` (`delta`+`reference_netlist`), or `power`
    # (`power_nets`+`networks`), all checked above.
    elif isinstance(envelope.get("tests"), list) and "test_count" in envelope:
        kind = "functional-verification"

    # `klt erc` (issue #2025; docs/cli/erc.md): a top-level `gates` list plus
    # `gate_role` (the `stackup[0].name` echo) is unique to this shape -- no
    # other verb emits either field. Recognised since `klt erc` grew a
    # top-level `status` (`"clean"`/`"violations"`) and the shared
    # `provenance` block in issue #1968, which is what made its output
    # gradeable at all. See this module's "Power delivery (structural)"
    # docstring section.
    elif isinstance(envelope.get("gates"), list) and "gate_role" in envelope:
        kind = "erc"

    # `klt place-and-route` (issue #2025; docs/cli/place-and-route.md): a
    # top-level `stage_reached` string plus the always-present `power`
    # object is unique to this response -- no other verb emits either. The
    # `power` block is the whole reason this kind is recognised (T1 item
    # 11's digital branch asks whether the routed artifact was produced with
    # a PDN at all); this module still deliberately ignores the response's
    # own `worst_setup_slack_ns`/`corners` timing, whose corner set is the
    # PDK's full shipped list rather than a declared one -- see "Digital-flow
    # evidence" above on why `klt sta`, not this, is item 5's timing
    # evidence.
    elif isinstance(envelope.get("stage_reached"), str) and isinstance(
        envelope.get("power"), dict
    ):
        kind = "place-and-route"

    else:
        # Issue #2198: refused either as version skew (a kind's primary
        # marker with none of the discriminators it is paired with -- the
        # shape a response written before the discriminator existed has) or
        # as a genuinely unrecognised shape. Never graded as evidence either
        # way; the distinction is only in what the report says to do about
        # it. See :func:`_raise_unclassified`.
        _raise_unclassified(envelope, source)

    # Issue #2033: recognised is not the same as well-formed -- see
    # :func:`_validate_envelope` and this module's "Typed, runtime-validated
    # evidence ingestion" docstring section.
    _validate_envelope(kind, envelope, source)
    return kind


# --------------------------------------------------------------------------- #
# Envelope -> check
# --------------------------------------------------------------------------- #


def _critical_metric_blockers(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Mechanically evaluate every *declared* ``critical: true`` metric
    present in ``envelope``'s own ``metrics`` block (issue #1850, following
    on from #247's metric-namespace registry and #1847/#1848/#1849's
    per-verb adoption of it).

    Returns one entry per critical metric whose value violates its domain
    or quality polarity. Entries retain ``metric``, ``value``, and
    ``higher_is_better``; invalid values add ``domain`` and ``reason``.
    Nonfinite floats are represented as strings in diagnostics, though JSON
    input readers reject them before grading.
    An empty list means no critical metric blocked this envelope (including
    the common case of no ``metrics`` block at all, or a ``metrics`` block
    with no critical entries).

    This reads *any* registered ``critical`` metric generically, purely from
    :mod:`klayout_tools.metrics`'s own registry -- never hard-coded per-verb
    knowledge, so a future verb's newly-declared critical metric is picked
    up automatically the moment it starts emitting a ``metrics`` block,
    with no change needed here.

    Polarity is applied per the metric's own declared
    :attr:`~klayout_tools.metrics.MetricDef.higher_is_better`:

    - ``False`` (smaller is better): a positive value blocks. For declared
      nonnegative error counts this is exactly the nonzero case.
    - ``True`` (larger is better): a zero-or-lower value blocks.
    - ``None`` (no declared polarity): every ``critical: true`` metric
      registered as of this issue declares a polarity, so this case is not
      currently reachable -- but a nonzero value is treated as blocking on
      the same "0 is the only passing value" shape every declared critical
      metric shares today, rather than silently ignoring a future
      polarity-less critical metric.

    Unknown and noncritical metrics do not gate signoff. A known critical
    metric outside its declared domain always blocks, with its ``domain``
    and a stable ``reason`` code in the blocker. Valid signed measurements
    retain their quality polarity; only declared count domains reject
    negatives independently of that polarity.
    """
    metrics_block = envelope.get("metrics")
    if not isinstance(metrics_block, dict):
        return []

    blockers: list[dict[str, Any]] = []
    for name, value in metrics_block.items():
        if not is_registered(name):
            continue
        metric_def = get_metric(name)
        if not metric_def.critical:
            continue
        invalid_reason = metric_def.invalid_value_reason(value)
        if invalid_reason is not None:
            blockers.append(
                {
                    "metric": name,
                    "value": repr(value) if invalid_reason == "non_finite" else value,
                    "higher_is_better": metric_def.higher_is_better,
                    "domain": metric_def.domain,
                    "reason": invalid_reason,
                }
            )
            continue

        if metric_def.higher_is_better is False:
            failing = value > 0
        elif metric_def.higher_is_better is True:
            failing = value <= 0
        else:
            failing = value != 0

        if failing:
            blockers.append(
                {
                    "metric": name,
                    "value": value,
                    "higher_is_better": metric_def.higher_is_better,
                }
            )
    return blockers


def _critical_metric_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    blockers = _critical_metric_blockers(envelope)
    return {"critical_metric_blockers": blockers} if blockers else {}


def _build_check(kind: str, envelope: _EvidenceEnvelope, source: str) -> dict[str, Any]:
    """Render one already-classified, already-validated envelope (issue
    #2033: ``envelope`` is the typed union, not a bare ``dict[str, Any]`` --
    :func:`_classify` is what narrows it) into one ``checks[]`` entry."""
    status = envelope.get("status") if kind != "error" else "error"
    # Issue #1996: an envelope that states it checked nothing never counts as
    # a passing check, whatever its own `status` says -- see this module's
    # "Vacuous-verdict refusal" docstring section. Applied here rather than
    # inside `_check_passed` (which still answers only "what does this
    # envelope's own verdict say") so the tier-report path can render the
    # more specific `_REASON_NOTHING_CHECKED` instead of `check_failed`.
    coverage_refusal = _coverage_refusal(kind, envelope)
    return {
        "source": source,
        "kind": kind,
        "status": status,
        "passed": _check_passed(kind, envelope) and coverage_refusal is None,
        "detail": _detail(kind, envelope),
        "provenance": envelope.get("provenance"),
    }


def _sta_corner_timing_passed(fields: dict[str, Any]) -> bool:
    """Whether one `klt sta` corner's reported timing counts as closed
    (issue #1959).

    Two conditions, in order:

    1. ``timing_status == "constrained"`` -- every slack value in this scope
       is a real measurement, not OpenSTA's unconstrained-design sentinel
       (``1e+39``, ``docs/cli/sta.md``: "Require ``timing_status ==
       'constrained'`` before reading any slack number"). The sentinel is a
       *positive* number, so skipping this check would report "timing
       closed" for a design that was never timed at all.
    2. ``worst_slack_ns`` is a real number ``>= 0`` (a corner with no
       reported setup slack proves nothing), and ``worst_hold_slack_ns`` is
       either ``None`` (no hold path to measure -- e.g. a purely
       combinational design, per ``docs/cli/sta.md``) or a real number
       ``>= 0``.
    """
    if fields.get("timing_status") != "constrained":
        return False

    setup = fields.get("worst_slack_ns")
    if not isinstance(setup, (int, float)) or isinstance(setup, bool) or setup < 0:
        return False

    hold = fields.get("worst_hold_slack_ns")
    if hold is None:
        return True
    if not isinstance(hold, (int, float)) or isinstance(hold, bool):
        return False
    return hold >= 0


def _sta_timing_passed(envelope: dict[str, Any]) -> bool:
    """Whether a `klt sta` envelope reports closed timing across every
    corner it characterised (issue #1959).

    Handles both documented response shapes (``docs/cli/sta.md``): the
    multi-corner ``pdk.corners`` shape (a ``corners`` list, one entry per
    requested corner, with the per-corner timing fields inside it) and the
    flat single-corner shape (those same fields at the top level). An empty
    ``corners`` list never passes -- a characterization of zero corners
    proves nothing.

    The graded corner set is exactly the set the cited run itself declared,
    which is what keeps this rule scoped (see this module's "Digital-flow
    evidence" docstring section on the issue's corner-scoping finding):
    `klt signoff` never widens a claim to corners the claimant did not run,
    and never narrows one to a nominal corner the claimant did run.
    """
    corners = envelope.get("corners")
    if isinstance(corners, list):
        if not corners:
            return False
        return all(
            isinstance(corner, dict) and _sta_corner_timing_passed(corner)
            for corner in corners
        )
    return _sta_corner_timing_passed(envelope)


def _sta_worst_corner(envelope: dict[str, Any]) -> dict[str, Any]:
    """The `klt sta` corner whose reported timing decides the verdict --
    the entry with the lowest ``worst_slack_ns`` on a multi-corner
    (``pdk.corners``) response, or the envelope itself on the flat
    single-corner response (whose per-corner fields live at the top level).
    A corner with no reported setup slack sorts as the worst of all, since
    an unmeasured corner is never evidence of closure. ``{}`` when a
    ``corners`` list is present but carries no usable entry."""
    corners = envelope.get("corners")
    if not isinstance(corners, list):
        return envelope

    entries = [corner for corner in corners if isinstance(corner, dict)]
    if not entries:
        return {}

    def sort_key(entry: dict[str, Any]) -> tuple[int, float]:
        value = entry.get("worst_slack_ns")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return (0, float(value))
        return (-1, 0.0)

    return min(entries, key=sort_key)


def _sdf_metadata(envelope: dict[str, Any]) -> dict[str, Any]:
    """Read optional SDF metadata without trusting its container types."""
    environment = envelope.get("environment")
    if not isinstance(environment, dict):
        return {}
    sdf = environment.get("sdf")
    return sdf if isinstance(sdf, dict) else {}


def _is_sdf_annotated(envelope: dict[str, Any]) -> bool:
    """Whether a `klt functional-verification` envelope reports a run with
    back-annotated SDF timing (issue #1959).

    ``environment.sdf`` is ``null`` on an ordinary (zero-delay) run and an
    object carrying ``annotated: true`` on an annotated one -- per
    ``docs/cli/functional-verification.md``, "an annotated run is
    identifiable from the JSON alone", which is exactly the property item 7
    needs to tell a post-route gate-level re-simulation from the pre-layout
    regression its own checklist text excludes. Only literal JSON ``true``
    qualifies; missing or malformed optional metadata gives no annotation
    credit (issue #2131)."""
    return _sdf_metadata(envelope).get("annotated") is True


def _is_post_layout_evidence(kind: str, envelope: dict[str, Any]) -> bool:
    """Whether a passing, accepted-kind citation actually proves a
    **post-layout** run, for the items that require one
    (:data:`_ITEMS_REQUIRING_POST_LAYOUT_EVIDENCE`, issue #1959).

    - ``"functional-verification"``: only when the regression ran with
      back-annotated SDF timing (:func:`_is_sdf_annotated`) -- an
      unannotated run is the pre-layout zero-delay simulation item 7
      excludes.
    - Every other kind: ``True``. ``"pex"`` re-simulates an extracted
      netlist by construction, so a passing `klt pex` report is post-layout
      evidence definitionally; kinds an item does not accept at all never
      reach this check (see :func:`_grade_evidence`), so they are not this
      function's problem to reject.
    """
    if kind == "functional-verification":
        return _is_sdf_annotated(envelope)
    return True


def _functional_verification_passed(envelope: _FunctionalVerificationEnvelope) -> bool:
    """Require executed, consistent evidence even from old/external producers.

    The three outcome counts must exactly match the individual test records
    and sum to ``test_count``. Code-coverage collection is unrelated and
    optional; skipped tests are allowed alongside a passing executed test.
    """
    fields = ("test_count", "passed_count", "failed_count", "skipped_count")
    counts = {field: envelope.get(field) for field in fields}
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in counts.values()
    ):
        return False
    if (
        envelope.get("status") != "pass"
        or counts["passed_count"] == 0
        or counts["failed_count"] != 0
    ):
        return False
    tests = envelope.get("tests")
    if not isinstance(tests, list) or len(tests) != counts["test_count"]:
        return False
    observed = {"passed": 0, "failed": 0, "skipped": 0}
    for test in tests:
        if not isinstance(test, dict):
            return False
        status = test.get("status")
        if not isinstance(status, str) or status not in observed:
            return False
        observed[status] += 1
    return all(counts[f"{status}_count"] == count for status, count in observed.items())


def _erc_connectivity_state(envelope: dict[str, Any]) -> str | None:
    """The `klt erc` connectivity scope's own rollup row (issue #2179), or
    ``None`` when this envelope states no connectivity verdict at all.

    ``None`` for every envelope written before #2179 -- and for any that
    carries ``erc_status`` without the ``erc_coverage`` block backing it,
    or whose block is malformed/zero/unknown. That is the same "absence of
    the field is absence of evidence" rule
    :mod:`klayout_tools.coverage` applies everywhere else: an old `klt erc`
    report must keep grading byte-identically, so a *missing* connectivity
    scope can never be the thing that turns a refusal into a pass.

    The block is read through :func:`~klayout_tools.coverage.coverage_state`
    exactly as a top-level ``coverage`` block is, by handing it over as one
    -- so the second scope is validated by the same code as the first, and
    an unreadable one degrades to ``None`` rather than to a guess.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return None
    state = coverage_state({"coverage": block})
    return state if state not in ("legacy", "malformed", "zero", "unknown") else None


def _erc_passed(envelope: dict[str, Any]) -> bool:
    """:func:`_check_passed`'s ``"erc"`` branch (issue #2025/#2179).

    ``status == "clean"`` is the verdict this kind has always been graded
    on, and it is unchanged: it means both of the envelope's independent
    violation signals came back clean *and* at least one antenna level was
    actually graded.

    The second clause exists because that verdict is **structurally
    unreachable** on a PDK whose antenna-ratio limits `klt erc` does not
    carry -- every PDK but sky130 today, and every run that omits ``--pdk``
    (``docs/cli/erc.md``, "Sky130 antenna-ratio limits"). There, *every*
    level comes back ``"unchecked"`` for *every* layout, so the common
    rollup rule correctly refuses to call the antenna question a pass and
    ``status`` is ``"not_checked"`` no matter what the design does. Grading
    that as a plain failure would mean the connectivity rules
    (``erc.unconnected_net``/``erc.supply_short``/
    ``erc.multiply_driven_net``/``erc.floating_gate``/``erc.missing_tie``)
    -- which need no PDK at all, and are the only thing `klt erc` can
    report on such a PDK -- could never back a citation, however clean.

    So a ``"not_checked"`` envelope passes on its *connectivity* roll-up
    alone, and only when that roll-up is a real, positively-stated one:
    ``erc_status == "clean"`` **and** an ``erc_coverage`` block that reached
    a verdict of its own (:func:`_erc_connectivity_state`). What this is
    deliberately not: a way for an antenna *violation* to pass (that is
    ``status == "violations"``, which matches neither clause), and not a
    way for a pre-#2179 envelope to start passing (it states no
    connectivity verdict, so the second clause cannot fire).
    """
    status = envelope.get("status")
    if status == "clean":
        return True
    if status != "not_checked":
        return False
    return (
        envelope.get("erc_status") == "clean"
        and _erc_connectivity_state(envelope) is not None
    )


def _coverage_refusal(kind: str, envelope: dict[str, Any]) -> str | None:
    """Why ``envelope`` reached no usable verdict at all, or ``None``.

    :func:`~klayout_tools.coverage.coverage_refusal_reason` applied to the
    envelope's own top-level ``coverage`` block -- the vacuous-verdict gate
    (issue #1996, see this module's "Vacuous-verdict refusal" section) --
    with one kind-specific widening.

    A `klt erc` envelope carries **two** coverage scopes (issue #2179): the
    antenna one at ``coverage`` and the connectivity one at
    ``erc_coverage``. The gate's question is "does this artifact contain any
    statement about the design at all", and a run whose connectivity rules
    all executed plainly does -- even when its antenna scope checked
    nothing, which is the permanent steady state on a PDK with no
    antenna-ratio table. Refusing it on the antenna scope alone would make
    the widened :func:`_erc_passed` unreachable through both entry points
    (:func:`_build_check` and :func:`_kind_gated_refusal` both AND this in),
    so the two are decided together here rather than drifting apart.

    Applied through both entry points on purpose: envelope-aggregation mode
    and numbered-citation grading must never disagree about whether an
    artifact is empty. Every other kind is unaffected -- their envelopes
    carry no second scope, so this is exactly
    :func:`~klayout_tools.coverage.coverage_refusal_reason`.
    """
    refusal = coverage_refusal_reason(envelope)
    if refusal is None or kind != "erc":
        return refusal
    return None if _erc_connectivity_state(envelope) is not None else refusal


def _check_passed(kind: str, envelope: _EvidenceEnvelope) -> bool:
    """Whether this one check counts as passing.

    ``envelope`` is the typed union declared above (issue #2033), already
    validated against ``kind``'s shape by :func:`_classify` -- so every
    field this function reads below is guaranteed present and of the
    declared type, rather than being defended against inline.

    Independent of every kind-specific rule below (issue #1850): if the
    envelope's own ``metrics`` block (issue #247/#1847) carries any
    registered ``critical: true`` metric whose value fails its declared
    ``higher_is_better`` polarity -- see :func:`_critical_metric_blockers`
    -- this check never passes, even when the kind-specific rule below
    would otherwise have counted it as passing. This mechanism is generic
    over every kind and every declared critical metric, not specific to
    ``drc``.

    - ``drc`` passes on ``status == "clean"``. Its ``coverage`` block is
      **not** consulted (issue #2002): a "clean" verdict from a deck with
      rule-free drawn layers or skipped rules passes exactly like one from a
      fully-covering deck. Those fields are surfaced, not graded -- see
      :func:`_drc_coverage_disclosure` and this module's "DRC coverage
      surfacing" docstring section for why the disclosure ``docs/design-
      evidence-tiers.md`` item 3 requires stays claimant-enforced.
    - ``lvs`` passes on ``status == "match"`` *and* (issue #1965, closing the
      gap left by #1952/#1964's additive ``power_connectivity`` block) its
      ``power_connectivity.status`` is not ``"mismatch"``. ``"unchecked"``
      (every non-``gate-level-verilog`` reference form, or a
      ``gate-level-verilog`` reference with the check explicitly disabled
      via ``options.power_connectivity: false``) still passes -- it means
      "does not apply here", not "unverified". An envelope with no
      ``power_connectivity`` key at all (committed before #1964 landed)
      also still passes -- ``.get("power_connectivity") or {}`` treats a
      missing block the same as ``"unchecked"`` rather than raising or
      retroactively failing old evidence. This is a deliberate hard-fail,
      not an opt-in or disclosure-only gate: #1952's own motivation was
      that "a T1 claim citing gate-level LVS ... is citing signal
      connectivity, not full LVS" -- so a `klt signoff` check labeled
      ``lvs`` should mean the compare *and* (when it applies) the
      power/ground wiring were both clean, matching what
      ``docs/cli/lvs.md`` already tells callers to gate on. Its
      ``body_verification`` block (issue #1983) is **not** consulted: a
      ``"match"`` from a layout whose MOS bodies were compared against a
      deck-synthesized net passes exactly like one whose bodies resolved to
      real drawn ties. That field is surfaced, not graded -- see this
      module's "Device-body bias surfacing" docstring section for why
      hard-fail is the right default for a power miswire and the wrong one
      for a body-tie coverage gap.
    - ``sim`` passes on ``status == "pass"``.
    - ``yield`` (issue #870) passes on ``status == "pass"`` (every
      measurement that declared a ``target_yield`` met it at the stated
      confidence) or ``status == "reported"`` (no measurement declared one,
      so nothing could fail -- docs/cli/yield.md's ``status`` field). A
      ``"fail"`` status (a declared ``target_yield`` was not supported) does
      not pass.
    - ``extract`` has no independent pass/fail: ``klt extract`` either
      produces a ``status: "extracted"`` envelope or raises (which surfaces
      here as the ``error`` kind, not a JSON envelope at all) -- so a
      present extract envelope is definitionally a successful extraction,
      always ``True``. It is still listed in ``checks[]`` (not dropped) so
      its ``provenance`` block participates in the consistency check below
      and its device/net counts are visible in the aggregated verdict.
    - ``pex`` (issue #871; shape ratified by #801, `klt pex`) passes on
      ``status == "pass"`` -- mirrors ``sim``: every graded delta row met
      its tolerance. Its ``body_bias`` block (issue #1983) is **not**
      consulted: a run whose extracted side had no DC bias path for its
      device bodies passes exactly like one that did. That field is
      surfaced (``detail.body_bias``, and ``citation.body_bias`` on a
      ``"met"`` item), not graded -- see this module's "Device-body bias
      surfacing" docstring section.
    - ``power`` (issue #1321) has no top-level ``status`` field at all
      (unlike every kind above -- ``docs/cli/power.md``'s JSON schema), so
      this derives a verdict from ``em_verdict`` instead: passes only when
      a static IR-drop solve actually ran (``em_verdict`` is not ``None`` --
      a spec declaring neither ``pads`` nor a ``current_model`` produces no
      solve at all, per ``docs/cli/power.md``, and proves nothing) *and*
      that solve's own EM current-density verdict rolled up to exactly
      ``em_verdict["status"] == "pass"``. A rolled-up ``"fail"`` (a checked
      edge exceeded its declared current-density limit), ``"not_checked"``
      (nothing in the whole spec had both a declared current limit and a
      solved current, so nothing was actually verified), or
      ``"pass_partial"`` (issue #1997 -- some edge in the design was never
      checked at all, even though every checked edge passed) does not
      pass, same as a missing ``em_verdict``.
      ``worst_case_droop_mv`` is not itself compared here -- the envelope
      declares no droop *limit* to check it against (see this module's
      "`klt power` (IR-drop/EM) evidence ingestion" docstring section) --
      it is surfaced informationally in :func:`_detail` instead.
    - ``sta`` (issue #1959) carries no pass/fail ``status`` of its own at
      all (``docs/cli/sta.md``: ``status`` is always ``"ok"``), so this
      derives a verdict from the reported timing -- see
      :func:`_sta_timing_passed`: every reported corner must be
      ``timing_status == "constrained"`` with non-negative setup and hold
      slack.
    - ``functional-verification`` passes on ``status == "pass"`` with
      consistent per-test counts, at least one passing test and no failures
      (skips are allowed) -- see :func:`_functional_verification_passed`.
      Whether the run was SDF-annotated is *not* a
      pass/fail input here (an unannotated regression is a perfectly valid
      pre-layout check); it only gates item 7, in :func:`_grade_evidence`.
    - ``erc`` (issue #2025/#2179) passes on ``status == "clean"`` -- the
      roll-up of both violation signals a `klt erc` envelope carries (zero
      ``erc_findings`` **and** no ``levels[].verdict == "violate"``, per
      ``docs/cli/erc.md``: "This is what `klt signoff` reads as this
      command's pass/fail verdict") -- or, when that roll-up could not be
      reached at all because the run's PDK has no antenna-ratio table, on
      the connectivity half's own ``erc_status``; see :func:`_erc_passed`.
      Note that T1 item 11 deliberately does
      **not** grade an ERC citation on this verdict -- it grades the
      specific supply-continuity rules the item names, so an antenna
      violation on some unrelated signal net does not block a power-delivery
      claim (see :func:`_grade_power_delivery`). This rule is what
      envelope-aggregation mode (:func:`build_signoff`) uses.
    - ``place-and-route`` (issue #2025) passes on ``status == "ok"`` -- a
      run that completed. Like ``extract``, this is a "the command ran"
      verdict, not a quality one: the response's own negative slack is
      expected, not an error (``docs/cli/place-and-route.md``), and nothing
      here turns a timing number into a pass/fail (that is `klt sta`'s job,
      see "Digital-flow evidence" above). Because it cannot fail on
      quality, this kind is opt-in per item exactly like ``generic`` --
      only T1 item 11 accepts it, and only for its ``power`` block.
    - ``generic`` (issue #1152) passes on ``status == "pass"`` -- the
      envelope's own, caller-asserted verdict; unlike every native kind
      above, nothing here re-derives that verdict from any other field,
      since a generic envelope's content is not otherwise defined.
    - ``error`` never passes.
    """
    if kind == "drc":
        passed = envelope.get("status") == "clean"
    elif kind == "lvs":
        power_connectivity = envelope.get("power_connectivity") or {}
        passed = (
            envelope.get("status") == "match"
            and power_connectivity.get("status") != "mismatch"
        )
    elif kind == "sim":
        passed = envelope.get("status") == "pass"
    elif kind == "yield":
        passed = envelope.get("status") in ("pass", "reported")
    elif kind == "extract":
        passed = True
    elif kind == "pex":
        passed = envelope.get("status") == "pass"
    elif kind == "power":
        em_verdict = envelope.get("em_verdict")
        passed = isinstance(em_verdict, dict) and em_verdict.get("status") == "pass"
    elif kind == "sta":
        passed = _sta_timing_passed(envelope)
    elif kind == "functional-verification":
        passed = _functional_verification_passed(envelope)
    elif kind == "erc":
        passed = _erc_passed(envelope)
    elif kind == "place-and-route":
        passed = envelope.get("status") == "ok"
    elif kind == "generic":
        passed = envelope.get("status") == "pass"
    else:
        passed = False  # kind == "error"

    if passed and _critical_metric_blockers(envelope):
        return False
    return passed


def _drc_coverage_disclosure(
    kind: str, envelope: dict[str, Any]
) -> dict[str, Any] | None:
    """The three ``coverage`` fields ``docs/design-evidence-tiers.md`` item 3
    requires a DRC claim to disclose, quoted verbatim off the cited envelope
    (issue #2002) -- or ``None`` when there is nothing to quote.

    ``None`` in exactly two cases, and they mean the same thing to a reader
    ("this artifact makes no coverage statement"), never "the deck has no
    gaps":

    - ``kind != "drc"``. No other envelope kind carries a ``coverage`` block
      of this shape, and item 3 is the only item whose doc text names these
      fields. (`klt lvs`'s own coverage-shaped disclosures are deliberately
      out of scope -- see this module's "DRC coverage surfacing" docstring
      section.)
    - The envelope has no ``coverage`` key at all, or a non-object one:
      evidence committed before ``klt drc`` reported coverage. Back-compat is
      the whole reason this is ``None`` rather than a dict of empty lists --
      an old envelope must not read as a deck that checked everything.

    Individual fields are normalised to a list (``[]`` for a missing or
    non-list value) so a consumer never has to re-do that check; this is a
    *shape* normalisation of a present ``coverage`` block, not a fabricated
    coverage claim for an absent one.
    """
    if kind != "drc":
        return None
    coverage = envelope.get("coverage")
    if not isinstance(coverage, dict):
        return None

    def _field(name: str) -> list[Any]:
        value = coverage.get(name)
        return list(value) if isinstance(value, list) else []

    return {
        "layers_in_stream_without_rules": _field("layers_in_stream_without_rules"),
        "rules_skipped": _field("rules_skipped"),
        "deck_scope": _field("deck_scope"),
    }


def _nothing_checked_reasons(envelope: dict[str, Any]) -> list[str] | None:
    """The reason codes behind ``envelope``'s own "I checked nothing"
    statement (issue #1996) -- or ``None`` when it makes no such statement.

    ``None`` (not ``[]``) for every envelope whose ``coverage`` block is
    absent, non-object, or reports ``nothing_checked: false``, so a caller
    can gate on the *presence* of a claim with a plain ``is None`` test and
    never has to decide what an empty list means. That covers evidence
    committed before the convention existed, and every kind that does not
    emit a ``coverage`` block at all -- neither reads as a vacuous run.

    Deliberately kind-agnostic, unlike :func:`_drc_coverage_disclosure` and
    :func:`_pex_body_bias_disclosure` beside it: ``nothing_checked`` is one
    shared convention across every verb that can reach a vacuous verdict
    (`klt drc`, `klt sim`, `klt pex` today), so hard-coding the list here
    would silently ignore the next verb that adopts it. Reading is delegated
    to :mod:`klayout_tools.coverage` so the "missing block makes no
    statement" rule is applied identically by every consumer.

    A run may report more than one reason; the list is returned in the
    producing verb's own order, verbatim -- including any code this `klt`
    build does not itself know, so a newer producer's reason survives.
    """
    if not coverage_nothing_checked(envelope):
        return None
    return coverage_nothing_checked_reasons(envelope)


def _nothing_checked_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    """:func:`_detail`'s ``nothing_checked_reasons`` key (issue #1996), or an
    empty dict when ``envelope`` makes no vacuous-run claim.

    A merge-in fragment rather than a mutation of the caller's dict, matching
    how every other optional detail key beside it is decided -- and keeping
    the "is this key present at all?" branch in one named place rather than
    adding a fifth conditional to :func:`_detail` itself.
    """
    reasons = _nothing_checked_reasons(envelope)
    detail = {} if reasons is None else {"nothing_checked_reasons": reasons}
    state = coverage_state(envelope)
    if state != "legacy":
        detail["coverage_state"] = state
    if state == "malformed":
        detail["coverage_error"] = coverage_validation_error(envelope.get("coverage"))
    partial = _partial_coverage_disclosure(envelope)
    if partial is not None:
        detail["coverage_qualification"] = partial
    return detail


def _partial_coverage_disclosure(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """The cited envelope's own statement that it skipped requested work
    (issue #2109) -- or ``None`` when it makes no such statement.

    ``None`` for every row of the common rollup table except ``partial``,
    including a legacy envelope whose verb-specific fields describe a gap
    (`klt drc`'s ``rules_skipped``, surfaced separately and unchanged by
    :func:`_drc_coverage_disclosure`). Re-reading those here would relabel
    pre-contract data as a common partial claim, which is exactly the
    "missing legacy coverage is not evidence either way" rule
    :mod:`klayout_tools.coverage` exists to apply identically everywhere.

    Quoted, never graded on: a `klt signoff` verdict still rests on the
    producer's own status token (see :func:`_non_passing_reason`). Until a
    verb's Phase 2 adapter lands, that token can still be the kind's
    unconditional success word on a run whose common coverage says
    ``partial`` -- and when it is, this key is what makes the gap visible in
    the report rather than silent, on both the ``checks[]`` and the
    ``"met"``-citation paths.
    """
    skipped = coverage_skipped_work(envelope)
    if not skipped:
        return None
    return {"reason": _REASON_PARTIAL_COVERAGE, "skipped": skipped}


def _reports_partial_status(kind: str, envelope: dict[str, Any]) -> bool:
    """Whether ``envelope``'s own status token is ``kind``'s *partial* one
    (issue #2109) -- the producer applied the common rollup rule and landed
    on :data:`~klayout_tools.coverage.RESULT_PARTIAL`.

    Matched against :data:`_PARTIAL_STATUS_BY_KIND`, and for ``power``
    inside ``em_verdict`` rather than at the top level, mirroring exactly
    where :func:`_check_passed` reads each kind's success token from -- so
    the two can never disagree about which field carries the verdict.
    """
    expected = _PARTIAL_STATUS_BY_KIND.get(kind)
    if expected is None:
        return False
    if kind == "power":
        em_verdict = envelope.get("em_verdict")
        return isinstance(em_verdict, dict) and em_verdict.get("status") == expected
    return envelope.get("status") == expected


def _non_passing_reason(kind: str, envelope: dict[str, Any]) -> str:
    """Why a non-passing envelope of a kind this item accepts is not
    evidence: :data:`_REASON_PARTIAL_COVERAGE` or
    :data:`_REASON_CHECK_FAILED` (issue #2109).

    Real failures keep precedence over the partial token, by two different
    mechanisms -- only one of which is structural (issue #2152):

    - *Producer-side*, by construction: a run that found a defect reports
      its *failure* token, never the partial one
      (:func:`~klayout_tools.coverage.coverage_rollup` decides ``failed``
      before it ever consults coverage), so those two tokens are disjoint
      at the producer and reading which one was reported is enough.
    - *Signoff-side*, by the ordering below: :func:`_check_passed` fails an
      envelope off :func:`_critical_metric_blockers` (issue #1850)
      regardless of what its ``status`` says, so a partial token and a
      failing registered ``critical: true`` metric can and do co-occur on
      one envelope. That case is decided **here**, not at the producer --
      the blocker is a real, mechanically-detected defect, so it must not
      be reported as a mere coverage gap, and the check for it therefore
      runs *before* the status token is read.
    """
    if _critical_metric_blockers(envelope):
        return _REASON_CHECK_FAILED
    if _reports_partial_status(kind, envelope):
        return _REASON_PARTIAL_COVERAGE
    return _REASON_CHECK_FAILED


def _kind_is_accepted(check_kind: str, allowed_kinds: set[str] | None) -> bool:
    """Whether the item :func:`_grade_evidence` is grading accepts a citation
    of ``check_kind`` at all -- ``True`` for an unrestricted item
    (``allowed_kinds is None``, see :func:`_allowed_kinds_for`).

    Both of :func:`_grade_evidence`'s kind-gated refusals consult this before
    firing, for the same reason: a citation of a kind the item does not accept
    must say "cite a different artifact" (``wrong_kind``, applied downstream by
    :func:`_build_tier_item`) rather than name a defect in the artifact that
    *was* cited -- issue #826's invariant that the two stay distinguishable.
    """
    return allowed_kinds is None or check_kind in allowed_kinds


def _kind_gated_refusal(
    check_kind: str,
    envelope: dict[str, Any],
    *,
    allowed_kinds: set[str] | None,
    require_post_layout: bool,
) -> str | None:
    """The reason :func:`_grade_evidence` must refuse this *passing*,
    right-kind envelope -- or ``None`` when it has no such reason.

    Both refusals resolved here apply only to a kind the item accepts (see
    :func:`_kind_is_accepted`), so they are decided in one place rather than
    repeating that gate per condition:

    - :data:`_REASON_NOTHING_CHECKED` (issue #1996) -- the envelope's own
      ``coverage`` block reports ``nothing_checked: true``, so its passing
      ``status`` was measured over nothing at all.
    - :data:`_REASON_NOT_POST_LAYOUT` -- the run is not the post-layout run
      the item requires.

    ``nothing_checked`` is checked first: an empty report is not post-layout
    evidence either, and "this measured nothing" names the more fundamental
    of the two problems. Both are ordered *after* :func:`_check_passed` in
    the caller, so a run that both failed and measured nothing is reported as
    the failure it is.
    """
    if not _kind_is_accepted(check_kind, allowed_kinds):
        return None
    refusal = _coverage_refusal(check_kind, envelope)
    if refusal is not None:
        return refusal
    if require_post_layout and not _is_post_layout_evidence(check_kind, envelope):
        return _REASON_NOT_POST_LAYOUT
    return None


def _pex_body_bias_disclosure(
    kind: str, envelope: dict[str, Any]
) -> dict[str, Any] | None:
    """The cited `klt pex` envelope's own statement about whether the
    extracted netlist it re-simulated had a DC bias path for every device
    body (issue #1983) -- or ``None`` when there is nothing to quote.

    Quoted verbatim off the envelope, never re-derived: `klt pex` builds it
    from the extraction it drove itself (``docs/cli/pex.md`` ->
    ``body_bias``), which is the only place the information exists.

    ``None`` in exactly two cases, and they mean the same thing to a reader
    ("this artifact makes no body-bias statement"), never "the bodies were
    biased":

    - ``kind != "pex"``. No other envelope kind carries a ``body_bias``
      block. (`klt lvs`'s parallel ``body_verification`` block is surfaced
      separately, as ``detail.body_verification_status`` -- it answers a
      related but distinct question: whether the *compare* verified the body
      ties, not whether the *simulated* netlist had a bias path.)
    - The envelope has no ``body_bias`` key at all, or a non-object one:
      post-layout evidence committed before `klt pex` reported it.
      Back-compat is the whole reason this is ``None`` rather than a
      fabricated ``"biased"`` -- an old record must not read as a netlist
      that was checked and found clean.

    ``unbiased_pmos_body_nets`` is deliberately **not** carried through: the
    per-device list can run to hundreds of entries on a real block, and a
    reader who needs it has the cited envelope. The count and the distinct
    net names are enough to tell a clean run from a compromised one and to
    find the devices in the source artifact.
    """
    if kind != "pex":
        return None
    body_bias = envelope.get("body_bias")
    if not isinstance(body_bias, dict):
        return None
    nets = body_bias.get("unbiased_nets")
    return {
        "status": body_bias.get("status"),
        "unbiased_device_count": body_bias.get("unbiased_device_count"),
        "unbiased_nets": list(nets) if isinstance(nets, list) else [],
    }


def _detail(kind: str, envelope: _EvidenceEnvelope) -> dict[str, Any]:
    """A small, kind-specific excerpt of the source envelope -- not a
    re-export of the full contract (a consumer that wants the raw
    ``violations[]``/``mismatches[]``/``devices[]``/``corners[]`` detail
    should read the original envelope file directly, exactly as
    ``report.py``'s ``sections[]`` documents for the same reason).

    Additionally (issue #1850), when :func:`_critical_metric_blockers` finds
    at least one registered ``critical: true`` metric in ``envelope``'s
    ``metrics`` block failing its declared polarity, this appends a
    ``critical_metric_blockers`` key -- naming exactly which metric(s)
    forced :func:`_check_passed` to ``False`` -- to every kind's detail dict
    (including ``error``, though a critical metric alongside an ``error``
    kind is not expected in practice). Absent when there are no blockers,
    so this stays purely additive to every existing detail shape."""
    if kind == "drc":
        detail: dict[str, Any] = {
            "file": envelope.get("file"),
            "deck": envelope.get("deck"),
            "violation_count": envelope.get("violation_count"),
        }
        # Issue #2002: what this "clean" was measured *inside*, quoted from
        # the envelope's own `coverage` block -- reported, never graded on
        # (see this module's "DRC coverage surfacing" docstring section).
        # Omitted entirely for an envelope committed before `coverage`
        # existed, so old evidence renders exactly as it did before.
        coverage = _drc_coverage_disclosure(kind, envelope)
        if coverage is not None:
            detail["coverage"] = coverage
    elif kind == "lvs":
        detail = {
            "layout": envelope.get("layout"),
            "reference": envelope.get("reference"),
            "mismatch_count": envelope.get("mismatch_count"),
            "counts": envelope.get("counts"),
            # Issue #1965: surfaced alongside the hard-fail gate in
            # `_check_passed` so a `"fail"` verdict caused by a power
            # miswire (rather than an ordinary signal mismatch) is visible
            # without re-opening the source envelope. `None` on an
            # envelope with no `power_connectivity` key at all (committed
            # before #1964), distinct from the real `"unchecked"`/
            # `"match"`/`"mismatch"` values.
            "power_connectivity_status": (envelope.get("power_connectivity") or {}).get(
                "status"
            ),
            # Issue #1983: whether the cited compare's MOS body terminals
            # were resolved from real drawn geometry or from a
            # deck-synthesized net (`klt lvs`'s `body_verification` block).
            # Reported, never graded on -- see this module's "Device-body
            # bias surfacing" docstring section. `None` on an envelope with
            # no `body_verification` key at all (committed before #1983),
            # distinct from the real `"verified"`/`"unverified"`/
            # `"unchecked"` values.
            "body_verification_status": (envelope.get("body_verification") or {}).get(
                "status"
            ),
        }
    elif kind == "sim":
        detail = {
            "netlist": envelope.get("netlist"),
            "corner_count": envelope.get("corner_count"),
            "passed": envelope.get("passed"),
            "failed": envelope.get("failed"),
            "errored": envelope.get("errored"),
        }
    elif kind == "yield":
        source = envelope.get("source") or {}
        detail = {
            "samples": envelope.get("samples"),
            "limits": envelope.get("limits"),
            "measurement_count": envelope.get("measurement_count"),
            "source_kind": source.get("kind"),
            "sample_count": source.get("sample_count"),
        }
    elif kind == "extract":
        detail = {
            "file": envelope.get("file"),
            "deck": envelope.get("deck"),
            "device_count": envelope.get("device_count"),
            "net_count": envelope.get("net_count"),
        }
    elif kind == "pex":
        detail = {
            "netlist": envelope.get("netlist"),
            "reference_netlist": envelope.get("reference_netlist"),
            "corner_count": envelope.get("corner_count"),
            "passed": envelope.get("passed"),
            "failed": envelope.get("failed"),
            "errored": envelope.get("errored"),
        }
        # Issue #1983: whether the netlist these numbers were measured on
        # actually had a DC bias path for every device body -- reported,
        # never graded on (see this module's "Device-body bias surfacing"
        # docstring section). Omitted entirely for a `pex` envelope
        # committed before `body_bias` existed, so old evidence renders
        # exactly as it did before.
        body_bias = _pex_body_bias_disclosure(kind, envelope)
        if body_bias is not None:
            detail["body_bias"] = body_bias
    elif kind == "power":
        em_verdict = envelope.get("em_verdict") or {}
        detail = {
            "file": envelope.get("file"),
            "spec": envelope.get("spec"),
            "worst_case_droop_mv": envelope.get("worst_case_droop_mv"),
            "em_verdict_status": em_verdict.get("status"),
            "em_verdict_fail_count": em_verdict.get("fail_count"),
            "em_verdict_checked_edge_count": em_verdict.get("checked_edge_count"),
        }
    elif kind == "sta":
        # Both documented response shapes reduce to the same excerpt: the
        # multi-corner shape reports the worst corner (the one that decides
        # the verdict) plus how many corners were characterised; the flat
        # single-corner shape reports its own fields with `corner_count`
        # None, so a reader can tell "one corner, unreported" from "N
        # corners" without re-parsing the envelope.
        worst = _sta_worst_corner(envelope)
        detail = {
            "def_path": envelope.get("def_path"),
            "verilog_path": envelope.get("verilog_path"),
            "geometry_source": envelope.get("geometry_source"),
            "spef_path": envelope.get("spef_path"),
            "corner_count": (
                len(envelope["corners"])
                if isinstance(envelope.get("corners"), list)
                else None
            ),
            "worst_corner": worst.get("corner"),
            "worst_slack_ns": worst.get("worst_slack_ns"),
            "worst_hold_slack_ns": worst.get("worst_hold_slack_ns"),
            "timing_status": worst.get("timing_status"),
        }
    elif kind == "functional-verification":
        sdf = _sdf_metadata(envelope)
        detail = {
            "hdl_toplevel": envelope.get("hdl_toplevel"),
            "testbench": envelope.get("testbench"),
            "test_count": envelope.get("test_count"),
            "passed_count": envelope.get("passed_count"),
            "failed_count": envelope.get("failed_count"),
            "skipped_count": envelope.get("skipped_count"),
            # Whether this regression ran against back-annotated SDF timing
            # -- informational here, load-bearing for item 7 (see
            # :func:`_is_post_layout_evidence`).
            "sdf_annotated": _is_sdf_annotated(envelope),
            "sdf_corner": sdf.get("corner"),
        }
    elif kind == "erc":
        detail = _erc_detail(envelope)
    elif kind == "place-and-route":
        detail = _place_and_route_detail(envelope)
    elif kind == "generic":
        detail = {
            "summary": envelope.get("summary"),
            "source": envelope.get("source"),
        }
    else:
        # kind == "error"
        error = envelope.get("error") or {}
        detail = {"command": error.get("command"), "message": error.get("message")}

    detail.update(_critical_metric_detail(envelope))
    # Issue #1996: why this envelope's own `coverage` block says the run
    # checked nothing -- present only when it makes that claim, so every
    # envelope predating the convention renders exactly as before. This is
    # the reader-facing half of the `passed: False` `_build_check` applies
    # for the same condition; see "Vacuous-verdict refusal" above.
    detail.update(_nothing_checked_detail(envelope))
    return detail


def _erc_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    """:func:`_detail`'s ``"erc"`` branch (issue #2025), split out so adding
    a kind does not push :func:`_detail`'s own dispatch chain up by the size
    of the branch as well as by the branch itself."""
    rule_counts: dict[str, int] = {}
    for finding in envelope.get("erc_findings") or []:
        rule = finding.get("rule") if isinstance(finding, dict) else None
        if isinstance(rule, str):
            rule_counts[rule] = rule_counts.get(rule, 0) + 1
    return {
        "file": envelope.get("file"),
        "spec": envelope.get("spec"),
        "gate_role": envelope.get("gate_role"),
        "gate_count": envelope.get("gate_count"),
        "erc_finding_count": envelope.get("erc_finding_count"),
        # The connectivity half's own roll-up (issue #2179), beside the
        # antenna-driven `status` this check's `checks[].status` already
        # reports. Surfaced rather than merely consulted because it is what
        # makes an otherwise contradictory-looking entry legible: a check
        # with `status: "not_checked"` and `passed: true` is a run on a PDK
        # with no antenna-ratio table whose connectivity rules all passed
        # (:func:`_erc_passed`), and this field is the evidence for that.
        # `None` for a pre-#2179 envelope, which states no such verdict.
        "erc_status": envelope.get("erc_status"),
        # Per-rule breakdown, so "which of the five ERC rules fired" is
        # readable without re-opening the envelope -- the same reason `klt
        # drc`'s own `rule_counts` exists. Item 11 grades only three of these
        # rules (see :func:`_erc_supply_findings`), so a reader needs to see
        # which ones actually fired.
        "erc_rule_counts": dict(sorted(rule_counts.items())),
    }


def _place_and_route_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    """:func:`_detail`'s ``"place-and-route"`` branch (issue #2025), split
    out for the same reason as :func:`_erc_detail`."""
    power = envelope.get("power") or {}
    straps = power.get("straps")
    return {
        "hdl_toplevel": envelope.get("hdl_toplevel"),
        "stage_reached": envelope.get("stage_reached"),
        "gds_path": envelope.get("gds_path"),
        "verilog_path": envelope.get("verilog_path"),
        # The `power` block is the only part of this response this module
        # reasons about -- T1 item 11's digital branch (issue #2025).
        # Surfaced verbatim so a "no PDN" verdict is readable beside the
        # artifact that caused it.
        "power_pdn": power.get("pdn"),
        "power_global_connect": power.get("global_connect"),
        "power_net": power.get("power_net"),
        "ground_net": power.get("ground_net"),
        "tapcell_master": power.get("tapcell_master"),
        "strap_layers": _strap_layers(envelope),
        "strap_count": len(straps) if isinstance(straps, list) else None,
    }


# --------------------------------------------------------------------------- #
# Provenance consistency (issue #251's provenance block; #309 AC #2)
# --------------------------------------------------------------------------- #


def _provenance_consistency(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Refuse to combine checks whose ``provenance`` blocks disagree about
    what was actually run.

    Four fields are compared, each only across the checks that actually
    populate it (``docs/json-contract.md``'s ``provenance`` block leaves a
    field ``None`` when a verb has nothing to report there -- e.g. ``klt
    sim`` simulates a netlist against a model library, has no input
    *layout* stream to pin, and so leaves ``provenance.input`` ``None`` and
    never participates in the ``input.content_hash`` comparison):

    - ``pdk.name`` / ``pdk.version`` -- every check that resolved a PDK
      must agree on which one and which release. A DRC report from a
      sky130 run combined with an LVS report from a gf180mcu run (or two
      sky130 runs against different PDK snapshots) is not one signoff.
    - ``input.content_hash`` -- populated by ``drc``/``extract``/``lvs``/
      ``pex`` (``docs/json-contract.md``); ``klt lvs`` joined them in issue
      #1969, which is what lets this comparison bind an LVS check to the
      very layout DRC ran on (issue #1987's second finding -- before that,
      ``klt lvs`` populated nothing here, so a clean DRC of last week's
      layout combined with a matching LVS of today's was "consistent" by
      omission). Compared **per ``input.role``** (issue #2027), the same way
      ``deck[<name>].content_hash`` is compared per deck name: checks that
      hashed a layout stream must agree with each other, and checks that
      hashed a netlist must agree with each other, but a netlist digest is
      never compared against a layout digest. Without that grouping, a
      ``klt lvs`` run in the pre-extracted ``layout.netlist`` shape (whose
      hash is of a *SPICE netlist*) could never agree with a ``klt drc``
      report of the same design, and this gate refused a perfectly
      consistent bundle. Within a role the rule is unchanged and unweakened:
      the whole point of "signoff" is that DRC, extraction and LVS ran
      against the *same* layout stream, not a stale pairing (the design
      doc's §1 "signoff rejection" failure mode). An envelope predating the
      verb's adoption of the field (``input`` absent or ``None``) is still
      excluded rather than forced into a mismatch -- ``None`` is "nothing
      to say", never "disagrees with everyone". An envelope predating
      ``role`` itself is read as ``"layout"``, the field's only documented
      meaning before #2027, so a mixed-vintage bundle of layout-hashing
      reports stays exactly as strict as it was.
    - ``deck[<name>].content_hash`` -- compared only among checks that name
      the *same* deck (an LVS run and a DRC run legitimately use different
      decks; two checks both naming ``"sky130"`` must be byte-identical).

    Checks with no ``provenance`` block at all (an ``error`` kind) or a
    ``None`` provenance are silently excluded from every comparison -- they
    already fail the check itself (see :func:`_check_passed`), they don't
    need to also fail the provenance gate to make the overall verdict
    ``"fail"``/``"refused"`` correctly reflect the problem.

    Returns ``{"ok": bool, "mismatches": [...]}`` -- see
    :func:`build_signoff`'s docstring for the ``mismatches[]`` shape.
    """
    mismatches: list[dict[str, Any]] = []

    _check_scalar_field(checks, mismatches, field=_PDK_NAME, path=("pdk", "name"))
    _check_scalar_field(checks, mismatches, field=_PDK_VERSION, path=("pdk", "version"))
    _check_input_hashes(checks, mismatches)
    _check_deck_hashes(checks, mismatches)

    return {"ok": not mismatches, "mismatches": mismatches}


def _check_scalar_field(
    checks: list[dict[str, Any]],
    mismatches: list[dict[str, Any]],
    *,
    field: str,
    path: tuple[str, str],
) -> None:
    """Append a ``mismatches[]`` entry for ``field`` if the checks that
    populate ``provenance.<path[0]>.<path[1]>`` don't all agree."""
    outer, inner = path
    entries: list[dict[str, Any]] = []
    for check in checks:
        provenance = check.get("provenance")
        if not isinstance(provenance, dict):
            continue
        block = provenance.get(outer)
        if not isinstance(block, dict):
            continue
        value = block.get(inner)
        if value is not None:
            entries.append({"source": check["source"], "value": value})

    distinct = {entry["value"] for entry in entries}
    if len(distinct) > 1:
        mismatches.append({"field": field, "values": entries})


def _check_input_hashes(
    checks: list[dict[str, Any]], mismatches: list[dict[str, Any]]
) -> None:
    """Append one ``mismatches[]`` entry per ``provenance.input.role`` whose
    ``content_hash`` disagrees across the checks declaring that role (issue
    #2027) -- the exact shape :func:`_check_deck_hashes` uses for deck names,
    for the same reason: a hash is only comparable against a hash of the same
    *kind* of artifact.

    ``klt drc``/``klt extract``/``klt pex`` hash an input layout stream, but
    ``klt lvs`` hashes whatever its ``request.layout`` named -- the original
    GDS/OASIS for the ``layout.file`` shape, and a *SPICE netlist* for the
    pre-extracted ``layout.netlist`` shape. Comparing those two digests
    without regard to role can only ever produce a mismatch, even when both
    describe the same design, which is a false alarm rather than caught
    staleness (the repo's own ``examples/signoff/`` pair reproduced it).

    A check whose ``input`` block predates the ``role`` field (or carries a
    non-string there) is read as :data:`_DEFAULT_INPUT_ROLE` -- ``"layout"``
    was the field's only documented meaning before #2027, so committed
    evidence from an older ``klt`` keeps participating in the layout-side
    comparison exactly as before instead of dropping into a bucket of its
    own and quietly weakening the gate.

    The emitted ``field`` stays ``"input.content_hash"`` for every role (the
    value ``docs/cli/signoff.md`` documents and consumers match on); the
    disagreeing role is carried in the additive ``role`` key beside it, so a
    bundle that disagrees on two roles at once produces two distinguishable
    entries.
    """
    by_role: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        provenance = check.get("provenance")
        if not isinstance(provenance, dict):
            continue
        block = provenance.get("input")
        if not isinstance(block, dict):
            continue
        content_hash = block.get("content_hash")
        if content_hash is None:
            continue
        role = block.get("role")
        if not isinstance(role, str):
            role = _DEFAULT_INPUT_ROLE
        by_role.setdefault(role, []).append(
            {"source": check["source"], "value": content_hash}
        )

    for role, entries in sorted(by_role.items()):
        distinct = {entry["value"] for entry in entries}
        if len(distinct) > 1:
            mismatches.append({"field": _INPUT_HASH, "role": role, "values": entries})


def _check_deck_hashes(
    checks: list[dict[str, Any]], mismatches: list[dict[str, Any]]
) -> None:
    """Append one ``mismatches[]`` entry per deck *name* whose
    ``content_hash`` disagrees across the checks naming it. Two checks
    naming different decks (e.g. a DRC deck vs. an LVS extraction deck)
    never collide with each other -- only same-named decks are compared."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        provenance = check.get("provenance")
        if not isinstance(provenance, dict):
            continue
        deck = provenance.get("deck")
        if not isinstance(deck, dict):
            continue
        name = deck.get("name")
        content_hash = deck.get("content_hash")
        if name is None or content_hash is None:
            continue
        by_name.setdefault(name, []).append(
            {"source": check["source"], "value": content_hash}
        )

    for name, entries in sorted(by_name.items()):
        distinct = {entry["value"] for entry in entries}
        if len(distinct) > 1:
            mismatches.append(
                {"field": f"deck[{name}].content_hash", "values": entries}
            )


# --------------------------------------------------------------------------- #
# Tier-verdict mode (issue #722, Phase 0 of epic #706)
# --------------------------------------------------------------------------- #


def build_tier_report(
    manifest: dict[str, Any], *, tiers_doc: str | Path | None = None
) -> dict[str, Any]:
    """Grade a block manifest against the T1-T4 evidence-tier item skeleton
    mechanically parsed from ``docs/design-evidence-tiers.md``
    (:mod:`.design_evidence_tiers`) -- the item list is never duplicated
    here, so tiers and this aggregator can never drift.

    ``tiers_doc`` (``klt signoff --tiers-doc``) points the parse at an
    explicit copy of that doc; with none given,
    :func:`~.design_evidence_tiers.default_doc_path` resolves it
    (``$KLT_TIERS_DOC``, then the copy bundled inside the installed package,
    then the source checkout's ``docs/`` -- issue #1050). ``source_doc`` in
    the result names the override when one is used, and the canonical
    ``docs/design-evidence-tiers.md`` otherwise. ``source_doc`` names *which*
    doc was read, not *what it said* -- two reports naming the same
    ``source_doc`` may still have been graded against a checklist that grew
    or reworded an item between the two runs (issue #2025 added item 11 to
    the doc without either report's ``source_doc`` changing). ``result
    ["source_doc_content_hash"]`` (issue #2175) closes that gap: the
    ``sha256:<hex>`` digest of the resolved doc's own bytes
    (:func:`~.design_evidence_tiers.parse_tier_doc`'s ``content_hash``,
    ``None`` only in the pathological case it documents), pinned exactly the
    way a ``content_hash``-carrying evidence entry already pins its own
    input -- so two committed reports can be diffed to tell whether a changed
    verdict came from changed evidence or a changed rulebook, and a caller
    can compare it against the checklist it currently ships to catch a stale
    read, entirely as its own policy (this function only emits the hash, it
    never fails a report for a mismatch).

    ``manifest`` (JSON in)::

        {
            "block": "my-block",              # optional, echoed back verbatim
            "kind": "analog" | "digital" | "mixed-signal",   # required
            # optional, "mixed-signal" only (issue #2278): what this block's
            # two partitions denote -- which nets/pins/cells belong to which
            # side, as the tiers doc's "Block kind" subsection requires a
            # mixed-signal claim to state. Echoed back verbatim, onto the
            # report and onto every row of the partition it names; never
            # graded. Either partition may be declared alone. See
            # :func:`_read_partition_boundary`.
            "partition_boundary": {
                "analog": "bandgap + LDO: nets vref/vbg, cells bg_core, ota",
                "digital": "trim SPI: nets sclk/sdi/csb, cells trim_ctl",
            },
            "evidence": {                      # optional, default {}
                "3": "drc.json",
                "4": {"file": "lvs.json", "content_hash": "sha256:..."},
                "5": {
                    "command": ["klt", "sim", "corners.json", "--format", "json"],
                    "cwd": "sim/",                  # optional, default: this cwd
                    "content_hash": "sha256:...",   # optional, same staleness gate
                },
                # issue #2342: cite an envelope nested inside a composite
                # report -- "pointer" is an RFC 6901 JSON Pointer, valid on
                # either binding, and everything downstream grades the value
                # it names exactly as if that value were the whole document.
                "7": {"file": "composed.json", "pointer": "/pex"},
                # for a mixed-signal block, per-kind items (1, 2, 5, 7) key
                # on "<item id>.<analog|digital>"; kind-independent items
                # (3, 4, 6, 8, 9, 10) may use the bare "<item id>" key and
                # are looked up by both partitions -- see the doc's "Block
                # kind" subsection for which items split which way.
            }
        }

    An evidence entry is either **file-backed** (a bare string, or
    ``{"file": ..., "content_hash": ...}`` -- Phase 0, issue #722: read a
    pre-existing ``klt`` JSON envelope off disk) or **command-backed**
    (``{"command": [<argv>, ...], "cwd": ..., "content_hash": ...}`` --
    Phase 1, issue #825: actually run the named gate -- ``klt
    drc``/``klt lvs``/``klt extract`` (netlist regeneration)/``klt sim``
    (corner sim) -- as a subprocess and grade *that run's* exit status and
    stdout). Either shape may also carry ``"pointer"`` (issue #2342), an RFC
    6901 JSON Pointer naming where inside the resolved document the envelope
    lives -- so a `klt drc` envelope stored under ``"drc"`` in a composition
    step's composite report is citable as itself, rather than only as the
    whole file that happens to contain it. See
    :func:`_normalize_evidence_entry`/:func:`_grade_evidence`.

    ``_grade_evidence`` grades an item's evidence by calling the same
    :func:`_check_passed` :func:`build_signoff` uses, so a registered
    ``critical: true`` metric (issue #1850) failing its declared
    ``higher_is_better`` polarity mechanically renders that item
    ``"unmet"`` with :data:`_REASON_CHECK_FAILED`, exactly as a failing
    ``status`` would -- no separate wiring needed in this function.

    Returns (JSON out)::

        {
            "schema_version": 1,
            "block": "my-block" | None,
            "kind": "analog",
            # Present only when the manifest declared one (issue #2278) --
            # a "mixed-signal" manifest's own statement of its partition
            # boundary, echoed verbatim and never graded.
            "partition_boundary": {"analog": "...", "digital": "..."},
            "tier": "T1" | None,
            "t1_item_count": 11,
            # How many T1 rows this build's *own* shipped doc would have
            # rendered (issue #2202) -- equal to `t1_item_count` unless
            # `--tiers-doc`/`$KLT_TIERS_DOC` points at a doc with a
            # different item list. `None` if the shipped doc is unreadable.
            "build_t1_item_count": 11,
            "t1_met_count": 3,
            "source_doc": "docs/design-evidence-tiers.md",
            "source_doc_content_hash": "sha256:...",
            # Which build graded this (issue #2176) -- `klt version`'s own
            # identity payload, so a committed report names the `klt` that
            # produced it.
            "build": {
                "version": "0.4.2+g0123456789ab",
                "package_version": "0.4.2",
                "git_commit": "0123456789ab...",
                "git_tag": None,
                "dirty": False,
                "is_release": False,
                # A content hash of the grading code itself (issue #2216) --
                # distinct from git_commit/git_tag, which identify the
                # checkout, not the grading rules. See
                # `~.build_identity.grading_ruleset_id`.
                "grading_ruleset_id": "sha256:...",
            },
            "items": [
                {
                    "tier": "T1",
                    "id": 3,
                    "title": "DRC clean",
                    "partition": None,
                    # `"partition_boundary": "<text>"` here too, on every row
                    # of a partition the manifest declared one for (issue
                    # #2278) -- absent otherwise, as above.
                    "text": "latest `klt drc` JSON report: ...",
                    "notes": [],
                    "status": "met",
                    "reason": None,
                    # Does this build have grading rules for this item at
                    # all (issue #2176)? Always True for the shipped doc.
                    "graded_by_build": True,
                    "citation": {
                        "file": "drc.json",
                        "command": None,
                        "kind": "drc",
                        "check_status": "clean",
                        "content_hash": "sha256:...",
                        # Was that hash checked against the artifact the
                        # envelope names, or only against the envelope's
                        # own claim about it (issue #2196)? Always present:
                        # True (re-hashed, matched) / False (re-hashed,
                        # disagreed) / None (nothing was re-hashed).
                        "input_verified": True,
                        "exit_status": 0,
                        # `drc` citations only, and only when the cited
                        # envelope reports coverage (issue #2002)
                        "coverage": {
                            "layers_in_stream_without_rules": ["met4/0"],
                            "rules_skipped": ["met5.4"],
                            "deck_scope": ["5.x", "6.x"],
                        },
                        # `pex` citations only, and only when the cited
                        # envelope reports body bias (issue #1983)
                        "body_bias": {
                            "status": "unbiased",
                            "unbiased_device_count": 148,
                            "unbiased_nets": ["\\$5"],
                        },
                    },
                },
                ...  # items 1-11 (doubled per partition for mixed-signal),
                     # then one entry per T2/T3/T4 ladder row
            ],
        }

    An item's ``status`` is ``"met"`` only when its ``evidence`` entry
    resolves to a *readable* ``klt`` JSON envelope, classifiable as one of
    ``drc``/``lvs``/``extract``/``sim``/``yield``/``pex``/``power``/``sta``/
    ``functional-verification``/``generic`` (:func:`_classify`), whose own
    check passed (:func:`_check_passed`) --
    though a ``"power"``-classified citation never actually reaches
    ``"met"`` for any item today (see "No T1 item accepts 'power' evidence"
    below) --
    and, if the evidence entry pinned an expected ``content_hash``, whose
    input content hash matches it (``provenance.input.content_hash`` for
    drc/lvs/extract/sim/pex/generic -- a ``generic`` envelope populates this
    only if its author chose to include a ``provenance`` block, see "Generic
    evidence ingestion" above; the hashed ``samples`` document for yield, per
    :func:`_yield_samples_content_hash` -- a mismatch means the check ran
    against a *different* layout/sample revision than the one being claimed
    -- stale, so it renders ``"unmet"``, never a false pass). Every other
    case (no evidence entry, a malformed entry, an unreadable/unparsable
    evidence file, a command that could not be run to completion or whose
    stdout didn't parse, an unrecognised envelope shape, a failing check, or
    (issue #871, extended by issue #1152) a passing check of a kind this
    item does not accept) also renders ``"unmet"``: this phase never infers
    a ``"met"`` verdict for an item with no runnable check behind it.

    **Items 3 and 4 are kind-restricted** (issue #1987): item 3 ("DRC
    clean") accepts only a ``"drc"`` citation and item 4 ("LVS clean") only
    an ``"lvs"`` citation, for every block kind -- any other recognised,
    passing kind (notably ``"extract"``, which :func:`_check_passed` never
    fails) renders ``"unmet"`` with ``reason: "wrong_kind"``.

    **Items 5, 6 and 8 are kind-restricted too** (issue #2044, extending
    #1987's reasoning to every remaining item that names evidence -- see
    this module's "Kind-restricting items 5, 6 and 8" docstring section):
    item 5 ("Full corner verification") accepts ``"sim"`` for an analog
    partition and ``"sta"``/``"functional-verification"``/``"sim"`` for a
    digital one (``"sim"`` being the full-custom digital sub-case's own
    artifact), item 6 ("Statistical claims carry Monte Carlo evidence")
    accepts only ``"yield"``, and item 8 ("Characterization report") accepts
    only ``"generic"`` -- the purpose-built envelope the doc gives the one
    item naming no `klt` verb. Items 1, 2, 9 and 10 stay unrestricted, as
    ``docs/design-evidence-tiers.md`` documents: they name no evidence at
    all, so there is nothing to bind them to.

    **Item 7 is kind-restricted, per block kind** (issue #871, Phase 2b of
    epic #706; made per-block-kind by issue #1959): items 1, 2, 9 and 10
    accept any recognised, passing envelope kind, but item 7 ("Post-layout
    verification") accepts only the kind(s) named by
    :data:`_ITEM_ALLOWED_KINDS` for the partition kind being graded
    (:func:`_allowed_kinds_for`) -- for an analog partition (or a
    mixed-signal block's analog partition), only a ``"pex"``-kind citation,
    the schematic-vs-extracted-netlist re-simulation delta a `klt pex`
    (Epic #709, issue #801, ``src/klayout_tools/pex.py``) run produces (see
    this module's "Post-layout binding" docstring section, and
    ``docs/cli/pex.md`` for `klt pex`'s own contract); for a digital
    partition, ``"pex"`` *or* a ``"functional-verification"`` citation that
    ran with back-annotated SDF timing (see "Digital-flow evidence" above --
    an unannotated run renders ``reason: "not_post_layout"``, not
    ``"wrong_kind"``). A ``drc``/``lvs``/``sim``/``extract``/``yield``/
    ``sta``/``generic`` citation for item 7 -- even a genuinely passing one
    -- renders ``"unmet"`` with ``reason: "wrong_kind"``, never a borrowed
    pass.

    **Only item 8 accepts ``"generic"`` evidence** (issue #1152): a
    ``"generic"``-kind citation (see "Generic evidence ingestion" above)
    satisfies only the T1 items whose own checklist text names no specific
    `klt` verb -- today, item 8 ("Characterization report") alone. Every
    other item, including the four unrestricted items 1, 2, 9 and 10 that
    accept any *native* recognised kind (items 3-8 carry their own kind
    restriction, see above), renders ``"unmet"`` with ``reason:
    "wrong_kind"`` for a ``"generic"`` citation -- this does not loosen
    items 3-7's own evidence requirements, only adds a new kind item 8 alone
    may satisfy. Since issue #2044 item 8's own ``allowed_kinds`` is
    ``{"generic"}`` as well, so the two mechanisms agree exactly rather than
    one being broader than the other.

    **Item 11 is compound, and per block kind** (issue #2025): "Power
    delivery (structural)" is the one T1 item no single artifact proves, so
    its ``evidence`` entry may be a **list** of ordinary evidence entries
    (each file- or command-backed) rather than one. The cited set must
    contain a `klt erc` supply-spec run and the same `klt lvs` report item 4
    grades; for an RTL-flow digital block it must additionally contain the
    `klt place-and-route` response proving a PDN was built
    (``power.pdn: true`` with a ``power.tapcell_master``), and that LVS
    report's ``power_connectivity.status`` must be ``"match"`` --
    ``"unchecked"`` satisfies item 4 but **not** item 11. Without a P&R
    citation (an analog block, or the doc's full-custom digital sub-case),
    the LVS half is instead that the reference netlist carried the supply
    nets. Item 11's four own ``reason`` values are listed below; see
    :func:`_grade_power_delivery` for the full rule. ``"erc"`` and
    ``"place-and-route"`` citations are accepted by item 11 alone
    (:data:`_OPT_IN_KIND_ITEMS`) -- for any other item they render
    ``"wrong_kind"``, exactly like a ``"generic"`` citation outside item 8.

    **No T1 item accepts ``"power"`` evidence** (issue #1321): `klt power`'s
    IR-drop/EM verdict (see this module's "`klt power` (IR-drop/EM) evidence
    ingestion" docstring section) is a recognised envelope kind, but no T1
    item's checklist text names it -- unlike ``"generic"``, which item 8
    alone accepts, :data:`_ITEMS_ACCEPTING_POWER_EVIDENCE` is empty, so a
    ``"power"`` citation for *any* item, including item 8, renders
    ``"unmet"`` with ``reason: "wrong_kind"``. `klt power` evidence is
    consumed only by :func:`build_signoff`'s envelope-aggregation mode
    today.

    **Every item says whether this build could grade it** (issue #2176):
    the item list comes from the parsed doc, the grading rules come from the
    running build, and ``tiers_doc``/``$KLT_TIERS_DOC`` exists precisely to
    let those two be different versions. So each T1 item carries
    ``graded_by_build`` (:func:`_is_graded_by_build`) -- ``True`` when a
    grading table names its id, or when this build's **own** shipped doc
    lists it; ``False`` for an item only the caller's doc knows about, whose
    accepted kinds, evidence shape and pass conditions are all absent from
    this build. A ``False`` item that the manifest nonetheless cites
    evidence for renders ``"unmet"`` with
    ``reason: "ungradeable_by_build"`` rather than falling through to the
    unrestricted ``allowed_kinds is None`` path, where any passing envelope
    would have produced a ``"met"`` from rules that do not exist here. With
    the shipped doc (no override, or an override that is the same item list)
    every item is ``True`` and nothing else changes. ``build`` names the
    grading build itself, in `klt version --format json`'s own shape, so a
    committed report still says what it could and could not check long after
    the terminal that produced it is gone.

    **And how much of the checklist it did not look at** (issue #2202): the
    other direction of the same divergence -- a doc *older* than the build,
    listing fewer items than this build grades -- produces no wrong verdict,
    because the report is the parsed doc's skeleton and the missing items are
    simply not rows. That makes it invisible: ``tier: "T1"`` awarded on 9/9
    items reads exactly like ``tier: "T1"`` awarded on 11/11, though it is a
    strictly weaker claim. ``build_t1_item_count``
    (:func:`_build_t1_row_count`) is what this build's own doc would have
    rendered, beside ``t1_item_count``'s what the parsed doc did, so the
    shortfall is a subtraction rather than a reconstruction from the
    ``build`` block by a reader who has the matching checkout. Equal for the
    shipped doc (and for any override with the same item list), and ``None``
    -- never a fabricated count -- when this build cannot read its own doc.
    Missing items are deliberately *not* rendered as rows: the report is the
    parsed doc's skeleton by design, and inventing rows it does not contain
    would destroy the property that makes the override meaningful at all.

    An ``"unmet"`` item's ``reason`` (issue #826, Phase 1b of epic #706)
    names *why*, machine-readably, so a reader never has to guess whether an
    item was skipped or actually failed:

    - ``"no_evidence"`` -- the manifest gave no ``evidence`` entry for this
      item at all.
    - ``"invalid_evidence"`` -- the manifest's entry for this item is
      present but malformed (neither a string, nor an object with a string
      ``"file"``, nor an object with a non-empty list-of-strings
      ``"command"``; or, issue #2342, one carrying a ``"pointer"`` that is
      not a string).
    - ``"unreadable_evidence"`` -- a file-backed entry's named file does not
      exist, is not readable, or is not valid JSON; or a command-backed
      entry's subprocess exited zero but its stdout was not valid JSON.
    - ``"unrecognized_envelope"`` -- the resolved evidence parsed as JSON
      but is not a JSON object, or is a JSON object that does not match any
      recognised ``klt`` envelope shape (:func:`_classify`).
    - ``"envelope_version_skew"`` (issue #2198) -- the resolved evidence is
      a ``klt`` envelope carrying a kind's *primary* marker (today:
      ``sta``'s ``geometry_source``) but none of the shape discriminators
      that marker is paired with (``timing_status``/``corners``), which is
      the shape a response written before those additive fields existed has.
      Refused exactly like an unrecognised shape -- it is never graded as
      that kind's evidence -- but reported distinguishably, because the
      remedy differs: re-run the check under the current ``klt``, rather
      than fix a citation that points at the wrong artifact. See
      :data:`_NEAR_MISS_MARKERS`.
    - ``"invalid_pointer"`` (issue #2342) -- the entry carried a
      ``"pointer"`` (an RFC 6901 JSON Pointer naming where inside the cited
      document the envelope lives) that did not resolve to a JSON object:
      malformed syntax, a path the document does not contain, or a value
      that is not an object. Distinct from ``"unrecognized_envelope"``,
      which says the resolved bytes are not a shape this build recognises:
      here nothing was ever classified, and the remedy is to fix the
      pointer, not the citation.
    - ``"command_failed"`` -- a command-backed entry's subprocess could not
      be launched, timed out, or exited nonzero *without* leaving a
      parseable envelope on stdout. A nonzero exit whose stdout *does* parse
      (e.g. a ``klt drc``/``klt lvs``/``klt sim`` extension exit code for a
      successful run that found a violation/mismatch/failure) is not this
      case -- it is graded by its envelope content instead, landing on
      ``"check_errored"`` or ``"check_failed"`` below.
    - ``"check_errored"`` -- the evidence resolved to a ``klt`` ``error``
      envelope: the underlying command itself failed to run to completion.
    - ``"check_failed"`` -- the evidence resolved to a recognised, non-error
      envelope, but that check's own verdict did not pass (e.g. DRC
      violations, an LVS mismatch, a failed sim corner).
    - ``"stale_evidence"`` -- the check passed, but its
      ``provenance.input.content_hash`` does not match the manifest's
      pinned ``content_hash`` -- the check ran against a different layout
      revision than the one being claimed.
    - ``"unverifiable_provenance"`` (issue #2182) -- the check passed, and
      the manifest pins a ``content_hash``, but the resolved envelope
      carries no input hash at all -- a ``functional-verification`` envelope
      (no ``provenance`` block by design) or an unprovenanced ``"generic"``
      envelope. Distinct from ``"stale_evidence"``: no revision was ever
      recorded to compare against, so the remedy is to re-produce the
      evidence with a producer that records provenance, not to re-run the
      same one again.
    - ``"nothing_checked"`` (issue #1996) -- the evidence resolved to a
      recognised, *passing* envelope whose own ``coverage`` block states that
      the run checked nothing at all (``coverage.nothing_checked: true``,
      the shared convention in :mod:`klayout_tools.coverage`): a DRC deck
      gated behind an unset ``--deck-var``, a `klt sim` corner matrix that
      expanded to zero corners, a `klt pex` run with no ``delta[]`` row.
      Grouped with the "no runnable check proves this item" reasons -- the
      cited check did not fail on its own terms, it simply measured nothing,
      so the fix is to re-run it with something to check. See this module's
      "Vacuous-verdict refusal" docstring section.
    - ``"wrong_kind"`` (issue #871, extended by issue #1152, extended by
      issue #1321, made per-block-kind by issue #1959) -- the evidence
      resolved to a recognised, *passing* envelope, but its classified kind
      is not one this item accepts: either item 7's per-block-kind
      restriction (see "Item 7 is kind-restricted, per block kind" above),
      a ``"generic"``-kind citation for any item other than item 8 (see
      "Only item 8 accepts 'generic' evidence" above), or a
      ``"power"``-kind citation for *any* item (see "No T1 item accepts
      'power' evidence" above). The cited check did not fail on its own
      terms; it simply does not prove what this item requires.
    - ``"not_post_layout"`` (issue #1959) -- the evidence resolved to a
      recognised, *passing* envelope of a kind this item does accept, but
      it does not prove a **post-layout** run (:func:`_is_post_layout_evidence`,
      see "Post-layout binding" above): today, only item 7's digital
      partition can render this -- a `klt functional-verification` citation
      that passed but ran without back-annotated SDF timing. Distinct from
      ``"wrong_kind"`` per issue #826's invariant: ``"wrong_kind"`` means
      "cite a different artifact"; ``"not_post_layout"`` means "re-run
      *this* artifact against the layout".
    - ``"no_pdn"`` (issue #2025, item 11 only) -- the cited `klt
      place-and-route` response says no power grid was built at all
      (``power.pdn`` is not ``true``, or no ``power.tapcell_master`` was
      placed). Re-run P&R with a ``request.power`` block.
    - ``"supply_spec_incomplete"`` (issue #2025, item 11 only) -- the cited
      `klt erc` run's own spec document does not ask the question item 11
      grades: unreadable, no ``"kind": "supply"`` net declared, no ``ties[]``
      declared with no disclosure of why (see
      ``"supply_spec_disclosed_unexpressible"`` below for the disclosed
      case) so ``erc.missing_tie`` was never computed, or a stackup that
      does not cover every strap layer the P&R response reports.
    - ``"supply_spec_disclosed_unexpressible"`` (issue #2234, item 11 only)
      -- the cited `klt erc` run declares zero ``ties[]``, exactly as
      ``"supply_spec_incomplete"``'s "no ``ties[]``" case, but its spec
      explicitly disclosed why no tap can be expressed on this stream
      (``ties_disclosure``). Still unmet -- a disclosure proves nothing
      about the tap's actual connectivity -- but distinguishable from
      "nobody declared ties at all". Fix: draw/name a tap.
    - ``"supply_spec_disclosed_tool_limitation"`` (issue #2247, item 11
      only) -- the same disclosed zero-``ties[]`` state, naming the other
      obstacle (``ties_disclosure.kind: "tool_limitation"``): the tap is
      expressible, but the `klt` build the evidence had to be produced on
      cannot grade a declared tie safely (issue #2169). Unmet on the same
      principle, and kept distinct from the reason above because the fix is
      a different *build*, not a different *layout*.
    - ``"supply_not_continuous"`` (issue #2025, item 11 only) -- the ERC run
      did ask, and the answer is no: a declared supply resolved to zero or
      several islands, two declared supplies resolved to one island, or a
      well/tub has no connected tap.
    - ``"lvs_supply_unproven"`` (issue #2025, item 11 only) -- the LVS half
      of the item is unproven: ``power_connectivity.status`` is not
      ``"match"`` (with a PDN citation), or the reference netlist did not
      carry the supply nets (without one).
    - ``"ungradeable_by_build"`` (issue #2176) -- the manifest cited
      evidence for an item this build has no grading rules for at all
      (``graded_by_build: False``, see above). Not a statement about the
      cited artifact, which is never even resolved: the *build* is what is
      missing, so the fix is a newer `klt` (or grading against the doc this
      one ships), not a different citation.
    - ``"tier_not_supported"`` -- a T2-T4 ladder row (see below): this
      repository has no mechanism to run a T2+ check at all.

    ``reason`` is ``None`` (and omitted from a plain-text reading, but
    always present as a JSON key) exactly when ``status`` is ``"met"``. The
    "no runnable check exists for this item" reasons above and the last are
    always distinct, in the JSON, from the "a check ran (or tried to run)
    and did not pass" reasons -- never collapsed into one ambiguous
    ``"unmet"`` with no further signal.

    A ``"met"`` item's ``citation`` always carries: the evidence file
    (``None`` for a command-backed entry -- no static file backs it), the
    executed command (the argv joined for display, ``None`` for a
    file-backed entry -- no command was run to produce it), the envelope's
    own status, its input content hash (``None`` when the envelope's own
    provenance doesn't populate one -- ``klt lvs``/``klt sim`` don't, per
    ``docs/json-contract.md``; for a ``klt yield`` envelope, which carries no
    ``provenance`` block of its own at all as of issue #816's current shape,
    this is instead the hash of the samples document it names -- see
    :func:`_yield_samples_content_hash`), ``input_verified`` (issue #2196:
    whether that input hash was itself checked against the artifact the
    envelope names -- ``True`` re-hashed and matched, ``False`` re-hashed
    and disagreed, ``None`` nothing was re-hashed, so the pinned hash was
    only ever compared to another claim; disclosure only, never graded on
    -- see :func:`_verify_input_artifact`), and ``exit_status``: for a file-backed entry
    this is *inferred* as ``0`` (a readable, classifiable, non-error
    envelope implies the producing command exited zero -- every ``klt``
    verb emits an ``error``-kind envelope, not a success envelope, on any
    nonzero exit); for a command-backed entry it is the subprocess's
    *actually observed* return code, never inferred.

    T2-T4 render as single ladder-row items (per the doc's "The ladder"
    table -- a Curator correction on issue #722: only T1 has an itemized
    checklist, T2-T4 are each one gated condition layered on top) and are
    always ``"unmet"`` in this phase (``reason: "tier_not_supported"``):
    this toolkit's closed loop targets T1, and T2+ require commercial
    tools/fab access this repo has no mechanism to check.

    ``tier`` is ``"T1"`` only when every rendered T1 item (across every
    partition, for a ``mixed-signal`` block) is ``"met"``; otherwise
    ``None`` -- there is no partial-credit tier.

    Raises :class:`SignoffError` if ``manifest`` is not a JSON object, its
    ``kind`` is missing or not one of ``analog``/``digital``/``mixed-signal``,
    its ``evidence`` field (when given) is not a JSON object, or its
    ``partition_boundary`` field (when given) is not a well-formed
    declaration for a ``mixed-signal`` block
    (:func:`_read_partition_boundary`). Raises
    :class:`~klayout_tools.design_evidence_tiers.DesignEvidenceTiersError`
    (re-exported here for convenient ``except`` handling alongside
    :class:`SignoffError`) if ``docs/design-evidence-tiers.md`` itself
    cannot be read or parsed.
    """
    if not isinstance(manifest, dict):
        raise SignoffError(
            f"block manifest must be a JSON object, got {type(manifest).__name__}"
        )

    kind = manifest.get("kind")
    if kind not in _BLOCK_KINDS:
        raise SignoffError(
            "block manifest 'kind' must be one of "
            f"{', '.join(repr(value) for value in _BLOCK_KINDS)} (got {kind!r})"
        )

    evidence = manifest.get("evidence", {})
    if not isinstance(evidence, dict):
        raise SignoffError(
            "block manifest 'evidence' must be a JSON object, got "
            f"{type(evidence).__name__}"
        )

    partition_boundary = _read_partition_boundary(manifest, kind)

    doc = parse_tier_doc(tiers_doc)
    partitions: tuple[str, ...] = (
        _MIXED_SIGNAL_PARTITIONS if kind == _PARTITIONED_BLOCK_KIND else (kind,)
    )
    # Issue #2176: the item ids *this build* was written against, so an item
    # the parsed doc lists but this build has no rules for is reported as
    # such instead of silently falling through to unrestricted grading.
    build_item_ids = _build_t1_item_ids()

    items: list[dict[str, Any]] = []
    met_count = 0
    total = 0
    for t1_item in doc["t1_items"]:
        graded_by_build = _is_graded_by_build(t1_item["id"], build_item_ids)
        for partition in partitions:
            if t1_item["id"] in _ITEMS_GRADED_AS_POWER_DELIVERY:
                # Item 11 (issue #2025) is the one T1 item no single
                # artifact proves -- graded against the *set* of evidence
                # entries cited for it, not one envelope. See
                # :func:`_grade_power_delivery`.
                entry = _build_power_delivery_item(
                    tier="T1",
                    item_id=t1_item["id"],
                    title=t1_item["title"],
                    text=_t1_item_text(t1_item, partition),
                    notes=list(t1_item["notes"]),
                    partition=partition if kind == "mixed-signal" else None,
                    partition_kind=partition,
                    partition_boundary=partition_boundary.get(partition),
                    evidence=evidence,
                    graded_by_build=graded_by_build,
                )
            else:
                entry = _build_tier_item(
                    tier="T1",
                    item_id=t1_item["id"],
                    title=t1_item["title"],
                    text=_t1_item_text(t1_item, partition),
                    notes=list(t1_item["notes"]),
                    partition=partition if kind == "mixed-signal" else None,
                    partition_boundary=partition_boundary.get(partition),
                    evidence=evidence,
                    allowed_kinds=_allowed_kinds_for(t1_item["id"], partition),
                    require_post_layout=(
                        t1_item["id"] in _ITEMS_REQUIRING_POST_LAYOUT_EVIDENCE
                    ),
                    graded_by_build=graded_by_build,
                )
            total += 1
            if entry["status"] == "met":
                met_count += 1
            items.append(entry)

    for ladder_row in doc["ladder"]:
        if ladder_row["tier"] == "T1":
            continue  # T1 is the itemized checklist above, not a ladder row
        items.append(
            {
                "tier": ladder_row["tier"],
                "id": None,
                "title": f"{ladder_row['tier']} — {ladder_row['name']}",
                "partition": None,
                "text": f"{ladder_row['claim']} ({ladder_row['demonstrated_by']})",
                "notes": [],
                "status": "unmet",
                "reason": _REASON_TIER_NOT_SUPPORTED,
                "citation": None,
            }
        )

    tier = "T1" if total > 0 and met_count == total else None

    return {
        "schema_version": TIER_REPORT_SCHEMA_VERSION,
        "block": manifest.get("block"),
        "kind": kind,
        # Issue #2278: the manifest's own statement of what its two
        # partitions denote, echoed verbatim -- present only when the
        # manifest declared one, so a report from a manifest that declares
        # nothing is byte-identical to the one this build rendered before the
        # field existed (and therefore does not read as `--check` drift on an
        # upgrade alone). Never graded: see `_read_partition_boundary`.
        **({"partition_boundary": partition_boundary} if partition_boundary else {}),
        "tier": tier,
        "t1_item_count": total,
        # Issue #2202: what this build's *own* doc would have rendered, so a
        # doc with fewer items than the build grades discloses the shortfall
        # instead of hiding it behind a full-looking `t1_met_count/
        # t1_item_count`. `None` when the shipped doc is unreadable.
        "build_t1_item_count": _build_t1_row_count(build_item_ids, len(partitions)),
        "t1_met_count": met_count,
        "source_doc": doc_source_label(tiers_doc),
        "source_doc_content_hash": doc["content_hash"],
        "build": _build_identity(),
        "items": items,
    }


def _read_partition_boundary(manifest: dict[str, Any], kind: str) -> dict[str, str]:
    """Validate and normalize a manifest's optional ``partition_boundary``
    declaration (issue #2278), returning ``{}`` when none was made.

    ``docs/design-evidence-tiers.md``'s "Block kind" subsection requires a
    mixed-signal claim to "state the partition boundary explicitly (which
    nets/pins/cells belong to which side) so a reviewer can tell which
    evidence covers which silicon". ``kind: "mixed-signal"`` already asserts
    that two partitions exist -- every T1 row of such a report carries
    ``"partition": "analog"``/``"digital"``, and an evidence key may select
    one with the ``"<id>.<analog|digital>"`` form -- but nothing in the
    manifest said what those two words denote for *this* block. This field is
    where that declaration lives::

        {
            "kind": "mixed-signal",
            "partition_boundary": {
                "analog": "bandgap + LDO: nets vref/vbg/vout, cells bg_core, ota",
                "digital": "trim SPI: nets sclk/sdi/csb, cells trim_ctl"
            }
        }

    It is a **declaration, not a verdict** -- reported, never graded, exactly
    like ``drc_coverage`` (issue #2002) and ``body_bias`` (issue #1983). A
    mixed-signal manifest that declares nothing still grades identically and
    can still reach ``tier: "T1"``; `klt signoff` has no way to check a
    free-text boundary against the silicon and must not pretend otherwise.
    Declaring only one of the two partitions is likewise allowed: a partial
    disclosure is worth carrying, and refusing it would make the honest
    half-statement unrepresentable.

    What *is* refused, as an authoring mistake rather than a missing
    disclosure (:class:`SignoffError`):

    - a declaration on an ``analog``/``digital`` manifest -- a single-partition
      block has no boundary to state, and silently dropping the field would
      leave the manifest looking like it made a disclosure it did not;
    - a non-object declaration, or an empty one;
    - a key that is not ``"analog"``/``"digital"`` (a typo'd partition name
      would otherwise vanish from the report with no signal);
    - a value that is not a non-blank string.

    The returned mapping is ordered by :data:`_MIXED_SIGNAL_PARTITIONS`, not
    by the manifest's own key order, so two manifests declaring the same
    boundary render the same report bytes.
    """
    raw = manifest.get("partition_boundary")
    if raw is None:
        return {}
    if kind != _PARTITIONED_BLOCK_KIND:
        raise SignoffError(
            "block manifest 'partition_boundary' is only meaningful for a "
            f"{_PARTITIONED_BLOCK_KIND!r} block, which grades one partition "
            f"per side (got kind {kind!r}, which has a single partition)"
        )
    if not isinstance(raw, dict):
        raise SignoffError(
            "block manifest 'partition_boundary' must be a JSON object keyed "
            f"by partition name ({_join_partitions()}), got "
            f"{type(raw).__name__}"
        )
    if not raw:
        raise SignoffError(
            "block manifest 'partition_boundary' must name at least one "
            f"partition ({_join_partitions()}), got an empty object"
        )
    for name, text in raw.items():
        if name not in _MIXED_SIGNAL_PARTITIONS:
            raise SignoffError(
                f"block manifest 'partition_boundary' key {name!r} is not a "
                f"partition of a {_PARTITIONED_BLOCK_KIND!r} block "
                f"({_join_partitions()})"
            )
        if not isinstance(text, str) or not text.strip():
            raise SignoffError(
                f"block manifest 'partition_boundary.{name}' must be a "
                "non-empty string stating which nets/pins/cells belong to "
                f"that side, got {text!r}"
            )
    return {name: raw[name] for name in _MIXED_SIGNAL_PARTITIONS if name in raw}


def _join_partitions() -> str:
    """``"'analog'/'digital'"`` -- the partition names an error message
    offers, derived from :data:`_MIXED_SIGNAL_PARTITIONS` so the message can
    never name a set the grading does not render."""
    return "/".join(repr(name) for name in _MIXED_SIGNAL_PARTITIONS)


def _allowed_kinds_for(item_id: int, partition_kind: str) -> set[str] | None:
    """Resolve which :func:`_classify` kinds may satisfy T1 item ``item_id``
    for the partition currently being graded (issue #1959).

    ``partition_kind`` is ``"analog"`` or ``"digital"`` -- for a
    ``"mixed-signal"`` manifest :func:`build_tier_report` grades one
    partition at a time, so the same per-block-kind rule applies to a
    mixed-signal block's digital partition as to a pure ``"digital"`` block,
    with no separate wiring.

    ``None`` means unrestricted (items 1, 2, 9 and 10 today -- every item
    that names no evidence at all, issue #2044). An item
    present in
    :data:`_ITEM_ALLOWED_KINDS` but with no entry for this partition kind
    falls back to the ``"analog"`` (strictest) set rather than becoming
    silently unrestricted -- an unrecognised partition kind must never
    *loosen* an item's evidence requirement.
    """
    by_partition_kind = _ITEM_ALLOWED_KINDS.get(item_id)
    if by_partition_kind is None:
        return None
    return by_partition_kind.get(partition_kind, by_partition_kind["analog"])


def _t1_item_text(t1_item: dict[str, Any], partition: str) -> str | None:
    """Select the body text a T1 checklist item shows for ``partition``:
    the matching column for a per-kind item (1, 2, 5, 7), or the item's
    single kind-independent body otherwise (3, 4, 6, 8, 9, 10)."""
    columns = t1_item["columns"]
    if columns:
        return columns.get(partition) or t1_item["text"]
    return t1_item["text"]


def _lookup_evidence(
    evidence: dict[str, Any], item_id: int, partition: str | None
) -> Any:
    """Look up a manifest ``evidence`` entry for ``item_id``, preferring a
    partition-qualified key (``"<id>.<partition>"``) over the bare
    ``"<id>"`` key -- so a mixed-signal manifest can give per-partition
    evidence for a per-kind item while still sharing one evidence entry
    across both partitions for a kind-independent item."""
    if partition:
        keyed = evidence.get(f"{item_id}.{partition}")
        if keyed is not None:
            return keyed
    return evidence.get(str(item_id))


def _normalize_evidence_pointer(raw: Any) -> tuple[str | None, bool]:
    """Normalize an evidence entry's optional ``"pointer"`` key (issue #2342)
    into ``(pointer, ok)``.

    ``pointer`` is the RFC 6901 JSON Pointer string the entry named, or
    ``None`` when the entry named none -- which includes the **empty**
    pointer ``""``, RFC 6901's "the whole document": an empty pointer and an
    absent one denote exactly the same citation, so they are normalized to
    the same ``None`` rather than to two paths that happen to agree. That
    keeps ``"pointer": ""`` a true no-op, right down to which ``_REASON_*``
    an unusable document renders (:data:`_REASON_UNRECOGNIZED_ENVELOPE`, as
    it always has -- never :data:`_REASON_INVALID_POINTER`, which would be a
    claim about a selector the caller did not really use).

    ``ok`` is ``False`` when the key is present but is not a string at all.
    That is deliberately **rejected** (the whole entry renders
    :data:`_REASON_INVALID_EVIDENCE`) rather than coerced to ``None`` the way
    a malformed ``content_hash`` is: dropping a malformed ``content_hash``
    drops a *constraint*, leaving a weaker-but-honest citation, whereas
    dropping a malformed ``pointer`` would silently re-aim the citation at
    the whole composite document -- a different artifact than the one the
    manifest meant to cite, which could then classify and pass on its own.
    A citation that cannot be read as written must never be quietly read as
    something else.
    """
    if "pointer" not in raw:
        return None, True
    pointer = raw.get("pointer")
    if not isinstance(pointer, str):
        return None, False
    if pointer == "":
        return None, True
    return pointer, True


def _normalize_evidence_entry(raw: Any) -> dict[str, Any] | None:
    """Normalize a manifest ``evidence[]`` entry into one of two shapes, or
    ``None`` if it matches neither -- a malformed *single* entry degrades
    that one item to ``"unmet"`` rather than aborting the whole report (see
    :func:`build_tier_report`'s docstring):

    - **File-backed** (Phase 0, issue #722): a bare file-path string, or
      ``{"file": <str>, "content_hash": <str>?, "pointer": <str>?}`` --
      returned as ``{"kind": "file", "file": ..., "content_hash": ...,
      "pointer": ...}``.
    - **Command-backed** (Phase 1, issue #825): ``{"command": [<str>, ...],
      "cwd": <str>?, "content_hash": <str>?, "pointer": <str>?}`` -- a
      non-empty list of strings is required; returned as ``{"kind":
      "command", "command": ..., "cwd": ..., "content_hash": ...,
      "pointer": ...}``. Checked before the file shape so a dict carrying
      both keys (which the schema does not ask for, but a caller might
      send) is treated as command-backed.

    ``"pointer"`` (issue #2342) is accepted on **both** shapes, and is always
    present in the returned spec (``None`` when the entry named none). It
    says *where inside* the cited document the `klt` envelope lives, so an
    envelope stored as a value inside a larger composite report -- a
    composition step's report carrying a `klt drc` envelope under ``"drc"``
    alongside its own findings -- is citable as itself. It is not
    file-specific: a command's stdout can equally be a composite document
    with more than one verb's report inside it. See
    :func:`_resolve_json_pointer` for the syntax and
    :func:`_resolve_evidence` for where it is applied.
    """
    if isinstance(raw, str):
        return {"kind": "file", "file": raw, "content_hash": None, "pointer": None}
    if not isinstance(raw, dict):
        return None

    pointer, pointer_ok = _normalize_evidence_pointer(raw)
    if not pointer_ok:
        return None

    command = raw.get("command")
    if (
        isinstance(command, list)
        and command
        and all(isinstance(part, str) for part in command)
    ):
        expected_hash = raw.get("content_hash")
        if expected_hash is not None and not isinstance(expected_hash, str):
            expected_hash = None
        cwd = raw.get("cwd")
        if not isinstance(cwd, str):
            cwd = None
        return {
            "kind": "command",
            "command": command,
            "cwd": cwd,
            "content_hash": expected_hash,
            "pointer": pointer,
        }

    file = raw.get("file")
    if not isinstance(file, str):
        return None
    expected_hash = raw.get("content_hash")
    if expected_hash is not None and not isinstance(expected_hash, str):
        expected_hash = None
    return {
        "kind": "file",
        "file": file,
        "content_hash": expected_hash,
        "pointer": pointer,
    }


def _cited_envelope(
    document: Any, pointer: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """Narrow a parsed evidence ``document`` to the single envelope the
    citation names, returning ``(envelope, None)`` or ``(None, <_REASON_*>)``.

    With no ``pointer``, the document *is* the envelope and must therefore be
    a JSON object -- the long-standing :data:`_REASON_UNRECOGNIZED_ENVELOPE`
    refusal, unchanged.

    With one (issue #2342), the envelope is the value at that RFC 6901 path
    (:func:`_resolve_json_pointer`) and the document itself may be any JSON
    value -- an array is a legal thing to point into. A pointer that does not
    resolve to a JSON object renders :data:`_REASON_INVALID_POINTER`, never
    :data:`_REASON_UNRECOGNIZED_ENVELOPE`: nothing was classified, so the
    reason must not read as a verdict on the cited bytes, and the two call
    for opposite remedies (fix the pointer vs. cite a different artifact).
    """
    if pointer is None:
        if not isinstance(document, dict):
            return None, _REASON_UNRECOGNIZED_ENVELOPE
        return document, None

    value, resolved = _resolve_json_pointer(document, pointer)
    if not resolved or not isinstance(value, dict):
        return None, _REASON_INVALID_POINTER
    return value, None


def _resolve_json_pointer(document: Any, pointer: str) -> tuple[Any, bool]:
    """Resolve ``pointer`` -- an RFC 6901 JSON Pointer -- against ``document``,
    returning ``(value, True)`` on success or ``(None, False)`` on any
    failure (issue #2342).

    Implements RFC 6901 as written, and nothing beyond it:

    - A non-empty pointer must start with ``"/"``; each subsequent
      ``"/"``-separated token names one step down.
    - Each token is unescaped ``~1`` -> ``"/"`` **then** ``~0`` -> ``"~"``,
      in that order (the order matters: the reverse would turn an escaped
      ``~1`` back into a separator). A ``~`` followed by anything other than
      ``0`` or ``1`` is malformed.
    - Against a JSON object, a token is a key, matched literally.
    - Against a JSON array, a token must be ``"0"`` or a digit string with
      no leading zero, and must be in range. ``"-"`` (RFC 6901's
      "past the last element") never resolves to an existing value, so it is
      a failure here.
    - Against any other value (a string, number, boolean, or ``null``) there
      is nothing to step into: failure.

    The empty pointer ``""`` -- "the whole document" -- never reaches this
    function: :func:`_normalize_evidence_pointer` normalizes it to ``None``
    (an absent pointer), which is the same citation by definition.

    Deliberately pure and total: no exception escapes, because a manifest is
    caller-supplied data and a mistyped pointer must degrade *that one item*
    to ``"unmet"``/:data:`_REASON_INVALID_POINTER`, never abort the report.
    """
    if not pointer.startswith("/"):
        return None, False

    current = document
    for raw_token in pointer.split("/")[1:]:
        token, ok = _unescape_json_pointer_token(raw_token)
        if not ok:
            return None, False
        if isinstance(current, dict):
            if token not in current:
                return None, False
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                return None, False
            index = int(token)
            if index >= len(current):
                return None, False
            current = current[index]
        else:
            return None, False
    return current, True


def _unescape_json_pointer_token(token: str) -> tuple[str, bool]:
    """Unescape one RFC 6901 reference token: ``~1`` -> ``"/"``, then ``~0``
    -> ``"~"``. Returns ``(unescaped, False)`` for a malformed escape -- a
    ``~`` that is last in the token or is followed by anything other than
    ``0``/``1`` -- which RFC 6901 leaves undefined and this module treats as
    an error rather than silently passing through (a pointer nobody can read
    as written must not be read as something else)."""
    out: list[str] = []
    index = 0
    while index < len(token):
        char = token[index]
        if char != "~":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(token):
            return "", False
        escaped = token[index + 1]
        if escaped == "0":
            out.append("~")
        elif escaped == "1":
            out.append("/")
        else:
            return "", False
        index += 2
    return "".join(out), True


def _normalize_evidence_parts(raw: Any) -> list[dict[str, Any]] | None:
    """Normalize a **compound** item's manifest entry (issue #2025, T1 item
    11 only) into a list of :func:`_normalize_evidence_entry` specs, or
    ``None`` if any part matches neither evidence shape.

    A JSON array is the compound shape: every element is an ordinary
    file-backed or command-backed evidence entry, and *all* of them must
    normalize -- one malformed part renders the whole item
    :data:`_REASON_INVALID_EVIDENCE` rather than being dropped from the set,
    since a silently-shortened cited set is exactly how a compound item would
    reach ``"met"`` without the artifact that was mistyped. An empty array is
    likewise invalid: it cites nothing, but says it cites something.

    A non-array entry is accepted too, as a one-element list -- so the
    ordinary ``"11": "erc.json"`` shape is a well-formed (if, on its own,
    insufficient) citation rather than a schema error.
    """
    if isinstance(raw, list):
        if not raw:
            return None
        specs = [_normalize_evidence_entry(part) for part in raw]
        if any(spec is None for spec in specs):
            return None
        return [spec for spec in specs if spec is not None]

    spec = _normalize_evidence_entry(raw)
    return None if spec is None else [spec]


def _resolve_relative_to_report(path: str, spec: dict[str, Any]) -> str | None:
    """Resolve ``path`` -- a document a **file-backed** evidence entry's own
    report *names* (not one the manifest names) -- against that report
    file's own directory, the way a reader who opened the report and
    followed its reference would.

    Returns ``None`` when there is no report file to be relative to (a
    command-backed ``spec`` -- :func:`_resolve_relative_to_spec` already
    resolves such a reference against the producing subprocess's own
    ``cwd``, the command-backed analogue of "the report's own directory";
    or a file-backed ``spec`` with no ``file`` recorded), or when ``path``
    is already absolute (nothing to rebase).

    Issue #2197: `klt yield`'s JSON report records the samples document path
    exactly as it was invoked with -- the ordinary ``klt yield
    mc-samples.json`` run made from inside its own evidence directory
    records ``"samples": "mc-samples.json"``, a path that resolves only from
    that directory. Grading a manifest from the repo root -- the only place
    a manifest with repo-relative evidence paths *can* be graded from --
    previously resolved that path against this process's own cwd instead,
    silently failing to find it. Resolving it against the report file's own
    directory first -- see :func:`_yield_samples_content_hash` -- fixes that
    without requiring the manifest or the `klt yield` invocation to change.
    """
    if os.path.isabs(path):
        return path
    if spec.get("kind") != "file":
        return None
    file = spec.get("file")
    if not file:
        return None
    return os.path.join(os.path.dirname(file), path)


def _yield_samples_content_hash(
    envelope: dict[str, Any], spec: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """The citation's ``content_hash`` for a ``klt yield`` evidence entry
    (issue #870, Phase 2a of epic #706).

    `klt yield`'s current JSON shape (issue #816, Phase 1a of epic #710)
    carries no `provenance` block of its own -- unlike drc/lvs/extract/sim,
    nothing inside the envelope names a content hash for the Monte Carlo
    sample document (``envelope["samples"]``) the report was computed from.
    Rather than leave a ``"met"`` yield citation with no input hash at all --
    the exact "assumed met" gap this module exists to close -- this hashes
    that referenced samples document directly, via the same ``sha256_file``
    helper every other kind's own `provenance` block already uses
    (``_provenance.py``).

    **Resolution order** (issue #2197): for a **file-backed** entry, the
    path is tried relative to the report file's own directory first (the way
    a reader opening the report and following its reference would --
    :func:`_resolve_relative_to_report`), then, if that does not exist,
    relative to this process's own current working directory (the previous,
    pre-#2197 behaviour, kept as a compatibility fallback for a samples
    document that genuinely lives elsewhere). For a **command-backed**
    entry, the path is resolved relative to ``spec["cwd"]`` -- the same
    directory the subprocess that produced ``samples`` ran in, so a relative
    path in the report resolves exactly as it did for that subprocess, and
    is unaffected by this issue (that directory is recorded independently of
    where `klt signoff` itself is invoked from).

    Returns ``(content_hash, unresolved)``. ``content_hash`` is ``None``
    when the envelope names no ``samples`` document (or it isn't a string),
    or when neither candidate location could be hashed (missing,
    unreadable) -- exactly mirroring ``sha256_file``'s own "unhashable
    input" fallback, never raising. ``unresolved`` is ``None`` unless a
    ``samples`` document *was* named but could not be found anywhere tried
    -- populated in that one case so a reader (and :func:`_citation`) can
    tell "this input could not be verified" apart from "no hash was ever
    recorded", per issue #2197 -- previously both collapsed into the same
    silent ``content_hash: null``.

    Follow-up reconciliation: if a later #710 phase adds its own
    ``provenance.input.content_hash`` to `klt yield`'s JSON shape, the
    generic ``input_block`` lookup in :func:`_grade_evidence` finds it first
    and this fallback simply never fires again -- no change needed here.
    """
    samples = envelope.get("samples")
    if not isinstance(samples, str):
        return None, None

    candidates: list[str] = []
    report_relative = _resolve_relative_to_report(samples, spec)
    if report_relative is not None:
        candidates.append(report_relative)
    cwd_relative = _resolve_relative_to_spec(samples, spec)
    if cwd_relative not in candidates:
        candidates.append(cwd_relative)

    for candidate in candidates:
        digest = sha256_file(candidate)
        if digest is not None:
            return f"sha256:{digest}", None

    return None, {"samples": samples, "searched": candidates}


#: Per kind, the top-level envelope field(s) naming the **input artifact**
#: whose bytes ``provenance.input.content_hash`` covers -- the file
#: :func:`_verify_input_artifact` re-hashes to check that self-reported hash
#: against the artifact itself (issue #2196).
#:
#: Each entry is the producing verb's own ``input_path=`` argument to
#: :func:`klayout_tools._provenance.build_provenance`, named by the field
#: that run echoes it back under -- so this table cannot claim to verify a
#: hash of something the envelope does not actually point at:
#:
#: - ``drc``/``extract``/``erc`` -- ``file``, the layout stream each ran on.
#: - ``lvs`` -- ``layout``: the *layout side* is what `klt lvs` hashes into
#:   ``provenance.input`` (issue #1969), either the GDS/OASIS stream of the
#:   ``layout.file`` request shape or the SPICE file of the pre-extracted
#:   ``layout.netlist`` one. ``reference`` is deliberately **absent**: the
#:   reference netlist is pinned under ``environment.reference_sha256``, a
#:   different digest, so re-hashing it here would compare two unrelated
#:   values and report a mismatch that is not one.
#: - ``sim`` -- ``netlist``, the SPICE deck it simulated.
#: - ``pex`` -- ``layout``: `klt pex` republishes its own `klt extract`
#:   run's ``provenance`` block verbatim (``pex.py``), and that run's input
#:   is this layout stream.
#: - ``sta`` -- ``def_path`` when the run was given a DEF, else
#:   ``verilog_path``, mirroring `klt sta`'s own
#:   ``input_path = def_path if has_def else verilog_path`` branch. Ordered,
#:   and only the first *present* field is consulted, so the two can never
#:   be crossed.
#:
#: A kind absent here is never re-hashed and reports ``input_verified:
#: null``: ``yield`` re-hashes its samples document already
#: (:func:`_yield_samples_content_hash` -- that path reports ``True`` on its
#: own); ``functional-verification``/``power``/``place-and-route``
#: populate no ``provenance.input`` at all today, or echo no path for
#: the artifact they hashed (`klt place-and-route` pins the gate-level
#: netlist it placed but echoes only its *outputs*); ``generic`` envelopes
#: choose their own field names by definition; ``error`` envelopes carry no
#: verdict to anchor.
_INPUT_ARTIFACT_FIELDS: dict[str, tuple[str, ...]] = {
    "drc": ("file",),
    "extract": ("file",),
    "erc": ("file",),
    "lvs": ("layout",),
    "sim": ("netlist",),
    "pex": ("layout",),
    "sta": ("def_path", "verilog_path"),
}


def _input_artifact_candidates(
    kind: str,
    envelope: Mapping[str, Any],
    spec: Mapping[str, Any],
    evidence_file: str | None,
) -> list[str]:
    """Every filesystem path the cited envelope's own input-artifact field
    (:data:`_INPUT_ARTIFACT_FIELDS`) could mean **from this grading
    context** -- ``[]`` when the kind names no such field, the field is
    absent, or its value is a shape that cannot be resolved here.

    A path an envelope names is not portable: it was written relative to
    whatever directory the producing run used, which is not necessarily the
    one grading happens in. Rather than guess a single interpretation, this
    returns each plausible one and lets :func:`_verify_input_artifact`
    prefer a *match* over a mismatch, so a coincidentally same-named file
    beside the envelope can never turn a genuinely fresh citation into a
    reported mismatch:

    - as the producing run itself named it -- :func:`_resolve_relative_to_spec`
      (``spec["cwd"]`` for a command-backed entry, this process's cwd
      otherwise), the same convention `klt drc --check`/`klt lvs --check`
      re-hash a committed report under;
    - relative to the **evidence file's own directory**, for a file-backed
      entry. This is the case the two ``--check`` modes document as a known
      limitation (``lvs.py``): ``klt lvs`` echoes ``layout``/``reference``
      exactly as the *request document* gave them, so evidence committed
      beside its inputs (``examples/signoff/`` -- ``lvs.json`` naming
      ``layout.spice``) is only resolvable this way.

    The ``{path, scope}`` shape (issue #1261 -- `klt sim`'s ``netlist``,
    `klt pex`'s ``layout``) is resolved against the repo root discovered
    from the evidence file's own location, which is what ``scope: "repo"``
    means by construction. ``scope: "external"`` carries no path at all (it
    is ``null`` on purpose, so a host-specific absolute path never lands in
    committed evidence) and is therefore unresolvable here -- correctly
    reported as "not verified" rather than guessed at.
    """
    evidence_dir = (
        os.path.dirname(os.path.abspath(evidence_file)) if evidence_file else None
    )
    for field in _INPUT_ARTIFACT_FIELDS.get(kind, ()):
        value = envelope.get(field)
        if value is None:
            # Not "unresolvable" -- this field simply was not the one the run
            # hashed (`klt sta`'s DEF/Verilog branch); try the next.
            continue
        candidates: list[str] = []
        if isinstance(value, str) and value:
            candidates.append(_resolve_relative_to_spec(value, spec))
            if evidence_dir is not None and not os.path.isabs(value):
                candidates.append(os.path.join(evidence_dir, value))
        elif isinstance(value, Mapping) and value.get("scope") == "repo":
            repo_relative = value.get("path")
            if isinstance(repo_relative, str) and repo_relative:
                root = find_repo_root(evidence_dir or spec.get("cwd") or os.getcwd())
                if root is not None:
                    candidates.append(os.path.join(root, repo_relative))
        # The first *present* field decides: it is the one the producing run
        # hashed, so falling through to another on a failed resolution would
        # verify the wrong artifact.
        return list(dict.fromkeys(candidates))
    return []


def _verify_input_artifact(
    kind: str,
    envelope: Mapping[str, Any],
    spec: Mapping[str, Any],
    recorded_hash: Any,
    evidence_file: str | None,
) -> bool | None:
    """Whether the envelope's self-reported ``recorded_hash`` still matches
    the **input artifact it names** -- ``True``/``False``/``None``, the
    ``input_verified`` disclosure issue #2196 adds to every ``"met"``
    citation. See this module's "Input-artifact verification" docstring
    section for what each value means.

    ``None`` (never raising, never fabricating) whenever no comparison was
    actually made: no recorded hash to check, no resolvable path for this
    kind, or no candidate path that resolves to a readable file here.
    ``False`` requires having genuinely read a named artifact and found it
    different -- which is why a *match* on any candidate wins over a
    mismatch on another (see :func:`_input_artifact_candidates`).

    Reuses :func:`~klayout_tools._provenance.sha256_file` and its
    ``sha256:``-prefixing convention rather than hashing a second way: a
    digest computed differently from the one the producing run recorded
    would report drift that is an artifact of this module, not of the
    evidence.
    """
    if not isinstance(recorded_hash, str) or not recorded_hash:
        return None
    read_any = False
    for candidate in _input_artifact_candidates(kind, envelope, spec, evidence_file):
        digest = sha256_file(candidate)
        if digest is None:
            continue
        read_any = True
        if f"sha256:{digest}" == recorded_hash:
            return True
    return False if read_any else None


def _grade_evidence(
    spec: dict[str, Any],
    *,
    require_post_layout: bool = False,
    allowed_kinds: set[str] | None = None,
) -> tuple[str, str | None, dict[str, Any] | None, dict[str, Any]]:
    """Grade one resolved evidence ``spec`` (:func:`_normalize_evidence_entry`)
    and return ``(status, reason, citation, failure_detail)``.

    ``failure_detail`` names any critical metrics that invalidated evidence;
    the citation remains absent for every unmet item.

    ``status`` is ``"met"`` or ``"unmet"``. ``reason`` is ``None`` when
    ``status == "met"``, otherwise one of the ``_REASON_*`` constants
    identifying exactly why this evidence does not back a ``"met"``
    verdict -- see :func:`build_tier_report`'s docstring for what each
    reason means. ``citation`` is populated only when ``status == "met"``.

    This is a pure grading step over an already-normalized evidence spec --
    it does not decide *whether* evidence was given at all (that is
    :func:`_build_tier_item`'s job, via :data:`_REASON_NO_EVIDENCE` /
    :data:`_REASON_INVALID_EVIDENCE`), only what a given spec proves once
    one *is* named.

    **File-backed** (``spec["kind"] == "file"``): reads the envelope off
    disk, exactly as Phase 0 (issue #722) did -- ``exit_status`` is
    *inferred* as ``0``.

    **Command-backed** (``spec["kind"] == "command"``, issue #825): actually
    runs the given argv as a subprocess (``cwd=spec["cwd"]`` when given),
    bounded by :data:`_COMMAND_EVIDENCE_TIMEOUT_S`. A launch failure or
    timeout renders :data:`_REASON_COMMAND_FAILED`. Stdout is parsed as JSON
    *before* the exit status is ever inspected -- exactly like the
    file-backed path, which does not gate on an inferred exit code at all --
    because a ``klt`` verb's own nonzero exit can mean "ran successfully and
    found a problem" (e.g. ``klt drc``'s ``EXIT_VIOLATIONS = 3``) just as
    easily as it can mean a genuine failure; per docs/json-contract.md, both
    shapes leave a documented envelope on stdout. If stdout parses, the
    envelope flows into :func:`_classify`/:func:`_check_passed` regardless of
    ``exit_status``, so :data:`_REASON_CHECK_ERRORED`/:data:`_REASON_CHECK_FAILED`
    work correctly whether the verb exited 0 or with an extension code. Only
    when stdout does *not* parse as JSON does ``exit_status`` matter: a zero
    exit with unparsable stdout renders :data:`_REASON_UNREADABLE_EVIDENCE`
    (the command-backed analogue of an unreadable evidence file); a nonzero
    exit with unparsable stdout renders :data:`_REASON_COMMAND_FAILED` (per
    the contract, an application-level error leaves stdout empty, so there is
    genuinely no evidence to grade). ``exit_status`` is the subprocess's
    *actually observed* return code, never inferred.

    **Content hash** (either binding): normally read from the resolved
    envelope's own ``provenance.input.content_hash``. A ``klt yield`` kind
    envelope carries no `provenance` block at all (issue #816's current
    shape) -- for that kind only, :func:`_yield_samples_content_hash`
    computes it instead, by hashing the samples document the report names,
    so the staleness gate (and the citation's ``content_hash``) still work
    for the statistical-evidence item, not just the four deterministic
    kinds. A ``"generic"`` envelope (issue #1152) gets no such fallback: its
    ``provenance`` block, when present, is read exactly like a native kind's
    (no generic-specific hashing), and when absent, ``actual_hash`` is
    simply ``None`` -- a manifest that pins ``content_hash`` for a generic
    entry with no matching provenance always renders that item
    ``"unmet"``/:data:`_REASON_UNVERIFIABLE_PROVENANCE`, per this function's
    own mismatch check below -- distinct from a genuine, non-``None`` hash
    mismatch, which still renders :data:`_REASON_STALE_EVIDENCE` (issue
    #2182: the two situations call for opposite remedies, see that
    constant's own docstring); a manifest that pins no ``content_hash`` at
    all is unaffected (no staleness claim was ever made).

    **Post-layout requirement** (issue #1959): ``require_post_layout`` is
    set for the items in :data:`_ITEMS_REQUIRING_POST_LAYOUT_EVIDENCE` (item
    7 today). A passing citation that is not post-layout evidence
    (:func:`_is_post_layout_evidence` -- today, a `klt
    functional-verification` run with no SDF back-annotation) renders
    :data:`_REASON_NOT_POST_LAYOUT`. ``allowed_kinds`` is consulted **only**
    to order that check correctly against :func:`_build_tier_item`'s own
    kind gate: a citation of a kind this item does not accept at all must
    report ``"wrong_kind"`` (cite a different artifact), not
    ``"not_post_layout"`` (re-run this artifact against the layout), so the
    post-layout gate is skipped for a kind that is about to be rejected as
    the wrong kind anyway.

    **Vacuous-verdict refusal** (issue #1996): a passing envelope whose own
    ``coverage`` block reports ``nothing_checked: true`` renders
    :data:`_REASON_NOTHING_CHECKED`, never ``"met"`` -- a report that
    measured nothing is not evidence, however clean its ``status``. Gated on
    ``allowed_kinds`` for the same ordering reason as the post-layout check
    above, and skipped entirely for any envelope that makes no such
    statement (including every envelope predating the convention). See this
    module's "Vacuous-verdict refusal" docstring section.
    """
    resolution, reason = _resolve_evidence(spec)
    if resolution is None:
        return "unmet", reason, None, {}

    check_kind = resolution["kind"]
    envelope = resolution["envelope"]

    checked = cast(_EvidenceEnvelope, envelope)

    if check_kind == "error":
        return "unmet", _REASON_CHECK_ERRORED, None, {}

    if not _check_passed(check_kind, checked):
        return (
            "unmet",
            _non_passing_reason(check_kind, envelope),
            None,
            _critical_metric_detail(envelope),
        )

    # The two refusals that apply only to a kind this item actually accepts --
    # `nothing_checked` (issue #1996) and `not_post_layout` -- resolved
    # together, in that order. See `_kind_gated_refusal`.
    kind_gated = _kind_gated_refusal(
        check_kind,
        envelope,
        allowed_kinds=allowed_kinds,
        require_post_layout=require_post_layout,
    )
    if kind_gated is not None:
        return "unmet", kind_gated, None, {}

    expected_hash = spec.get("content_hash")
    if expected_hash is not None and resolution["content_hash"] != expected_hash:
        if resolution["content_hash"] is None:
            return "unmet", _REASON_UNVERIFIABLE_PROVENANCE, None, {}
        return "unmet", _REASON_STALE_EVIDENCE, None, {}

    return "met", None, _citation(resolution), {}


def _resolve_evidence(
    spec: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve one normalized evidence ``spec`` to the envelope it names,
    without grading it -- returns ``(resolution, None)`` on success, or
    ``(None, <_REASON_* constant>)`` when the evidence could not be resolved
    to a recognised ``klt`` envelope at all.

    Split out of :func:`_grade_evidence` (issue #2025) so the compound
    item-11 path (:func:`_grade_power_delivery`) resolves its several cited
    parts through *exactly* the same read/run/classify/hash logic every other
    item's single citation goes through -- rather than a parallel
    reimplementation that could drift from it -- while still applying its
    own, item-specific pass rules to the resolved envelopes.

    The ``resolution`` dict carries everything a caller needs to both grade
    and cite the evidence: ``envelope`` (the parsed JSON object), ``kind``
    (:func:`_classify`'s verdict -- possibly ``"error"``, which this function
    deliberately does *not* itself reject, leaving that to the caller's own
    grading rules), ``file``/``command`` (exactly one of which is non-``None``,
    per the citation contract), ``exit_status`` (observed for a
    command-backed entry, inferred ``0`` for a file-backed one),
    ``content_hash`` (the resolved *actual* input hash, including `klt
    yield`'s samples-document fallback -- never compared against the spec's
    pin here; that stays the caller's decision), and
    ``content_hash_unresolved`` (``None`` unless `klt yield`'s
    samples-document fallback named a document it could not find anywhere it
    looked -- issue #2197; see :func:`_yield_samples_content_hash`).

    **Nested envelopes** (issue #2342): when the spec carries a ``pointer``,
    the parsed document is descended per RFC 6901
    (:func:`_resolve_json_pointer`) *before* anything grades it, and the
    value found there is the envelope for every subsequent step. This is the
    only place nesting exists: ``envelope`` in the returned resolution is a
    plain envelope dict either way, so :func:`_classify`,
    :func:`_check_passed`, the caller's ``content_hash`` staleness gate and
    :func:`_citation` are all unchanged by it -- a composition step's report
    that wraps a `klt drc` envelope under ``"drc"`` alongside its own
    findings grades exactly as the same envelope would in a file of its own.
    A pointer that does not resolve to a JSON object renders
    :data:`_REASON_INVALID_POINTER`, never
    :data:`_REASON_UNRECOGNIZED_ENVELOPE`: nothing was classified, so the
    reason must not read as a verdict on the cited bytes. Consistently for
    both bindings -- a command's stdout can be a composite document too.
    With a pointer, the *top-level* document need not be a JSON object at
    all (an array is a legal thing to point into); without one, the
    long-standing "not an object" -> :data:`_REASON_UNRECOGNIZED_ENVELOPE`
    refusal is untouched.
    """
    pointer = spec.get("pointer")
    if spec["kind"] == "command":
        command = spec["command"]
        command_label = shlex.join(command)
        try:
            completed = subprocess.run(
                command,
                cwd=spec.get("cwd"),
                capture_output=True,
                text=True,
                timeout=_COMMAND_EVIDENCE_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, _REASON_COMMAND_FAILED

        exit_status = completed.returncode

        try:
            envelope = json.loads(
                completed.stdout,
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
            )
        except ValueError:
            if exit_status == 0:
                return None, _REASON_UNREADABLE_EVIDENCE
            return None, _REASON_COMMAND_FAILED

        file_label: str | None = None
        source_label = command_label
    else:
        file = spec["file"]
        try:
            envelope = _read_envelope(file)
        except SignoffError:
            return None, _REASON_UNREADABLE_EVIDENCE

        file_label = file
        command_label = None
        exit_status = 0
        source_label = file

    # The parsed bytes narrowed to the one envelope this citation names --
    # the whole document, or (issue #2342) the value a `pointer` selects out
    # of it. Done here, after the bytes are parsed and before anything grades
    # them, so everything downstream receives a plain envelope dict.
    envelope, envelope_reason = _cited_envelope(envelope, pointer)
    if envelope is None:
        return None, envelope_reason

    try:
        check_kind = _classify(envelope, source_label)
    except SignoffError as exc:
        # Issue #2198: an envelope from a `klt` predating a marker field
        # `_classify` now requires is refused like any unrecognised shape,
        # but reported distinguishably -- the remedy ("re-run under a newer
        # klt") is not the remedy for a foreign document.
        return None, _classify_failure_reason(exc)

    provenance = envelope.get("provenance") or {}
    input_block = provenance.get("input") or {}
    actual_hash = input_block.get("content_hash")
    content_hash_unresolved: dict[str, Any] | None = None
    input_verified: bool | None
    if actual_hash is None and check_kind == "yield":
        # klt yield's current JSON shape (issue #816) carries no
        # `provenance` block of its own -- see this module's "Statistical-
        # evidence binding" docstring section and
        # :func:`_yield_samples_content_hash`.
        actual_hash, content_hash_unresolved = _yield_samples_content_hash(
            envelope, spec
        )
        # That hash *is* a live re-hash of the samples document itself, not
        # a self-report about it, so this kind arrives at issue #2196's
        # guarantee by construction -- `None` only when nothing could be
        # hashed, which is the same "no comparison was made" case
        # :func:`_verify_input_artifact` reports `None` for, and exactly the
        # case issue #2197's `content_hash_unresolved` then names.
        input_verified = True if actual_hash is not None else None
    else:
        # Issue #2196: the manifest's pinned hash is checked against
        # `actual_hash` by the caller; this checks `actual_hash` itself
        # against the artifact the envelope names. Disclosure only -- see
        # this module's "Input-artifact verification" docstring section.
        input_verified = _verify_input_artifact(
            check_kind, envelope, spec, actual_hash, file_label
        )

    return (
        {
            "spec": spec,
            "envelope": envelope,
            "kind": check_kind,
            "file": file_label,
            "command": command_label,
            "exit_status": exit_status,
            "content_hash": actual_hash,
            "content_hash_unresolved": content_hash_unresolved,
            "input_verified": input_verified,
        },
        None,
    )


def _citation(resolution: dict[str, Any]) -> dict[str, Any]:
    """Build a ``"met"`` item's ``citation`` block from a
    :func:`_resolve_evidence` resolution -- see :func:`build_tier_report`'s
    docstring for what each field means."""
    citation: dict[str, Any] = {
        "file": resolution["file"],
        "command": resolution["command"],
        "kind": resolution["kind"],
        "check_status": resolution["envelope"].get("status"),
        "content_hash": resolution["content_hash"],
        # Issue #2196: was that `content_hash` checked against the input
        # artifact itself, or only against the envelope's own claim about
        # it? Always present -- including the `None` ("nothing was
        # re-hashed") case, which is the one this field exists to stop
        # being silent. See this module's "Input-artifact verification"
        # docstring section; never consulted by any grading rule.
        "input_verified": resolution.get("input_verified"),
        "exit_status": resolution["exit_status"],
    }
    # Issue #2002: a `drc` citation also carries the three `coverage` fields
    # item 3's doc text requires the claim to disclose -- so the disclosure a
    # claim owes and the numbers it owes it about are in the same artifact.
    # Present only when the cited envelope actually reports coverage, and
    # never consulted by any grading rule: item 3's verdict is still
    # `status == "clean"` alone.
    coverage = _drc_coverage_disclosure(resolution["kind"], resolution["envelope"])
    if coverage is not None:
        citation["coverage"] = coverage
    # Issue #1983: a `pex` citation also carries the cited envelope's own
    # body-bias statement -- item 7's verdict and the one property that can
    # silently invalidate the numbers backing it end up in the same
    # artifact. Present only when the cited envelope reports it, and never
    # consulted by any grading rule: the verdict is still `status == "pass"`
    # alone.
    body_bias = _pex_body_bias_disclosure(resolution["kind"], resolution["envelope"])
    if body_bias is not None:
        citation["body_bias"] = body_bias
    # Issue #2109: a citation whose cited run's common coverage reports
    # skipped *requested* work carries that statement, so an item graded
    # `"met"` off a pre-adapter producer -- one still reporting its
    # unconditional success token on a partial run -- discloses the gap
    # instead of hiding it. Present only for a v1 `coverage` block that says
    # `partial`, and never consulted by any grading rule.
    partial = _partial_coverage_disclosure(resolution["envelope"])
    if partial is not None:
        citation["coverage_qualification"] = partial
    # Issue #2197: a `yield` citation whose report named a samples document
    # that could not be found anywhere this module looked (report-relative,
    # then cwd-relative) carries that fact explicitly, so `content_hash:
    # null` here reads as "this input could not be verified" rather than
    # being indistinguishable from any other benign reason a hash might be
    # absent. Present only in that one case -- never when the report simply
    # named no samples document at all, and never consulted by any grading
    # rule (an unpinned manifest entry still renders `"met"`; a pinned one
    # already renders `"unmet"`/`unverifiable_provenance` via the
    # `content_hash` mismatch check above `_citation` is only reached past).
    content_hash_unresolved = resolution.get("content_hash_unresolved")
    if content_hash_unresolved is not None:
        citation["content_hash_unresolved"] = content_hash_unresolved
    return citation


# --------------------------------------------------------------------------- #
# T1 item 11: power delivery (structural) -- issue #2025
# --------------------------------------------------------------------------- #


def _resolve_relative_to_spec(path: str, spec: dict[str, Any]) -> str:
    """Resolve a path an envelope *names* (not one the manifest names)
    against the directory the producing run itself used: ``spec["cwd"]`` for
    a command-backed entry, this process's own cwd otherwise.

    Shared by :func:`_yield_samples_content_hash` (the `klt yield` samples
    document -- as the compatibility fallback tried *after*
    :func:`_resolve_relative_to_report`, per issue #2197) and
    :func:`_erc_supply_spec` (the `klt erc` spec document) -- both are "the
    envelope points at a second document this module has to read, because
    the envelope itself does not carry what we need".
    """
    cwd = spec.get("cwd") if spec.get("kind") == "command" else None
    if cwd and not os.path.isabs(path):
        return os.path.join(cwd, path)
    return path


def _erc_supply_spec(resolution: dict[str, Any]) -> dict[str, Any] | None:
    """Read the **spec document** a resolved `klt erc` citation names
    (``envelope["spec"]``), and reduce it to the facts T1 item 11 grades
    against -- or ``None`` when it cannot be read or parsed.

    Returns ``{"supply_nets": [<name>, ...], "stackup": {<name/layer>, ...},
    "tie_count": <int>, "ties_disclosure_reason": <str> | None}``:

    - ``supply_nets`` -- every ``nets[]`` entry declared ``"kind":
      "supply"``, by name. Item 11 requires at least one: `klt erc` computes
      ``erc.unconnected_net``/``erc.supply_short`` *only* for declared nets
      (``docs/cli/erc.md``), so a run whose spec declared no supply reports
      zero supply findings for the same reason a DRC deck with no rules
      reports zero violations -- it never asked. That must never read as a
      clean supply.
    - ``stackup`` -- every ``stackup[]`` entry's ``name`` *and* ``layer``,
      in one set, so a strap layer named either way (a role name like
      ``"met4"``, or a raw ``"71/20"``) matches.
    - ``tie_count`` -- ``len(ties)``. Item 11 requires at least one for the
      same "an uncomputed check is not a clean one" reason: ``ties`` omitted
      means ``erc.missing_tie`` was never computed at all.
    - ``ties_disclosure_reason`` -- the spec's top-level
      ``ties_disclosure.reason`` (issue #2234), if it declared one, else
      ``None``. Purely a human-readable detail: the actual
      disclosed-vs-omitted *gate*, and *which* disclosure was made (issue
      #2247's ``kind``), both read the envelope's own ``erc_coverage``, not
      this field -- see :func:`_erc_missing_tie_disclosed`.

    **Why this reads a second document at all.** `klt erc`'s envelope
    (``docs/cli/erc.md``'s JSON schema) echoes the spec's *path* but not its
    content -- not the declared nets, not their ``kind``, not the stackup,
    not the ties. So the envelope alone cannot distinguish "every declared
    supply resolved to one island" from "no supply was ever declared". This
    is the same gap, and the same remedy, as `klt yield`'s missing
    ``provenance`` block (see :func:`_yield_samples_content_hash`): read the
    document the envelope names, via the same cwd resolution, rather than
    fabricate a verdict from its absence. ``klt signoff`` stays a pure
    *consumer* either way -- it changes no verb's own output.

    Follow-up reconciliation, exactly as for `klt yield`: if `klt erc` later
    echoes its resolved ``nets``/``ties``/``stackup`` declarations in its own
    envelope, this function should prefer that echo and the disk read
    becomes the fallback -- no change needed at any call site.
    """
    envelope = resolution["envelope"]
    spec_path = envelope.get("spec")
    if not isinstance(spec_path, str):
        return None

    try:
        document = _read_json_source(
            _resolve_relative_to_spec(spec_path, resolution["spec"]), "erc spec"
        )
    except SignoffError:
        return None
    if not isinstance(document, dict):
        return None

    ties = document.get("ties")
    disclosure = document.get("ties_disclosure")
    disclosure_reason = (
        disclosure.get("reason")
        if isinstance(disclosure, dict) and isinstance(disclosure.get("reason"), str)
        else None
    )
    return {
        "supply_nets": [
            entry["name"]
            for entry in document.get("nets") or []
            if isinstance(entry, dict)
            and entry.get("kind") == "supply"
            and isinstance(entry.get("name"), str)
            and entry["name"]
        ],
        "stackup": {
            entry[field]
            for entry in document.get("stackup") or []
            if isinstance(entry, dict)
            for field in ("name", "layer")
            if isinstance(entry.get(field), str) and entry[field]
        },
        "tie_count": len(ties) if isinstance(ties, list) else 0,
        # Issue #2234: the spec's own `ties_disclosure.reason`, read purely
        # for a human-readable `detail` when item 11 renders one of the
        # disclosed reasons -- see :func:`_resolve_erc_supply_spec`. The
        # disclosure gate itself reads the *envelope*'s own `erc_coverage`
        # (:func:`_erc_missing_tie_disclosed`), not this field, so a
        # deleted/edited spec document never flips a rendered reason -- only
        # degrades the detail text. Same for #2247's `kind`: which
        # disclosure was made is decided by the envelope's own recorded
        # coverage reason, never by re-reading the spec.
        "ties_disclosure_reason": disclosure_reason,
    }


def _erc_supply_findings(
    envelope: dict[str, Any], supply_nets: list[str]
) -> list[dict[str, Any]]:
    """Every ``erc_findings[]`` entry that contradicts T1 item 11's own
    supply-continuity rule -- ``[]`` when the run reports none.

    Exactly three of `klt erc`'s five rules are graded here
    (``docs/cli/erc.md`` → "ERC finding checks"), and only for the *declared
    supply* nets:

    - ``erc.unconnected_net`` naming a declared supply -- that supply matched
      zero islands (nothing carries its label) or more than one (the rail is
      split into pieces that never touch). Either way it is not the "exactly
      one island per declared supply" the item requires.
    - ``erc.supply_short`` -- two declared supplies resolved to the *same*
      island, which is likewise not one island per supply.
    - ``erc.missing_tie`` -- a well/tub with no tap drawn inside it, or a tap
      wired to the wrong net.

    The other two rules (``erc.floating_gate`` and the signal-side
    ``erc.multiply_driven_net``) and every antenna verdict are deliberately
    **not** graded: they are real defects, but they are not power delivery,
    and item 11 must not be blocked by an unrelated signal-net finding (nor
    by the tie-cell antenna false positives issue #1994 tracks). This is why
    item 11 does not simply require the ERC envelope's own
    ``status == "clean"``.
    """
    declared = {name.upper() for name in supply_nets}
    offending: list[dict[str, Any]] = []
    for finding in envelope.get("erc_findings") or []:
        if not isinstance(finding, dict):
            continue
        rule = finding.get("rule")
        if rule == "erc.missing_tie" or rule == "erc.supply_short":
            offending.append(finding)
            continue
        if rule != "erc.unconnected_net":
            continue
        for field in ("net", "other_net"):
            value = finding.get(field)
            if isinstance(value, str) and value.upper() in declared:
                offending.append(finding)
                break
    return offending


def _strap_layers(envelope: dict[str, Any]) -> list[str]:
    """Every ``power.straps[].layer`` a `klt place-and-route` response
    reports, in the response's own bottom-to-top order -- ``[]`` when the
    request carried no ``power`` block (``docs/cli/place-and-route.md``)."""
    power = envelope.get("power") or {}
    straps = power.get("straps")
    if not isinstance(straps, list):
        return []
    return [
        strap["layer"]
        for strap in straps
        if isinstance(strap, dict) and isinstance(strap.get("layer"), str)
    ]


def _lvs_reference_carries_supplies(
    envelope: dict[str, Any], supply_nets: list[str]
) -> bool:
    """Whether an LVS report proves its **reference netlist carried the
    supply nets**, i.e. that the supplies were part of the compare rather
    than absent from it (T1 item 11's analog/full-custom branch).

    True only when ``options.power_connectivity`` was not explicitly
    disabled (``False``) *and* every declared supply appears in the report's
    own ``net_correspondence`` (``docs/cli/lvs.md``) paired to a non-``None``
    reference-side net. A SPICE reference satisfies both by construction --
    it declares its own supply nets and pins, which is exactly why item 4's
    ``power_connectivity`` reports ``"unchecked"`` for it, and it has no
    reason to ever disable the option. A signal-only ``gate-level-verilog``
    reference does not: its supplies normally exist on the layout side
    alone, so they never pair, and such a block must instead prove item 11
    through the PDN branch (the `klt place-and-route` response plus
    ``power_connectivity``). Rejecting an explicit ``options.power_connectivity:
    false`` closes the remaining gap -- a gate-level-verilog reference that
    *does* declare explicit power ports could otherwise pair here even
    though the caller turned the power/ground check off, letting a block
    reach ``"met"`` with power delivery never actually verified.

    Name comparison is case-insensitive, matching how `klt lvs` itself
    matches power pin/net names (``NetlistSpiceReader`` upper-cases what it
    reads -- see ``docs/cli/lvs.md``'s ``options.power_connectivity``).
    """
    if (envelope.get("options") or {}).get("power_connectivity") is False:
        return False
    paired: set[str] = set()
    for row in envelope.get("net_correspondence") or []:
        if not isinstance(row, dict):
            continue
        layout = row.get("layout")
        reference = row.get("reference")
        if isinstance(layout, str) and isinstance(reference, str) and reference:
            paired.add(layout.upper())
    return all(name.upper() in paired for name in supply_nets)


def _resolve_power_delivery_parts(
    specs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """Resolve every part of T1 item 11's compound citation (issue #2025) and
    index the resolutions by :func:`_classify` kind -- ``(by_kind, None)`` on
    success, ``(None, <_REASON_* constant>)`` on the first part that cannot
    be used.

    Each part goes through :func:`_resolve_evidence`, the same
    read/run/classify/hash path every single-artifact item's citation uses,
    and is then subject to the three rules that apply to a part *as a part*:
    an ``error`` envelope is ``check_errored``, a kind outside
    :data:`_POWER_DELIVERY_KINDS` is ``wrong_kind`` (it proves nothing about
    power delivery), and a part whose own pinned ``content_hash`` no longer
    matches is ``stale_evidence`` when the resolved envelope carries a
    different, non-``None`` hash, or :data:`_REASON_UNVERIFIABLE_PROVENANCE`
    when it carries no input hash at all (issue #2182 -- see that constant's
    docstring). The item-specific rules (:func:`_grade_power_delivery`) apply
    only to a set that survives all of these.

    A later part of the same kind replaces an earlier one -- citing two ERC
    runs for one item is a manifest authoring mistake, not a shape this
    grading has a meaning for; the last one named wins, the same way a
    duplicate JSON key would.
    """
    by_kind: dict[str, dict[str, Any]] = {}
    for spec in specs:
        resolution, reason = _resolve_evidence(spec)
        if resolution is None:
            return None, reason
        kind = resolution["kind"]
        if kind == "error":
            return None, _REASON_CHECK_ERRORED
        if kind not in _POWER_DELIVERY_KINDS:
            return None, _REASON_WRONG_KIND
        expected_hash = spec.get("content_hash")
        if expected_hash is not None and resolution["content_hash"] != expected_hash:
            if resolution["content_hash"] is None:
                return None, _REASON_UNVERIFIABLE_PROVENANCE
            return None, _REASON_STALE_EVIDENCE
        by_kind[kind] = resolution
    return by_kind, None


def _pdn_branch_reason(
    par: dict[str, Any],
    lvs: dict[str, Any],
    supply_spec: dict[str, Any],
) -> str | None:
    """T1 item 11's **PDN branch** (issue #2025) -- the extra conditions a
    cited `klt place-and-route` response brings with it. ``None`` when they
    all hold; otherwise the ``reason`` that does not.

    Three conditions, in the order a reader would debug them: the response
    itself must pass (a P&R run that errored proves nothing); it must report
    ``power.pdn: true`` with a ``power.tapcell_master`` named, i.e. a grid
    was actually built (:data:`_REASON_NO_PDN`); and every
    ``power.straps[].layer`` it reports must be covered by the ERC spec's own
    stackup (:data:`_REASON_SUPPLY_SPEC_INCOMPLETE` -- an ERC run that never
    looked at the layers the supply is routed on says nothing about the grid
    this response built). Finally the LVS half tightens: with a PDN in play
    the same report's ``power_connectivity.status`` must be ``"match"``, and
    ``"unchecked"`` does **not** satisfy item 11 even though it satisfies
    item 4.
    """
    if not _check_passed("place-and-route", par["envelope"]):
        return _REASON_CHECK_FAILED
    power = par["envelope"].get("power") or {}
    if power.get("pdn") is not True or not power.get("tapcell_master"):
        return _REASON_NO_PDN
    strap_layers = _strap_layers(par["envelope"])
    if not strap_layers or any(
        layer not in supply_spec["stackup"] for layer in strap_layers
    ):
        return _REASON_SUPPLY_SPEC_INCOMPLETE
    power_connectivity = lvs["envelope"].get("power_connectivity") or {}
    if power_connectivity.get("status") != "match":
        return _REASON_LVS_SUPPLY_UNPROVEN
    return None


def _erc_missing_tie_skipped(envelope: dict[str, Any]) -> bool:
    """Whether the cited `klt erc` run reports any ``erc.missing_tie`` work
    it **declined to perform** (issue #2199).

    `klt erc` records a ``ties[]`` entry whose declared tap region cannot
    be told apart from an ordinary source/drain contact in
    ``erc_coverage.skipped`` rather than ``checked``
    (``docs/cli/erc.md`` → "Well/tap connectivity"). Item 11 requires zero
    ``erc.missing_tie`` findings, and such a tie reports zero for a reason
    that has nothing to do with taps -- the same "an uncomputed check is
    not a clean one" rule that already rejects a spec declaring no
    ``ties[]`` at all, applied to a declaration that was made but could not
    be answered.

    Matched on the work identity's ``erc.missing_tie:`` domain prefix
    (:func:`~klayout_tools.coverage.work_id`) rather than on the skip
    reason, so a future `klt erc` that declines this rule for some *other*
    stated reason is caught by the same gate. Issue #2255 is the first such
    reason to actually arrive -- ``degenerate_well_assertion``, a
    caller-asserted substrate region indistinguishable from the whole
    top-cell extent -- and needed no change here, which is the property this
    prefix match was chosen for. An envelope with no ``erc_coverage`` block
    (every report before #2179) skips nothing and is graded exactly as it
    was.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return False
    return any(
        isinstance(record, dict)
        and isinstance(record.get("id"), str)
        and record["id"].startswith("erc.missing_tie:")
        for record in block.get("skipped") or []
    )


#: The item-11 reason each of `klt erc`'s *disclosed* zero-ties coverage
#: reasons renders (issues #2234, #2247). A reason token outside this table
#: -- ``"no_ties_declared"``, or anything a future `klt erc` invents -- is
#: not a disclosure this build knows how to render, and falls through to the
#: plain :data:`_REASON_SUPPLY_SPEC_INCOMPLETE`: an unrecognised token must
#: never be read as "something was disclosed", which would let an
#: unfamiliar string soften the verdict of record.
_DISCLOSED_TIE_REASONS: dict[str, str] = {
    "ties_disclosed_unexpressible": _REASON_SUPPLY_SPEC_DISCLOSED_UNEXPRESSIBLE,
    "ties_disclosed_tool_limitation": _REASON_SUPPLY_SPEC_DISCLOSED_TOOL_LIMITATION,
}


def _erc_missing_tie_disclosed(envelope: dict[str, Any]) -> str | None:
    """The item-11 reason constant for a cited `klt erc` run that declares
    zero ``ties[]`` *and* explicitly disclosed why (issues #2234, #2247) --
    ``None`` when it disclosed nothing this build recognises.

    `klt erc` records the undeclared ``erc.missing_tie`` work in
    ``erc_coverage.inapplicable`` with a reason of ``"no_ties_declared"``
    (``ties`` simply omitted/empty, no explanation),
    ``"ties_disclosed_unexpressible"`` (the spec's top-level
    ``ties_disclosure`` was given, and this stream has no tap to name), or
    ``"ties_disclosed_tool_limitation"`` (that disclosure named
    ``"kind": "tool_limitation"``: the tap is nameable, but the build the
    evidence had to be produced on cannot grade a declared tie safely) --
    see ``docs/cli/erc.md``. All three describe the identical "zero ties"
    fact reported by :func:`_erc_supply_spec`'s ``tie_count == 0``; this
    function is what lets :func:`_resolve_erc_supply_spec` tell them apart
    and render :data:`_REASON_SUPPLY_SPEC_DISCLOSED_UNEXPRESSIBLE` or
    :data:`_REASON_SUPPLY_SPEC_DISCLOSED_TOOL_LIMITATION` instead of the
    plain :data:`_REASON_SUPPLY_SPEC_INCOMPLETE`. The *status* is
    ``"unmet"`` in all three cases -- a disclosure is the caller's word, not
    a computed ``erc.missing_tie`` result, so it can never substitute for
    one; only the *reason* differs, and it differs because the three name
    different things to go fix.

    Matched on ``erc_coverage.inapplicable`` (not ``skipped`` --
    :func:`_erc_missing_tie_skipped` covers a *declared but degenerate* tie,
    a different case) and on the reason string itself, since presence alone
    does not distinguish disclosed from undisclosed here -- all three render
    an ``erc.missing_tie:`` entry in ``inapplicable`` regardless. An
    envelope with no ``erc_coverage`` block (every report before #2179)
    discloses nothing and is graded exactly as it was.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return None
    for record in block.get("inapplicable") or []:
        if (
            isinstance(record, dict)
            and isinstance(record.get("id"), str)
            and record["id"].startswith("erc.missing_tie:")
            and record.get("reason") in _DISCLOSED_TIE_REASONS
        ):
            return _DISCLOSED_TIE_REASONS[record["reason"]]
    return None


def _erc_ties_checked_by_assertion(envelope: dict[str, Any]) -> list[str]:
    """The cited `klt erc` run's ``erc_coverage.checked_by_assertion``
    (issue #2234): the ``erc.missing_tie`` work identities whose tap region
    came from a caller **assertion** (``ties[].tap_boxes``) rather than
    PDK-marker narrowing -- ``[]`` for every run that used none, and for
    every report produced before the field existed.

    Carried into a ``"met"`` item 11 citation's ``power_delivery`` block
    (:func:`_grade_power_delivery`) for the same reason `klt erc` grades it
    as its own classification rather than folding it into ``checked``: an
    asserted tie is real, evaluated work -- the geometry was intersected and
    the connectivity walked, and a degenerate or unmatched assertion is
    rejected exactly as any other narrowing form is (``docs/cli/erc.md`` →
    "A tie with no distinguishing marker layer at all") -- but *which
    geometry counts as the tap* rested on the caller's word rather than on a
    drawn marker. That is a provenance difference a reader of the verdict of
    record should not have to re-open the cited ERC envelope to discover.
    It does not change the verdict: item 11 is ``"met"`` on an asserted tie
    exactly as on a marker-derived one.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return []
    return [
        identity
        for identity in block.get("checked_by_assertion") or []
        if isinstance(identity, str)
    ]


def _erc_ties_checked_by_well_assertion(envelope: dict[str, Any]) -> list[str]:
    """The cited `klt erc` run's ``erc_coverage.checked_by_well_assertion``
    (issue #2255): the ``erc.missing_tie`` work identities whose **well
    region itself** came from a caller assertion (``ties[].well_layer:
    null`` + ``ties[].well_boxes``) because the block sits in a native
    substrate that draws no well/tub layer at all -- ``[]`` for every run
    that used none, and for every report produced before the field existed.

    Carried into a ``"met"`` item 11 citation beside
    :func:`_erc_ties_checked_by_assertion`'s tap-side list, and deliberately
    **not** merged into it, because the two state different things about the
    same verdict. A ``tap_boxes`` assertion says which of the drawn tap
    geometry counts as the tap; the well is still drawn, and still measured.
    A ``well_boxes`` assertion says where the substrate *is*, for a stream
    that draws nothing to corroborate it -- the weaker of the two claims,
    and the one a grader is most likely to want to see stated explicitly.

    It does not change the verdict: item 11 is ``"met"`` on an asserted
    substrate tie exactly as on a drawn-well one. It can be, because the
    assertion is falsifiable and `klt erc` falsifies it where it can -- each
    asserted polygon must independently contain a tap that reaches the
    declared net, and an assertion indistinguishable from the whole top-cell
    extent is rejected as degenerate (``docs/cli/erc.md`` → "A block with no
    drawn well at all"), landing in ``erc_coverage.skipped`` where
    :func:`_erc_missing_tie_skipped` already renders it ``unmet``.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return []
    return [
        identity
        for identity in block.get("checked_by_well_assertion") or []
        if isinstance(identity, str)
    ]


def _resolve_erc_supply_spec(
    erc: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """Validate the ERC half of item 11's cited set (critical metrics, then
    supply-spec completeness and continuity) and return the resolved supply
    spec, split out of :func:`_grade_power_delivery` to keep its own
    complexity under the repo's ratchet (issue #2094).

    Returns ``(supply_spec, reason, detail)``: on success, ``supply_spec`` is
    the resolved spec and ``reason``/``detail`` are ``None``/``{}``; on
    failure, ``supply_spec`` is ``None`` and ``reason``/``detail`` are the
    values :func:`_grade_power_delivery` should return directly (with status
    ``"unmet"`` and no citation).
    """
    erc_metric_detail = _critical_metric_detail(erc["envelope"])
    if erc_metric_detail:
        return None, _REASON_CHECK_FAILED, erc_metric_detail

    supply_spec = _erc_supply_spec(erc)
    if supply_spec is None or not supply_spec["supply_nets"]:
        return None, _REASON_SUPPLY_SPEC_INCOMPLETE, {}
    if supply_spec["tie_count"] == 0:
        # Issues #2234/#2247: same "zero ties" fact however it got here, but
        # a *disclosed* non-declaration gets its own reason, one per
        # disclosed obstacle -- see `_erc_missing_tie_disclosed`. Unmet
        # either way; what differs is what the report tells a reader to go
        # fix.
        disclosed_reason = _erc_missing_tie_disclosed(erc["envelope"])
        if disclosed_reason is not None:
            return (
                None,
                disclosed_reason,
                {"ties_disclosure_reason": supply_spec["ties_disclosure_reason"]},
            )
        return None, _REASON_SUPPLY_SPEC_INCOMPLETE, {}
    if _erc_missing_tie_skipped(erc["envelope"]):
        return None, _REASON_SUPPLY_SPEC_INCOMPLETE, {}

    if _erc_supply_findings(erc["envelope"], supply_spec["supply_nets"]):
        return None, _REASON_SUPPLY_NOT_CONTINUOUS, {}

    return supply_spec, None, {}


def _grade_power_delivery(
    specs: list[dict[str, Any]], *, partition_kind: str
) -> tuple[str, str | None, dict[str, Any] | None, dict[str, Any]]:
    """Grade T1 item 11 ("Power delivery (structural)", issue #2025) against
    the **set** of evidence entries cited for it, and return ``(status,
    reason, citation, failure_detail)`` in the same shape :func:`_grade_evidence`
    returns.

    Unlike every other T1 item, no single artifact proves this one. The
    cited set must contain:

    - an ``"erc"`` citation -- a `klt erc` run against a **supply spec**
      (:func:`_erc_supply_spec`): at least one ``"kind": "supply"`` net
      declared, at least one ``ties[]`` entry declared, none of them
      reported as degenerate by the run itself
      (:func:`_erc_missing_tie_skipped` -- on the tap side, issue #2199, or
      the well side, issue #2255), and no supply-side finding
      (:func:`_erc_supply_findings`). A **native-substrate** block, whose
      ties assert their substrate region because no well/tub layer is drawn
      (``ties[].well_boxes``), reaches ``"met"`` on the same terms as a
      drawn-well one; which of its ties rested on that assertion is stated
      in the citation's ``power_delivery.ties_checked_by_well_assertion``
      (:func:`_erc_ties_checked_by_well_assertion`);
    - an ``"lvs"`` citation -- the same report item 4 grades, which must
      itself pass (:func:`_check_passed`);
    - and, for an RTL-flow digital block, a ``"place-and-route"`` citation
      whose ``power.pdn`` is ``true`` with a ``power.tapcell_master`` named,
      and every ``power.straps[].layer`` covered by the ERC spec's own
      stackup.

    **Which branch applies is decided by whether a ``"place-and-route"``
    citation is present**, not by ``partition_kind`` alone -- because
    ``docs/design-evidence-tiers.md``'s "Full-custom digital sub-case"
    declares ``kind: "digital"`` for a hand-captured block that has no P&R
    run to cite at all, exactly as it does for items 1, 2, and 5. With a PDN
    citation, the LVS half is ``power_connectivity.status == "match"``
    (``"unchecked"`` does **not** satisfy item 11, unlike item 4, where it
    means "the question does not apply here"). Without one, the LVS half is
    that the reference carried the supply nets
    (:func:`_lvs_reference_carries_supplies`) -- which a signal-only
    ``gate-level-verilog`` reference cannot satisfy, so an RTL-flow digital
    block cannot reach ``"met"`` by simply omitting its P&R citation.

    ``partition_kind`` is accepted (and carried into the citation) so the
    report says which column's rule was applied, and so a future per-kind
    divergence has a place to land.

    Every part is resolved through :func:`_resolve_evidence` -- the same
    read/run/classify/hash path every other item uses -- so a part that is
    unreadable, unrecognised, an ``error`` envelope, or stale (or
    unverifiable, issue #2182) against its own pinned ``content_hash``
    renders that part's own ordinary reason
    (``unreadable_evidence``/``unrecognized_envelope``/
    ``envelope_version_skew``/``check_errored``/
    ``stale_evidence``/``unverifiable_provenance``), never a
    power-delivery-specific one. Item-specific
    reasons (:data:`_REASON_NO_PDN`, :data:`_REASON_SUPPLY_SPEC_INCOMPLETE`,
    :data:`_REASON_SUPPLY_SPEC_DISCLOSED_UNEXPRESSIBLE`,
    :data:`_REASON_SUPPLY_SPEC_DISCLOSED_TOOL_LIMITATION`,
    :data:`_REASON_SUPPLY_NOT_CONTINUOUS`,
    :data:`_REASON_LVS_SUPPLY_UNPROVEN`) are reserved for a cited set that
    resolved cleanly and still does not prove power delivery.

    The declared-critical-metric gate (issue #2094) applies to **both** the
    LVS and ERC parts, independent of everything else this function checks.
    The LVS part gets it for free through :func:`_check_passed`. The ERC
    part does not go through :func:`_check_passed` at all -- it deliberately
    tolerates unrelated ERC findings (antenna/signal violations on nets this
    item does not care about, see :func:`_check_passed`'s ``"erc"`` note) --
    so its critical metrics are checked directly, via
    :func:`_critical_metric_detail`, without making those unrelated findings
    newly fatal.
    """
    by_kind, reason = _resolve_power_delivery_parts(specs)
    if by_kind is None:
        return "unmet", reason, None, {}

    erc = by_kind.get("erc")
    lvs = by_kind.get("lvs")
    if erc is None or lvs is None:
        # The cited set does not contain the artifacts this item names at
        # all -- "cite a different artifact", which is exactly what
        # `wrong_kind` means everywhere else in this module.
        return "unmet", _REASON_WRONG_KIND, None, {}

    if not _check_passed("lvs", lvs["envelope"]):
        return (
            "unmet",
            _REASON_CHECK_FAILED,
            None,
            _critical_metric_detail(lvs["envelope"]),
        )

    supply_spec, reason, detail = _resolve_erc_supply_spec(erc)
    if supply_spec is None:
        return "unmet", reason, None, detail

    par = by_kind.get("place-and-route")
    if par is not None:
        reason = _pdn_branch_reason(par, lvs, supply_spec)
    elif _lvs_reference_carries_supplies(lvs["envelope"], supply_spec["supply_nets"]):
        reason = None
    else:
        reason = _REASON_LVS_SUPPLY_UNPROVEN
    if reason is not None:
        return "unmet", reason, None, {}

    # The compound citation keeps the single-citation contract every existing
    # consumer reads (`file`/`command`/`kind`/`check_status`/`content_hash`/
    # `exit_status` -- signoff_cmd.py's text rendering, the fleet roll-up's
    # `drc_coverage` reduction) by leading with the ERC part, the one
    # artifact both columns of item 11 always cite; `parts` carries every
    # cited artifact in full, so nothing a reader needs is only reachable
    # through the leading part.
    citation = _citation(erc)
    citation["parts"] = [
        _citation(by_kind[kind])
        for kind in ("erc", "lvs", "place-and-route")
        if kind in by_kind
    ]
    citation["power_delivery"] = {
        "partition_kind": partition_kind,
        "supply_nets": list(supply_spec["supply_nets"]),
        "pdn": par is not None,
        "strap_layers": _strap_layers(par["envelope"]) if par is not None else [],
        "tapcell_master": (
            (par["envelope"].get("power") or {}).get("tapcell_master")
            if par is not None
            else None
        ),
        "power_connectivity_status": (
            lvs["envelope"].get("power_connectivity") or {}
        ).get("status"),
        # Issue #2234: which of the cited run's `erc.missing_tie` checks
        # rested on a caller assertion (`ties[].tap_boxes`) rather than on
        # PDK-marker narrowing -- `[]` for a purely marker-derived (or
        # pre-#2234) run. See `_erc_ties_checked_by_assertion`.
        "ties_checked_by_assertion": _erc_ties_checked_by_assertion(erc["envelope"]),
        # Issue #2255: which of them rested on a caller-asserted *well*
        # region (`ties[].well_layer: null` + `ties[].well_boxes`) rather
        # than on a drawn well/tub layer -- the native-substrate case, `[]`
        # for every drawn-well (or pre-#2255) run. A separate list from the
        # tap-side one above because it is a separate, weaker claim; see
        # `_erc_ties_checked_by_well_assertion`.
        "ties_checked_by_well_assertion": _erc_ties_checked_by_well_assertion(
            erc["envelope"]
        ),
    }
    return "met", None, citation, {}


def _build_power_delivery_item(
    *,
    tier: str,
    item_id: int,
    title: str,
    text: str | None,
    notes: list[str],
    partition: str | None,
    partition_kind: str,
    partition_boundary: str | None = None,
    evidence: dict[str, Any],
    graded_by_build: bool = True,
) -> dict[str, Any]:
    """Render T1 item 11's report entry (issue #2025) -- the compound
    counterpart of :func:`_build_tier_item`, which grades every other item.

    Identical in shape and in its "no evidence is never a pass" discipline;
    the only differences are that a *list* of evidence entries is accepted
    (and required to normalize entry-by-entry, so one malformed part renders
    the whole item :data:`_REASON_INVALID_EVIDENCE` rather than being
    silently dropped from the cited set), and that grading is delegated to
    :func:`_grade_power_delivery`.

    ``partition_boundary`` (issue #2278) is echoed exactly as in
    :func:`_build_tier_item`.

    ``graded_by_build`` (issue #2176) is reported and honoured exactly as in
    :func:`_build_tier_item`. In practice it is always ``True`` here --
    reaching this function at all means the item id is a member of
    :data:`_ITEMS_GRADED_AS_POWER_DELIVERY`, which is one of the two tables
    :func:`_is_graded_by_build` derives its answer from -- but it is
    threaded through rather than hardcoded so the field has exactly one
    source of truth for every rendered item.
    """
    citation = None
    failure_detail: dict[str, Any] = {}
    status = "unmet"
    reason: str | None = _REASON_NO_EVIDENCE

    raw_entry = _lookup_evidence(evidence, item_id, partition)
    if raw_entry is not None and not graded_by_build:
        reason = _REASON_UNGRADEABLE_BY_BUILD
    elif raw_entry is not None:
        specs = _normalize_evidence_parts(raw_entry)
        if specs is None:
            reason = _REASON_INVALID_EVIDENCE
        else:
            status, reason, citation, failure_detail = _grade_power_delivery(
                specs, partition_kind=partition_kind
            )

    return {
        "tier": tier,
        "id": item_id,
        "title": title,
        "partition": partition,
        **(
            {"partition_boundary": partition_boundary}
            if partition_boundary is not None
            else {}
        ),
        "text": text,
        "notes": notes,
        "status": status,
        "reason": reason,
        "graded_by_build": graded_by_build,
        "citation": citation,
        **({"detail": failure_detail} if failure_detail else {}),
    }


def _build_tier_item(
    *,
    tier: str,
    item_id: int,
    title: str,
    text: str | None,
    notes: list[str],
    partition: str | None,
    partition_boundary: str | None = None,
    evidence: dict[str, Any],
    allowed_kinds: set[str] | None = None,
    require_post_layout: bool = False,
    graded_by_build: bool = True,
) -> dict[str, Any]:
    """Grade one T1 checklist item against ``evidence`` -- see
    :func:`build_tier_report`'s docstring for the full met/unmet rule and
    the ``reason`` enum.

    ``allowed_kinds`` (issue #871, Phase 2b of epic #706; resolved
    per-block-kind since issue #1959): when given, a ``"met"`` grading is
    only accepted if the resolved evidence's classified kind
    (:func:`_classify`, via the citation's ``"kind"``) is a member of this
    set -- otherwise the item is downgraded to ``"unmet"`` with ``reason:
    "wrong_kind"`` and no citation. ``None`` (the default) means no
    restriction, preserving Phase 0/1's original behaviour where any
    recognised, passing envelope kind satisfies any item -- since issue
    #2044 only T1 items 1, 2, 9 and 10 (the four naming no evidence at all)
    still pass ``None`` (see :data:`_ITEM_ALLOWED_KINDS` and
    :func:`_allowed_kinds_for`).

    ``partition_boundary`` (issue #2278) is the manifest's own statement of
    what *this row's* partition denotes, resolved by
    :func:`_read_partition_boundary` and echoed verbatim onto the entry so a
    row and the definition of the silicon it covers are readable together.
    Omitted entirely (not rendered as ``None``) when the manifest declared
    none for this partition -- an absent statement must not be
    indistinguishable from a declared-empty one, and an undeclared manifest's
    report must stay byte-identical to what this build rendered before the
    field existed. It never touches ``status``/``reason``: there is no way to
    check free text against silicon.

    ``require_post_layout`` (issue #1959) is forwarded to
    :func:`_grade_evidence` for the items in
    :data:`_ITEMS_REQUIRING_POST_LAYOUT_EVIDENCE` (item 7 today): an
    accepted-kind, passing citation that does not itself prove a
    post-layout run renders ``reason: "not_post_layout"`` instead.

    A ``"generic"``-kind citation (issue #1152) is gated independently of
    ``allowed_kinds``: it only satisfies an item whose id is a member of
    :data:`_ITEMS_ACCEPTING_GENERIC_EVIDENCE` (today, item 8 only) -- checked
    first, so ``"generic"`` never borrows a pass from an item's otherwise
    unrestricted ``allowed_kinds=None`` (which was written for the six
    `klt`-verb-native kinds, before ``"generic"`` existed, and would
    otherwise happily accept it too). The two gates agree on item 8 (issue
    #2044 gave it ``allowed_kinds={"generic"}``) rather than
    double-restricting it: a ``"generic"`` citation for item 8 passes both,
    and a native-kind citation for item 8 is rejected by ``allowed_kinds``
    alone.

    A ``"power"``-kind citation (issue #1321) is gated the same way, against
    :data:`_ITEMS_ACCEPTING_POWER_EVIDENCE` -- deliberately empty today, since
    no T1 item names `klt power`'s IR-drop/EM evidence (see this module's
    "`klt power` (IR-drop/EM) evidence ingestion" docstring section), so a
    passing ``"power"`` citation never satisfies any tier-report item.

    ``graded_by_build`` (issue #2176, resolved by
    :func:`_is_graded_by_build`) is echoed onto the rendered entry, and,
    when ``False``, **refuses** a cited item outright: ``"unmet"`` with
    :data:`_REASON_UNGRADEABLE_BY_BUILD` and no citation, decided before the
    evidence is resolved at all (so a command-backed entry is never run for
    an item this build could not interpret the answer to). An *uncited*
    ungradeable item is untouched -- still ``"unmet"``/
    :data:`_REASON_NO_EVIDENCE`, now carrying ``graded_by_build: False`` so
    the row no longer implies this build checked it. The refusal is
    deliberately ahead of the ``allowed_kinds`` gate rather than folded into
    it: ``allowed_kinds`` answers "which artifact does this item accept",
    which is a question a build with no rules for the item cannot even pose.
    """
    citation = None
    failure_detail: dict[str, Any] = {}
    status = "unmet"
    reason: str | None = _REASON_NO_EVIDENCE

    raw_entry = _lookup_evidence(evidence, item_id, partition)
    if raw_entry is not None and not graded_by_build:
        reason = _REASON_UNGRADEABLE_BY_BUILD
    elif raw_entry is not None:
        spec = _normalize_evidence_entry(raw_entry)
        if spec is None:
            reason = _REASON_INVALID_EVIDENCE
        else:
            status, reason, citation, failure_detail = _grade_evidence(
                spec,
                require_post_layout=require_post_layout,
                allowed_kinds=allowed_kinds,
            )
            opt_in_items = (
                _OPT_IN_KIND_ITEMS.get(citation["kind"]) if status == "met" else None
            )
            if opt_in_items is not None and item_id not in opt_in_items:
                status = "unmet"
                reason = _REASON_WRONG_KIND
                citation = None
            elif (
                status == "met"
                and allowed_kinds is not None
                and citation["kind"] not in allowed_kinds
            ):
                status = "unmet"
                reason = _REASON_WRONG_KIND
                citation = None

    return {
        "tier": tier,
        "id": item_id,
        "title": title,
        "partition": partition,
        **(
            {"partition_boundary": partition_boundary}
            if partition_boundary is not None
            else {}
        ),
        "text": text,
        "notes": notes,
        "status": status,
        "reason": reason,
        "graded_by_build": graded_by_build,
        "citation": citation,
        **({"detail": failure_detail} if failure_detail else {}),
    }


# --------------------------------------------------------------------------- #
# Fleet roll-up (issue #827, Phase 1c of epic #706)
# --------------------------------------------------------------------------- #


def build_fleet_report(
    fleet: dict[str, Any], *, tiers_doc: str | Path | None = None
) -> dict[str, Any]:
    """Grade every block in a **fleet manifest** against the T1-T4 item
    skeleton (:func:`build_tier_report`, called once per block) and reduce
    each block's result down to its current tier and, for any block not yet
    at T1, the single T1 item still blocking it -- turning "which canaries
    are at which tier, and what's blocking each not-yet-T1 block" into one
    query instead of a survey (Epic #706, Phase 1c).

    ``tiers_doc`` is forwarded verbatim to every per-block
    :func:`build_tier_report` call, so one roll-up always grades every block
    against the same doc.

    ``fleet`` (JSON in)::

        {
            "blocks": [
                "manifests/sky130-bandgap.json",
                {"block": "gf180-bandgap", "kind": "analog", "evidence": {...}}
            ]
        }

    Each ``blocks[]`` entry is either a **path** to a block manifest JSON
    file (or ``"-"`` for stdin -- read exactly like ``--manifest``'s own
    input), or an **inline** block manifest object -- the same shape
    :func:`build_tier_report` accepts (``block``/``kind``/``evidence``).
    Every resolved manifest's ``block`` field is required here (unlike
    single-block tier-report mode, where it is optional and merely echoed)
    since it is how a roll-up row is identified.

    Returns (JSON out)::

        {
            "schema_version": 2,
            "block_count": 3,
            "t1_count": 1,
            "not_t1_count": 2,
            "source_doc": "docs/design-evidence-tiers.md",
            "source_doc_content_hash": "sha256:...",
            # Which build graded the fleet (issue #2176) -- the same block
            # `build_tier_report` reports, in `klt version`'s own shape.
            "build": {
                "version": "0.4.2+g0123456789ab",
                "package_version": "0.4.2",
                "git_commit": "0123456789ab...",
                "git_tag": None,
                "dirty": False,
                "is_release": False,
                # A content hash of the grading code itself (issue #2216).
                "grading_ruleset_id": "sha256:...",
            },
            "blocks": [
                {
                    "block": "sky130-bandgap",
                    "source": "manifests/sky130-bandgap.json",
                    "kind": "analog",
                    "tier": "T1",
                    "t1_item_count": 11,
                    # What this build's own doc would have rendered
                    # (issue #2202) -- equal unless the parsed doc's item
                    # list differs; None if that doc is unreadable.
                    "build_t1_item_count": 11,
                    "t1_met_count": 11,
                    "source_doc_content_hash": "sha256:...",
                    "blocking_item": None,
                    "ungraded_items": [],
                    "drc_coverage": [
                        {
                            "item": 3,
                            "partition": None,
                            "layers_in_stream_without_rules": ["met4/0"],
                            "rules_skipped": ["met5.4"],
                            "deck_scope": ["5.x", "6.x"],
                        }
                    ],
                },
                {
                    "block": "gf180-bandgap",
                    "source": None,
                    "kind": "analog",
                    "tier": None,
                    "t1_item_count": 11,
                    "build_t1_item_count": 11,
                    "t1_met_count": 3,
                    "source_doc_content_hash": "sha256:...",
                    "blocking_item": {
                        "id": 4,
                        "title": "LVS clean",
                        "partition": None,
                        "reason": "no_evidence",
                    },
                    "ungraded_items": [
                        {
                            "id": 1,
                            "title": "Design sources",
                            "partition": None,
                            "reason": "no_evidence",
                        },
                        ...
                    ],
                    "drc_coverage": [],
                },
                ...
            ],
        }

    ``blocking_item`` is the one T1 item this roll-up names as the block's
    blocker, or ``None`` when ``tier == "T1"``. Candidates are taken in the
    order :func:`build_tier_report` renders items (item id, then partition
    for a mixed-signal block), ranked in three classes
    (:func:`_blocking_t1_item`, issues #2178 and #2203):

    1. an unmet item **this build has no grading rules for**
       (``graded_by_build: False``) wins outright -- the roll-up could not
       evaluate it at all, which no other explanation outranks and which no
       manifest edit can clear;
    2. otherwise the first unmet item that has a check behind it;
    3. otherwise the first unmet **structurally ungradeable** item (items 1,
       2, 9 and 10, which have no `klt` verb at all).

    It is deliberately a single item, not the full unmet list: the roll-up's
    job is "what is the next thing to fix", not a re-rendering of the
    per-block report (open that block's own ``--manifest`` report for the
    full item-by-item detail).

    ``ungraded_items`` is every unmet T1 item with no runnable check behind
    it -- both ungradeable classes above -- in render order and in
    ``blocking_item``'s own shape (:func:`_ungraded_t1_items`), so that
    ranking them never silently hides them. ``[]`` for a block that cites all
    four, and for a block at ``tier: "T1"``.

    No evidence is read or graded here beyond what :func:`build_tier_report`
    already did -- this function only reduces its output, so a block's
    roll-up row and its full tier report can never disagree about *why* it
    isn't T1 yet, and every T1 item (the ungradeable four included) still has
    to be ``"met"`` for ``tier == "T1"``.

    ``drc_coverage`` (issue #2002) is the same kind of reduction over that
    per-block report's ``drc``-kind citations: what deck coverage each
    block's DRC evidence itself reported, so a fleet-wide "which canaries are
    at T1" answer also shows what each of those "clean" verdicts was measured
    inside -- see :func:`_drc_coverage_rows`. It changes no block's tier: a
    block with rule-free drawn layers rolls up exactly as it did before.

    ``source_doc_content_hash`` (issue #2175), at both the top level and on
    each ``blocks[]`` row, is :func:`build_tier_report`'s own field of the
    same name, echoed straight from that block's ``tier_report`` -- never
    recomputed here. Within one call every row's copy is the same value,
    because ``tiers_doc`` is forwarded verbatim to every per-block
    :func:`build_tier_report` call above; the top-level field is that one
    shared value, and is the direct answer to "were these N verdicts taken
    against the same checklist" -- issue #2175's own framing for why a fleet
    roll-up needs this at all. The per-row copy exists for a consumer that
    reduces a *committed history* of
    these roll-ups (e.g. one per CI run, over time) down to per-block rows
    from different runs -- there the row-level hash, not the top-level one
    from whichever run it was pulled from, is what actually travels with
    that row.

    ``build`` (issue #2176) is the same build-identity block
    :func:`build_tier_report` reports, for the same reason: a roll-up
    committed beside a fleet manifest is read later by someone who cannot
    see which `klt` produced it. It is reported once for the whole roll-up
    rather than per block -- one process grades every block. Per-item
    ``graded_by_build`` lives in each block's own tier report; here an item
    this build cannot grade surfaces as an ``ungraded_items`` row **and** as
    the ``blocking_item`` (class 1 above, issue #2203), with
    ``reason: "ungradeable_by_build"`` -- or ``"no_evidence"`` if it was
    never cited -- naming the *build*, not the manifest, as what is missing.

    ``blocks[].build_t1_item_count`` (issue #2202) is the *reverse*
    divergence, and unlike per-item ``graded_by_build`` it is carried here
    as well as in each block's own ``--manifest`` report -- because
    ``blocks[].t1_item_count``, the count whose shortfall it discloses, is
    carried here too. A row reading ``T1: 9/9 items met`` against a doc older
    than this build is a weaker claim than ``11/11`` and must not be
    indistinguishable from it in the roll-up either. Its value is identical
    across rows within one roll-up for blocks of the same ``kind``
    (``tiers_doc`` and the build are both shared), and doubles for a
    ``mixed-signal`` row exactly as ``t1_item_count`` does.

    Raises :class:`SignoffError` if ``fleet`` is not a JSON object, its
    ``blocks`` field is missing, not a JSON array, or empty; if any
    ``blocks[]`` entry is neither a string nor a JSON object, or a
    string entry cannot be read/parsed as JSON; or if any resolved block
    manifest is not a JSON object or has no non-empty string ``block``
    field. Also propagates :class:`SignoffError` /
    :class:`~klayout_tools.design_evidence_tiers.DesignEvidenceTiersError`
    from :func:`build_tier_report` for a structurally invalid per-block
    manifest (e.g. a missing/invalid ``kind``) -- a malformed block is a
    fleet-manifest authoring error, not a "no evidence yet" grading outcome,
    so it aborts the whole roll-up rather than rendering that one block as
    silently unmet.
    """
    if not isinstance(fleet, dict):
        raise SignoffError(
            f"fleet manifest must be a JSON object, got {type(fleet).__name__}"
        )

    raw_blocks = fleet.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise SignoffError(
            "fleet manifest 'blocks' must be a non-empty JSON array of "
            "block manifests (or paths/'-' to them)"
        )

    blocks: list[dict[str, Any]] = []
    t1_count = 0
    for index, raw_entry in enumerate(raw_blocks):
        source, manifest = _read_fleet_block_manifest(raw_entry, index)

        if not isinstance(manifest, dict):
            raise SignoffError(
                f"fleet manifest blocks[{index}] must resolve to a JSON "
                f"object, got {type(manifest).__name__}"
            )

        block_name = manifest.get("block")
        if not isinstance(block_name, str) or not block_name:
            raise SignoffError(
                f"fleet manifest blocks[{index}] resolves to a manifest with "
                "no non-empty 'block' name -- required to identify the "
                "canary in the roll-up"
            )

        tier_report = build_tier_report(manifest, tiers_doc=tiers_doc)
        blocking_item = _blocking_t1_item(tier_report["items"])
        if tier_report["tier"] == "T1":
            t1_count += 1

        blocks.append(
            {
                "block": block_name,
                "source": source,
                "kind": tier_report["kind"],
                "tier": tier_report["tier"],
                "t1_item_count": tier_report["t1_item_count"],
                # Issue #2202: carried here too, unlike per-item
                # `graded_by_build`, because `t1_item_count` itself is
                # carried here -- the shortfall this discloses is a property
                # of that count, so the two must never be rendered apart.
                "build_t1_item_count": tier_report["build_t1_item_count"],
                "t1_met_count": tier_report["t1_met_count"],
                "source_doc_content_hash": tier_report["source_doc_content_hash"],
                "blocking_item": blocking_item,
                "ungraded_items": _ungraded_t1_items(tier_report["items"]),
                "drc_coverage": _drc_coverage_rows(tier_report["items"]),
            }
        )

    return {
        "schema_version": FLEET_REPORT_SCHEMA_VERSION,
        "block_count": len(blocks),
        "t1_count": t1_count,
        "not_t1_count": len(blocks) - t1_count,
        "source_doc": doc_source_label(tiers_doc),
        # `blocks` is never empty here: `raw_blocks` was already required to
        # be non-empty above, and the loop above appends exactly one row (or
        # raises) per `raw_blocks` entry. Every row's hash is identical
        # within one call (see this function's docstring), so the first row
        # names the shared value for the whole roll-up.
        "source_doc_content_hash": blocks[0]["source_doc_content_hash"],
        "build": _build_identity(),
        "blocks": blocks,
    }


#: The tier/fleet-report paths ``--check`` excludes from its drift diff
#: (issue #2249): the ``build`` block, and only it.
#:
#: This is the same exclusion
#: :data:`~._report_verify.VOLATILE_PROVENANCE_PATHS` makes for the five
#: verbs that already have ``--check`` ("tool identity is excluded from the
#: diff, and only tool identity" -- ``docs/json-contract.md``), applied to
#: the field *this* verb's reports carry it in.
#:
#: **Why it has to be excluded at all.** A ``build`` block states how the
#: *install* was provisioned, not only which commit it came from, so two
#: byte-legitimate installs of the **same pinned commit** can report
#: different ones -- and therefore different report bytes -- while grading
#: every item identically:
#:
#: - ``uv tool install "klayout-tools @ git+URL@<sha>"`` builds in a
#:   package-manager-owned scratch checkout, so ``hatch_build.py`` records
#:   real git facts: ``git_commit: "<sha>"``, ``version: "X.Y.Z+g<sha>"``
#:   (and, before issue #2248, ``dirty: true`` from that checkout's own
#:   build-tool residue).
#: - ``pip install`` of a source *tarball* of the same ``<sha>`` (a GitHub
#:   ``/archive/<sha>.tar.gz``, a vendored copy) builds in a tree with no
#:   ``.git`` at all, so the hook records nothing and the same commit
#:   reports ``git_commit: null``, ``version: "X.Y.Z+unknown"``,
#:   ``is_release: null``.
#:
#: Neither is wrong -- each honestly describes the build it identifies, which
#: is exactly why issue #2176 put it in the report -- and no fix to ``dirty``
#: can collapse the second case, because the facts were never present to
#: record. So "did this committed report drift" cannot be answered by
#: comparing report *bytes*; it is answered by comparing everything the
#: grading actually depends on, which is every other field. Every one of
#: those is a function of the manifest, the cited evidence and the tiers doc
#: -- including ``source_doc`` (normalised to
#: :data:`~.design_evidence_tiers.CANONICAL_DOC_LABEL` regardless of whether
#: this install reads its bundled copy or a checkout's ``docs/``) and
#: ``build_t1_item_count``/``items[].graded_by_build`` (properties of the
#: grading code, identical for a given commit however it was installed).
#:
#: A committed report that carries **no** ``build`` block at all (rendered
#: before issue #2176) is unaffected: an excluded path is skipped whether or
#: not either side has it, so such a report still verifies on its graded
#: content rather than reporting one spurious whole-block drift entry.
VOLATILE_REPORT_PATHS: frozenset[tuple[str, ...]] = frozenset({("build",)})


def check_tier_report(
    report_path: str, manifest: dict[str, Any], *, tiers_doc: str | None = None
) -> dict[str, Any]:
    """``klt signoff --manifest M --check REPORT`` (issue #2249): verify that
    a previously committed tier report at ``report_path`` still reproduces
    from manifest ``M``, ignoring build identity.

    Re-grades ``manifest`` (:func:`build_tier_report` -- the same work
    rendering the report does, including actually running any command-backed
    evidence it cites) and diffs the result against the committed report,
    excluding :data:`VOLATILE_REPORT_PATHS`. ``status: "match"`` when nothing
    else moved; ``"drifted"``, naming every field that did, otherwise --
    a changed item ``status``/``reason``, a moved citation ``content_hash``,
    a different ``source_doc_content_hash`` (the checklist itself changed), a
    different ``t1_met_count``.

    This is the mode a consumer gating on "does the committed evidence still
    hold" should use **instead of byte-comparing the report file against a
    fresh render**: that comparison fails between two correct installs of the
    same pinned commit, on nothing but how each was provisioned (see
    :data:`VOLATILE_REPORT_PATHS`).

    Raises :class:`SignoffError` for a missing/unparseable committed report,
    or one that is not a tier report at all -- never a traceback.
    """
    committed = _load_committed_signoff_report(report_path, "items", "--manifest")
    fresh = build_tier_report(manifest, tiers_doc=tiers_doc)
    return build_rerun_result(
        report_path=report_path,
        committed=committed,
        fresh=fresh,
        exclude=VOLATILE_REPORT_PATHS,
        mode="check",
    )


def check_fleet_report(
    report_path: str, fleet: dict[str, Any], *, tiers_doc: str | None = None
) -> dict[str, Any]:
    """``klt signoff --fleet F --check REPORT`` (issue #2249): the
    :func:`check_tier_report` contract, one level up -- verify a committed
    *fleet* roll-up still reproduces from fleet manifest ``F``.

    Inherits everything, including the exclusion, by re-rendering through
    :func:`build_fleet_report` (which itself calls :func:`build_tier_report`
    once per block): a roll-up carries exactly one ``build`` block, at the
    top level, so the same one-path exclusion covers it.
    """
    committed = _load_committed_signoff_report(report_path, "blocks", "--fleet")
    fresh = build_fleet_report(fleet, tiers_doc=tiers_doc)
    return build_rerun_result(
        report_path=report_path,
        committed=committed,
        fresh=fresh,
        exclude=VOLATILE_REPORT_PATHS,
        mode="check",
    )


def _load_committed_signoff_report(
    report_path: str, required_key: str, flag: str
) -> dict[str, Any]:
    """Load the committed report ``--check`` will diff against, refusing one
    the requested mode could not have produced (issue #2249).

    :func:`~._report_verify.load_committed_report` already rejects a missing,
    unparseable, or non-object file. This adds the one shape question that
    matters here: a tier report always carries ``items``, a fleet roll-up
    always carries ``blocks``, so pointing ``--manifest --check`` at a fleet
    roll-up (or at an unrelated JSON object) is refused as a *failure to
    verify* -- exit ``1`` -- rather than diffed into a "drifted" verdict
    listing every field of both shapes, which would read as evidence drift
    when the real fault is the wrong file path.
    """
    committed = load_committed_report(report_path, SignoffError)
    if required_key not in committed:
        raise SignoffError(
            f"committed report '{report_path}' carries no '{required_key}' key "
            f"-- {flag} --check expects a report this mode produced (a "
            f"'{required_key}' array is what identifies one); check the path"
        )
    return committed


def _read_fleet_block_manifest(raw_entry: Any, index: int) -> tuple[str | None, Any]:
    """Resolve one ``fleet["blocks"][index]`` entry into ``(source,
    manifest)``: ``source`` is the file path/``"-"`` the manifest was read
    from, or ``None`` for an inline manifest object. Raises
    :class:`SignoffError` if ``raw_entry`` is neither a string nor a JSON
    object, or a string entry cannot be read/parsed as JSON."""
    if isinstance(raw_entry, dict):
        return None, raw_entry
    if isinstance(raw_entry, str):
        return raw_entry, _read_json_source(raw_entry, "fleet block manifest")
    raise SignoffError(
        f"fleet manifest blocks[{index}] must be a JSON object or a file "
        f"path string, got {type(raw_entry).__name__}"
    )


def _drc_coverage_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The coverage disclosure carried by every ``"met"`` ``drc``-kind
    citation in one block's rendered items (issue #2002), one row per
    citation: ``{"item", "partition", "layers_in_stream_without_rules",
    "rules_skipped", "deck_scope"}``.

    A list rather than a single object because a mixed-signal block renders
    item 3 once per partition, and because a row is identified by the item it
    backs, not assumed to be item 3 -- nothing here hard-codes which item a
    DRC citation may appear under.

    Empty when no item resolved to a coverage-reporting `klt drc` envelope:
    an unmet item 3, a cited envelope committed before ``coverage`` existed,
    or a fleet whose blocks predate it entirely. **Empty therefore means "no
    coverage was reported", never "the deck covered everything"** -- the same
    distinction :func:`_drc_coverage_disclosure` draws by returning ``None``.

    Reduces the per-block tier report this roll-up already computed; it reads
    no evidence of its own, so a block's roll-up row and its full tier report
    can never disagree about what the deck covered -- the same discipline
    :func:`_blocking_t1_item` follows.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        citation = item.get("citation") or {}
        coverage = citation.get("coverage")
        if not isinstance(coverage, dict):
            continue
        rows.append(
            {
                "item": item["id"],
                "partition": item["partition"],
                **coverage,
            }
        )
    return rows


def _blocking_t1_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a trimmed view of the T1 item in ``items`` this roll-up reports
    as the block's blocker, or ``None`` if every T1 item is met.

    The candidate order is :func:`build_tier_report`'s own item order (item
    id, then partition), with a **three-class priority** layered on top
    (issues #2178, #2203). Concretely:

    1. the first unmet T1 item this build has no grading rules for at all
       (:func:`_is_ungradeable_by_build`), if any;
    2. otherwise the first unmet T1 item that has a check behind it;
    3. otherwise the first unmet **structurally ungradeable** T1 item
       (:func:`_is_structurally_ungradeable_item` -- items 1, 2, 9 and 10
       today);
    4. otherwise ``None`` (every T1 item is met).

    **Rule 2 over rule 3 is issue #2178's rule.** ``docs/cli/signoff.md``'s
    "Items 1, 2, 9, and 10" section tells a manifest author that the honest
    default for those four is to leave them **uncited**, since no `klt` verb
    can check their topical relevance -- which means every honestly-authored
    manifest renders item 1 ``unmet``/``no_evidence`` *by construction*.
    Reducing on "first unmet in render order" therefore answered "blocked on
    item 1" for every such block, hiding whatever its real gaps were.
    Demoting those four keeps the answer to "what is the next thing to fix" a
    thing that can actually be fixed by running something. Rule 3 then keeps
    the degenerate case honest rather than silently clean: a block whose
    *only* unmet items are those four is still not T1, so it must still name
    a blocker -- one of those four -- never ``None``.

    **Rule 1 is issue #2203's rule, and it is the deliberate opposite.**
    Before it, an ``ungradeable_by_build`` row (an item id only a
    ``--tiers-doc``/``$KLT_TIERS_DOC`` copy of the doc lists) was swept into
    rule 3 *incidentally*, because it satisfies
    :func:`_is_structurally_ungradeable_item` too -- an id no grading table
    names is structurally ungradeable by that function's own derivation. But
    #2178's argument for demoting an item does not transfer to it, and in
    fact inverts:

    - **Items 1/2/9/10 are demoted because they are expected.** Every honest
      manifest has them unmet; they are the roll-up's background noise, and
      a weak answer to "why isn't this block T1 yet".
    - **An ``ungradeable_by_build`` row is never expected**, and it is the
      *sharpest* available answer to that question: it says this roll-up
      could not evaluate that item at all. Every other explanation it could
      print is conditional on it being able to grade the checklist it was
      handed; when it cannot, naming some other item implies a completeness
      the verdict does not have. It is also the only class of blocker a
      manifest edit cannot clear -- a ``graded_by_build: False`` item can
      never render ``"met"`` on this build, so "blocked on item 4, run `klt
      lvs`" would point the reader at work that cannot get this block to T1.
      The fix is a newer `klt` (or grading against the doc this one ships).

    So it outranks *both* other classes, cited (``ungradeable_by_build``) or
    uncited (``no_evidence``) -- the class is read from ``graded_by_build``,
    not from ``reason``, because the build is the gap either way.

    The full set demoted by rules 1 and 2 -- both ungradeable classes -- is
    reported alongside as the roll-up row's ``ungraded_items``
    (:func:`_ungraded_t1_items`), so nothing is dropped. When rule 1 fires,
    the named blocker is itself one of those rows; that was already true of
    rule 3's fallback before #2203, so the two fields' relationship is
    unchanged.

    T2-T4 ladder rows are never candidates -- they are always ``"unmet"`` by
    design (this toolkit's closed loop targets T1) and are not what gates the
    ``tier == "T1"`` verdict this roll-up reports against.

    This stays a pure reduction of ``items``: no evidence is re-read and no
    item's ``status``/``reason`` is recomputed, so the row and the block's own
    tier report can never disagree about *why* an item is unmet -- only about
    which unmet item is worth naming first.
    """
    # One pass, three "first match" slots rather than an early return: rule 1
    # dominates rules 2 and 3, and an ungradeable-by-build item can sit
    # anywhere in render order, so no candidate is final until every T1 row
    # has been seen.
    by_build: dict[str, Any] | None = None
    gradeable: dict[str, Any] | None = None
    structural: dict[str, Any] | None = None

    for item in items:
        if item["tier"] != "T1" or item["status"] == "met":
            continue
        # Order matters: today every ungradeable-by-build id is *also*
        # structurally ungradeable, so this branch has to be asked first for
        # the two classes to stay distinguishable at all.
        if _is_ungradeable_by_build(item):
            if by_build is None:
                by_build = _blocking_item_view(item)
        elif _is_structurally_ungradeable_item(item["id"]):
            if structural is None:
                structural = _blocking_item_view(item)
        elif gradeable is None:
            gradeable = _blocking_item_view(item)

    for candidate in (by_build, gradeable, structural):
        if candidate is not None:
            return candidate
    return None


def _ungraded_t1_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every unmet T1 item in ``items`` with **no runnable check behind it in
    this roll-up**, in render order, trimmed to the same ``{"id", "title",
    "partition", "reason"}`` shape ``blocking_item`` uses (issues #2178,
    #2203).

    That is the union of the two ungradeable classes
    :func:`_blocking_t1_item` ranks separately:

    - **structurally ungradeable** (:func:`_is_structurally_ungradeable_item`
      -- items 1, 2, 9 and 10 today): no `klt` verb exists for the id at all;
    - **ungradeable by this build** (:func:`_is_ungradeable_by_build`): an id
      only a ``--tiers-doc``/``$KLT_TIERS_DOC`` copy of the doc lists, which
      this build has no rules for.

    The union is written out explicitly even though the first predicate
    happens to cover both populations today (issue #2203): this function's
    contract is the union, not whatever one derived predicate incidentally
    spans, and relying on that coincidence is exactly what made the two
    indistinguishable in the blocker reduction.

    Reported so that ranking those items below a runnable gap *demotes* them
    rather than hides them: a reader still sees that this block claims items
    1/2/9/10 with nothing cited behind them, but is no longer told that is
    the one thing standing between it and T1 when a real, runnable gap exists
    elsewhere. Empty for a block that cites all four (met items are never
    listed) and for a block at ``tier: "T1"``.

    Unchanged by #2203's re-ranking: which rows are listed here is the same
    set as before -- only which of them ``blocking_item`` names can move.

    Like :func:`_drc_coverage_rows`, this reduces the per-block tier report
    this roll-up already computed and reads no evidence of its own.
    """
    return [
        _blocking_item_view(item)
        for item in items
        if item["tier"] == "T1"
        and item["status"] != "met"
        and (
            _is_structurally_ungradeable_item(item["id"])
            or _is_ungradeable_by_build(item)
        )
    ]


def _blocking_item_view(item: dict[str, Any]) -> dict[str, Any]:
    """The trimmed, roll-up-sized view of one rendered tier-report item --
    shared by ``blocking_item`` and every ``ungraded_items`` row so the two
    fields can never drift into different shapes."""
    return {
        "id": item["id"],
        "title": item["title"],
        "partition": item["partition"],
        "reason": item["reason"],
    }

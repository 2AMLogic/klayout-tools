"""T1 item 11 ("Power delivery (structural)") grading subsystem for ``klt signoff``.

Split out of ``signoff.py`` (issue #2626), following the precedent of
``extract.py``'s splits (``extract_report.py`` #2070, ``extract_abstract.py``
#1303, ``extract_spef.py`` #1195, ``extract_parasitics.py`` #1572) and
``signoff.py``'s own prior split (``signoff_envelopes.py``, issue #2313).
This module owns the power-delivery/ERC-supply-grading subsystem behind T1
item 11: reading and hash-verifying a cited ERC supply spec
(:func:`_erc_supply_spec` and its candidate/document/hash helpers), the LVS
and place-and-route halves of the item's two branches
(:func:`_pdn_branch_reason`, :func:`_lvs_reference_carries_supplies`), and
rendering the item's final report entry (:func:`_build_power_delivery_item`,
via :func:`_grade_power_delivery`).

Every name here is private (`_`-prefixed) implementation detail -- none
appears in ``signoff.py``'s ``__all__``. The single outward call site is
``signoff.py``'s ``build_tier_report``, which imports
:func:`_build_power_delivery_item` back in. The eight reason constants (plus
:data:`_POWER_DELIVERY_KINDS`) that are specific to this item moved here
with their functions, since nothing outside this subsystem ever referenced
them; the generic reasons shared across every tier item
(:data:`~klayout_tools.signoff._REASON_CHECK_FAILED`,
:data:`~klayout_tools.signoff._REASON_WRONG_KIND`, etc.) stay in
``signoff.py`` and are imported back into the functions that need them --
deferred into each function body, not at module level, because
``signoff.py`` imports this module at its own top level (the same
circular-import hazard ``extract_report.py`` documents for its own single
back-reference into ``extract.py``).
"""

from __future__ import annotations

import json
from typing import Any

from ._provenance import sha256_file

#: Issue #2025, T1 item 11 ("Power delivery (structural)") only. Six
#: reasons, not one, for the same reason
#: :data:`~klayout_tools.signoff._REASON_NOT_POST_LAYOUT` is distinct from
#: :data:`_REASON_WRONG_KIND`: item 11 is a *compound* claim,
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
#:   read at *any* path it could mean from here (issue #2608 --
#:   :func:`_erc_supply_spec_candidates` tries the evidence file's own
#:   directory as well as the producing run's, so this no longer fires for a
#:   spec committed beside its evidence), declares no ``"kind": "supply"``
#:   net, declares no ``ties[]``
#:   with no disclosure of why (see :data:`_REASON_SUPPLY_SPEC_DISCLOSED_UNEXPRESSIBLE`
#:   below for the disclosed case, issue #2234) -- an uncomputed check is
#:   not a clean one -- declares a ``ties[]`` entry the ERC run itself
#:   reported as *degenerate* (issue #2199, see
#:   :func:`_erc_missing_tie_skipped` -- a check that could not tell a tap
#:   from a source/drain contact is likewise not a clean one), or its
#:   stackup does not cover every strap layer the P&R response reports.
#:   Deliberately distinct from
#:   :data:`~klayout_tools.signoff._REASON_STALE_EVIDENCE`/
#:   :data:`~klayout_tools.signoff._REASON_UNVERIFIABLE_PROVENANCE` (issue
#:   #2496): a spec document
#:   that could be read *and* still matches the envelope's own
#:   ``provenance.spec.content_hash``, but simply declares an incomplete
#:   set, is a layout-authoring gap (fix the spec); a spec whose content no
#:   longer matches what the envelope pins, or that pins nothing at all, is
#:   a provenance gap (re-run `klt erc`, or upgrade past #2049) -- see
#:   :func:`_erc_supply_spec_hash_reason`.
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
#:   (``erc.unconnected_net``), a declared supply's owned roles carry
#:   conductor reachable from no label at all (``erc.unlabelled_conductor``,
#:   issue #2524 -- the severed single-label rail the island count
#:   structurally cannot see), two declared supplies resolved to the same
#:   island (``erc.supply_short``), or a well/tub has no connected tap
#:   (``erc.missing_tie``). Fix: the layout.
#: - :data:`_REASON_LVS_SUPPLY_UNPROVEN` -- the LVS half of the item is not
#:   proven: for a digital partition with a PDN citation, the same report's
#:   ``power_connectivity.status`` is not ``"match"`` (``"unchecked"`` does
#:   not satisfy item 11, unlike item 4); otherwise, its
#:   ``net_correspondence`` does not pair every declared supply net to a
#:   reference-side net, so the supplies were not part of the compare.
#: - :data:`_REASON_LVS_DID_NOT_PASS` -- the ERC half of the item *is*
#:   complete (every declared supply resolved to its declared island count,
#:   every declared tie was computed and found, no supply-side finding), and
#:   the cited LVS report did not pass on its own terms, so the LVS half is
#:   **unavailable** rather than answered. Still ``"unmet"`` -- an
#:   unavailable half is not a proven one -- but distinct from the plain
#:   :data:`~klayout_tools.signoff._REASON_CHECK_FAILED` a failing LVS
#:   citation renders when the ERC
#:   half proves nothing either (issue #2495). The distinction is what the
#:   item's own scope note asks for: ``net_correspondence`` lists only
#:   *matched* nets, so the supply-pairing predicate
#:   (:func:`_lvs_reference_carries_supplies`) is destroyed by **any**
#:   mismatch -- including a device-parameter delta or a signal-net
#:   reconnection that cannot make or break a supply connection. Without this
#:   reason, "this block's rails are proven continuous and tied; its compare
#:   has one disclosed, supply-irrelevant defect" and "this block cited no
#:   supply evidence at all" render identically. Fix: the LVS defect (item 4
#:   is blocked on it too) -- never the ERC spec, which already asked and
#:   answered.
_REASON_NO_PDN = "no_pdn"
_REASON_SUPPLY_SPEC_INCOMPLETE = "supply_spec_incomplete"
_REASON_SUPPLY_SPEC_DISCLOSED_UNEXPRESSIBLE = "supply_spec_disclosed_unexpressible"
_REASON_SUPPLY_SPEC_DISCLOSED_TOOL_LIMITATION = "supply_spec_disclosed_tool_limitation"
_REASON_SUPPLY_NOT_CONTINUOUS = "supply_not_continuous"
_REASON_LVS_SUPPLY_UNPROVEN = "lvs_supply_unproven"
_REASON_LVS_DID_NOT_PASS = "lvs_did_not_pass"

#: The three :func:`~klayout_tools.signoff._classify` kinds T1 item 11's
#: compound evidence list may
#: cite (issue #2025). A cited part of any other recognised kind renders
#: :data:`~klayout_tools.signoff._REASON_WRONG_KIND` -- it proves nothing
#: about power delivery, and
#: item 11 must never reach ``"met"`` by borrowing an unrelated check's pass.
_POWER_DELIVERY_KINDS: frozenset[str] = frozenset({"erc", "lvs", "place-and-route"})


def _erc_supply_spec_hash_reason(
    envelope: dict[str, Any], resolved_path: str
) -> str | None:
    """Verify the spec document :func:`_erc_supply_spec` just read against
    the envelope's own ``provenance.spec.content_hash`` (issue #2049) --
    ``None`` when they agree (or there is nothing to check against), else
    the ``_REASON_*`` :func:`_erc_supply_spec` should fail with (issue
    #2496).

    `klt erc`'s envelope pins the spec document's content at the time it
    ran, the same ``sha256:``-prefixed way ``provenance.input.content_hash``
    pins the layout (``erc.py``, issue #2036). :func:`_erc_supply_spec`
    re-reads that document off disk to recover the declarations the
    envelope itself does not echo -- so, without this check, editing the
    spec *after* the run (adding a supply net, flipping a ``kind``, editing
    a ``ties[]`` entry) silently changes what item 11 is graded on while
    the cited ``erc_findings``/``erc_coverage`` still describe the old
    declarations. This re-hashes the document at ``resolved_path`` --
    exactly the file `klt erc` hashed to produce
    ``provenance.spec.content_hash`` in the first place -- and compares.

    Returns :data:`~klayout_tools.signoff._REASON_STALE_EVIDENCE` when the
    document *can* be
    hashed but disagrees with the recorded pin -- the same reason a
    manifest-pinned ``content_hash`` mismatch on any other item renders,
    since both describe "this evidence no longer matches the revision it
    was recorded against". Returns
    :data:`~klayout_tools.signoff._REASON_UNVERIFIABLE_PROVENANCE`
    when there is nothing to compare against: no ``provenance.spec`` block
    at all (every `klt erc` envelope produced before #2049), or the
    document could no longer be hashed at all (it existed a moment ago, for
    :func:`_erc_supply_spec`'s own read, but this second read failed --
    treated the same "nothing to compare against" way rather than as a
    confirmed mismatch, since no comparison was actually made). Returns
    ``None`` -- proceed -- only when a recorded hash is present and matches.
    """
    from .signoff import _REASON_STALE_EVIDENCE, _REASON_UNVERIFIABLE_PROVENANCE

    provenance = envelope.get("provenance")
    spec_provenance = provenance.get("spec") if isinstance(provenance, dict) else None
    recorded_hash = (
        spec_provenance.get("content_hash")
        if isinstance(spec_provenance, dict)
        else None
    )
    if not isinstance(recorded_hash, str) or not recorded_hash:
        return _REASON_UNVERIFIABLE_PROVENANCE
    digest = sha256_file(resolved_path)
    actual_hash = f"sha256:{digest}" if digest is not None else None
    if actual_hash is None:
        return _REASON_UNVERIFIABLE_PROVENANCE
    if actual_hash != recorded_hash:
        return _REASON_STALE_EVIDENCE
    return None


def _erc_supply_spec_candidates(spec_path: str, spec: dict[str, Any]) -> list[str]:
    """Every filesystem path the cited `klt erc` envelope's own ``spec``
    field could mean **from this grading context**, in the order
    :func:`_erc_supply_spec_document` should try them (issue #2608).

    A path an envelope names is not portable -- it was written relative to
    whatever directory the producing run used, which need not be the one
    grading happens in
    (:func:`~klayout_tools.signoff._resolve_input_artifact_value` documents
    the same hazard for the input-artifact re-hash, and resolves it the same
    two-candidate way). So:

    - relative to the **evidence file's own directory** first
      (:func:`~klayout_tools.signoff._resolve_relative_to_report`), the way
      a reader who opened the
      ERC report and followed its reference would. This is the ordinary `klt
      erc supply.gds erc_supply_spec.json` run made from the layout
      directory -- the natural place to run a per-block check -- whose
      envelope records ``"spec": "erc_supply_spec.json"``, a path that
      resolves only from there. ``None`` for a command-backed entry (the
      cwd resolution below already *is* "the producing run's own
      directory" for one) and for an absolute path (nothing to rebase).
    - relative to the directory the producing run itself used
      (:func:`~klayout_tools.signoff._resolve_relative_to_spec`:
      ``spec["cwd"]`` for a
      command-backed entry, this process's own cwd otherwise) second -- the
      pre-#2608 behaviour, kept as the compatibility fallback for a spec
      document that genuinely lives elsewhere.

    Exactly the resolution order issue #2197 gave `klt yield`'s samples
    document (:func:`~klayout_tools.signoff._yield_samples_content_hash`);
    this was the last
    "the envelope points at a second document" site still resolving from
    one directory only.
    """
    from .signoff import _resolve_relative_to_report, _resolve_relative_to_spec

    candidates: list[str] = []
    evidence_relative = _resolve_relative_to_report(spec_path, spec)
    if evidence_relative is not None:
        candidates.append(evidence_relative)
    candidates.append(_resolve_relative_to_spec(spec_path, spec))
    return list(dict.fromkeys(candidates))


def _erc_supply_spec_document(
    envelope: dict[str, Any], spec: dict[str, Any], spec_path: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the ERC spec document ``spec_path`` names, trying each path it
    could mean here (:func:`_erc_supply_spec_candidates`) -- ``(document,
    None)`` for the first candidate that both parses as a JSON object *and*
    still matches the envelope's own ``provenance.spec.content_hash``
    (:func:`_erc_supply_spec_hash_reason`).

    A **hash match decides** which candidate is the cited document, for the
    same reason :func:`~klayout_tools.signoff._verify_input_artifact` prefers
    a match over a
    mismatch: two directories can hold a same-named spec, and a
    coincidentally-named neighbour must never turn a fresh citation into a
    reported ``stale_evidence``. When no candidate matches, the *first*
    readable candidate's own reason is reported (``stale_evidence`` /
    ``unverifiable_provenance``) -- a genuinely edited or unpinned spec
    still fails exactly as it did before this fallback existed. ``(None,
    None)`` only when no candidate could be read and parsed at all.
    """
    from .signoff import SignoffError, _read_json_source

    read_reason: str | None = None
    for candidate in _erc_supply_spec_candidates(spec_path, spec):
        try:
            document = _read_json_source(candidate, "erc spec")
        except SignoffError:
            continue
        if not isinstance(document, dict):
            continue
        hash_reason = _erc_supply_spec_hash_reason(envelope, candidate)
        if hash_reason is None:
            return document, None
        if read_reason is None:
            read_reason = hash_reason
    return None, read_reason


def _erc_supply_spec(
    resolution: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the **spec document** a resolved `klt erc` citation names
    (``envelope["spec"]``), and reduce it to the facts T1 item 11 grades
    against -- or ``(None, <reason>)`` when it cannot be read or parsed
    (``reason`` is ``None``), or when it can be read but no longer matches
    the envelope's own recorded ``provenance.spec.content_hash`` (``reason``
    is :data:`~klayout_tools.signoff._REASON_STALE_EVIDENCE` or
    :data:`~klayout_tools.signoff._REASON_UNVERIFIABLE_PROVENANCE`
    -- see :func:`_erc_supply_spec_hash_reason`, issue #2496).

    Returns ``({"supply_nets": [<name>, ...], "stackup": {<name/layer>,
    ...}, "tie_count": <int>, "ties_disclosure_reason": <str> | None}, None)``
    on success:

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
    ``provenance`` block (see
    :func:`~klayout_tools.signoff._yield_samples_content_hash`): read the
    document the envelope names, via the same two-candidate resolution,
    rather than fabricate a verdict from its absence. ``klt signoff`` stays a
    pure *consumer* either way -- it changes no verb's own output.

    **Where the document is looked for** (issue #2608):
    :func:`_erc_supply_spec_candidates` -- the evidence file's own directory
    first, then the producing run's own (``spec["cwd"]``, else this
    process's cwd). An ERC envelope committed beside the spec it names is
    resolvable only the first way, and one run from a directory this
    grading process cannot know only the second; neither convention is
    guessed at, both are tried, and :func:`_erc_supply_spec_document`
    settles which is the cited document by hash.

    **Verified against the envelope's own hash of it** (issue #2496). A
    second document read off disk is only as trustworthy as its freshness:
    without a check, editing the spec after the ERC run silently changes
    what item 11 grades while the cited findings still describe the old
    declarations. :func:`_erc_supply_spec_hash_reason` re-hashes the
    candidate and compares against ``provenance.spec.content_hash`` (issue
    #2049) before any declaration below is trusted.

    Follow-up reconciliation, exactly as for `klt yield`: if `klt erc` later
    echoes its resolved ``nets``/``ties``/``stackup`` declarations in its own
    envelope, this function should prefer that echo and the disk read
    becomes the fallback -- no change needed at any call site (and the hash
    check above becomes unnecessary for the same reason: the declarations
    would then live *inside* the hash-pinned envelope itself).
    """
    envelope = resolution["envelope"]
    spec_path = envelope.get("spec")
    if not isinstance(spec_path, str):
        return None, None

    document, read_reason = _erc_supply_spec_document(
        envelope, resolution["spec"], spec_path
    )
    if document is None:
        return None, read_reason

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
    }, None


def _erc_supply_findings(
    envelope: dict[str, Any], supply_nets: list[str]
) -> list[dict[str, Any]]:
    """Every ``erc_findings[]`` entry that contradicts T1 item 11's own
    supply-continuity rule -- ``[]`` when the run reports none.

    Exactly five of `klt erc`'s seven rules are graded here
    (``docs/cli/erc.md`` → "ERC finding checks"), and only for the *declared
    supply* nets:

    - ``erc.unconnected_net`` naming a declared supply -- that supply matched
      zero islands (nothing carries its label) or more than one (the rail is
      split into pieces that never touch). Either way it is not the "exactly
      one island per declared supply" the item requires.
    - ``erc.unlabelled_conductor`` naming a declared supply (issue #2524) --
      the case ``erc.unconnected_net`` structurally cannot see. That rule
      counts islands *carrying the declared label*, so a single-label supply
      rail severed into a labelled piece and an unlabelled orphan grades
      clean (``docs/cli/erc.md`` → "`erc.unconnected_net` counts labelled
      islands, not conductor islands"): before this rule existed, item 11
      could not cite a severed-rail negative for such a block at all. A
      supply whose declared owned roles (``nets[].roles``) carry conductor
      reachable from no label is not delivered to everything the spec says
      it owns, which is this item's own question. Graded under the same
      declared-name filter ``erc.unconnected_net`` uses, and reachable only
      for a spec that opted into ``nets[].roles`` -- a spec that declares
      none never acquires this blocker.
    - ``erc.supply_short`` -- two declared supplies resolved to the *same*
      island, which is likewise not one island per supply.
    - ``erc.expected_short_missing`` naming a declared supply (issue #2463)
      -- the spec declared two names as intentionally one net
      (``nets[].same_net_as``) and the layout does not draw the tie, so that
      supply is delivered to only part of what the spec says it feeds. Graded
      under the same declared-name filter ``erc.unconnected_net`` uses, since
      unlike ``erc.supply_short`` this rule can also name two *signal* nets,
      which item 11 says nothing about.
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
        if rule not in (
            "erc.unconnected_net",
            "erc.expected_short_missing",
            "erc.unlabelled_conductor",
        ):
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

    A row's ``layout`` is `klt lvs`'s own ``expanded_name()``-derived alias
    string (``_build_net_correspondence`` in ``lvs_mismatch.py``): for a
    label-merged net -- the ordinary shape a routed supply grid extracts as,
    with per-cell rail labels, strap labels, and the promoted pin label all
    landing on one electrical net -- every alias is joined with ``|``, e.g.
    ``"G_VDDR_M1|MNT_G|VDDR|VDDR1"``. A declared supply name is proven when
    it is *any one* of a row's ``|``-split aliases, not only when it equals
    the row's full joined string -- exact-string equality would reject
    exactly the shape `klt lvs` produces for a real supply grid (issue
    #2405). This mirrors the alias-membership interpretation
    ``_match_net_clusters`` already applies on the erc side of item 11
    (``erc.py``), keeping the two halves' notion of "the declared name
    matches this net" consistent -- ``_match_net_clusters`` splits
    ``expanded_name()`` on KLayout's own ``,`` separator (the raw,
    un-escaped spelling ERC reads directly from ``pya``), while this
    function splits on ``|`` (the ``spice_safe_net_name``-escaped spelling
    `klt lvs` writes into its JSON envelope) -- different separators for the
    same underlying alias set, each matching the convention of the string
    it actually receives.
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
            paired.update(alias.upper() for alias in layout.split("|") if alias)
    return all(name.upper() in paired for name in supply_nets)


def _resolve_power_delivery_parts(
    specs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """Resolve every part of T1 item 11's compound citation (issue #2025) and
    index the resolutions by :func:`~klayout_tools.signoff._classify` kind --
    ``(by_kind, None)`` on
    success, ``(None, <_REASON_* constant>)`` on the first part that cannot
    be used.

    Each part goes through :func:`~klayout_tools.signoff._resolve_evidence`,
    the same
    read/run/classify/hash path every single-artifact item's citation uses,
    and is then subject to the three rules that apply to a part *as a part*:
    an ``error`` envelope is ``check_errored``, a kind outside
    :data:`_POWER_DELIVERY_KINDS` is ``wrong_kind`` (it proves nothing about
    power delivery), and a part whose own pinned ``content_hash`` no longer
    matches is ``stale_evidence`` when the resolved envelope carries a
    different, non-``None`` hash, or
    :data:`~klayout_tools.signoff._REASON_UNVERIFIABLE_PROVENANCE`
    when it carries no input hash at all (issue #2182 -- see that constant's
    docstring). The item-specific rules (:func:`_grade_power_delivery`) apply
    only to a set that survives all of these.

    A later part of the same kind replaces an earlier one -- citing two ERC
    runs for one item is a manifest authoring mistake, not a shape this
    grading has a meaning for; the last one named wins, the same way a
    duplicate JSON key would.
    """
    from .signoff import (
        _REASON_CHECK_ERRORED,
        _REASON_STALE_EVIDENCE,
        _REASON_UNVERIFIABLE_PROVENANCE,
        _REASON_WRONG_KIND,
        _resolve_evidence,
    )

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
    from .signoff import _REASON_CHECK_FAILED, _check_passed

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
    prefix match was chosen for. Issue #2339's
    ``degenerate_well_selection`` -- a declared well-side class selection
    (``ties[].well_requires``/``well_excludes``) that kept every merged
    shape of the drawn well layer, or none of them -- is the second, and
    needed no change here either. Issue #2377's ``empty_well_region`` -- a
    ``ties[]`` entry naming a *drawn* ``well_layer`` with no geometry at all
    in the stream, the unselected form of the same absence-of-evidence state
    -- is the third, and needed no change here either.
    An envelope with no ``erc_coverage`` block
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


#: The item-11 reason each of `klt erc`'s *disclosed* ``erc.missing_tie``
#: coverage reasons renders (issues #2234, #2247) -- consulted by
#: :func:`_erc_missing_tie_disclosed` for its zero-ties entry
#: (``erc.missing_tie:[]``) and, as the recognised-reasons set alone (issue
#: #2623), by :func:`_erc_disclosed_undeclared_tie_classes` for a named-class
#: entry (``erc.missing_tie:["<class>"]``). A reason token outside this table
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

    This is the *zero-ties* half of item 11's disclosure surface only --
    the ``tie_count == 0`` branch of :func:`_resolve_erc_supply_spec`. A
    *partial* declaration (one or more ``ties[]`` entries declared and
    checked, plus one or more further classes named in
    ``ties_disclosure.undeclared_classes`` as inexpressible or
    tool-limited, issue #2541) is a different, ``tie_count > 0`` shape this
    function never sees -- see :func:`_erc_disclosed_undeclared_tie_classes`
    for that half, consulted from item 11's ``"met"`` path instead.

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


def _erc_disclosed_undeclared_tie_classes(
    envelope: dict[str, Any],
) -> list[dict[str, str]]:
    """The *named-class* half of item 11's disclosure surface (issue
    #2541, consumer-side follow-up #2623): every ``erc.missing_tie:["<class>"]``
    entry the cited `klt erc` run recorded in ``erc_coverage.inapplicable``
    with a disclosed reason, as ``[{"class": "<class>", "reason":
    "ties_disclosed_unexpressible" | "ties_disclosed_tool_limitation"}, ...]``
    in the order `klt erc` recorded them -- ``[]`` when the cited run
    disclosed no named class (including every run that declares zero
    ``ties[]`` and disclosed *that* instead, which
    :func:`_erc_missing_tie_disclosed` already surfaces on its own,
    ``tie_count == 0`` path).

    `klt erc` records one ``erc.missing_tie:["<class>"]`` entry per class a
    spec's top-level ``ties_disclosure.undeclared_classes`` names --
    distinct from the bare ``erc.missing_tie:[]`` entry
    :func:`_erc_missing_tie_disclosed` matches, which stands for "no
    ``ties[]`` at all", not for any one named class (``docs/cli/erc.md`` --
    "Well/tap connectivity"). This is what lets a block that declares the
    one well class it *can* express (checked, ``tie_count > 0``, item 11
    reaches ``"met"`` on its own merits) and discloses the other as
    inexpressible or tool-limited carry that second class into the report
    of record instead of it being legible only one layer down, in the cited
    `klt erc` envelope.

    Purely additive: it never changes item 11's ``status``/``reason`` on the
    ``tie_count > 0`` path (a disclosure is the caller's word, never a
    substitute for the computed ``erc.missing_tie`` result the declared
    ties already supplied -- issues #2234/#2247's invariant, unchanged
    here), only what a ``"met"`` citation additionally states. Distinguished
    from the bare zero-ties entry by parsing the work identity's own JSON
    argument list (:func:`~klayout_tools.coverage.work_id`) rather than by
    string length or prefix alone, so a future `klt erc` that changes how it
    spells the class name is still read correctly as long as the one-element
    list shape holds. An envelope with no ``erc_coverage`` block (every
    report before #2179) discloses nothing and is graded exactly as it was.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return []
    classes: list[dict[str, str]] = []
    for record in block.get("inapplicable") or []:
        if not (isinstance(record, dict) and isinstance(record.get("id"), str)):
            continue
        record_id = record["id"]
        prefix = "erc.missing_tie:"
        if not record_id.startswith(prefix):
            continue
        reason = record.get("reason")
        if reason not in _DISCLOSED_TIE_REASONS:
            continue
        try:
            args = json.loads(record_id[len(prefix) :])
        except (TypeError, ValueError):
            continue
        if not (isinstance(args, list) and len(args) == 1 and isinstance(args[0], str)):
            # The bare zero-ties identity (`[]`) or a shape this build does
            # not recognise -- not a named class.
            continue
        classes.append({"class": args[0], "reason": reason})
    return classes


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
    (issues #2255 and #2540): the ``erc.missing_tie`` work identities whose
    **well side** rested on caller-named coordinates rather than purely on
    drawn geometry -- ``[]`` for every run that named none, and for every
    report produced before the field existed. Two spec forms land there:

    - ``ties[].well_layer: null`` + ``ties[].well_boxes`` (issue #2255) -- the
      **well region itself** asserted, because the block sits in a native
      substrate that draws no well/tub layer at all;
    - ``ties[].well_requires_boxes`` / ``ties[].well_excludes_boxes`` (issue
      #2540) -- a *drawn* well whose **class selection** was named in boxes,
      because no drawn layer separates the tub's two bias classes.

    Carried into a ``"met"`` item 11 citation beside
    :func:`_erc_ties_checked_by_assertion`'s tap-side list, and deliberately
    **not** merged into it, because the two state different things about the
    same verdict. A ``tap_boxes`` assertion says which of the drawn tap
    geometry counts as the tap; the well is still drawn, and still measured.
    This list says the *well* side was the caller's word -- the weaker of the
    two claims, and the one a grader is most likely to want to see stated
    explicitly. A purely marker-layer selection
    (``well_requires``/``well_excludes``) is not in it: the stream draws its
    own partition, so that is an ordinary geometrically-derived pass.

    It does not change the verdict: item 11 is ``"met"`` on either asserted
    form exactly as on a drawn-well one. It can be, because both are
    falsifiable and `klt erc` falsifies them where it can -- each asserted
    polygon must independently contain a tap that reaches the declared net, an
    assertion indistinguishable from the whole top-cell extent is rejected as
    degenerate (``docs/cli/erc.md`` → "A block with no drawn well at all"),
    and a box selection keeping every shape of the drawn layer or none of them
    is rejected the same way (same doc → "A two-class tub with no marker layer
    to separate them"). Both land in ``erc_coverage.skipped`` where
    :func:`_erc_missing_tie_skipped` already renders them ``unmet``.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return []
    return [
        identity
        for identity in block.get("checked_by_well_assertion") or []
        if isinstance(identity, str)
    ]


def _erc_supply_unlabelled_islands(
    envelope: dict[str, Any], supply_nets: list[str]
) -> dict[str, int]:
    """The severed-rail **negative** a met item 11 can now cite (issue
    #2524): per declared supply that asserted the ``stackup`` roles it owns
    (``nets[].roles``, issue #2510), that net's ``nets[].unlabelled_islands``
    -- ``0`` meaning every piece of conductor on its owned roles is reachable
    from a label.

    ``{}`` for a cited run whose supplies declared no ``roles`` (and for
    every pre-#2510 envelope, whose ``nets[]`` entries carry no such key) --
    the honest empty, never a fabricated zero: an undeclared role was not
    measured, and this item must never read "not measured" as "checked
    clean".

    Why the item reports it at all. ``erc.unconnected_net`` counts islands
    *carrying the declared label*, so on a single-label block -- the ordinary
    shape for hand-built or generated analog, where the label names a port
    rather than annotating every rail segment -- a severed rail still grades
    clean, and a met item 11 rested on a negative it could not actually
    state (``docs/cli/erc.md`` → "`erc.unconnected_net` counts labelled
    islands, not conductor islands"). ``unlabelled_islands: 0`` **is** that
    negative. Its non-zero counterpart is already a blocker via
    :func:`_erc_supply_findings`'s ``erc.unlabelled_conductor`` clause, so
    this key is strictly the positive-side evidence: what a reader needs to
    see that the clean verdict was measured over the whole conductor, not
    only over what carried a label.

    Name comparison is case-insensitive, matching the declared-supply filter
    every other item-11 predicate applies.
    """
    declared = {name.upper() for name in supply_nets}
    islands: dict[str, int] = {}
    for entry in envelope.get("nets") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        count = entry.get("unlabelled_islands")
        if not isinstance(name, str) or name.upper() not in declared:
            continue
        if isinstance(count, bool) or not isinstance(count, int):
            continue
        islands[name] = count
    return islands


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

    Issue #2496: when :func:`_erc_supply_spec` itself reports a reason (the
    document it re-read no longer matches the envelope's own
    ``provenance.spec.content_hash``, or that envelope predates #2049 and
    carries no such hash at all), that reason is returned directly rather
    than collapsed into :data:`_REASON_SUPPLY_SPEC_INCOMPLETE` -- a stale or
    unverifiable spec is a different failure than one that was faithfully
    read and simply declares no supply.
    """
    from .signoff import _REASON_CHECK_FAILED, _critical_metric_detail

    erc_metric_detail = _critical_metric_detail(erc["envelope"])
    if erc_metric_detail:
        return None, _REASON_CHECK_FAILED, erc_metric_detail

    supply_spec, supply_spec_reason = _erc_supply_spec(erc)
    if supply_spec_reason is not None:
        return None, supply_spec_reason, {}
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


def _failed_lvs_verdict(
    envelope: dict[str, Any], supply_spec: dict[str, Any] | None
) -> tuple[str, dict[str, Any]]:
    """The ``(reason, detail)`` T1 item 11 reports for a cited LVS report
    that did not pass on its own terms
    (:func:`~klayout_tools.signoff._check_passed`), given the
    ERC half's already-resolved ``supply_spec`` (issue #2495).

    A failing LVS citation can never carry item 11 to ``"met"`` -- both
    branches' LVS half rests on the same compare, and an unavailable half is
    not a proven one. What this decides is *which* ``"unmet"`` the report
    states, because the two states behind it call for opposite actions:

    - ``supply_spec is None`` -- the ERC half proved nothing about power
      delivery either (no supply declared, no computed tie, or a supply-side
      finding), so nothing here is established at all. Plain
      :data:`~klayout_tools.signoff._REASON_CHECK_FAILED`, exactly as before
      this split existed.
    - a resolved ``supply_spec`` -- the ERC half *is* complete: every
      declared supply resolved to its declared island count, every declared
      tie was computed and found, and no supply-side finding was reported.
      Only the LVS half is unavailable, which
      :data:`_REASON_LVS_DID_NOT_PASS` says and ``check_failed`` cannot.

    The distinction matters because item 11's analog branch reads its LVS
    half out of ``net_correspondence``, which lists only *matched* nets
    (``_build_net_correspondence`` in ``lvs_mismatch.py``) -- so the
    supply-pairing predicate is destroyed by **any** mismatch, including a
    restated device parameter or a moved signal-net connection that cannot
    make or break a supply connection. Collapsing that into the same
    ``check_failed`` a block with zero supply evidence renders would read as
    "power delivery unverified" when the actual state is "power delivery
    verified structurally; the item's second half is unavailable for an
    unrelated reason".

    The detail keeps the LVS part's declared-critical-metric blockers (issue
    #2094) either way -- they are the reader's pointer to *why* the report
    did not pass -- and, for the distinct reason, adds a ``power_delivery``
    block naming what the ERC half actually proved (its supply nets) beside
    the two verdict tokens that made the LVS half unavailable, so the claim
    the reason makes stays falsifiable against the cited artifacts.
    """
    from .signoff import _REASON_CHECK_FAILED, _critical_metric_detail

    detail = _critical_metric_detail(envelope)
    if supply_spec is None:
        return _REASON_CHECK_FAILED, detail
    return _REASON_LVS_DID_NOT_PASS, {
        **detail,
        "power_delivery": {
            "supply_nets": list(supply_spec["supply_nets"]),
            "lvs_status": envelope.get("status"),
            "power_connectivity_status": (envelope.get("power_connectivity") or {}).get(
                "status"
            ),
        },
    }


def _grade_power_delivery(
    specs: list[dict[str, Any]], *, partition_kind: str
) -> tuple[str, str | None, dict[str, Any] | None, dict[str, Any]]:
    """Grade T1 item 11 ("Power delivery (structural)", issue #2025) against
    the **set** of evidence entries cited for it, and return ``(status,
    reason, citation, failure_detail)`` in the same shape
    :func:`~klayout_tools.signoff._grade_evidence`
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
      drawn-well one -- as does a block whose one drawn tub layer carries two
      bias classes that no drawn layer separates, scoped by caller-named
      boxes (``ties[].well_requires_boxes``/``well_excludes_boxes``, issue
      #2540). Which of its ties rested on the caller's word about the well
      side is stated in the citation's
      ``power_delivery.ties_checked_by_well_assertion``
      (:func:`_erc_ties_checked_by_well_assertion`). Issue #2524 adds the
      severed-rail half of the same question: a supply whose declared owned
      roles (``nets[].roles``) carry conductor reachable from no label is
      ``erc.unlabelled_conductor`` and blocks the item, and a met citation
      states the negative it now rests on in
      ``power_delivery.supply_unlabelled_islands``
      (:func:`_erc_supply_unlabelled_islands`) rather than leaving it
      implicit in a clean island count that could not have seen it. Issue
      #2623: a spec can declare (and get checked) the one well class it
      *can* express while disclosing a further class as inexpressible or
      tool-limited (``ties_disclosure.undeclared_classes``, issue #2541) --
      this does not change the verdict (the declared, checked tie already
      carries item 11 to ``"met"`` on its own merits), but a met citation
      names the disclosed class too, in
      ``power_delivery.disclosed_undeclared_tie_classes``
      (:func:`_erc_disclosed_undeclared_tie_classes`), present only when the
      cited run actually disclosed one;
    - an ``"lvs"`` citation -- the same report item 4 grades, which must
      itself pass (:func:`~klayout_tools.signoff._check_passed`);
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

    **A failing LVS citation blocks both branches, and the reason says which
    half is missing** (issue #2495). The ERC half is therefore resolved
    *before* the LVS pass gate rather than after it: a report that did not
    pass renders :data:`_REASON_LVS_DID_NOT_PASS` when the ERC half is a
    complete, continuous supply spec and plain
    :data:`~klayout_tools.signoff._REASON_CHECK_FAILED`
    when it is not -- see :func:`_failed_lvs_verdict` for why the two must
    not collapse into one token. The item stays ``"unmet"`` either way; no
    block reaches ``"met"`` on an LVS report that did not pass.

    ``partition_kind`` is accepted (and carried into the citation) so the
    report says which column's rule was applied, and so a future per-kind
    divergence has a place to land.

    Every part is resolved through
    :func:`~klayout_tools.signoff._resolve_evidence` -- the same
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
    :data:`_REASON_LVS_SUPPLY_UNPROVEN`,
    :data:`_REASON_LVS_DID_NOT_PASS`) are reserved for a cited set that
    resolved cleanly and still does not prove power delivery.
    Issue #2496: ``stale_evidence``/``unverifiable_provenance`` can also
    arise a second way for the ``erc`` part specifically -- not from the
    manifest's pinned ``content_hash`` above, but from
    :func:`_erc_supply_spec` re-verifying the *second* document it reads
    (the spec named by ``envelope["spec"]``, which the envelope only
    points at, never echoes) against that envelope's own
    ``provenance.spec.content_hash`` before trusting any declaration read
    from it. Both routes render the same two reasons for the same reason:
    a citation whose input can no longer be confirmed against what was
    pinned is not evidence, regardless of which of the part's two
    documents drifted.

    The declared-critical-metric gate (issue #2094) applies to **both** the
    LVS and ERC parts, independent of everything else this function checks.
    The LVS part gets it for free through
    :func:`~klayout_tools.signoff._check_passed`. The ERC
    part does not go through that check at all -- it deliberately
    tolerates unrelated ERC findings (antenna/signal violations on nets this
    item does not care about, see that function's ``"erc"`` note) --
    so its critical metrics are checked directly, via
    :func:`~klayout_tools.signoff._critical_metric_detail`, without making
    those unrelated findings
    newly fatal.
    """
    from .signoff import _REASON_WRONG_KIND, _check_passed, _citation

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

    # Issue #2495: the ERC half is resolved *before* the LVS pass gate, not
    # after it, so a failing LVS citation can say which of the two halves is
    # actually missing (`_failed_lvs_verdict`). Resolution is a pure read of
    # the already-resolved ERC part plus the spec document it names, so
    # doing it unconditionally changes nothing but the reason rendered.
    supply_spec, reason, detail = _resolve_erc_supply_spec(erc)
    if not _check_passed("lvs", lvs["envelope"]):
        lvs_reason, lvs_detail = _failed_lvs_verdict(lvs["envelope"], supply_spec)
        return "unmet", lvs_reason, None, lvs_detail
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
        # Issue #2524: the severed-rail negative, per declared supply that
        # owns its roles -- `{name: unlabelled_islands}`, all zero for a met
        # item (a non-zero one is `erc.unlabelled_conductor`, which
        # `_erc_supply_findings` already blocks on). `{}` when no supply
        # declared `nets[].roles`, which is the honest "not measured" rather
        # than a fabricated clean. See `_erc_supply_unlabelled_islands`.
        "supply_unlabelled_islands": _erc_supply_unlabelled_islands(
            erc["envelope"], supply_spec["supply_nets"]
        ),
    }
    # Issue #2623: a *partial* tie declaration (one or more `ties[]` entries
    # declared and checked -- this is the `tie_count > 0` path, already
    # `"met"` on those merits -- plus one or more further classes disclosed
    # as inexpressible/tool-limited via `ties_disclosure.undeclared_classes`,
    # issue #2541) previously left that disclosure legible only one layer
    # down, in the cited `klt erc` envelope. Additive only -- present only
    # when the cited run actually disclosed a named class, so a run that
    # disclosed nothing renders this citation exactly as it always has; see
    # `_erc_disclosed_undeclared_tie_classes`.
    disclosed_undeclared_tie_classes = _erc_disclosed_undeclared_tie_classes(
        erc["envelope"]
    )
    if disclosed_undeclared_tie_classes:
        citation["power_delivery"]["disclosed_undeclared_tie_classes"] = (
            disclosed_undeclared_tie_classes
        )
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
    counterpart of :func:`~klayout_tools.signoff._build_tier_item`, which
    grades every other item.

    Identical in shape and in its "no evidence is never a pass" discipline;
    the only differences are that a *list* of evidence entries is accepted
    (and required to normalize entry-by-entry, so one malformed part renders
    the whole item :data:`~klayout_tools.signoff._REASON_INVALID_EVIDENCE`
    rather than being
    silently dropped from the cited set), and that grading is delegated to
    :func:`_grade_power_delivery`.

    ``partition_kind`` -- the partition being graded -- both selects the
    per-block-kind rule :func:`_grade_power_delivery` applies and (issue
    #2362) qualifies the evidence-key lookup, exactly as in
    :func:`~klayout_tools.signoff._build_tier_item`; ``partition_boundary``
    (issue #2278) is echoed
    exactly as in that function too.

    ``graded_by_build`` (issue #2176) is reported and honoured exactly as in
    :func:`~klayout_tools.signoff._build_tier_item`. In practice it is
    always ``True`` here --
    reaching this function at all means the item id is a member of
    :data:`~klayout_tools.signoff._ITEMS_GRADED_AS_POWER_DELIVERY`, which is
    one of the two tables
    :func:`~klayout_tools.signoff._is_graded_by_build` derives its answer
    from -- but it is
    threaded through rather than hardcoded so the field has exactly one
    source of truth for every rendered item.
    """
    from .signoff import (
        _REASON_INVALID_EVIDENCE,
        _REASON_NO_EVIDENCE,
        _REASON_UNGRADEABLE_BY_BUILD,
        _lookup_evidence,
        _normalize_evidence_parts,
    )

    citation = None
    failure_detail: dict[str, Any] = {}
    status = "unmet"
    reason: str | None = _REASON_NO_EVIDENCE

    raw_entry = _lookup_evidence(evidence, item_id, partition_kind)
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

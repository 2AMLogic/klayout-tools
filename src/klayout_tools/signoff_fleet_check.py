"""``--check`` drift comparison for the ``klt signoff`` fleet roll-up.

Split out of ``signoff.py`` (issue #5636) with the rest of the fleet
roll-up (issue #827, Phase 1c of epic #706); this module owns the
``--check`` half (issue #2249): verifying a committed tier or fleet
report still reproduces from its manifest (:func:`check_tier_report`,
:func:`check_fleet_report`), the volatile/doc-prose/run-identity path
exclusions the comparison applies (:data:`VOLATILE_REPORT_PATHS`,
:data:`DOC_PROSE_REPORT_PATHS`, :data:`RUN_IDENTITY_REPORT_PATHS`), and
the committed-report loading and diffing both checks share
(:func:`_load_committed_signoff_report`, :func:`_check_signoff_report`).

``signoff_fleet.py`` re-imports all of these at its own top level; the
one call back into it (:func:`check_fleet_report` re-rendering through
:func:`build_fleet_report`) and everything needed from ``signoff.py``
(:class:`SignoffError`, :func:`build_tier_report`) are deferred into the
function bodies that need them -- ``signoff.py`` imports this subsystem
at its own top level, so a module-level back-reference would be a
circular import.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._report_verify import (
    LIST_ITEM,
    build_rerun_result,
    diff_verdict_fields,
    load_committed_report,
    strip_path_patterns,
)
from .signoff_generator_batch import GeneratorExecution

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
#:   real git facts: ``git_commit: "<sha>"``, a version such as
#:   ``"X.Y.(Z+1).devN+g<sha>"`` (``"X.Y.Z+g<sha>"`` before issue #2823)
#:   (and, before issue #2248, ``dirty: true`` from that checkout's own
#:   build-tool residue).
#: - ``pip install`` of a source *tarball* of the same ``<sha>`` (a GitHub
#:   ``/archive/<sha>.tar.gz``, a vendored copy) builds in a tree with no
#:   ``.git`` at all, so the hook records nothing and the same commit
#:   reports ``git_commit: null``, ``version: "X.Y.Z.dev0+unknown"``,
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

#: The tier/fleet-report paths ``--check`` excludes from its **pass/fail**
#: verdict but still reports, separately, as ``doc_drift`` (issue #2526): the
#: report's verbatim quotation of the checklist doc, and the hash of that doc.
#:
#: **The distinction this draws.** ``--check`` exists to answer "does this
#: committed evidence still hold". Two different things can make a committed
#: report stop reproducing, and before issue #2526 both rendered as the same
#: ``"drifted"``:
#:
#: - **the evidence moved** -- an item's ``status``/``reason``, a citation's
#:   ``content_hash``, a count derived from them. That is the answer the gate
#:   exists to produce, and it is untouched here.
#: - **the yardstick's wording moved** -- ``docs/design-evidence-tiers.md``
#:   gained a paragraph under an item. Every ``id``/``tier``/``status``/
#:   ``reason``/``citation`` in the report is identical, but the report
#:   *inlines* the doc's prose (:func:`_build_tier_item`'s ``title``/``text``/
#:   ``notes``, parsed straight out of the doc by
#:   :func:`~.design_evidence_tiers.parse_design_evidence_tiers` and echoed on
#:   unmodified), and ``source_doc_content_hash`` hashes the doc's whole
#:   bytes -- so an upstream wording change alone rewrote a consumer's
#:   committed verdict of record and turned its CI freshness gate red for a
#:   reason it did not cause.
#:
#: **Why exactly these fields.** No grading rule reads any of them: an item's
#: verdict is a function of the manifest's evidence and the item's *id*, and
#: its identity in the report is ``id`` + ``tier`` + ``partition`` -- all
#: still compared. ``title``/``text``/``notes`` are quotation; changing a
#: quotation changes no verdict. ``source_doc_content_hash`` is one step
#: removed but the same class of fact: it moves on *any* byte of the doc,
#: including a typo fix in a section this report does not even render, so it
#: cannot distinguish "the checklist changed what it requires" from "the
#: checklist was reworded". What *would* distinguish that -- the checklist
#: gaining, losing or renumbering an item -- is caught by the fields that stay
#: compared: ``t1_item_count``, ``build_t1_item_count``, ``t1_met_count``, and
#: the per-item ``id``/``graded_by_build`` rows themselves.
#:
#: **It is an exclusion from the verdict, not from the report.** Every path
#: named here that actually moved is still listed, by name, in the response's
#: ``doc_drift_fields`` with ``doc_drift: true`` -- so a consumer that wants
#: to warn (or fail) on "the yardstick moved" still can; it just no longer
#: has to conflate it with "your evidence moved". And an exclusion never
#: masks a co-occurring real change: an item whose ``text`` *and* ``status``
#: both moved still reports ``"drifted"`` on the ``status``.
#:
#: Deliberately **not** excluded: ``build.grading_ruleset_id`` (already
#: covered by :data:`VOLATILE_REPORT_PATHS`'s whole-``build`` exclusion, and
#: still reported verbatim by ``--describe-grader``, which this does not
#: touch) and ``source_doc`` (which names *where* the doc came from -- a
#: report graded against a ``--tiers-doc`` override instead of the bundled
#: copy is a different grading run, not a reworded one).
DOC_PROSE_REPORT_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        # Tier report (`--manifest`).
        ("source_doc_content_hash",),
        ("items", LIST_ITEM, "title"),
        ("items", LIST_ITEM, "text"),
        ("items", LIST_ITEM, "notes"),
        # Fleet roll-up (`--fleet`): the same hash at the top level and once
        # per block, plus the doc-sourced `title` the two reduced item views
        # (`blocking_item`, `ungraded_items[]`) carry. Their `id`/`reason`
        # -- what actually names the blocker -- stay compared.
        ("blocks", LIST_ITEM, "source_doc_content_hash"),
        ("blocks", LIST_ITEM, "blocking_item", "title"),
        ("blocks", LIST_ITEM, "ungraded_items", LIST_ITEM, "title"),
    }
)


#: The tier-report paths ``--check`` drops from **both** the verdict diff and
#: the ``doc_drift`` diff because they identify one Spot run, not the evidence
#: (issue #3690): the ``remote`` block of an operator-approved generator
#: execution (issue #3289) -- ``job_id`` (a fresh ``uuid4`` per run),
#: instance id/type/AZ, ``lifecycle``, ``elapsed_seconds`` and the rest of
#: :func:`~.sim_batch._batch_environment`. Re-grading submits a *new* job, so
#: every one of these differs between any two runs of an unchanged manifest
#: and a self-``--check`` of an executed report could never match.
#:
#: Spelled as one whole-``remote`` subtree per location rather than a key
#: list, so a field the batch environment gains later is volatile too. What
#: stays compared is everything the grade depends on: ``executed_by_klt``,
#: ``execution.recipe``/``command``/``script_sha256``/``input_tree``/
#: ``committed_hash``/``output_hash``. The second path is the same block on a
#: refused or failed run (``detail.generator_execution``). ``--fleet`` reports
#: carry only reduced per-block views, never these rows, so one shape suffices.
RUN_IDENTITY_REPORT_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        (
            "items",
            LIST_ITEM,
            "citation",
            "artifact_binding",
            "regeneration",
            "execution",
            "remote",
        ),
        ("items", LIST_ITEM, "detail", "generator_execution", "remote"),
    }
)


def _check_signoff_report(
    report_path: str, committed: dict[str, Any], fresh: dict[str, Any]
) -> dict[str, Any]:
    """The diff half both ``--check`` modes share (issues #2249, #2526):
    ``{schema_version, mode, report, status, drift, doc_drift,
    doc_drift_fields, fresh}``.

    Two diffs of the same pair, both through
    :func:`~._report_verify.diff_verdict_fields`, both excluding
    :data:`VOLATILE_REPORT_PATHS`:

    1. the **verdict** diff, over a view of each side with
       :data:`DOC_PROSE_REPORT_PATHS` stripped out
       (:func:`~._report_verify.strip_path_patterns`) -- this one alone
       decides ``status`` (and therefore the exit code);
    2. the **whole** diff, un-stripped. Every entry it has that the first
       does not is by construction a doc-prose path that moved, and is
       reported as ``doc_drift_fields`` with ``doc_drift: true``.

    Subtracting one from the other, rather than diffing a prose-only
    projection, is what guarantees the two signals partition the drift: no
    field can be reported in both, and no field that moved can go unreported
    in either.

    ``fresh`` is embedded un-stripped, exactly as before -- the exclusion
    applies to the comparison, never to what the response shows.
    """
    # Run identity is dropped from both diffs (and so is never doc drift).
    committed_graded = strip_path_patterns(committed, RUN_IDENTITY_REPORT_PATHS)
    fresh_graded = strip_path_patterns(fresh, RUN_IDENTITY_REPORT_PATHS)
    result = build_rerun_result(
        report_path=report_path,
        committed=committed,
        fresh=fresh,
        exclude=VOLATILE_REPORT_PATHS,
        committed_for_diff=strip_path_patterns(
            committed_graded, DOC_PROSE_REPORT_PATHS
        ),
        fresh_for_diff=strip_path_patterns(fresh_graded, DOC_PROSE_REPORT_PATHS),
        mode="check",
    )
    verdict_fields = {entry["field"] for entry in result["drift"]}
    doc_drift_fields = [
        entry
        for entry in diff_verdict_fields(
            committed_graded, fresh_graded, exclude=VOLATILE_REPORT_PATHS
        )
        if entry["field"] not in verdict_fields
    ]
    # Re-seat `fresh` last: it is the whole report, so the two small signals
    # this adds belong above it for a reader of the raw JSON.
    fresh_payload = result.pop("fresh")
    result["doc_drift"] = bool(doc_drift_fields)
    result["doc_drift_fields"] = doc_drift_fields
    result["fresh"] = fresh_payload
    return result


def check_tier_report(
    report_path: str,
    manifest: dict[str, Any],
    *,
    tiers_doc: str | None = None,
    repo_root: str | Path | None = None,
    generator_exec: GeneratorExecution | None = None,
) -> dict[str, Any]:
    """``klt signoff --manifest M --check REPORT`` (issue #2249): verify that
    a previously committed tier report at ``report_path`` still reproduces
    from manifest ``M``, ignoring build identity.

    Re-grades ``manifest`` (:func:`build_tier_report` -- the same work
    rendering the report does, including actually running any command-backed
    evidence it cites) and diffs the result against the committed report,
    excluding :data:`VOLATILE_REPORT_PATHS` (build identity) and
    :data:`DOC_PROSE_REPORT_PATHS` (the checklist's own wording).
    ``status: "match"`` when nothing else moved; ``"drifted"``, naming every
    field that did, otherwise -- a changed item ``status``/``reason``, a moved
    citation ``content_hash``, a different ``t1_met_count``.

    A doc-prose-only difference is **reported, not graded** (issue #2526):
    ``doc_drift: true`` plus a ``doc_drift_fields`` list naming what moved,
    alongside ``status: "match"``. See :data:`DOC_PROSE_REPORT_PATHS` for why
    "the checklist was reworded" and "your evidence moved" have to be two
    signals rather than one.

    This is the mode a consumer gating on "does the committed evidence still
    hold" should use **instead of byte-comparing the report file against a
    fresh render**: that comparison fails between two correct installs of the
    same pinned commit, on nothing but how each was provisioned (see
    :data:`VOLATILE_REPORT_PATHS`), and again on any upstream rewording of
    the bundled checklist (see :data:`DOC_PROSE_REPORT_PATHS`).

    Raises :class:`SignoffError` for a missing/unparseable committed report,
    or one that is not a tier report at all -- never a traceback.
    """
    from .signoff import build_tier_report

    committed = _load_committed_signoff_report(report_path, "items", "--manifest")
    fresh = build_tier_report(
        manifest,
        tiers_doc=tiers_doc,
        repo_root=repo_root,
        generator_exec=generator_exec,
    )
    return _check_signoff_report(report_path, committed, fresh)


def check_fleet_report(
    report_path: str,
    fleet: dict[str, Any],
    *,
    tiers_doc: str | None = None,
    repo_root: str | Path | None = None,
    generator_exec: GeneratorExecution | None = None,
) -> dict[str, Any]:
    """``klt signoff --fleet F --check REPORT`` (issue #2249): the
    :func:`check_tier_report` contract, one level up -- verify a committed
    *fleet* roll-up still reproduces from fleet manifest ``F``.

    Inherits everything, including both exclusions and the ``doc_drift``
    signal, by re-rendering through :func:`build_fleet_report` (which itself
    calls :func:`build_tier_report` once per block) and diffing through the
    same :func:`_check_signoff_report`: a roll-up carries exactly one
    ``build`` block, at the top level, so
    :data:`VOLATILE_REPORT_PATHS`' one path covers it, while
    :data:`DOC_PROSE_REPORT_PATHS` names the roll-up's own doc-sourced
    surface separately -- ``source_doc_content_hash`` once per block as well
    as at the top level, and the ``title`` its reduced item views carry.
    """
    from .signoff_fleet import build_fleet_report

    committed = _load_committed_signoff_report(report_path, "blocks", "--fleet")
    fresh = build_fleet_report(
        fleet,
        tiers_doc=tiers_doc,
        repo_root=repo_root,
        generator_exec=generator_exec,
    )
    return _check_signoff_report(report_path, committed, fresh)


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
    from .signoff import SignoffError

    committed = load_committed_report(report_path, SignoffError)
    if required_key not in committed:
        raise SignoffError(
            f"committed report '{report_path}' carries no '{required_key}' key "
            f"-- {flag} --check expects a report this mode produced (a "
            f"'{required_key}' array is what identifies one); check the path"
        )
    return committed

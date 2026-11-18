"""Fleet roll-up subsystem for ``klt signoff``.

Split out of ``signoff.py`` (issue #5636), following the precedent of
``signoff_power_delivery.py`` (issue #2626) and ``signoff.py``'s own prior
split (``signoff_envelopes.py``, issue #2313). This module owns the fleet
roll-up itself (issue #827, Phase 1c of epic #706): reading the fleet
manifest of already-committed per-block reports
(:func:`_read_fleet_block_manifest`) and grading every block against the
T1-T4 item skeleton into one roll-up (:func:`build_fleet_report`).

The rest of the subsystem moved out with it and is re-imported here *by
name* so this module stays the subsystem's single import point (and
``signoff.py`` re-exports every name from one place, exactly as it did
before the split): ``signoff_fleet_reduction.py`` owns the per-block
blocker/coverage reduction ``build_fleet_report`` folds into the roll-up,
and ``signoff_fleet_check.py`` owns the ``--check`` drift comparison of a
committed report against a fresh one (issue #2249).

Everything this module needs back from ``signoff.py``
(:class:`SignoffError`, :func:`build_tier_report`, the fleet-envelope
schema version and build identity) is imported into the function that
needs it -- deferred into each function body, not at module level,
because ``signoff.py`` imports this module at its own top level (the
same circular-import hazard ``signoff_power_delivery.py`` documents for
its own back-references into ``signoff.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .design_evidence_tiers import doc_source_label

# The per-block blocker reduction and the ``--check`` drift comparison
# live in the sibling modules below; both are re-imported here *by name*
# so this module stays the subsystem's single import point and
# ``signoff.py`` re-exports every fleet name from one place.
from .signoff_fleet_check import DOC_PROSE_REPORT_PATHS as DOC_PROSE_REPORT_PATHS
from .signoff_fleet_check import RUN_IDENTITY_REPORT_PATHS as RUN_IDENTITY_REPORT_PATHS
from .signoff_fleet_check import VOLATILE_REPORT_PATHS as VOLATILE_REPORT_PATHS
from .signoff_fleet_check import _check_signoff_report as _check_signoff_report
from .signoff_fleet_check import (
    _load_committed_signoff_report as _load_committed_signoff_report,
)
from .signoff_fleet_check import check_fleet_report as check_fleet_report
from .signoff_fleet_check import check_tier_report as check_tier_report
from .signoff_fleet_reduction import _blocking_item_view as _blocking_item_view
from .signoff_fleet_reduction import (
    _blocking_t1_item,
    _drc_coverage_rows,
    _ungraded_t1_items,
)
from .signoff_generator_batch import GeneratorExecution

# --------------------------------------------------------------------------- #
# Fleet roll-up (issue #827, Phase 1c of epic #706)
# --------------------------------------------------------------------------- #


def build_fleet_report(
    fleet: dict[str, Any],
    *,
    tiers_doc: str | Path | None = None,
    repo_root: str | Path | None = None,
    generator_exec: GeneratorExecution | None = None,
) -> dict[str, Any]:
    """Grade every block in a **fleet manifest** against the T1-T4 item
    skeleton (:func:`build_tier_report`, called once per block) and reduce
    each block's result down to its current tier and, for any block not yet
    at T1, the single T1 item still blocking it -- turning "which canaries
    are at which tier, and what's blocking each not-yet-T1 block" into one
    query instead of a survey (Epic #706, Phase 1c).

    ``tiers_doc`` is forwarded verbatim to every per-block
    :func:`build_tier_report` call, so one roll-up always grades every block
    against the same doc. ``repo_root`` (``klt signoff --repo-root``,
    issue #2878) is forwarded the same way: one roll-up resolves every
    block's ``scope: "repo"`` citations against the same explicitly named
    root.

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
    from .signoff import (
        FLEET_REPORT_SCHEMA_VERSION,
        SignoffError,
        _build_identity,
        build_tier_report,
    )

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

        tier_report = build_tier_report(
            manifest,
            tiers_doc=tiers_doc,
            repo_root=repo_root,
            generator_exec=generator_exec,
        )
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
                **(
                    {"t1_not_applicable_count": tier_report["t1_not_applicable_count"]}
                    if tier_report.get("t1_not_applicable_count")
                    else {}
                ),
                # Issue #5599: carried like `t1_not_applicable_count` above --
                # omitted when zero, so a roll-up over manifests declaring no
                # justified gap is byte-identical to the previous rendering.
                **(
                    {"t1_justified_gap_count": tier_report["t1_justified_gap_count"]}
                    if tier_report.get("t1_justified_gap_count")
                    else {}
                ),
                **(
                    {"findings": tier_report["findings"]}
                    if tier_report.get("findings")
                    else {}
                ),
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


def _read_fleet_block_manifest(raw_entry: Any, index: int) -> tuple[str | None, Any]:
    """Resolve one ``fleet["blocks"][index]`` entry into ``(source,
    manifest)``: ``source`` is the file path/``"-"`` the manifest was read
    from, or ``None`` for an inline manifest object. Raises
    :class:`SignoffError` if ``raw_entry`` is neither a string nor a JSON
    object, or a string entry cannot be read/parsed as JSON."""
    from .signoff import SignoffError, _read_json_source

    if isinstance(raw_entry, dict):
        return None, raw_entry
    if isinstance(raw_entry, str):
        return raw_entry, _read_json_source(raw_entry, "fleet block manifest")
    raise SignoffError(
        f"fleet manifest blocks[{index}] must be a JSON object or a file "
        f"path string, got {type(raw_entry).__name__}"
    )

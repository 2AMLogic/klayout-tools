"""Composition of a parent block's T1 rows from its sub-block manifests.

Issue #6927: a design assembled from independently signed-off sub-blocks
could not state its status honestly. Citing a sub-block's DRC/LVS report in
the parent's manifest overstates scope (``--manifest`` grades every cited
item as covering the manifest's ``block``); leaving it uncited renders
``no_evidence``, indistinguishable from "evidence exists one level down";
and a ``--fleet`` run over the sub-block manifests produces per-block rows
with no parent verdict at all.

The construct this module adds is one optional manifest key,
``composed_of``: a non-empty array whose entries take the **same shapes
``--fleet``'s ``blocks`` take** (a path, ``"-"``, or an inline manifest
object), each resolving to a sub-block manifest with a non-empty ``block``
name. ``compose_rows`` grades every sub-block with the ordinary
:func:`~klayout_tools.signoff.build_tier_report` and rewrites the parent's
eligible rows from their verdicts, so a sub-block is never graded by any
rule its own single-block report would not have applied.

**Composition is visible evidence, never T1.**
``docs/design-evidence-tiers.md`` defines T1 at the named block's *own
boundary*: two sub-blocks that each pass LVS say nothing about whether the
parent wires them together correctly. So a composed row renders
``status: "met_by_composition"`` -- deliberately **outside**
``_T1_SATISFIED_STATUSES`` and counted in neither ``t1_met_count`` nor
``t1_not_applicable_count`` -- and the parent is ``tier: null`` until it
cites its own item 3/4 evidence for the assembled layout. That own evidence
always wins (rule 2 below): a parent whose own LVS fails is graded as a
failing own citation, never masked by its sub-blocks' passes. If the
operator ever wants composition to qualify for parent T1, that is a
separate, explicit checklist change -- not something this module decides.

v1 rules (deliberately narrow):

1. **Composable items: only 3 (DRC clean) and 4 (LVS clean)** --
   :data:`COMPOSABLE_ITEM_IDS`. Every other item is graded from the
   parent's own evidence alone.
2. A parent row ``(id, partition)`` becomes ``met_by_composition`` **only
   when the parent has no own evidence for it** (it would grade
   ``unmet``/``no_evidence``) **and every sub-block's own report has
   ``met`` at the same ``(id, partition)``**.
3. If any sub-block is not ``met`` there, the row stays
   ``unmet``/``no_evidence`` and gains ``detail.composition`` naming each
   sub-block and its status there.
4. All sub-blocks must have the same ``kind`` as the parent, and no
   sub-block may declare its own ``composed_of`` (no nesting) -- each is a
   :class:`~klayout_tools.signoff.SignoffError`.
5. A composed row carries ``reason:
   "composed_from_sub_blocks_only"`` (so ``blocking_item`` and the text
   renderer say *why* it is not T1) and, in place of ``citation``,
   ``composed_from: [{"block", "source", "status", "content_hash"?},
   ...]`` -- per sub-block, its manifest path (or ``null`` for an inline
   entry) and the content hash of the evidence its own report item cited,
   so staleness stays visible. ``--check`` re-grades the parent and
   re-reads the sub-block manifests, so drift reproduces with no new
   mechanism.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .signoff_generator_batch import GeneratorExecution

#: The only T1 items composition may satisfy in v1 (rule 1): 3 ("DRC
#: clean") and 4 ("LVS clean") -- the two items whose per-sub-block
#: evidence is plausibly *all* the evidence a hardwired assembly will ever
#: have. Kept a module-level frozenset (like ``_REPORT_LIST_ITEMS`` in
#: ``signoff.py``) so narrowing or widening it is a one-line review.
COMPOSABLE_ITEM_IDS: frozenset[int] = frozenset({3, 4})

#: The row status a composed parent item renders (issue #6927) -- snake_case
#: like ``STATUS_NOT_APPLICABLE`` in ``signoff.py``. Deliberately **not** a
#: member of ``_T1_SATISFIED_STATUSES`` and counted in neither
#: ``t1_met_count`` nor ``t1_not_applicable_count``: composition is visible
#: evidence, never T1 (see this module's docstring).
STATUS_MET_BY_COMPOSITION = "met_by_composition"

#: The ``reason`` a composed row carries (rule 5) -- naming *why* the row is
#: not T1 in ``blocking_item`` and the text renderer, instead of reading as
#: a plain unmet gap.
REASON_COMPOSED_FROM_SUB_BLOCKS_ONLY = "composed_from_sub_blocks_only"


def composed_count_field(count: int) -> dict[str, Any]:
    """``{"t1_composed_count": n}`` when ``n > 0``, ``{}`` otherwise -- the
    conditional top-level spread ``build_tier_report`` renders, exactly like
    ``t1_not_applicable_count`` (issue #2865): omitted when zero so a
    report that composes nothing is byte-identical to the one this build
    rendered before the field existed (and therefore does not read as
    ``--check`` drift on an upgrade alone)."""
    return {"t1_composed_count": count} if count else {}


def compose_rows(
    items: list[dict[str, Any]],
    manifest: dict[str, Any],
    tiers_doc: str | Path | None,
    repo_root: str | Path | None,
    generator_exec: GeneratorExecution | None,
) -> int:
    """Rewrite the parent's eligible ``no_evidence`` rows in ``items`` from
    the sub-block manifests named by ``manifest["composed_of"]``, returning
    how many rows became ``met_by_composition`` (the caller's
    ``t1_composed_count``).

    ``items`` is the graded T1 row list ``build_tier_report`` rendered
    (mutated in place, after items are graded and before ``tier`` is
    computed); ``tiers_doc``/``repo_root``/``generator_exec`` are forwarded
    verbatim to each sub-block's own ``build_tier_report`` call, so one
    parent always grades every sub-block against the same doc. A manifest
    without a ``composed_of`` key is the ordinary single-block case: the
    function returns ``0`` without touching a row.

    Raises :class:`SignoffError` (rule 4 and the reader's own errors) when
    ``composed_of`` is present but not a non-empty array, an entry is
    neither a path string nor a JSON object (or cannot be read/parsed), a
    resolved sub-block manifest is not a JSON object, has no non-empty
    ``block`` name, has a ``kind`` different from the parent's, or declares
    its own ``composed_of`` -- a composition authoring mistake, not a "no
    evidence yet" grading outcome, so it aborts the report rather than
    rendering the parent as silently unmet. Sub-block ``kind`` is checked
    before grading so a mixed-kind fleet can never half-compose.
    """
    from .signoff import SignoffError

    composed_of = manifest.get("composed_of")
    if composed_of is None:
        return 0
    if not isinstance(composed_of, list) or not composed_of:
        raise SignoffError(
            "block manifest 'composed_of' must be a non-empty JSON array of "
            "sub-block manifests (or paths/'-' to them)"
        )
    subs = _read_sub_blocks(
        composed_of, manifest.get("kind"), tiers_doc, repo_root, generator_exec
    )
    return _rewrite_parent_rows(items, subs)


def _read_sub_blocks(
    composed_of: list[Any],
    parent_kind: Any,
    tiers_doc: str | Path | None,
    repo_root: str | Path | None,
    generator_exec: GeneratorExecution | None,
) -> list[tuple[str, str | None, dict[str, Any]]]:
    """Read, validate and grade every ``composed_of`` entry into
    ``(block name, source, tier report)`` -- the same entry shapes
    ``--fleet``'s ``blocks[]`` takes (path, ``"-"``, inline object), read
    through the same ``_read_json_source`` machinery (issue #827), each
    graded by the ordinary :func:`build_tier_report` so a sub-block is
    never graded by any rule its own single-block report would not have
    applied. Raises :class:`SignoffError` naming ``composed_of[i]`` for
    every authoring mistake (see :func:`compose_rows`)."""
    from .signoff import SignoffError, _read_json_source, build_tier_report

    subs: list[tuple[str, str | None, dict[str, Any]]] = []
    for index, raw_entry in enumerate(composed_of):
        if isinstance(raw_entry, dict):
            source: str | None = None
            sub = raw_entry
        elif isinstance(raw_entry, str):
            source = raw_entry
            sub = _read_json_source(raw_entry, "composed_of sub-block manifest")
        else:
            raise SignoffError(
                f"composed_of[{index}] must be a JSON object or a file path "
                f"string, got {type(raw_entry).__name__}"
            )
        if not isinstance(sub, dict):
            raise SignoffError(
                f"composed_of[{index}] must resolve to a JSON object, got "
                f"{type(sub).__name__}"
            )
        name = sub.get("block")
        if not isinstance(name, str) or not name:
            raise SignoffError(
                f"composed_of[{index}] resolves to a manifest with no "
                "non-empty 'block' name -- required to name the sub-block's "
                "rows in the parent's report"
            )
        if sub.get("kind") != parent_kind:
            raise SignoffError(
                f"composed_of[{index}] (block {name!r}) has kind "
                f"{sub.get('kind')!r}, which does not match the parent's "
                f"kind {parent_kind!r} -- mixed-kind composition is not "
                "supported in v1"
            )
        if "composed_of" in sub:
            raise SignoffError(
                f"composed_of[{index}] (block {name!r}) declares its own "
                "'composed_of' -- nested composition is not supported in v1"
            )
        subs.append(
            (
                name,
                source,
                build_tier_report(
                    sub,
                    tiers_doc=tiers_doc,
                    repo_root=repo_root,
                    generator_exec=generator_exec,
                ),
            )
        )
    return subs


def _rewrite_parent_rows(
    items: list[dict[str, Any]],
    subs: list[tuple[str, str | None, dict[str, Any]]],
) -> int:
    """Apply rules 1-3 and 5 to ``items`` in place (see this module's
    docstring), returning how many rows became ``met_by_composition``."""
    from .signoff import _REASON_NO_EVIDENCE

    composed = 0
    for item in items:
        if item.get("tier") != "T1" or item.get("id") not in COMPOSABLE_ITEM_IDS:
            continue  # rule 1: items outside {3, 4} are never composed
        if item.get("status") != "unmet" or item.get("reason") != _REASON_NO_EVIDENCE:
            continue  # rule 2: the parent's own evidence always wins
        key = (item["id"], item.get("partition"))
        rows: list[dict[str, Any]] = []
        for name, source, report in subs:
            row = next(
                (
                    r
                    for r in report["items"]
                    if r.get("tier") == "T1"
                    and r.get("id") == key[0]
                    and r.get("partition") == key[1]
                ),
                None,
            )
            # ``"no_row"`` is unreachable for a same-kind sub-block (the
            # doc and partitions are shared) but stays defensive rather
            # than crashing on a hand-forged report.
            status = row["status"] if row is not None else "no_row"
            citation = (row or {}).get("citation") or {}
            content_hash = citation.get("content_hash")
            rows.append(
                {
                    "block": name,
                    "source": source,
                    "status": status,
                    **({"content_hash": content_hash} if content_hash else {}),
                }
            )
        if all(row["status"] == "met" for row in rows):
            # Rule 5: `citation` is replaced by `composed_from`, and the
            # reason names why the row is not T1. Counts are untouched by
            # the caller (`met_count`/`not_applicable_count` were computed
            # before this rewrite), so `tier` is unaffected by design.
            item["status"] = STATUS_MET_BY_COMPOSITION
            item["reason"] = REASON_COMPOSED_FROM_SUB_BLOCKS_ONLY
            item["citation"] = None
            item["composed_from"] = rows
            composed += 1
        else:
            # Rule 3: the row stays unmet/no_evidence, but the sub-block
            # verdicts it rested on stay visible.
            detail = dict(item.get("detail") or {})
            detail["composition"] = rows
            item["detail"] = detail
    return composed

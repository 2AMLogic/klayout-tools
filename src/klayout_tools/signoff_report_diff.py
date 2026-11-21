"""Checklist-surface comparison of two committed tier reports (issue #6850).

``klt signoff --check`` (:mod:`.signoff_fleet_check`) answers "does this
committed tier report still reproduce" by **re-grading its manifest** -- so
it needs the manifest, the evidence that manifest cites, and a working
grading build. An evidence reviewer holding two *saved* tier reports and
none of that (the manifest was never committed, the evidence was archived,
the build was upgraded) still has one question no existing verb answers:
*what changed in the checklist between these two snapshots?* Before issue
#6850 every such consumer hand-rolled a JSON field diff over the two files.

:func:`diff_tier_reports` answers it from the two files alone. It compares
exactly the checklist-describing surfaces a tier report carries --

- the top-level ``source_doc``/``source_doc_content_hash`` (which checklist
  doc the report quotes, and the hash of what it said), and
- each row's ``title``/``text``/``notes`` (the doc's prose, inlined by
  :func:`~.signoff.build_tier_report`),

-- and nothing else, by design: the rows are matched on their identity
first, so a row's ``status``/``reason``/``citation`` (what the *grading*
said) and the report's ``build`` block (which install rendered it) are not
compared at all. That is the exact complement of ``--check``: this mode
reports the movement ``--check`` deliberately excludes from its verdict
(``DOC_PROSE_REPORT_PATHS``, issue #2526), without needing anything the
grading touched. The two modes' notions of "the checklist's own wording"
cannot drift apart -- :data:`DIFF_ROW_FIELDS` is those three item paths.

Rows are matched by ``(tier, id, partition)`` -- the row identity
``--check``'s own exclusions document -- so reordering ``items[]`` alone
compares as unchanged, the analog and digital rows of a mixed-signal pair
that share a numeric id stay distinct, and the T2-T4 ladder rows (``id:
null``) are distinguished by their tier. A report carrying two rows with
the same key cannot be matched unambiguously and is refused.

Split out of (rather than grown into) :mod:`.signoff_fleet_check` for the
per-file size ratchet, mirroring the ``signoff_fleet``/``signoff_fleet_check``
split; the committed-report loader is reused from there unchanged.
"""

from __future__ import annotations

from typing import Any

from .signoff_fleet_check import _load_committed_signoff_report

#: The top-level tier-report fields the comparison reads (issue #6850): the
#: two that describe *which checklist doc the report quotes* --
#: ``source_doc`` names where the doc came from (a ``--tiers-doc`` override
#: vs the bundled copy is a different grading run, not a rewording, so this
#: moves first when the doc source changes) and
#: ``source_doc_content_hash`` pins what it said. Every other top-level
#: field (``tier``, the ``t1_*`` counts, ``build``, ...) is grading output
#: or install identity, which this mode exists to ignore.
DIFF_SOURCE_FIELDS: tuple[str, ...] = ("source_doc", "source_doc_content_hash")

#: The per-row checklist prose the comparison reads -- exactly the three
#: item paths ``--check`` excludes from its verdict
#: (:data:`~.signoff_fleet_check.DOC_PROSE_REPORT_PATHS`, issue #2526), so
#: "the checklist's own wording" means the same fields in both modes and a
#: future prose field joins both lists or neither.
DIFF_ROW_FIELDS: tuple[str, ...] = ("title", "text", "notes")

#: One report row's identity: ``(tier, id, partition)``. ``id`` is an int
#: (T1 items) or ``None`` (the T2-T4 ladder rows); ``partition`` is a str
#: (one side of a mixed-signal pair) or ``None``.
_RowKey = tuple[str, Any, Any]


def _row_order(key: _RowKey) -> tuple[Any, ...]:
    """A sort key giving the output rows a deterministic order -- the
    report's own (T1 items by id, then one row per ladder tier) -- without
    comparing ``None`` ids/partitions against strings (a plain tuple sort
    would raise on exactly the ladder rows)."""
    tier, item_id, partition = key
    return (
        str(tier),
        item_id is None,
        item_id if isinstance(item_id, int) else 0,
        str(partition or ""),
    )


def _identity(key: _RowKey) -> dict[str, Any]:
    """The row key as JSON fields, so every output entry is self-contained
    (a consumer filtering ``items.changed[]`` never re-derives the key)."""
    return {"tier": key[0], "id": key[1], "partition": key[2]}


def _key_label(key: _RowKey) -> str:
    """The row key as one human-readable label for refusal messages."""
    item_id = "-" if key[1] is None else f"#{key[1]}"
    partition = f" [{key[2]}]" if key[2] else ""
    return f"{key[0]} {item_id}{partition}"


def _row_entry(key: _RowKey, row: dict[str, Any]) -> dict[str, Any]:
    """An added/removed row's identity plus its full checklist content --
    the issue-#6850 contract that each addition/removal is identified *once,
    with what the checklist said*, not just by its key."""
    return {
        **_identity(key),
        "title": row.get("title"),
        "text": row.get("text"),
        "notes": row.get("notes"),
    }


def _index_rows(report: dict[str, Any], report_path: str) -> dict[_RowKey, dict]:
    """Index one report's ``items[]`` by ``(tier, id, partition)``.

    Refuses (never half-matches) a report whose rows are not the shape a
    tier report renders, or that carries two rows with the same key -- the
    pair could not be matched unambiguously, and guessing which of the two
    a row in the *other* report corresponds to would fabricate a comparison.
    """
    from .signoff import SignoffError

    items = report["items"]
    if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
        raise SignoffError(
            f"committed report '{report_path}' carries no usable 'items' "
            "array -- --diff-reports matches tier-report rows by "
            "(tier, id, partition); check the path"
        )
    rows: dict[_RowKey, dict] = {}
    for row in items:
        key = (row.get("tier"), row.get("id"), row.get("partition"))
        if key in rows:
            raise SignoffError(
                f"committed report '{report_path}' carries two rows with "
                f"the identity {_key_label(key)} -- the pair cannot be "
                "matched unambiguously; check the report"
            )
        rows[key] = row
    return rows


def diff_tier_reports(old_path: str, new_path: str) -> dict[str, Any]:
    """``klt signoff --diff-reports OLD NEW`` (issue #6850): compare the
    checklist surfaces of two previously committed tier reports, reading
    nothing but the two files -- no manifest, no evidence, no re-grading.

    Rows are matched by ``(tier, id, partition)`` (see the module
    docstring), so reordered rows compare as unchanged; a row present on
    only one side is reported once, with its checklist content. A
    verdict-only or build-only difference between the two reports is not a
    checklist change and yields an empty comparison.

    Returns the ``--format json`` payload::

        {
            "schema_version": 1,
            "mode": "diff-reports",
            "old_report": "old.json",
            "new_report": "new.json",
            "changed": true,
            "source": [
                {"field": "source_doc_content_hash",
                 "old": "sha256:...", "new": "sha256:..."}
            ],
            "items": {
                "changed": [
                    {"tier": "T1", "id": 7, "partition": null,
                     "field": "text", "old": "...", "new": "..."}
                ],
                "added": [
                    {"tier": "T1", "id": 12, "partition": null,
                     "title": "...", "text": "...", "notes": []}
                ],
                "removed": []
            }
        }

    ``changed`` is whether anything moved at all; the CLI maps it to no
    exit-code distinction (a completed comparison exits ``0`` -- the
    movement is the answer, not a failure).

    Raises :class:`~.signoff.SignoffError` for an unreadable/non-JSON
    report, one that is not a tier report (no ``items`` array), or an
    ambiguous duplicate row key -- the caller renders those as exit ``1``.
    """
    old = _load_committed_signoff_report(old_path, "items", "--diff-reports")
    new = _load_committed_signoff_report(new_path, "items", "--diff-reports")
    old_rows = _index_rows(old, old_path)
    new_rows = _index_rows(new, new_path)

    source = [
        {"field": field, "old": old.get(field), "new": new.get(field)}
        for field in DIFF_SOURCE_FIELDS
        if old.get(field) != new.get(field)
    ]
    changed_rows: list[dict[str, Any]] = []
    for key in sorted(set(old_rows) & set(new_rows), key=_row_order):
        old_row, new_row = old_rows[key], new_rows[key]
        changed_rows.extend(
            {
                **_identity(key),
                "field": field,
                "old": old_row.get(field),
                "new": new_row.get(field),
            }
            for field in DIFF_ROW_FIELDS
            if old_row.get(field) != new_row.get(field)
        )
    added = [
        _row_entry(key, new_rows[key])
        for key in sorted(set(new_rows) - set(old_rows), key=_row_order)
    ]
    removed = [
        _row_entry(key, old_rows[key])
        for key in sorted(set(old_rows) - set(new_rows), key=_row_order)
    ]

    return {
        "schema_version": 1,
        "mode": "diff-reports",
        "old_report": old_path,
        "new_report": new_path,
        "changed": bool(source or changed_rows or added or removed),
        "source": source,
        "items": {"changed": changed_rows, "added": added, "removed": removed},
    }

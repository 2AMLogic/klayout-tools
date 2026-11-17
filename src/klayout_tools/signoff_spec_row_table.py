"""The per-spec-row table on ``klt signoff --manifest`` reports (issue
#5691), in its own module because ``signoff.py`` sits at its file-size
ratchet ceiling (#2766 baseline).

A manifest may declare a top-level ``spec_rows`` map (``{"<item>":
[{"id", "gap"?}, ...]}``, issues #2959/#3768). When it does,
:func:`~klayout_tools.signoff.build_tier_report` adds one additive
top-level ``spec_rows[]`` block to its report: one record per declared
row, in declaration order, carrying each row's limit, worst value,
binding corner, verdict and yield estimate, and the cited file behind
each number. A manifest that declares nothing omits the key entirely, so
``--check`` sees no drift on an upgrade alone (the ``partition_boundary``
back-compat rule, #2278). Additive: no ``schema_version`` bump. The
record contract is documented in ``docs/cli/signoff.md`` ("The
per-spec-row table"); the load-bearing rules:

- **Quote, never re-derive, anything one envelope already states.**
  ``worst`` quotes the binding measurement's own ``worst_case`` verbatim
  (plus the one new quotient below) and ``yield`` quotes
  ``yield.empirical`` verbatim; the one genuine derivation is the reduce.
- **Row matching is exact string equality** between a declared row
  ``id`` and a ``measurements[].spec_row`` tag; no prefix is stripped
  (the auto-prefixed ``<bench>.<row>`` form is a `klt pex` scheme,
  #5681). A citation that never names the row, or whose tags name only
  other rows, contributes nothing (#3778).
- **The reduce**: several tagged measurements may cover one row across
  several citations. The binding one is picked by **minimum relative
  margin** -- ``margin / |active bound|``, recorded as
  ``worst.relative_margin`` -- because absolute margin is wrong when
  instance limits scale with the supply (a 1 V rail's 1 mV headroom is
  proportionally tighter than a 1.8 V rail's 20 mV). When the active
  bound is ``0``, or the measurement declares no ``limits``, relative
  margin is undefined: the pick falls back to the absolute ``margin``
  and ``relative_margin`` is ``null``. A non-``pass`` measurement
  outranks any ``pass`` one regardless of margin, by the house
  ``error > inconclusive > fail > pass`` precedence extended monotonic
  for `klt yield`'s two extra tokens (``fail > insufficient_evidence >
  pass > reported``, docs/cli/yield.md). Ties keep citation order.
- **Disagreeing limits stay renderable, unreconciled**: the binding
  measurement's ``limits`` are quoted as ``limits`` and every cited
  measurement declaring a different set is listed under an additive
  ``limit_disagreement`` -- the grader has no basis to pick a "true"
  limit.
- **States**: ``graded``; ``gap`` (the declared ``gap`` string carried
  as an explicit no-evidence record, never an omission -- a gap is never
  covered, exactly as grading refuses it); ``no_evidence`` (no citation
  names the row); ``unattributable`` (a citation names the row but its
  envelope carries no ``spec_row`` tags at all -- #3778's *asserted*
  coverage: the measurements cannot be attributed to a row, and guessing
  is the bug this verb exists to prevent). Every declared row gets a
  record in every state; the measured fields are ``null`` where the
  state has nothing to quote.
- **Static projection only**: file-backed citations are re-read;
  command-backed citations would have to be re-executed to render, so
  they contribute nothing here, as does an unreadable, malformed or
  unresolvable citation. Grading reports those on the item axis; this
  block never contradicts it.

The module also hosts ``_members_rows_declared`` (moved here from
``signoff.py`` as part of the size offset that pays for the hook lines;
it is the spec-row naming rule the multi-citation grader applies, so
this is its thematic home). ``klayout_tools.signoff`` re-imports it by
name so ``signoff._members_rows_declared`` keeps resolving exactly as
before (the #2313 ``signoff_envelopes.py`` precedent).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ._jsonnum import is_number

__all__ = ["build_spec_row_records", "print_spec_row_table"]


def _members_rows_declared(
    members: list[tuple[dict[str, Any], list[str] | None]],
    spec_rows: list[dict[str, Any]] | None,
) -> bool:
    """True unless a member names an undeclared (or declared-``gap``) row."""
    if spec_rows is None:
        return True
    declared = {row["id"]: row for row in spec_rows}
    return all(
        row_id in declared and declared[row_id]["gap"] is None
        for _, rows in members
        for row_id in rows or []
    )


#: The house status precedence ``error > inconclusive > fail > pass`` --
#: extended monotonic for `klt yield`'s two extra tokens (docs/cli/yield.md's
#: ``fail > insufficient_evidence > pass > reported``): lower wins the
#: reduce. Unknown tokens (`klt yield`'s ``reported``, `klt sim`'s
#: ``absent``) share the last rank and yield to declaration order.
_STATUS_RANK: dict[str, int] = {
    "error": 0,
    "inconclusive": 1,
    "fail": 2,
    "insufficient_evidence": 3,
    "pass": 4,
}
_DEFAULT_RANK = 5


def _envelope_measurements(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """A ``klt sim``/``klt yield`` envelope's well-typed ``measurements[]``
    entries, in report order; ``[]`` for anything else."""
    raw = envelope.get("measurements")
    return [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []


def _envelope_has_tags(envelope: dict[str, Any]) -> bool:
    """Whether any ``measurements[]`` entry declares a ``spec_row`` -- the
    #3778 tagged/untagged line under ``unattributable``."""
    return any(
        isinstance(m.get("spec_row"), str) and m["spec_row"]
        for m in _envelope_measurements(envelope)
    )


def _active_bound(limits: Any, value: Any, margin: Any) -> float | int | None:
    """The limit side ``margin`` was measured against, or ``None`` when it
    cannot be proven from the envelope alone: with one declared bound that
    side is it; with both, ``margin`` is ``min(value - min, max - value)``
    and the side whose arithmetic reproduces ``margin`` is the binding one
    (equidistant resolves to ``min`` -- either answer is the same number).
    Re-deriving the side from the quoted numbers is not re-deriving the
    margin: the quotient this feeds is the one computation #5691 adds, and
    every input is read off the envelope. ``None`` when ``limits`` is
    absent, ``value`` is not a finite number, or neither arithmetic
    reproduces ``margin``.
    """
    if not isinstance(limits, dict) or not limits or not is_number(value):
        return None
    low = limits.get("min")
    high = limits.get("max")
    has_low = is_number(low)
    has_high = is_number(high)
    if has_low and not has_high:
        return low
    if has_high and not has_low:
        return high
    if has_low and has_high:
        if value - low == margin:
            return low
        if high - value == margin:
            return high
    return None


def _relative_margin(measurement: dict[str, Any]) -> float | int | None:
    """``margin / |active bound|`` for one measurement, or ``None`` when
    undefined: no ``limits``, no numeric ``worst_case.margin``, an active
    bound of ``0``, or a bound that cannot be proven. ``margin``/``value``
    come from the measurement's own ``worst_case``."""
    worst_case = measurement.get("worst_case")
    if not isinstance(worst_case, dict):
        return None
    margin = worst_case.get("margin")
    if not is_number(margin):
        return None
    bound = _active_bound(measurement.get("limits"), worst_case.get("value"), margin)
    if bound is None or bound == 0:
        return None
    return margin / abs(bound)


def _sort_key(candidate: dict[str, Any]) -> tuple[int, int, Any]:
    """The reduce's ordering key: status precedence first, then relative
    margin when computable, else the absolute ``margin``, else declaration
    order (a stable ``min`` keeps citation order on full ties). The second
    element groups computable-relative candidates ahead of absolute-fallback
    ones: a quotient that can be computed is the better estimator, and each
    group is internally consistent."""
    measurement = candidate["measurement"]
    rank = _STATUS_RANK.get(measurement.get("status"), _DEFAULT_RANK)
    relative = _relative_margin(measurement)
    if relative is not None:
        return rank, 0, relative
    worst_case = measurement.get("worst_case")
    margin = worst_case.get("margin") if isinstance(worst_case, dict) else None
    if is_number(margin):
        return rank, 1, margin
    return rank, 2, candidate["order"]


def _limits_identity(limits: Any) -> str | None:
    """A canonical identity for one declared ``limits`` set, or ``None``
    when the measurement declares none (a non-set never counts as a
    divergent set)."""
    if not isinstance(limits, dict) or not limits:
        return None
    return json.dumps(limits, sort_keys=True, default=repr)


def _base_record(row_id: str, item_id: int, state: str, gap: str | None) -> dict:
    """The record skeleton for a row this projection has nothing to quote:
    every measured field is ``null`` where the state has nothing to put
    there."""
    return {
        "id": row_id,
        "item": item_id,
        "state": state,
        "gap": gap,
        "limits": None,
        "unit": None,
        "worst": None,
        "verdict": None,
        "yield": None,
        "sources": [],
    }


def _graded_record(row_id: str, item_id: int, candidates: list[dict]) -> dict:
    """One ``graded`` record: the binding measurement's own fields, quoted
    verbatim, plus the relative-margin quotient that picked it."""
    binding = min(candidates, key=_sort_key)
    measurement = binding["measurement"]
    limits = measurement.get("limits")
    has_limits = isinstance(limits, dict) and bool(limits)
    relative = _relative_margin(measurement) if has_limits else None
    worst_case = measurement.get("worst_case")
    worst = None
    if isinstance(worst_case, dict):
        worst = dict(worst_case)
        worst["relative_margin"] = relative
    verdict = measurement.get("status")
    # A measurement with no ``limits`` has no graded bound to carry a
    # verdict about (its ``worst_case`` is not an extreme, docs/cli/sim.md):
    # quoted above, but never dressed up as a graded verdict here.
    if not has_limits or not isinstance(verdict, str):
        verdict = None
    yield_block = None
    for candidate in candidates:
        empirical = (candidate["measurement"].get("yield") or {}).get("empirical")
        if isinstance(empirical, dict):
            yield_block = empirical
            break
    binding_identity = _limits_identity(limits)
    disagreement = [
        {
            "file": candidate["file"],
            "measurement": candidate["measurement"].get("name"),
            "limits": candidate["measurement"].get("limits"),
        }
        for candidate in candidates
        if candidate is not binding
        and _limits_identity(candidate["measurement"].get("limits"))
        not in (None, binding_identity)
    ]
    record: dict[str, Any] = {
        "id": row_id,
        "item": item_id,
        "state": "graded",
        "gap": None,
        "limits": limits if has_limits else None,
        "unit": measurement.get("unit"),
        "worst": worst,
        "verdict": verdict,
        "yield": yield_block,
        "sources": [
            {
                "file": candidate["file"],
                "measurement": candidate["measurement"].get("name"),
            }
            for candidate in candidates
        ],
    }
    if disagreement:
        record["limit_disagreement"] = disagreement
    return record


def build_spec_row_records(
    manifest: dict[str, Any],
    *,
    read_spec_rows: Callable[..., Any],
    parse_members: Callable[..., Any],
    resolve_evidence: Callable[..., Any],
    lookup_evidence: Callable[..., Any],
    repo_root: Any = None,
) -> list[dict[str, Any]] | None:
    """The ``spec_rows[]`` block for one manifest, or ``None`` when the
    manifest declares no top-level ``spec_rows`` (the report then omits the
    key entirely, so ``--check`` sees no drift on an upgrade alone).

    The four callables are :mod:`klayout_tools.signoff`'s own readers,
    passed in rather than imported back (this module is imported *by* it):
    ``read_spec_rows`` parses the declaration exactly as grading does,
    ``parse_members`` normalizes an evidence entry exactly as grading does,
    ``lookup_evidence`` applies the qualified-then-bare key rule, and
    ``resolve_evidence`` resolves one citation to its envelope. One record
    per declared row, in declaration order; a malformed declaration (or one
    whose per-item rows do not parse) contributes no records -- grading
    already reports that as ``invalid_evidence`` on the item axis.
    """
    if manifest.get("spec_rows") is None:
        return None
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    resolved_cache: dict[str, dict[str, Any] | None] = {}
    records: list[dict[str, Any]] = []
    for (item_id, suffix), rows in read_spec_rows(manifest).items():
        if rows is None:
            continue
        per_row = _collect_slot_state(
            evidence,
            rows,
            item_id=item_id,
            suffix=suffix,
            parse_members=parse_members,
            resolve_evidence=resolve_evidence,
            lookup_evidence=lookup_evidence,
            repo_root=repo_root,
            resolved_cache=resolved_cache,
        )
        records.extend(_emit_slot_records(rows, item_id, suffix, per_row))
    return records


def _collect_slot_state(
    evidence: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    item_id: int,
    suffix: str | None,
    parse_members: Callable[..., Any],
    resolve_evidence: Callable[..., Any],
    lookup_evidence: Callable[..., Any],
    repo_root: Any,
    resolved_cache: dict[str, dict[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    """Per declared row of one slot: which citations name it, whether the
    envelopes they cite carry ``spec_row`` tags at all, and every tagged
    measurement matching it. A member naming an undeclared row is ignored
    (#3778); a citation that is not file-backed, or that does not resolve
    to a readable envelope, contributes nothing (grading reports those on
    the item axis)."""
    per_row: dict[str, dict[str, Any]] = {
        row["id"]: {"untagged": False, "candidates": []} for row in rows
    }
    raw_entry = lookup_evidence(evidence, item_id, suffix)
    members = []
    if raw_entry is not None:
        members, _problem = parse_members(raw_entry)
        if members is None:
            members = []
    for spec, member_rows in members or []:
        if spec.get("kind") != "file":
            continue  # command-backed: static projection only
        resolved = _resolved_envelope(spec, resolve_evidence, repo_root, resolved_cache)
        if resolved is None:
            continue  # unreadable/malformed citation: grading reports it
        _attribute_rows(resolved, member_rows, per_row)
    return per_row


def _resolved_envelope(
    spec: dict[str, Any],
    resolve_evidence: Callable[..., Any],
    repo_root: Any,
    resolved_cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    """One citation's envelope read (its file label, tagged?, the
    measurements), cached by the citation's file+pointer identity so the
    same envelope cited for several rows resolves once."""
    key = json.dumps([spec.get("file"), spec.get("pointer")], sort_keys=True)
    if key not in resolved_cache:
        resolution, _reason = resolve_evidence(spec, repo_root)
        envelope = resolution["envelope"] if resolution is not None else None
        resolved_cache[key] = (
            None
            if not isinstance(envelope, dict)
            else {
                "file_label": spec["file"],
                "tagged": _envelope_has_tags(envelope),
                "measurements": _envelope_measurements(envelope),
            }
        )
    return resolved_cache[key]


def _attribute_rows(
    resolved: dict[str, Any],
    member_rows: list[str] | None,
    per_row: dict[str, dict[str, Any]],
) -> None:
    """Attribute one citation's envelope to the declared rows its member
    entry names: an untagged envelope marks the rows it names
    ``unattributable``; a tagged one contributes every measurement whose
    ``spec_row`` equals the row id, in report order."""
    for row_id in member_rows or []:
        state = per_row.get(row_id)
        if state is None:
            continue  # a member naming an undeclared row: ignored (#3778)
        state["untagged"] = state["untagged"] or not resolved["tagged"]
        if not resolved["tagged"]:
            continue
        for measurement in resolved["measurements"]:
            if measurement.get("spec_row") == row_id:
                state["candidates"].append(
                    {
                        "file": resolved["file_label"],
                        "order": len(state["candidates"]),
                        "measurement": measurement,
                    }
                )


def _emit_slot_records(
    rows: list[dict[str, Any]],
    item_id: int,
    suffix: str | None,
    per_row: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """One record per declared row, in declaration order, in the row's
    state: ``gap`` outranks evidence (a gap is never covered), then any
    attributed candidates grade the record, then the citation-named-but-
    untagged ``unattributable`` state, then ``no_evidence``. A
    partition-qualified slot marks its records with ``partition``."""
    records: list[dict[str, Any]] = []
    for row in rows:
        gap = row.get("gap")
        row_id = row["id"]
        state = per_row[row_id]
        if gap is not None:
            records.append(_base_record(row_id, item_id, "gap", gap))
        elif state["candidates"]:
            records.append(_graded_record(row_id, item_id, state["candidates"]))
        elif state["untagged"]:
            records.append(_base_record(row_id, item_id, "unattributable", None))
        else:
            records.append(_base_record(row_id, item_id, "no_evidence", None))
        if suffix is not None:
            records[-1]["partition"] = suffix
    return records


def _verdict_marker(verdict: Any, palette: Any) -> tuple[str, str]:
    """``(marker, colour)`` for one record's ``verdict`` in the text table:
    green only for a ``pass``, red for a definite ``fail``/``error``,
    uncoloured otherwise."""
    if verdict == "pass":
        return "PASS ", palette.green
    if verdict in ("fail", "error"):
        return f"{verdict.upper():<5}", palette.red
    return f"{str(verdict or 'grade')[:5].upper():<5}", ""


def print_spec_row_table(records: list[dict[str, Any]] | None, palette: Any) -> None:
    """Render the ``spec_rows[]`` block for ``--format text`` -- one line
    per record plus its sources, after the item rows it qualifies. Silent
    for a report without the block, so a non-declaring manifest's text
    rendering is byte-identical to before."""
    if not records:
        return
    print()
    print(f"spec rows ({len(records)}):")
    for record in records:
        state = record["state"]
        if state == "graded":
            marker, colour = _verdict_marker(record["verdict"], palette)
            detail = f"limit {json.dumps(record['limits'], sort_keys=True)}"
            if record["unit"]:
                detail += f" {record['unit']}"
            worst = record["worst"]
            if isinstance(worst, dict):
                detail = (
                    f"worst {worst.get('value')} @ {worst.get('corner_id')} "
                    f"(margin {worst.get('margin')}, "
                    f"rel {worst.get('relative_margin')}) -- {detail}"
                )
            if isinstance(record["yield"], dict):
                interval = record["yield"].get("confidence_interval") or {}
                detail += (
                    f" -- yield {record['yield'].get('estimate')} "
                    f"CI [{interval.get('low')}, {interval.get('high')}] "
                    f"({record['yield'].get('method')})"
                )
        elif state == "gap":
            marker, colour = "GAP  ", ""
            detail = f"declared gap: {record['gap']}"
        elif state == "unattributable":
            marker, colour = "UNATT", ""
            detail = "cited, but its envelope declares no spec_row tags"
        else:
            marker, colour = "MISS ", ""
            detail = "no cited evidence names this row"
        reset = palette.reset if colour else ""
        print(f"[{colour}{marker}{reset}] {record['id']}: {detail}")
        for source in record["sources"]:
            print(f"        from: {source['file']} ({source['measurement']})")

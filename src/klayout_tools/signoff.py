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
``"met"`` verdict for an item with no runnable check behind it. Wiring the
actual DRC/LVS/sim *gates* (deciding which item maps to which verb, running
them, not just reading pre-existing JSON) is Phase 1; this phase is the item
model, the doc parser, and the interface only.

Pure library: both :func:`build_signoff` and :func:`build_tier_report`
return plain Python data (a ``dict`` of JSON-serialisable primitives) and
never print, mirroring ``report.py``. Serialisation and console printing
live in the CLI command module (``cli/signoff_cmd.py``).

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
import os
import sys
from typing import Any

from .design_evidence_tiers import DesignEvidenceTiersError, parse_tier_doc

__all__ = [
    "SignoffError",
    "DesignEvidenceTiersError",
    "build_signoff",
    "build_tier_report",
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
TIER_REPORT_SCHEMA_VERSION = 1

#: Block kinds recognised by ``docs/design-evidence-tiers.md``'s "Block
#: kind" subsection -- the manifest's ``kind`` field must be one of these.
_BLOCK_KINDS = ("analog", "digital", "mixed-signal")

#: Provenance sub-fields compared for consistency across every input
#: envelope that carries them -- see _provenance_consistency()'s docstring
#: for what each check means and why a mismatch is refused rather than
#: silently aggregated.
_PDK_NAME = "pdk.name"
_PDK_VERSION = "pdk.version"
_INPUT_HASH = "input.content_hash"


class SignoffError(Exception):
    """Raised when an envelope cannot even be read/parsed, or does not match
    any recognized ``klt`` envelope shape.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other ``klt`` verb's error contract.
    """


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
                        "values": [{"source": <str>, "value": <str>}, ...],
                    },
                    ...
                ],
            },
            "checks": [
                {
                    "source": <str, the entry from `sources`>,
                    "kind": "drc" | "lvs" | "extract" | "sim" | "error",
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

    Raises :class:`SignoffError` if ``sources`` is empty, any entry cannot
    be read/parsed as JSON, is not a JSON object, or does not match a
    recognized envelope shape (see :func:`_classify`).
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
        kind = _classify(envelope, source)
        checks.append(_build_check(kind, envelope, source))

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


def _read_envelope(source: str) -> Any:
    """Read and JSON-decode one envelope: ``source == "-"`` reads stdin,
    otherwise ``source`` is a file path. Raises :class:`SignoffError` on any
    read/parse failure -- never lets a malformed input silently become an
    incomplete verdict.

    Deliberately mirrors ``report.py``'s ``_read_envelope`` (same
    read/parse/error-message contract) rather than importing it: the two
    commands' input-reading needs are identical but incidental, not a
    shared abstraction worth coupling two independently-versioned CLI
    verbs over.
    """
    if source == "-":
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise SignoffError(f"stdin envelope is not valid JSON: {exc}") from exc

    if not os.path.exists(source):
        raise SignoffError(f"file not found: {source}")
    if os.path.isdir(source):
        raise SignoffError(f"not a file: {source}")

    try:
        with open(source, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise SignoffError(f"could not read envelope file '{source}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SignoffError(
            f"envelope file '{source}' is not valid JSON: {exc}"
        ) from exc


def _classify(envelope: dict[str, Any], source: str) -> str:
    """Detect an envelope's kind from its own structural shape. Raises
    :class:`SignoffError` for an envelope that matches none of them.

    Order matters: an ``error`` envelope (any verb's own ``--format json``
    failure output) is checked first since a failed verb run carries none
    of the success-shape markers below.
    """
    if "schema_version" not in envelope:
        raise SignoffError(
            f"envelope '{source}' has no 'schema_version' field -- not a "
            "recognized klt JSON envelope (see docs/json-contract.md)"
        )

    error = envelope.get("error")
    if isinstance(error, dict) and "message" in error:
        return "error"

    if isinstance(envelope.get("violations"), list):
        return "drc"

    if isinstance(envelope.get("mismatches"), list):
        return "lvs"

    if isinstance(envelope.get("measurements"), list) and "corner_count" in envelope:
        return "sim"

    if "device_count" in envelope and isinstance(envelope.get("nets"), list):
        return "extract"

    raise SignoffError(
        f"envelope '{source}' has an unrecognized shape (schema_version="
        f"{envelope.get('schema_version')!r}): not a klt drc/lvs/extract/sim "
        "success or error envelope -- klt signoff aggregates only those "
        "four verbs today (see docs/cli/signoff.md)"
    )


# --------------------------------------------------------------------------- #
# Envelope -> check
# --------------------------------------------------------------------------- #


def _build_check(kind: str, envelope: dict[str, Any], source: str) -> dict[str, Any]:
    status = envelope.get("status") if kind != "error" else "error"
    return {
        "source": source,
        "kind": kind,
        "status": status,
        "passed": _check_passed(kind, envelope),
        "detail": _detail(kind, envelope),
        "provenance": envelope.get("provenance"),
    }


def _check_passed(kind: str, envelope: dict[str, Any]) -> bool:
    """Whether this one check counts as passing.

    - ``drc`` passes on ``status == "clean"``.
    - ``lvs`` passes on ``status == "match"``.
    - ``sim`` passes on ``status == "pass"``.
    - ``extract`` has no independent pass/fail: ``klt extract`` either
      produces a ``status: "extracted"`` envelope or raises (which surfaces
      here as the ``error`` kind, not a JSON envelope at all) -- so a
      present extract envelope is definitionally a successful extraction,
      always ``True``. It is still listed in ``checks[]`` (not dropped) so
      its ``provenance`` block participates in the consistency check below
      and its device/net counts are visible in the aggregated verdict.
    - ``error`` never passes.
    """
    if kind == "drc":
        return envelope.get("status") == "clean"
    if kind == "lvs":
        return envelope.get("status") == "match"
    if kind == "sim":
        return envelope.get("status") == "pass"
    if kind == "extract":
        return True
    return False  # kind == "error"


def _detail(kind: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """A small, kind-specific excerpt of the source envelope -- not a
    re-export of the full contract (a consumer that wants the raw
    ``violations[]``/``mismatches[]``/``devices[]``/``corners[]`` detail
    should read the original envelope file directly, exactly as
    ``report.py``'s ``sections[]`` documents for the same reason)."""
    if kind == "drc":
        return {
            "file": envelope.get("file"),
            "deck": envelope.get("deck"),
            "violation_count": envelope.get("violation_count"),
        }
    if kind == "lvs":
        return {
            "layout": envelope.get("layout"),
            "reference": envelope.get("reference"),
            "mismatch_count": envelope.get("mismatch_count"),
            "counts": envelope.get("counts"),
        }
    if kind == "sim":
        return {
            "netlist": envelope.get("netlist"),
            "corner_count": envelope.get("corner_count"),
            "passed": envelope.get("passed"),
            "failed": envelope.get("failed"),
            "errored": envelope.get("errored"),
        }
    if kind == "extract":
        return {
            "file": envelope.get("file"),
            "deck": envelope.get("deck"),
            "device_count": envelope.get("device_count"),
            "net_count": envelope.get("net_count"),
        }
    # kind == "error"
    error = envelope.get("error") or {}
    return {"command": error.get("command"), "message": error.get("message")}


# --------------------------------------------------------------------------- #
# Provenance consistency (issue #251's provenance block; #309 AC #2)
# --------------------------------------------------------------------------- #


def _provenance_consistency(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Refuse to combine checks whose ``provenance`` blocks disagree about
    what was actually run.

    Four fields are compared, each only across the checks that actually
    populate it (``docs/json-contract.md``'s ``provenance`` block leaves a
    field ``None`` when a verb has nothing to report there -- e.g. ``klt
    lvs``'s ``provenance.input`` is always ``None``, so it never
    participates):

    - ``pdk.name`` / ``pdk.version`` -- every check that resolved a PDK
      must agree on which one and which release. A DRC report from a
      sky130 run combined with an LVS report from a gf180mcu run (or two
      sky130 runs against different PDK snapshots) is not one signoff.
    - ``input.content_hash`` -- populated by ``drc``/``extract`` only
      (``docs/json-contract.md``). When more than one check populates it,
      they must agree: the whole point of "signoff" is that DRC and
      extraction ran against the *same* layout stream, not a stale pairing
      (the design doc's §1 "signoff rejection" failure mode).
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
    _check_scalar_field(
        checks, mismatches, field=_INPUT_HASH, path=("input", "content_hash")
    )
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


def build_tier_report(manifest: dict[str, Any]) -> dict[str, Any]:
    """Grade a block manifest against the T1-T4 evidence-tier item skeleton
    mechanically parsed from ``docs/design-evidence-tiers.md``
    (:mod:`.design_evidence_tiers`) -- the item list is never duplicated
    here, so tiers and this aggregator can never drift.

    ``manifest`` (JSON in)::

        {
            "block": "my-block",              # optional, echoed back verbatim
            "kind": "analog" | "digital" | "mixed-signal",   # required
            "evidence": {                      # optional, default {}
                "3": "drc.json",
                "4": {"file": "lvs.json", "content_hash": "sha256:..."},
                # for a mixed-signal block, per-kind items (1, 2, 5, 7) key
                # on "<item id>.<analog|digital>"; kind-independent items
                # (3, 4, 6, 8, 9, 10) may use the bare "<item id>" key and
                # are looked up by both partitions -- see the doc's "Block
                # kind" subsection for which items split which way.
            }
        }

    Returns (JSON out)::

        {
            "schema_version": 1,
            "block": "my-block" | None,
            "kind": "analog",
            "tier": "T1" | None,
            "t1_item_count": 10,
            "t1_met_count": 3,
            "source_doc": "docs/design-evidence-tiers.md",
            "items": [
                {
                    "tier": "T1",
                    "id": 3,
                    "title": "DRC clean",
                    "partition": None,
                    "text": "latest `klt drc` JSON report: ...",
                    "notes": [],
                    "status": "met",
                    "citation": {
                        "file": "drc.json",
                        "kind": "drc",
                        "check_status": "clean",
                        "content_hash": "sha256:...",
                        "exit_status": 0,
                    },
                },
                ...  # items 1-10 (doubled per partition for mixed-signal),
                     # then one entry per T2/T3/T4 ladder row
            ],
        }

    An item's ``status`` is ``"met"`` only when its ``evidence`` entry
    resolves to a *readable* ``klt`` JSON envelope, classifiable as one of
    ``drc``/``lvs``/``extract``/``sim`` (:func:`_classify`), whose own check
    passed (:func:`_check_passed`) -- and, if the evidence entry pinned an
    expected ``content_hash``, whose ``provenance.input.content_hash``
    matches it (a mismatch means the check ran against a *different* layout
    revision than the one being claimed -- stale, so it renders ``"unmet"``,
    never a false pass). Every other case (no evidence entry, a malformed
    entry, an unreadable/unparsable evidence file, an unrecognised envelope
    shape, or a failing check) also renders ``"unmet"``: this phase never
    infers a ``"met"`` verdict for an item with no runnable check behind it.

    A ``"met"`` item's ``citation`` always carries the evidence file, the
    envelope's own status, its input content hash (``None`` when the
    envelope's own provenance doesn't populate one -- ``klt lvs``/``klt
    sim`` don't, per ``docs/json-contract.md``), and ``exit_status: 0``
    (inferred: a readable, classifiable, non-error envelope implies the
    producing command exited zero -- every ``klt`` verb emits an
    ``error``-kind envelope, not a success envelope, on any nonzero exit).

    T2-T4 render as single ladder-row items (per the doc's "The ladder"
    table -- a Curator correction on issue #722: only T1 has an itemized
    checklist, T2-T4 are each one gated condition layered on top) and are
    always ``"unmet"`` in this phase: this toolkit's closed loop targets T1,
    and T2+ require commercial tools/fab access this repo has no mechanism
    to check.

    ``tier`` is ``"T1"`` only when every rendered T1 item (across every
    partition, for a ``mixed-signal`` block) is ``"met"``; otherwise
    ``None`` -- there is no partial-credit tier.

    Raises :class:`SignoffError` if ``manifest`` is not a JSON object, its
    ``kind`` is missing or not one of ``analog``/``digital``/``mixed-signal``,
    or its ``evidence`` field (when given) is not a JSON object. Raises
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

    doc = parse_tier_doc()
    partitions: tuple[str, ...] = (
        ("analog", "digital") if kind == "mixed-signal" else (kind,)
    )

    items: list[dict[str, Any]] = []
    met_count = 0
    total = 0
    for t1_item in doc["t1_items"]:
        for partition in partitions:
            entry = _build_tier_item(
                tier="T1",
                item_id=t1_item["id"],
                title=t1_item["title"],
                text=_t1_item_text(t1_item, partition),
                notes=list(t1_item["notes"]),
                partition=partition if kind == "mixed-signal" else None,
                evidence=evidence,
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
                "citation": None,
            }
        )

    tier = "T1" if total > 0 and met_count == total else None

    return {
        "schema_version": TIER_REPORT_SCHEMA_VERSION,
        "block": manifest.get("block"),
        "kind": kind,
        "tier": tier,
        "t1_item_count": total,
        "t1_met_count": met_count,
        "source_doc": "docs/design-evidence-tiers.md",
        "items": items,
    }


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


def _normalize_evidence_entry(raw: Any) -> tuple[str | None, str | None]:
    """Return ``(file, expected_content_hash)`` from a manifest evidence[]
    entry -- either a bare file-path string, or an object with a required
    ``"file"`` string and an optional ``"content_hash"`` string to pin the
    check to a specific layout revision. Returns ``(None, None)`` for any
    entry whose shape isn't recognised -- a malformed *single* entry
    degrades that one item to ``"unmet"`` rather than aborting the whole
    report (see :func:`build_tier_report`'s docstring)."""
    if isinstance(raw, str):
        return raw, None
    if isinstance(raw, dict):
        file = raw.get("file")
        if not isinstance(file, str):
            return None, None
        expected_hash = raw.get("content_hash")
        if expected_hash is not None and not isinstance(expected_hash, str):
            expected_hash = None
        return file, expected_hash
    return None, None


def _build_tier_item(
    *,
    tier: str,
    item_id: int,
    title: str,
    text: str | None,
    notes: list[str],
    partition: str | None,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Grade one T1 checklist item against ``evidence`` -- see
    :func:`build_tier_report`'s docstring for the full met/unmet rule."""
    citation = None
    status = "unmet"

    raw_entry = _lookup_evidence(evidence, item_id, partition)
    if raw_entry is not None:
        file, expected_hash = _normalize_evidence_entry(raw_entry)
        if file is not None:
            try:
                envelope = _read_envelope(file)
            except SignoffError:
                envelope = None
            if isinstance(envelope, dict):
                try:
                    check_kind = _classify(envelope, file)
                except SignoffError:
                    check_kind = None
                if (
                    check_kind is not None
                    and check_kind != "error"
                    and _check_passed(check_kind, envelope)
                ):
                    provenance = envelope.get("provenance") or {}
                    input_block = provenance.get("input") or {}
                    actual_hash = input_block.get("content_hash")
                    stale = expected_hash is not None and actual_hash != expected_hash
                    if not stale:
                        citation = {
                            "file": file,
                            "kind": check_kind,
                            "check_status": envelope.get("status"),
                            "content_hash": actual_hash,
                            "exit_status": 0,
                        }
                        status = "met"

    return {
        "tier": tier,
        "id": item_id,
        "title": title,
        "partition": partition,
        "text": text,
        "notes": notes,
        "status": status,
        "citation": citation,
    }

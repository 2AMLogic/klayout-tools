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

**Provisional envelope shape.** `klt pex` (Epic #709) does not exist in this
codebase as of this writing, and its defining issue, **#801** ("Define `klt
pex`"), is stalled with an empty body pending an operator decision -- there
is no ratified JSON shape to build against yet. :func:`_classify` recognises
a **Curator-proposed, provisional** `pex` shape instead (a top-level
``delta`` list plus a ``reference_netlist`` field, mirroring how `klt sim`'s
shape is detected by ``measurements``/``corner_count`` and `klt extract`'s by
``device_count``/``nets``), scoped narrowly enough that #801 landing later is
very likely additive to it, not a rewrite. **This is not #801's ratified
shape** -- reconcile against #801's real report shape once it lands (re-check
#801's state before relying on this note).

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
import os
import shlex
import subprocess
import sys
from typing import Any

from ._provenance import sha256_file
from .design_evidence_tiers import DesignEvidenceTiersError, parse_tier_doc

__all__ = [
    "SignoffError",
    "DesignEvidenceTiersError",
    "build_signoff",
    "build_tier_report",
    "build_fleet_report",
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

#: Schema version for :func:`build_fleet_report`'s own JSON shape -- distinct
#: from both :data:`SCHEMA_VERSION` and :data:`TIER_REPORT_SCHEMA_VERSION`
#: for the same reason: the three modes' top-level fields are unrelated, so
#: bumping one must never imply either of the others changed.
FLEET_REPORT_SCHEMA_VERSION = 1

#: Block kinds recognised by ``docs/design-evidence-tiers.md``'s "Block
#: kind" subsection -- the manifest's ``kind`` field must be one of these.
_BLOCK_KINDS = ("analog", "digital", "mixed-signal")

#: Per-T1-item-id restriction on which :func:`_classify` kinds may satisfy
#: that item (issue #871, Phase 2b of epic #706) -- passed as
#: :func:`_build_tier_item`'s ``allowed_kinds`` parameter. An item id absent
#: from this map (every id but 7, today) is unrestricted (``None``),
#: preserving the original Phase 0/1 behaviour where any recognised, passing
#: envelope kind satisfies any item. Item 7 ("Post-layout verification")
#: is the only item this phase restricts: it requires a ``"pex"``-kind
#: citation (a `klt pex` schematic-vs-extracted-netlist delta report -- see
#: this module's "Post-layout binding" docstring section) -- a `klt
#: drc`/`klt lvs`/generic `klt sim`/`klt extract`/`klt yield` citation no
#: longer satisfies it, even if that check itself passed.
_ITEM_ALLOWED_KINDS: dict[int, set[str]] = {
    7: {"pex"},
}

#: Provenance sub-fields compared for consistency across every input
#: envelope that carries them -- see _provenance_consistency()'s docstring
#: for what each check means and why a mismatch is refused rather than
#: silently aggregated.
_PDK_NAME = "pdk.name"
_PDK_VERSION = "pdk.version"
_INPUT_HASH = "input.content_hash"

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
                    "kind": "drc" | "lvs" | "extract" | "sim" | "yield" | "pex"
                            | "error",
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


def _read_json_source(source: str, description: str) -> Any:
    """Read and JSON-decode one JSON source: ``source == "-"`` reads stdin,
    otherwise ``source`` is a file path. Raises :class:`SignoffError` on any
    read/parse failure -- never lets a malformed input silently become an
    incomplete verdict. ``description`` (e.g. ``"envelope"``, ``"manifest"``)
    only affects error-message wording, so each caller's failures still read
    naturally.

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
            return json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise SignoffError(f"stdin {description} is not valid JSON: {exc}") from exc

    if not os.path.exists(source):
        raise SignoffError(f"file not found: {source}")
    if os.path.isdir(source):
        raise SignoffError(f"not a file: {source}")

    try:
        with open(source, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise SignoffError(
            f"could not read {description} file '{source}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SignoffError(
            f"{description} file '{source}' is not valid JSON: {exc}"
        ) from exc


def _read_envelope(source: str) -> Any:
    """Read and JSON-decode one ``klt`` envelope -- see
    :func:`_read_json_source`."""
    return _read_json_source(source, "envelope")


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

    # `klt yield` (issue #816, Phase 1a of epic #710): also carries a
    # `measurements` list, but never a `corner_count` (checked above first,
    # so the two can never collide) -- `measurement_count` plus a `source`
    # object are unique to this shape (docs/cli/yield.md's JSON schema).
    if (
        isinstance(envelope.get("measurements"), list)
        and "measurement_count" in envelope
        and isinstance(envelope.get("source"), dict)
    ):
        return "yield"

    if "device_count" in envelope and isinstance(envelope.get("nets"), list):
        return "extract"

    # `klt pex` (issue #871, Phase 2b of epic #706): does not exist in this
    # codebase yet -- this recognition rule matches a Curator-proposed,
    # *provisional* envelope shape (issue #871's own proposal), not a shape
    # ratified by #801 ("Define `klt pex`", still stalled with an empty body
    # as of this writing). A top-level `delta` list (per-corner/per-spec-row
    # schematic-vs-extracted comparisons) plus `reference_netlist` (the
    # schematic netlist compared against) is unique to this shape -- it
    # cannot collide with `sim` (detected by `measurements`+`corner_count`
    # above, checked first) or `extract` (`device_count`+`nets`, checked
    # just above). Reconcile with #801's real shape once it lands.
    if isinstance(envelope.get("delta"), list) and "reference_netlist" in envelope:
        return "pex"

    raise SignoffError(
        f"envelope '{source}' has an unrecognized shape (schema_version="
        f"{envelope.get('schema_version')!r}): not a klt drc/lvs/extract/sim/"
        "yield/pex success or error envelope -- klt signoff aggregates only "
        "those six verbs today (see docs/cli/signoff.md)"
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
    - ``pex`` (issue #871, provisional shape pending #801) passes on
      ``status == "pass"`` -- mirrors ``sim``: every graded delta row met
      its tolerance.
    - ``error`` never passes.
    """
    if kind == "drc":
        return envelope.get("status") == "clean"
    if kind == "lvs":
        return envelope.get("status") == "match"
    if kind == "sim":
        return envelope.get("status") == "pass"
    if kind == "yield":
        return envelope.get("status") in ("pass", "reported")
    if kind == "extract":
        return True
    if kind == "pex":
        return envelope.get("status") == "pass"
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
    if kind == "yield":
        source = envelope.get("source") or {}
        return {
            "samples": envelope.get("samples"),
            "limits": envelope.get("limits"),
            "measurement_count": envelope.get("measurement_count"),
            "source_kind": source.get("kind"),
            "sample_count": source.get("sample_count"),
        }
    if kind == "extract":
        return {
            "file": envelope.get("file"),
            "deck": envelope.get("deck"),
            "device_count": envelope.get("device_count"),
            "net_count": envelope.get("net_count"),
        }
    if kind == "pex":
        return {
            "netlist": envelope.get("netlist"),
            "reference_netlist": envelope.get("reference_netlist"),
            "corner_count": envelope.get("corner_count"),
            "passed": envelope.get("passed"),
            "failed": envelope.get("failed"),
            "errored": envelope.get("errored"),
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
                "5": {
                    "command": ["klt", "sim", "corners.json", "--format", "json"],
                    "cwd": "sim/",                  # optional, default: this cwd
                    "content_hash": "sha256:...",   # optional, same staleness gate
                },
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
    stdout). See :func:`_normalize_evidence_entry`/:func:`_grade_evidence`.

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
                    "reason": None,
                    "citation": {
                        "file": "drc.json",
                        "command": None,
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
    ``drc``/``lvs``/``extract``/``sim``/``yield``/``pex`` (:func:`_classify`),
    whose own check passed (:func:`_check_passed`) -- and, if the evidence
    entry pinned an expected ``content_hash``, whose input content hash
    matches it (``provenance.input.content_hash`` for drc/lvs/extract/sim;
    the hashed ``samples`` document for yield, per
    :func:`_yield_samples_content_hash` -- a mismatch means the check ran
    against a *different* layout/sample revision than the one being claimed
    -- stale, so it renders ``"unmet"``, never a false pass). Every other
    case (no evidence entry, a malformed entry, an unreadable/unparsable
    evidence file, a command that could not be run to completion or whose
    stdout didn't parse, an unrecognised envelope shape, a failing check, or
    (issue #871) a passing check of a kind this item does not accept) also
    renders ``"unmet"``: this phase never infers a ``"met"`` verdict for an
    item with no runnable check behind it.

    **Item 7 is kind-restricted** (issue #871, Phase 2b of epic #706): every
    other T1 item accepts any recognised, passing envelope kind, but item 7
    ("Post-layout verification") only accepts a ``"pex"``-kind citation --
    the schematic-vs-extracted-netlist re-simulation delta a `klt pex`
    (Epic #709) run would produce (see this module's "Post-layout binding"
    docstring section for the provisional envelope shape and its
    reconciliation status against #801). A ``drc``/``lvs``/``sim``/``extract``/
    ``yield`` citation for item 7 -- even a genuinely passing one -- renders
    ``"unmet"`` with ``reason: "wrong_kind"``, never a borrowed pass.

    An ``"unmet"`` item's ``reason`` (issue #826, Phase 1b of epic #706)
    names *why*, machine-readably, so a reader never has to guess whether an
    item was skipped or actually failed:

    - ``"no_evidence"`` -- the manifest gave no ``evidence`` entry for this
      item at all.
    - ``"invalid_evidence"`` -- the manifest's entry for this item is
      present but malformed (neither a string, nor an object with a string
      ``"file"``, nor an object with a non-empty list-of-strings
      ``"command"``).
    - ``"unreadable_evidence"`` -- a file-backed entry's named file does not
      exist, is not readable, or is not valid JSON; or a command-backed
      entry's subprocess exited zero but its stdout was not valid JSON.
    - ``"unrecognized_envelope"`` -- the resolved evidence parsed as JSON
      but is not a JSON object, or is a JSON object that does not match any
      recognised ``klt`` envelope shape (:func:`_classify`).
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
    - ``"wrong_kind"`` (issue #871) -- the evidence resolved to a recognised,
      *passing* envelope, but its classified kind is not one this item
      accepts (today, only item 7 restricts kinds -- see "Item 7 is
      kind-restricted" above). The cited check did not fail on its own
      terms; it simply does not prove what this item requires.
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
    :func:`_yield_samples_content_hash`), and ``exit_status``: for a file-backed entry
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
                allowed_kinds=_ITEM_ALLOWED_KINDS.get(t1_item["id"]),
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


def _normalize_evidence_entry(raw: Any) -> dict[str, Any] | None:
    """Normalize a manifest ``evidence[]`` entry into one of two shapes, or
    ``None`` if it matches neither -- a malformed *single* entry degrades
    that one item to ``"unmet"`` rather than aborting the whole report (see
    :func:`build_tier_report`'s docstring):

    - **File-backed** (Phase 0, issue #722): a bare file-path string, or
      ``{"file": <str>, "content_hash": <str>?}`` -- returned as
      ``{"kind": "file", "file": ..., "content_hash": ...}``.
    - **Command-backed** (Phase 1, issue #825): ``{"command": [<str>, ...],
      "cwd": <str>?, "content_hash": <str>?}`` -- a non-empty list of
      strings is required; returned as ``{"kind": "command", "command":
      ..., "cwd": ..., "content_hash": ...}``. Checked before the file
      shape so a dict carrying both keys (which the schema does not ask
      for, but a caller might send) is treated as command-backed.
    """
    if isinstance(raw, str):
        return {"kind": "file", "file": raw, "content_hash": None}
    if not isinstance(raw, dict):
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
        }

    file = raw.get("file")
    if not isinstance(file, str):
        return None
    expected_hash = raw.get("content_hash")
    if expected_hash is not None and not isinstance(expected_hash, str):
        expected_hash = None
    return {"kind": "file", "file": file, "content_hash": expected_hash}


def _yield_samples_content_hash(
    envelope: dict[str, Any], spec: dict[str, Any]
) -> str | None:
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

    The path is resolved relative to ``spec["cwd"]`` for a command-backed
    entry -- the same directory the subprocess that produced ``samples``
    ran in, so a relative path in the report resolves exactly as it did for
    that subprocess -- or relative to this process's own current working
    directory for a file-backed entry, matching how the evidence *file*
    itself is resolved (no recorded cwd exists for a pre-existing report).

    Returns ``None`` when the envelope names no ``samples`` document (or it
    isn't a string), or the referenced file can't be hashed (missing,
    unreadable) -- exactly mirroring ``sha256_file``'s own "unhashable
    input" fallback, never raising.

    Follow-up reconciliation: if a later #710 phase adds its own
    ``provenance.input.content_hash`` to `klt yield`'s JSON shape, the
    generic ``input_block`` lookup in :func:`_grade_evidence` finds it first
    and this fallback simply never fires again -- no change needed here.
    """
    samples = envelope.get("samples")
    if not isinstance(samples, str):
        return None
    cwd = spec.get("cwd") if spec.get("kind") == "command" else None
    path = os.path.join(cwd, samples) if cwd and not os.path.isabs(samples) else samples
    digest = sha256_file(path)
    return f"sha256:{digest}" if digest is not None else None


def _grade_evidence(
    spec: dict[str, Any],
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Grade one resolved evidence ``spec`` (:func:`_normalize_evidence_entry`)
    and return ``(status, reason, citation)``.

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
    kinds.
    """
    expected_hash = spec.get("content_hash")

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
            return "unmet", _REASON_COMMAND_FAILED, None

        exit_status = completed.returncode

        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError:
            if exit_status == 0:
                return "unmet", _REASON_UNREADABLE_EVIDENCE, None
            return "unmet", _REASON_COMMAND_FAILED, None

        if not isinstance(envelope, dict):
            return "unmet", _REASON_UNRECOGNIZED_ENVELOPE, None

        file_label: str | None = None
        source_label = command_label
    else:
        file = spec["file"]
        try:
            envelope = _read_envelope(file)
        except SignoffError:
            return "unmet", _REASON_UNREADABLE_EVIDENCE, None

        if not isinstance(envelope, dict):
            return "unmet", _REASON_UNRECOGNIZED_ENVELOPE, None

        file_label = file
        command_label = None
        exit_status = 0
        source_label = file

    try:
        check_kind = _classify(envelope, source_label)
    except SignoffError:
        return "unmet", _REASON_UNRECOGNIZED_ENVELOPE, None

    if check_kind == "error":
        return "unmet", _REASON_CHECK_ERRORED, None

    if not _check_passed(check_kind, envelope):
        return "unmet", _REASON_CHECK_FAILED, None

    provenance = envelope.get("provenance") or {}
    input_block = provenance.get("input") or {}
    actual_hash = input_block.get("content_hash")
    if actual_hash is None and check_kind == "yield":
        # klt yield's current JSON shape (issue #816) carries no
        # `provenance` block of its own -- see this module's "Statistical-
        # evidence binding" docstring section and
        # :func:`_yield_samples_content_hash`.
        actual_hash = _yield_samples_content_hash(envelope, spec)
    if expected_hash is not None and actual_hash != expected_hash:
        return "unmet", _REASON_STALE_EVIDENCE, None

    citation = {
        "file": file_label,
        "command": command_label,
        "kind": check_kind,
        "check_status": envelope.get("status"),
        "content_hash": actual_hash,
        "exit_status": exit_status,
    }
    return "met", None, citation


def _build_tier_item(
    *,
    tier: str,
    item_id: int,
    title: str,
    text: str | None,
    notes: list[str],
    partition: str | None,
    evidence: dict[str, Any],
    allowed_kinds: set[str] | None = None,
) -> dict[str, Any]:
    """Grade one T1 checklist item against ``evidence`` -- see
    :func:`build_tier_report`'s docstring for the full met/unmet rule and
    the ``reason`` enum.

    ``allowed_kinds`` (issue #871, Phase 2b of epic #706): when given, a
    ``"met"`` grading is only accepted if the resolved evidence's classified
    kind (:func:`_classify`, via the citation's ``"kind"``) is a member of
    this set -- otherwise the item is downgraded to ``"unmet"`` with
    ``reason: "wrong_kind"`` and no citation. ``None`` (the default) means no
    restriction, preserving Phase 0/1's original behaviour where any
    recognised, passing envelope kind satisfies any item -- every T1 item
    except item 7 still passes ``None`` (see :data:`_ITEM_ALLOWED_KINDS`).
    """
    citation = None
    status = "unmet"
    reason: str | None = _REASON_NO_EVIDENCE

    raw_entry = _lookup_evidence(evidence, item_id, partition)
    if raw_entry is not None:
        spec = _normalize_evidence_entry(raw_entry)
        if spec is None:
            reason = _REASON_INVALID_EVIDENCE
        else:
            status, reason, citation = _grade_evidence(spec)
            if (
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
        "text": text,
        "notes": notes,
        "status": status,
        "reason": reason,
        "citation": citation,
    }


# --------------------------------------------------------------------------- #
# Fleet roll-up (issue #827, Phase 1c of epic #706)
# --------------------------------------------------------------------------- #


def build_fleet_report(fleet: dict[str, Any]) -> dict[str, Any]:
    """Grade every block in a **fleet manifest** against the T1-T4 item
    skeleton (:func:`build_tier_report`, called once per block) and reduce
    each block's result down to its current tier and, for any block not yet
    at T1, the single T1 item still blocking it -- turning "which canaries
    are at which tier, and what's blocking each not-yet-T1 block" into one
    query instead of a survey (Epic #706, Phase 1c).

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
            "schema_version": 1,
            "block_count": 3,
            "t1_count": 1,
            "not_t1_count": 2,
            "source_doc": "docs/design-evidence-tiers.md",
            "blocks": [
                {
                    "block": "sky130-bandgap",
                    "source": "manifests/sky130-bandgap.json",
                    "kind": "analog",
                    "tier": "T1",
                    "t1_item_count": 10,
                    "t1_met_count": 10,
                    "blocking_item": None,
                },
                {
                    "block": "gf180-bandgap",
                    "source": None,
                    "kind": "analog",
                    "tier": None,
                    "t1_item_count": 10,
                    "t1_met_count": 3,
                    "blocking_item": {
                        "id": 4,
                        "title": "LVS clean",
                        "partition": None,
                        "reason": "no_evidence",
                    },
                },
                ...
            ],
        }

    ``blocking_item`` is the *first* rendered T1 item (in the same order
    :func:`build_tier_report` renders items -- item id, then partition for a
    mixed-signal block) whose ``status`` is not ``"met"``, or ``None`` when
    ``tier == "T1"``. It is deliberately a single item, not the full unmet
    list: the roll-up's job is "what is the next thing to fix", not a
    re-rendering of the per-block report (open that block's own
    ``--manifest`` report for the full item-by-item detail). No evidence is
    read or graded here beyond what :func:`build_tier_report` already did --
    this function only reduces its output, so a block's roll-up row and its
    full tier report can never disagree about *why* it isn't T1 yet.

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

        tier_report = build_tier_report(manifest)
        blocking_item = _first_unmet_t1_item(tier_report["items"])
        if tier_report["tier"] == "T1":
            t1_count += 1

        blocks.append(
            {
                "block": block_name,
                "source": source,
                "kind": tier_report["kind"],
                "tier": tier_report["tier"],
                "t1_item_count": tier_report["t1_item_count"],
                "t1_met_count": tier_report["t1_met_count"],
                "blocking_item": blocking_item,
            }
        )

    return {
        "schema_version": FLEET_REPORT_SCHEMA_VERSION,
        "block_count": len(blocks),
        "t1_count": t1_count,
        "not_t1_count": len(blocks) - t1_count,
        "source_doc": "docs/design-evidence-tiers.md",
        "blocks": blocks,
    }


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


def _first_unmet_t1_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a trimmed view of the first rendered T1 item in ``items``
    (:func:`build_tier_report`'s own item order) whose ``status`` is not
    ``"met"``, or ``None`` if every T1 item is met. T2-T4 ladder rows are
    never candidates -- they are always ``"unmet"`` by design (this
    toolkit's closed loop targets T1) and are not what gates the ``tier ==
    "T1"`` verdict this roll-up reports against."""
    for item in items:
        if item["tier"] == "T1" and item["status"] != "met":
            return {
                "id": item["id"],
                "title": item["title"],
                "partition": item["partition"],
                "reason": item["reason"],
            }
    return None

"""The OpenSTA Liberty-reader warnings that discard a timing model
(issue #7411).

When a Liberty file names a ``lu_table_template`` the library does not
define, OpenSTA's reader warns and drops the table::

    [WARNING STA-1253] <liberty> line N, table_template <name> not found.
    [WARNING STA-1311] <liberty> line N, no table models found in timing group.

The timing group is discarded, so the arc carries no delay model. For a
sequential cell's CLK-to-Q group that removes every register-launched path
from the analysis: ``report_checks`` returns only input-port-launched
paths and ``check_timing`` reports the flop-driven output pins as
unconstrained endpoints. Refusing or downgrading the run would be wrong --
a library with one dropped non-critical group is legitimate -- but the
envelope must say so where the slacks are, because numbers that silently
omit every register-launched path look exactly like complete measurements.

This module is the single scan point for those two codes:

- :func:`parse_liberty_model_warnings` builds the per-corner
  ``liberty_warnings`` block ``klt sta`` carries (always present, ``count:
  0`` on a clean deck, so the field is a stable contract);
- :func:`merge_liberty_model_warning_lines` accumulates lines across the
  ``klt place-and-route`` stage loop -- every stage script re-reads the
  same deck, so identical lines are deduplicated and a repeated warning
  across stages is counted once;
- :func:`format_liberty_model_warning` renders the one human string
  ``klt place-and-route`` appends to its top-level ``warnings`` when the
  deduplicated count is non-zero (a clean deck adds nothing).

Only ``STA-1253`` and ``STA-1311`` are matched -- the two codes that mean
*a model was dropped*. Scanning every ``[WARNING STA-*]`` would fold
unrelated Liberty-reader diagnostics (units, duplicate-library, ...) into
a field whose meaning is precisely "these slacks may omit the paths the
discarded groups would have launched", and parsing the Liberty ourselves
to find undefined templates would second-guess the engine that already
made the decision. OpenROAD builds differ in whether the logger writes to
stdout or stderr -- the same reason ``post_route_sta``'s SPEF
reader-warning scan reads both streams -- so both streams are always
scanned. The post-route corner sweep ``klt place-and-route`` runs at the
``route`` stage is deliberately *not* scanned; it reads the same
cell-library decks the stage scripts do, so a deck the reader discards a
model from is already reported by the stage loop.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from . import env_provenance


class _ReportPaths(Protocol):
    """Path operations used to make a Liberty warning portable."""

    def output_envelope(self, path: str, /) -> dict[str, Any]: ...
    def scrub(self, text: str, /) -> str: ...


#: The two OpenSTA Liberty-reader warnings that discard a timing model:
#: ``STA-1253`` (``table_template <name> not found`` -- the timing table
#: names a ``lu_table_template`` the library does not define) and
#: ``STA-1311`` (``no table models found in timing group`` -- the group
#: the dropped table leaves behind). Matched stream-wide in both stdout
#: and stderr: there is no marker-delimited stdout region bracketing
#: ``read_liberty`` the way ``post_route_sta`` brackets ``read_spef``, and
#: stderr cannot be interleaved with Tcl ``puts`` markers anyway.
_LIBERTY_MODEL_WARNING_RE = re.compile(r"\[WARNING STA-(1253|1311)\]")

#: The codes in reporting order. ``by_code`` always carries both keys,
#: zero counts included, so the block shape never depends on which codes
#: a particular deck happened to trigger.
_LIBERTY_MODEL_WARNING_CODES = ("STA-1253", "STA-1311")

#: Upper bound on how many verbatim warning lines are echoed into the
#: response -- a diagnostic sample, not an exhaustive log (a library with
#: hundreds of undefined templates emits one line per group). Same value
#: as the SPEF reader-warning sample cap.
LIBERTY_MODEL_WARNING_SAMPLE_LIMIT = 5


def liberty_model_warning_lines(*streams: str) -> list[str]:
    """The verbatim ``STA-1253``/``STA-1311`` lines across the given
    streams, stripped, deduplicated, in first-seen order.

    Deduplication is by whole stripped line: the *same* warning (same
    deck, same line number, same text) repeated -- within one stream, or
    across the stage re-reads :func:`merge_liberty_model_warning_lines`
    accumulates -- collapses to one entry, while two genuinely distinct
    warnings (a different line number or template name) both count.
    """
    matched: list[str] = []
    seen: set[str] = set()
    for stream in streams:
        for line in stream.splitlines():
            stripped = line.strip()
            if _LIBERTY_MODEL_WARNING_RE.search(stripped) and stripped not in seen:
                seen.add(stripped)
                matched.append(stripped)
    return matched


def liberty_model_warning_block(lines: Sequence[str]) -> dict[str, Any]:
    """The response block for a deduplicated line list:
    ``{count, by_code, sample, sample_limit, truncated}`` -- the same
    self-describing shape ``constraint_completeness``'s checks use."""
    by_code = dict.fromkeys(_LIBERTY_MODEL_WARNING_CODES, 0)
    for line in lines:
        match = _LIBERTY_MODEL_WARNING_RE.search(line)
        assert match is not None  # every line came from the matcher above
        by_code[f"STA-{match.group(1)}"] += 1
    sample = list(lines[:LIBERTY_MODEL_WARNING_SAMPLE_LIMIT])
    return {
        "count": len(lines),
        "by_code": by_code,
        "sample": sample,
        "sample_limit": LIBERTY_MODEL_WARNING_SAMPLE_LIMIT,
        "truncated": len(lines) > len(sample),
    }


def parse_liberty_model_warnings(stdout: str, stderr: str = "") -> dict[str, Any]:
    """The ``liberty_warnings`` block for one engine session's streams
    (issue #7411). Always present on the response -- a clean deck reports
    ``count: 0``, both ``by_code`` keys zeroed, never a missing field."""
    return liberty_model_warning_block(liberty_model_warning_lines(stdout, stderr))


def scrubbed_liberty_warnings(
    stdout: str | None,
    stderr: str | None,
    decks: Sequence[str],
    report_paths: _ReportPaths | None,
) -> dict[str, Any]:
    """``parse_liberty_model_warnings`` with every resolved Liberty path in
    the verbatim sample rewritten to its report identity (issue #2879's
    rule, extended by #7411 to the decks the session actually read) --
    never an absolute host path in the persisted report.

    ``report_paths`` is ``klt sta``'s ``_ReportPaths`` (duck-typed:
    ``output_envelope(path)`` and ``scrub(text)``, the latter also
    rewriting the three input paths); ``None`` degrades to
    :func:`klayout_tools.env_provenance.repo_relative_path` envelopes and
    no input-path pass. Longest engine path first, so an
    ``abspath``/``realpath`` pair never half-rewrites the other; a deck
    outside any repository renders ``<outside repo>``.
    """

    def envelope_for(deck: str) -> dict[str, Any]:
        if report_paths is not None:
            return report_paths.output_envelope(deck)
        return env_provenance.repo_relative_path(deck)

    labels: dict[str, str] = {}
    for deck in decks:
        envelope = envelope_for(deck)
        label = (
            envelope["path"]
            if envelope.get("scope") == "repo"
            else env_provenance.render_path_field(envelope)
        )
        for variant in (deck, os.path.realpath(deck)):
            labels.setdefault(variant, label)
    replacements = sorted(labels.items(), key=lambda item: len(item[0]), reverse=True)

    def scrub_line(line: str) -> str:
        for engine_path, label in replacements:
            line = line.replace(engine_path, label)
        return report_paths.scrub(line) if report_paths is not None else line

    block = parse_liberty_model_warnings(stdout or "", stderr or "")
    block["sample"] = [scrub_line(line) for line in block["sample"]]
    return block


def merge_liberty_model_warning_lines(
    accumulated: list[str] | None, *streams: str
) -> list[str]:
    """``accumulated`` (a previous return value, or ``None`` to start)
    extended by this call's streams' own deduplicated lines.

    The ``klt place-and-route`` stage-loop accumulator: each stage passes
    that stage's ``(stdout, stderr)`` and carries the return value
    forward, so a warning repeated across stages (every stage script
    re-reads the same deck) is counted once while a stage-unique warning
    is kept.
    """
    return liberty_model_warning_lines(*(accumulated or []), *streams)


def format_liberty_model_warning(block: Mapping[str, Any]) -> str | None:
    """One human string for ``klt place-and-route``'s top-level
    ``warnings`` array, or ``None`` when there is nothing to say (a clean
    deck adds no entry).

    Names the per-code counts and states the consequence conditionally --
    an undefined template *can* leave a timing group without a delay
    model, so reported slacks may omit the paths launched through it; the
    warning counts alone cannot establish how many register-launched
    paths were affected.
    """
    if not block.get("count"):
        return None
    counts = block.get("by_code") or {}
    reported = ", ".join(
        f"{code} x {counts.get(code, 0)}"
        for code in _LIBERTY_MODEL_WARNING_CODES
        if counts.get(code, 0)
    )
    return (
        f"liberty model warnings ({reported}): OpenSTA discarded timing "
        "groups whose tables name an undefined lu_table_template or carry "
        "no table model, so this run's timing numbers may omit the paths "
        "launched through them (for a sequential cell's CLK-to-Q group, "
        "typically every register-launched path); the counts alone cannot "
        "say how many paths were affected -- see the retained openroad logs"
    )


def parse(
    completed: Any,
    report_paths: _ReportPaths | None,
    decks: Sequence[str],
) -> dict[str, Any]:
    """One corner session's ``liberty_warnings`` block from its completed
    OpenROAD result (``completed`` is duck-typed: ``stdout``/``stderr``
    strings, i.e. ``_openroad_engine.OpenRoadResult``)."""
    return scrubbed_liberty_warnings(
        completed.stdout, completed.stderr, decks, report_paths
    )

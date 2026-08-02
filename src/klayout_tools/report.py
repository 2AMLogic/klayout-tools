"""Render one or more ``klt`` JSON envelopes into a combined text/markdown
report.

Pure library: :func:`build_report` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``drc.py`` /
``lvs.py``. Serialisation and console printing live in the CLI command
module (``cli/report_cmd.py``).

This module is a **consumer** of the shared JSON envelope
(``docs/json-contract.md``), not a producer of a new one for any other
verb -- it is purely additive: it never changes any verb's own JSON output,
it only reads it. Every ``klt`` command's success payload already carries a
``schema_version`` (see ``docs/json-contract.md``); an envelope's *kind* is
detected from the structural shape of its own fields instead of a hardcoded
per-verb name, since the envelope itself never states which command
produced it:

- an object with a top-level ``error`` object (``command``/``message``) is
  an *error* envelope -- typically a verb's own ``--format json`` failure
  output, captured to a file and handed to this command for rendering
  alongside the rest of a CI run's evidence.
- an object with a top-level ``violations`` list is a *violations-style*
  report (``klt drc``'s shape today; any future verb reusing the same
  ``violations[]``/``status: "clean"|"violations"`` convention is picked up
  automatically, with no code change here).
- an object with a top-level ``mismatches`` list is a *mismatches-style*
  report (``klt lvs``'s shape today), rendered the same way as
  violations-style reports (a table of structured findings) under a
  different table schema (category/severity/side/description instead of
  rule/cell/layer/bbox/description).
- an object carrying ``slug``/``name``/``status`` together (the fields
  ``klt layout-metrics``'s ``layout.json`` documents as *always* present,
  see ``layout_metrics.py``) is a *metrics-style* report -- key/value
  metrics, not a list of findings.

An envelope matching none of the above (missing ``schema_version``
entirely, or none of the shape markers above) raises :class:`ReportError`
loudly rather than silently producing an empty section -- see this
module's docstring section "Malformed input" below and
``docs/cli/report.md``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

__all__ = ["ReportError", "build_report"]

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md. This is *this* command's own
#: envelope version, independent of the schema_version of any envelope it
#: renders.
SCHEMA_VERSION = 1

_STATUS_ICONS = {
    "clean": "✅",
    "match": "✅",
    "ok": "✅",
    "pass": "✅",
    "violations": "❌",
    "mismatch": "❌",
    "fail": "❌",
    "error": "❌",
    "partial": "⚠️",
    "no_artifacts": "⚠️",
    "skipped": "⚠️",
}


class ReportError(Exception):
    """Raised when an envelope cannot even be read/parsed, or does not match
    any recognized envelope shape.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other ``klt`` verb's error contract.
    """


def build_report(sources: list[str]) -> dict[str, Any]:
    """Read every entry in ``sources`` (a file path, or ``"-"`` for stdin)
    as a ``klt`` JSON envelope and render a combined report.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/report.md``)::

        {
            "schema_version": 1,
            "envelope_count": <int>,
            "sections": [
                {
                    "kind": "drc" | "lvs" | "layout-metrics" | "error",
                    "source": <str, the entry from `sources`>,
                    "title": <str>,
                    "status": <str> | None,
                    "item_count": <int> | None,
                },
                ...
            ],
            "markdown": <str, GitHub Flavored Markdown -- the `--format
                github-summary` rendering>,
            "text": <str, a plain-text rendering -- the `--format text`
                rendering>,
        }

    Raises :class:`ReportError` if ``sources`` is empty, any entry cannot be
    read/parsed as JSON, is not a JSON object, or does not match a
    recognized envelope shape (see this module's docstring).
    """
    if not sources:
        raise ReportError("at least one envelope file (or '-' for stdin) is required")

    sections: list[dict[str, Any]] = []
    for source in sources:
        envelope = _read_envelope(source)
        if not isinstance(envelope, dict):
            raise ReportError(
                f"envelope '{source}' must be a JSON object, got "
                f"{type(envelope).__name__}"
            )
        kind = _classify(envelope, source)
        sections.append(_build_section(kind, envelope, source))

    return {
        "schema_version": SCHEMA_VERSION,
        "envelope_count": len(sources),
        "sections": [_section_summary(section) for section in sections],
        "markdown": _render(sections, markup=True),
        "text": _render(sections, markup=False),
    }


# --------------------------------------------------------------------------- #
# Reading + classifying input envelopes
# --------------------------------------------------------------------------- #


def _read_envelope(source: str) -> Any:
    """Read and JSON-decode one envelope: ``source == "-"`` reads stdin,
    otherwise ``source`` is a file path. Raises :class:`ReportError` on any
    read/parse failure -- never lets a malformed input silently become an
    empty report (see docs/cli/report.md's edge-case contract)."""
    if source == "-":
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise ReportError(f"stdin envelope is not valid JSON: {exc}") from exc

    if not os.path.exists(source):
        raise ReportError(f"file not found: {source}")
    if os.path.isdir(source):
        raise ReportError(f"not a file: {source}")

    try:
        with open(source, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise ReportError(f"could not read envelope file '{source}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReportError(f"envelope file '{source}' is not valid JSON: {exc}") from exc


def _classify(envelope: dict[str, Any], source: str) -> str:
    """Detect an envelope's kind from its own structural shape -- see this
    module's docstring for the detection rules. Raises :class:`ReportError`
    for an envelope that matches none of them."""
    if "schema_version" not in envelope:
        raise ReportError(
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

    if "slug" in envelope and "name" in envelope and "status" in envelope:
        return "layout-metrics"

    raise ReportError(
        f"envelope '{source}' has an unrecognized shape (schema_version="
        f"{envelope.get('schema_version')!r}): no violations[], mismatches[], "
        "or layout-metrics-style (slug/name/status) fields found"
    )


# --------------------------------------------------------------------------- #
# Envelope -> normalized section
# --------------------------------------------------------------------------- #


def _build_section(kind: str, envelope: dict[str, Any], source: str) -> dict[str, Any]:
    builder = {
        "drc": _section_drc,
        "lvs": _section_lvs,
        "layout-metrics": _section_layout_metrics,
        "error": _section_error,
    }[kind]
    return builder(envelope, source)


def _section_drc(envelope: dict[str, Any], source: str) -> dict[str, Any]:
    violations = envelope.get("violations") or []
    title = "DRC Report"
    file_ = envelope.get("file")
    if file_:
        title += f": {file_}"

    info_lines = []
    if envelope.get("deck"):
        info_lines.append(f"Deck: {envelope['deck']}")
    if file_:
        info_lines.append(f"File: {file_}")

    rows = []
    for violation in violations:
        bbox = violation.get("bbox") or {}
        bbox_str = (
            f"({bbox.get('left')},{bbox.get('bottom')})-"
            f"({bbox.get('right')},{bbox.get('top')})"
            if bbox
            else ""
        )
        rows.append(
            (
                str(violation.get("rule", "")),
                str(violation.get("cell", "")),
                str(violation.get("layer", "")),
                bbox_str,
                str(violation.get("description", "")),
            )
        )

    return {
        "kind": "drc",
        "source": source,
        "title": title,
        "status": envelope.get("status"),
        "item_count": len(violations),
        "info_lines": info_lines,
        "table_headers": ("Rule", "Cell", "Layer", "BBox", "Description"),
        "table_rows": rows,
        "empty_message": "No violations found.",
    }


def _section_lvs(envelope: dict[str, Any], source: str) -> dict[str, Any]:
    mismatches = envelope.get("mismatches") or []
    title = "LVS Report"
    layout = envelope.get("layout")
    reference = envelope.get("reference")
    if layout or reference:
        title += f": {layout} vs {reference}"

    info_lines = []
    if envelope.get("top"):
        info_lines.append(f"Top: {envelope['top']}")
    if envelope.get("engine"):
        info_lines.append(f"Engine: {envelope['engine']}")

    rows = []
    for mismatch in mismatches:
        rows.append(
            (
                str(mismatch.get("category", "")),
                str(mismatch.get("severity", "")),
                str(mismatch.get("side", "")),
                str(mismatch.get("description", "")),
            )
        )

    return {
        "kind": "lvs",
        "source": source,
        "title": title,
        "status": envelope.get("status"),
        "item_count": len(mismatches),
        "info_lines": info_lines,
        "table_headers": ("Category", "Severity", "Side", "Description"),
        "table_rows": rows,
        "empty_message": "No mismatches found.",
    }


def _section_layout_metrics(envelope: dict[str, Any], source: str) -> dict[str, Any]:
    title = "Layout Metrics"
    slug = envelope.get("slug")
    if slug:
        title += f": {slug}"

    rows = []
    for label, key in (
        ("Name", "name"),
        ("Status", "status"),
        ("Layout file", "layout_file"),
        ("Layer count", "layer_count"),
        ("Cell count", "cell_count"),
        ("Instance count", "instance_count"),
    ):
        # omit-absent (never render null/undefined), matching this schema's
        # own convention (site/src/data/types.ts).
        if envelope.get(key) is not None:
            rows.append((label, str(envelope[key])))

    info_lines = []
    drc = envelope.get("drc")
    if isinstance(drc, dict):
        info_lines.append(
            f"DRC: deck={drc.get('deck')} status={drc.get('status')} "
            f"violations={drc.get('violation_count')}"
        )
    renders = envelope.get("renders")
    if isinstance(renders, dict) and renders:
        info_lines.append(f"Renders: {', '.join(sorted(renders))}")
    signals = envelope.get("signals")
    if isinstance(signals, dict):
        info_lines.append(
            f"Signals: status={signals.get('status')} "
            f"corners={signals.get('corner_count')} "
            f"passed={signals.get('passed')} failed={signals.get('failed')} "
            f"errored={signals.get('errored')}"
        )

    return {
        "kind": "layout-metrics",
        "source": source,
        "title": title,
        "status": envelope.get("status"),
        "item_count": None,
        "info_lines": info_lines,
        "table_headers": ("Metric", "Value"),
        "table_rows": rows,
        "empty_message": "No metrics available.",
    }


def _section_error(envelope: dict[str, Any], source: str) -> dict[str, Any]:
    error = envelope.get("error") or {}
    title = "Error"
    command = error.get("command")
    if command:
        title += f": {command}"

    info_lines = [f"Message: {error.get('message', '(no message)')}"]

    return {
        "kind": "error",
        "source": source,
        "title": title,
        "status": "error",
        "item_count": None,
        "info_lines": info_lines,
        "table_headers": None,
        "table_rows": None,
        "empty_message": None,
    }


def _section_summary(section: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": section["kind"],
        "source": section["source"],
        "title": section["title"],
        "status": section.get("status"),
        "item_count": section.get("item_count"),
    }


# --------------------------------------------------------------------------- #
# Section -> rendered text
# --------------------------------------------------------------------------- #


def _render(sections: list[dict[str, Any]], markup: bool) -> str:
    blocks = [_render_section(section, markup) for section in sections]
    return "\n\n".join(blocks) + "\n"


def _render_section(section: dict[str, Any], markup: bool) -> str:
    lines: list[str] = []
    title = section["title"]
    if markup:
        lines.append(f"## {title}")
    else:
        lines.append(title)
        lines.append("-" * len(title))

    status = section.get("status")
    if status is not None:
        lines.append(
            f"**Status:** {_status_badge(status)}" if markup else f"Status: {status}"
        )

    for info in section.get("info_lines") or []:
        lines.append(f"- {info}" if markup else info)

    headers = section.get("table_headers")
    rows = section.get("table_rows")
    if headers and rows:
        lines.append("")
        lines.append(
            _markdown_table(headers, rows) if markup else _plain_table(headers, rows)
        )
    elif headers and not rows and section.get("empty_message"):
        lines.append("")
        lines.append(section["empty_message"])

    return "\n".join(lines)


def _status_badge(status: str) -> str:
    icon = _STATUS_ICONS.get(status)
    return f"{icon} {status}" if icon else str(status)


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    def escape(cell: str) -> str:
        return cell.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(escape(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _plain_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [
        max(len(headers[col]), max((len(row[col]) for row in rows), default=0))
        for col in range(len(headers))
    ]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(row[col].ljust(widths[col]) for col in range(len(headers)))

    lines = [fmt(headers), "  ".join("-" * width for width in widths)]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)

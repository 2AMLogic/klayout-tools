"""Mechanically parse the T1-T4 evidence-tier item list out of
``docs/design-evidence-tiers.md`` -- so ``klt signoff``'s tier-verdict mode
(:mod:`.signoff`) and the doc can never drift apart (issue #722, Phase 0 of
epic #706).

This module owns exactly one job: turn the doc's Markdown structure into
plain Python data. It never grades anything -- see
:func:`klayout_tools.signoff.build_tier_report` for the verdict model built
on top of this parse.

## What gets parsed

- **The ladder** (the doc's "## The ladder" table): one row per tier
  ``T1``-``T4``, each a ``{tier, name, claim, demonstrated_by}`` dict. T2-T4
  are single gated *conditions* layered on T1 ("T1, plus DRC/LVS signoff...
  on commercial tools", "T2, plus a tapeout...") -- not itemized lists of
  their own (a Curator correction on this issue: only T1 has an itemized
  checklist).
- **The T1 checklist** (the doc's "## T1 checklist" numbered list, items
  1-10): each item's title, and either one kind-independent body of text
  (items 3, 4, 6, 8, 9, 10 -- these apply as written to every block kind)
  or a per-kind ``{"analog": ..., "digital": ...}`` pair of bodies (items 1,
  2, 5, 7 -- the doc's "Block kind" subsection explains which items split
  this way). Item 5 additionally carries a kind-independent note (spec
  ratification) alongside its two per-kind bodies -- captured in
  ``notes[]``, not folded into either column.

## Why regex over a Markdown library

The doc's structure (a numbered list with nested ``- *Analog* --``/``-
*Digital* --`` bullets, and small GFM tables) is simple and stable enough
that a small, purpose-built line parser is more legible and has zero new
dependencies -- and, unlike a general Markdown AST, it fails loudly
(:class:`DesignEvidenceTiersError`) the moment the doc's shape changes in a
way this parser doesn't understand, rather than silently mis-parsing.
That's a deliberate trade: this parser is tightly coupled to the doc's
current prose structure and *must* be revisited if that structure changes
non-trivially -- exactly the drift this issue exists to prevent noticing
late.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

__all__ = [
    "CANONICAL_DOC_LABEL",
    "DEFAULT_DOC_PATH",
    "DOC_PATH_ENV_VAR",
    "DesignEvidenceTiersError",
    "default_doc_path",
    "doc_source_label",
    "parse_tier_doc",
]

#: Repo-relative identity of the canonical doc, as reported in ``klt
#: signoff``'s ``source_doc`` field. Stays stable across install layouts on
#: purpose: a wheel install parses the same doc's bundled copy, and reporting
#: its ``site-packages`` path instead would make otherwise-identical reports
#: differ by install location.
CANONICAL_DOC_LABEL = "docs/design-evidence-tiers.md"

#: Environment variable naming an alternate ``design-evidence-tiers.md`` to
#: parse. Lets a consumer point a packaged install at a vendored copy of the
#: doc without a source checkout and without passing ``--tiers-doc`` on every
#: invocation (issue #1050). ``klt signoff --tiers-doc`` still wins over it.
DOC_PATH_ENV_VAR = "KLT_TIERS_DOC"

#: The doc as it ships *inside* an installed package: ``pyproject.toml``'s
#: ``[tool.hatch.build.targets.wheel.force-include]`` copies the canonical
#: ``docs/design-evidence-tiers.md`` to ``klayout_tools/data/`` in the built
#: wheel, so this resolves next to the installed module rather than a fixed
#: number of ``.parent`` hops above it. This is the only location that
#: survives ``pip install``/``uv tool install`` (issue #1050) -- a wheel
#: unpacks into ``site-packages`` with no sibling ``docs/`` directory.
_PACKAGED_DOC_PATH: Path = (
    Path(__file__).resolve().parent / "data" / "design-evidence-tiers.md"
)

#: The canonical doc in a source checkout (or an editable install, whose
#: ``klayout_tools`` package still lives at ``src/klayout_tools`` in the
#: repo): ``src/klayout_tools/design_evidence_tiers.py`` -> parent
#: (``src/klayout_tools``) -> parent (``src``) -> parent (repo root). Used
#: only when the bundled copy above is absent, i.e. exactly when the doc has
#: not been packaged alongside the module.
_SOURCE_DOC_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "design-evidence-tiers.md"
)


def default_doc_path() -> Path:
    """Resolve which ``design-evidence-tiers.md`` :func:`parse_tier_doc`
    reads when given no explicit path, in precedence order:

    1. ``$KLT_TIERS_DOC`` (:data:`DOC_PATH_ENV_VAR`), if set and non-empty.
    2. The copy bundled inside the installed package
       (``klayout_tools/data/design-evidence-tiers.md``), if present -- this
       is what a wheel install has.
    3. The canonical ``docs/design-evidence-tiers.md`` in the source
       checkout -- what a repo checkout / editable install has.

    Resolved per call (not frozen at import) so the environment override
    applies to a process that sets it after import, and so a missing bundled
    copy in one layout never poisons another.
    """
    override = os.environ.get(DOC_PATH_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if _PACKAGED_DOC_PATH.is_file():
        return _PACKAGED_DOC_PATH
    return _SOURCE_DOC_PATH


def doc_source_label(path: str | Path | None = None) -> str:
    """Which doc a report should say it was built from: the explicit
    ``path`` (or ``$KLT_TIERS_DOC``) when one overrides the default,
    otherwise :data:`CANONICAL_DOC_LABEL`.

    An overridden doc is a *different* document, so naming it is part of the
    evidence; the default is the same canonical document in every install
    layout, so it is named canonically rather than by wherever this install
    happens to keep its copy.
    """
    if path is not None:
        return str(path)
    override = os.environ.get(DOC_PATH_ENV_VAR)
    if override:
        return str(Path(override).expanduser())
    return CANONICAL_DOC_LABEL


#: The default doc location *ignoring* the environment override -- the
#: bundled copy when this package was installed as a wheel, otherwise the
#: source checkout's canonical doc. Kept as a module constant for
#: introspection/back-compat; :func:`default_doc_path` is what
#: :func:`parse_tier_doc` actually consults.
DEFAULT_DOC_PATH: Path = (
    _PACKAGED_DOC_PATH if _PACKAGED_DOC_PATH.is_file() else _SOURCE_DOC_PATH
)

_LADDER_HEADING = re.compile(r"^##\s+The ladder\s*$")
_T1_HEADING = re.compile(r"^##\s+T1 checklist\b")
_H2_HEADING = re.compile(r"^##\s+")

_ITEM_START = re.compile(r"^(\d+)\.\s+\*\*(.+?)\*\*\s*(.*)$")
_COLUMN_BULLET = re.compile(r"^-\s+\*(Analog|Digital)\*\s*[—–-]\s*(.*)$")
_GENERIC_BULLET = re.compile(r"^-\s+(.*)$")
_LEADING_DASH = re.compile(r"^[—–-]\s*")


class DesignEvidenceTiersError(Exception):
    """Raised when ``docs/design-evidence-tiers.md`` cannot be read, or its
    structure no longer matches what this parser understands (see this
    module's docstring, "Why regex over a Markdown library"). Never raised
    for a doc that merely reworded prose within the shape this parser
    expects.
    """


def parse_tier_doc(path: str | Path | None = None) -> dict[str, Any]:
    """Parse ``design-evidence-tiers.md`` -- ``path`` if given, otherwise
    whatever :func:`default_doc_path` resolves (bundled package copy, source
    checkout, or ``$KLT_TIERS_DOC``) -- into::

        {
            "ladder": [
                {
                    "tier": "T1",
                    "name": "sim-validated",
                    "claim": "Designed and simulation-validated with open tools",
                    "demonstrated_by": "Open-source DRC/LVS/simulation evidence, ...",
                },
                ...  # T2, T3, T4
            ],
            "t1_items": [
                {
                    "id": 1,
                    "title": "Design sources",
                    "text": None,
                    "columns": {
                        "analog": "committed schematic sources (or generator) ...",
                        "digital": "committed RTL sources ...",
                    },
                    "notes": [],
                },
                {
                    "id": 3,
                    "title": "DRC clean",
                    "text": "latest `klt drc` JSON report: ...",
                    "columns": {},
                    "notes": [],
                },
                ...  # items 1-10, in doc order
            ],
        }

    Exactly one of ``text`` / ``columns`` is populated per item: ``columns``
    (with ``"analog"``/``"digital"`` keys) for the four items the doc's
    "Block kind" subsection calls out as kind-specific (1, 2, 5, 7); ``text``
    for the six kind-independent items (3, 4, 6, 8, 9, 10). ``notes`` holds
    any additional bullet under an item that names neither kind (only item 5
    has one today: the spec-ratification caveat).

    Raises :class:`DesignEvidenceTiersError` if the file cannot be read, or
    if either the ladder table or the T1 item list is empty/malformed --
    a doc restructuring this parser doesn't understand is refused rather
    than silently producing a wrong (or empty) tier skeleton.
    """
    explicit = path is not None
    doc_path = Path(path) if path is not None else default_doc_path()
    try:
        text = doc_path.read_text(encoding="utf-8")
    except OSError as exc:
        hint = (
            ""
            if explicit
            else (
                f" -- pass an explicit path (`klt signoff --tiers-doc PATH`) "
                f"or set ${DOC_PATH_ENV_VAR} to point at a copy of the doc"
            )
        )
        raise DesignEvidenceTiersError(
            f"could not read design-evidence-tiers doc at '{doc_path}': {exc}{hint}"
        ) from exc

    lines = text.splitlines()

    ladder = _parse_ladder(lines, doc_path)
    t1_items = _parse_t1_items(lines, doc_path)

    return {"ladder": ladder, "t1_items": t1_items}


# --------------------------------------------------------------------------- #
# "## The ladder" table
# --------------------------------------------------------------------------- #


def _parse_ladder(lines: list[str], doc_path: Path) -> list[dict[str, Any]]:
    section = _section_lines(lines, _LADDER_HEADING, doc_path, "## The ladder")

    tier_row = re.compile(r"^\*\*(T\d)\s*[—–-]\s*(.+?)\*\*$")
    rows: list[dict[str, Any]] = []
    for line in section:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 3:
            continue
        m = tier_row.match(cells[0])
        if not m:
            continue  # header row ("Tier | Claim | ...") or separator row
        rows.append(
            {
                "tier": m.group(1),
                "name": m.group(2).strip(),
                "claim": cells[1],
                "demonstrated_by": cells[2],
            }
        )

    if not rows:
        raise DesignEvidenceTiersError(
            f"found no tier rows in the '## The ladder' table of '{doc_path}' "
            "-- doc structure has changed in a way this parser doesn't "
            "understand (see design_evidence_tiers.py's docstring)"
        )
    return rows


# --------------------------------------------------------------------------- #
# "## T1 checklist" numbered list
# --------------------------------------------------------------------------- #


def _parse_t1_items(lines: list[str], doc_path: Path) -> list[dict[str, Any]]:
    section = _section_lines(lines, _T1_HEADING, doc_path, "## T1 checklist")

    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    # target: ("text",) | ("column", "analog"|"digital") | ("note", index)
    target: tuple[Any, ...] | None = None

    def finalize(raw: dict[str, Any]) -> dict[str, Any]:
        text_parts: list[str] = raw["text"]
        columns_parts: dict[str, list[str]] = raw["columns"]
        notes_parts: list[list[str]] = raw["notes"]
        return {
            "id": raw["id"],
            "title": raw["title"],
            "text": " ".join(text_parts).strip() or None,
            "columns": {
                kind: " ".join(parts).strip()
                for kind, parts in columns_parts.items()
                if parts
            },
            "notes": [" ".join(parts).strip() for parts in notes_parts if parts],
        }

    for line in section:
        stripped = line.strip()
        if not stripped:
            continue

        m = _ITEM_START.match(stripped)
        if m:
            if current is not None:
                items.append(finalize(current))
            current = {
                "id": int(m.group(1)),
                "title": m.group(2).strip(),
                "text": [],
                "columns": {},
                "notes": [],
            }
            target = ("text",)
            remainder = _LEADING_DASH.sub("", m.group(3).strip()).strip()
            if remainder:
                current["text"].append(remainder)
            continue

        if current is None:
            # Prose ahead of item 1 (the "### Block kind" subsection) --
            # not part of the itemized checklist.
            continue

        m = _COLUMN_BULLET.match(stripped)
        if m:
            kind = m.group(1).lower()
            current["columns"].setdefault(kind, [])
            current["columns"][kind].append(m.group(2).strip())
            target = ("column", kind)
            continue

        m = _GENERIC_BULLET.match(stripped)
        if m:
            current["notes"].append([m.group(1).strip()])
            target = ("note", len(current["notes"]) - 1)
            continue

        # Continuation line: append to whichever target the last bullet (or
        # the item's own lead-in) opened.
        if target is None:
            continue
        kind_tag = target[0]
        if kind_tag == "text":
            current["text"].append(stripped)
        elif kind_tag == "column":
            current["columns"][target[1]].append(stripped)
        elif kind_tag == "note":
            current["notes"][target[1]].append(stripped)

    if current is not None:
        items.append(finalize(current))

    if not items:
        raise DesignEvidenceTiersError(
            f"found no numbered items in the '## T1 checklist' section of "
            f"'{doc_path}' -- doc structure has changed in a way this "
            "parser doesn't understand (see design_evidence_tiers.py's "
            "docstring)"
        )
    return items


# --------------------------------------------------------------------------- #
# Shared section-slicing helper
# --------------------------------------------------------------------------- #


def _section_lines(
    lines: list[str],
    heading: re.Pattern[str],
    doc_path: Path,
    heading_label: str,
) -> list[str]:
    """Return the lines strictly between a ``## <heading>`` line and the next
    ``## `` (level-2) heading, or raise if ``heading`` is never found."""
    start = None
    for i, line in enumerate(lines):
        if heading.match(line):
            start = i + 1
            break
    if start is None:
        raise DesignEvidenceTiersError(
            f"could not find a '{heading_label}' heading in '{doc_path}' -- "
            "doc structure has changed in a way this parser doesn't "
            "understand (see design_evidence_tiers.py's docstring)"
        )

    end = len(lines)
    for i in range(start, len(lines)):
        if _H2_HEADING.match(lines[i]):
            end = i
            break

    return lines[start:end]

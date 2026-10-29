#!/usr/bin/env python3
"""Generate the klayout-tools.org capability data from the CLI registry.

The landing page (``site/src/pages/IndexPage.tsx``) shows the latest release,
the ``klt`` verb list, the MCP tool count and the built-in rule decks. Those
used to be hand-written, so they went stale: the page still showed four verbs
under "the interface contract we are building toward" months after 60+ had
shipped (issue #4597). This script derives every one of them from the code:

- verbs and their one-line summaries: ``klt``'s argparse registry
  (``klayout_tools.cli.parser.create_parser``), the same source ``klt --help``
  and the MCP bridge read;
- MCP tool count: ``klayout_tools.mcp_tools.derive_tools`` (one tool per leaf
  verb);
- deck names: ``klayout_tools.decks.deck_names``;
- latest release and date: the newest ``## X.Y.Z (YYYY-MM-DD)`` heading in
  ``CHANGELOG.md``.

The output, ``site/src/data/cli.generated.json``, is committed so the site
build stays Node-only. ``tests/test_site_cli_data.py`` regenerates it and
fails on any difference, so a new verb, a renamed help string or a release
heading cannot land without the site picking it up. Because the file lives
under ``site/``, refreshing it also triggers ``deploy-site.yml``.

Usage::

    uv run python scripts/gen_site_cli_data.py          # rewrite the file
    uv run python scripts/gen_site_cli_data.py --check  # exit 1 if stale
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "site" / "src" / "data" / "cli.generated.json"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
DOCS_CLI = REPO_ROOT / "docs" / "cli"
REPO_URL = "https://github.com/2AMLogic/klayout-tools"

# Presentation grouping for the site only. A verb missing from this table is
# still listed, under "More" -- adding a verb never fails because of it.
GROUPS: list[tuple[str, str, list[str]]] = [
    (
        "inspect",
        "Read and inspect layout",
        [
            "layers",
            "cells",
            "stats",
            "diff",
            "clip",
            "components",
            "render",
            "economy",
            "layout-metrics",
        ],
    ),
    (
        "check",
        "Physical checks",
        [
            "drc",
            "precheck",
            "erc",
            "socket-check",
            "interface-check",
            "ring-check",
            "seam-check",
            "keepout-check",
            "array-check",
            "tap-distance",
        ],
    ),
    (
        "generate",
        "Generate and compose layout",
        ["gen", "gen-compose", "layout-plan", "draw", "lef-abstract"],
    ),
    (
        "extract",
        "Netlist, extraction and LVS",
        ["netlist", "extract", "lvs", "pex", "mom", "power"],
    ),
    (
        "simulate",
        "Simulation and sizing",
        ["sim", "sim-plan", "sim-merge", "corner-matrix", "size", "characterize"],
    ),
    (
        "yield",
        "Monte Carlo yield",
        [
            "yield",
            "yield-samples",
            "yield-csv",
            "yield-campaign",
            "yield-sensitivity",
            "yield-threshold",
            "design-centering",
        ],
    ),
    (
        "digital",
        "Digital flow",
        [
            "synthesize",
            "arith-gen",
            "techmap",
            "equiv",
            "formal",
            "functional-verification",
            "wave",
            "place-and-route",
            "sta",
            "reset-coverage",
            "power-intent",
        ],
    ),
    (
        "signoff",
        "Signoff, reports and provenance",
        [
            "signoff",
            "report",
            "eval",
            "trajectory",
            "evidence",
            "artifact",
            "env-provenance",
            "version",
        ],
    ),
    (
        "pdk",
        "PDKs, decks and knowledge base",
        ["pdk", "deck", "kb"],
    ),
    ("agents", "Agent integration", ["mcp"]),
]

# Verbs documented on another verb's page.
DOC_ALIASES: dict[str, str] = {"sim-plan": "sim", "sim-merge": "sim"}

# Site-only summary overrides, used where the argparse help is written for
# someone already inside the repo (internal infrastructure names, process
# vocabulary) rather than for a first-time visitor. Every key must be a
# registered verb; tests/test_site_cli_data.py enforces that.
SUMMARY_OVERRIDES: dict[str, str] = {
    "signoff": (
        "aggregate klt drc/lvs/extract/sim JSON envelopes into one pass/fail verdict"
    ),
    "artifact": (
        "put/get/verify/delete evidence bytes in an artifact store by sha256, "
        "with a committed manifest"
    ),
}

# The yield verbs' help still says the Rust core must be built by hand; it
# ships as the `yield` extra on PyPI now (README "Install").
_RUST_EXT = re.compile(r"\(needs a separately built Rust extension\)")

_ISSUE_REF = re.compile(r"\s*\((?:issue|epic) #\d+\)", re.IGNORECASE)
_RELEASE_HEADING = re.compile(r"^## (\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)\s*$", re.M)


def _summary(help_text: str) -> str:
    """One-line verb summary: argparse help minus internal issue references."""
    text = _ISSUE_REF.sub("", " ".join(help_text.split()))
    text = _RUST_EXT.sub("(yield extra)", text)
    # The site renders summaries as plain text; drop markdown code ticks.
    return text.replace("`", "").strip()


def latest_release(changelog: str) -> dict[str, str]:
    match = _RELEASE_HEADING.search(changelog)
    if match is None:
        raise SystemExit("CHANGELOG.md has no '## X.Y.Z (YYYY-MM-DD)' heading")
    return {"version": match.group(1), "date": match.group(2)}


def build_data() -> dict:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from klayout_tools.cli.parser import create_parser
    from klayout_tools.decks import deck_names
    from klayout_tools.mcp_tools import derive_tools

    root = create_parser()
    sub = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    helps = {a.dest: (a.help or "") for a in sub._choices_actions}
    verbs = list(sub.choices)

    def entry(name: str) -> dict[str, str | None]:
        page = DOC_ALIASES.get(name, name)
        doc = DOCS_CLI / f"{page}.md"
        summary = SUMMARY_OVERRIDES.get(name) or _summary(helps.get(name, ""))
        return {
            "name": name,
            "summary": summary,
            "doc": f"{REPO_URL}/blob/main/docs/cli/{page}.md" if doc.exists() else None,
        }

    grouped: set[str] = set()
    groups = []
    for key, title, members in GROUPS:
        present = [v for v in members if v in sub.choices]
        grouped.update(present)
        if present:
            groups.append(
                {"id": key, "title": title, "verbs": [entry(v) for v in present]}
            )
    rest = [v for v in verbs if v not in grouped]
    if rest:
        groups.append(
            {"id": "more", "title": "More", "verbs": [entry(v) for v in rest]}
        )

    return {
        "_generated": (
            "Generated by scripts/gen_site_cli_data.py from the klt CLI registry "
            "-- do not edit by hand."
        ),
        "release": latest_release(CHANGELOG.read_text(encoding="utf-8")),
        "verb_count": len(verbs),
        "mcp_tool_count": len(derive_tools()),
        "decks": sorted(deck_names()),
        "groups": groups,
    }


def render(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the committed file is stale"
    )
    args = parser.parse_args(argv)

    text = render(build_data())
    rel = OUTPUT.relative_to(REPO_ROOT)
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print(
                f"{rel} is stale; run: uv run python scripts/gen_site_cli_data.py",
                file=sys.stderr,
            )
            return 1
        print(f"{rel} is up to date")
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

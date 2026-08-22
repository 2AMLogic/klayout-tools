#!/usr/bin/env python3
"""Ingest a public canary block repo into the gallery's `blocks/` tree
(issue #62, epic #13 -- "block content pipeline").

Per operator decision 2026-07-31, the canary block repos (real analog/mixed-
signal blocks designed end-to-end by AI agents, e.g. `2AMLogic/gf180-bandgap`,
`2AMLogic/sky130-bandgap`) are first-class gallery content, not just the #4
demo corpus. This script clones/pulls one such repo at a pinned ref and
emits `blocks/<slug>/output/layout.json` in the same schema `klt
layout-metrics` (#61) emits -- see `src/klayout_tools/layout_metrics.py` and
`docs/cli/layout-metrics.md` -- extended with three pipeline-specific fields
documented in `blocks/README.md`:

* `source`     -- `{repo, ref, path}` provenance (always present).
* `spec_summary` -- the ratified/draft target-spec table parsed from the
  source repo's `README.md` "Target specification" section (present when
  found).
* `signals`    -- reuses `layout.json`'s `signals` object (#99), populated
  here from the source repo's own `sim/` evidence trail (append-only
  `records/*.md`, `suite/summaries/*.md`) rather than from a fresh `klt sim`
  run -- these are the source repo's *own* simulation results, not a
  standard-cell sweep this repo runs itself.

When a canary's GDS lands (see "GDS detection" below), this script also
attaches `renders` -- the gallery-thumbnail composite (`"overview"`), one
entry per non-empty layer, and a zoomed "center crop" composite (issue
#942; previously just `{"overview": "renders/overview.png"}`, "Option A"
from #651) -- via `klt render`'s library function.

Public-repo gate (fail-closed)
-------------------------------
Before touching the filesystem, `assert_public_repo()` calls `gh api
repos/<repo> --jq .visibility` and refuses to proceed unless the answer is
exactly `"public"`. Any other outcome -- `"private"`, an API error, `gh` not
installed/authenticated, no network -- raises `IngestError` and nothing is
written. There is no allowlist to keep in sync: the API call is the source
of truth at ingest time, every run.

Pre-layout blocks ship "sim-evidence cards"; refreshing a canary's render
--------------------------------------------------------------------------
A block with no layout file findable under `layout/` (or `gds/`, or the
repo root -- see "GDS detection" below) gets `status: "in design —
simulation evidence"` (the honest status chip, mirroring issue #140's
"never oversell an incomplete block" rule) instead of `"ok"`/`"partial"`/
`"no_artifacts"`. Both wave-1 canaries started here.

**Refreshing a canary's render as its layout advances is just re-running
this same command** -- no separate refresh mechanism exists or is needed:

    python scripts/ingest-canary.py --repo 2AMLogic/sky130-bandgap
    python scripts/ingest-canary.py --repo 2AMLogic/gf180-bandgap

Once a block's layout lands, this upgrades the entry to a normal
`"ok"`/`"partial"` metrics record (layer/cell counts via the same `klt
layers`/`klt cells` library functions `klt layout-metrics` uses) plus a
fresh `renders.overview` render and `downloadable: true` -- same slug, same
card, no hand-editing of `layout.json`. Both `sky130-bandgap` and
`gf180-bandgap` exercise this path against their real, committed routed GDS
as of issue #896 (`bandgap_core_routed.gds` under sky130-bandgap's
`reports/LATEST`-pointed run directory, and `bandgap_top.gds` under
gf180-bandgap's flatter `layout/bandgap_top/`) -- see
`tests/test_ingest_canary.py::test_gds_found_upgrades_to_ok_status` for the
synthetic-fixture unit coverage and
`test_full_ingest_real_public_canary_with_layout` for the real-network
integration coverage of both shapes.

`sim/` evidence is heterogeneous across repos (different harnesses, still
evolving) -- see this module's docstrings on `_extract_bullet_nominal` /
`_extract_table_nominal` / `_parse_suite_summary` for the two markdown
record shapes this script actually understands. An experiment whose latest
record carries no extractable `**Overall: PASS/FAIL/ERROR**` verdict (pure
device-characterization "recorded, not claimed" data, e.g.
`gf180-bandgap/sim/device-pnp-vbe/`) is skipped for `signals` -- there is no
honest way to map "no spec claim" onto the `pass`/`fail`/`error` enum
`LayoutSignals` (`site/src/data/types.ts`) uses.

Usage
-----

    python scripts/ingest-canary.py --repo 2AMLogic/gf180-bandgap
    python scripts/ingest-canary.py --repo 2AMLogic/sky130-bandgap
    python scripts/ingest-canary.py --repo 2AMLogic/<a-still-private-canary>  # refused

Clones are cached (fetched, not re-cloned) under `.cache/canary-repos/`
(gitignored) at the repo root, mirroring `pdks/`'s gitignore pattern.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = REPO_ROOT / ".cache" / "canary-repos"
DEFAULT_BLOCKS_DIR = REPO_ROOT / "blocks"

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from _gallery_common import (  # noqa: E402
    attach_overview_render,
    infer_layer_names,
    infer_pdk,
)

from klayout_tools.cells import CellsError, cells_report  # noqa: E402
from klayout_tools.decks import deck_names  # noqa: E402
from klayout_tools.layers import LayersError, layers_report  # noqa: E402

# RenderError is not used directly in this module anymore (the shared
# _gallery_common.attach_overview_render helper catches it) -- kept as
# `ic.RenderError` for tests/test_ingest_canary.py, which raises it via a
# monkeypatched render_report to exercise the best-effort path.
from klayout_tools.render import RenderError, render_report  # noqa: E402,F401

SCHEMA_VERSION = 1
IN_DESIGN_STATUS = "in design — simulation evidence"

_GDS_GLOBS = ("*.gds", "*.gds.gz", "*.oas")
_LAYOUT_SEARCH_DIRS = ("layout", "gds", ".")

# Directory names never worth descending into while hunting for the block's
# own layout GDS -- issue #896. `reports`/`fixed-offset-variants`/`mim-
# overlay-feasibility` hold historical run archives or feasibility probes
# (many GDS files, none of them the current block), `drc`/`lvs` hold DRC/LVS
# *report* trees (which may themselves contain unrelated fixture GDS, e.g.
# gf180-bandgap's `layout/drc/fixtures/trivial_poly_res.gds`), and
# `renders` holds this same pipeline's own PNG output on a re-run.
_EXCLUDED_SEARCH_DIR_NAMES = frozenset(
    {"reports", "fixtures", "drc", "lvs", "renders", "output"}
)
# How many directory levels to descend below a `_LAYOUT_SEARCH_DIRS` entry
# when no `reports/LATEST` pointer is found (gf180-bandgap's
# `layout/bandgap_top/bandgap_top.gds` is one level deep).
_MAX_SEARCH_DEPTH = 3
_LATEST_POINTER_NAME = "LATEST"
_ROUTED_GDS_RE = re.compile(r"_routed(?!.*_inner)", re.IGNORECASE)


class IngestError(Exception):
    """Raised for any refusal/failure that must abort before writing files."""


# --------------------------------------------------------------------------- #
# Public-repo gate
# --------------------------------------------------------------------------- #


def repo_visibility(repo: str) -> str:
    """Return `repo`'s GitHub visibility via `gh api`, or raise `IngestError`.

    Any failure -- private repo, nonexistent repo, no access, `gh` missing
    or unauthenticated, no network -- is a hard failure here (not a special
    case in the caller): the gate downstream treats "could not confirm
    public" and "confirmed private" identically. Monkeypatched by tests.
    """
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}", "--jq", ".visibility"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "no error output"
        raise IngestError(
            f"could not determine visibility of {repo} via `gh api` "
            f"(exit {result.returncode}): {stderr}"
        )
    visibility = result.stdout.strip()
    if not visibility:
        raise IngestError(f"`gh api repos/{repo} --jq .visibility` returned no output")
    return visibility


def assert_public_repo(repo: str) -> None:
    """Fail-closed public-repo gate. Raises `IngestError` unless public.

    Called before any clone or filesystem write -- see `ingest()`.
    """
    visibility = repo_visibility(repo)
    if visibility != "public":
        raise IngestError(
            f'refusing to ingest {repo}: visibility is {visibility!r}, not "public" '
            "(fail-closed public-repo gate -- see blocks/README.md)"
        )


def repo_description(repo: str) -> str | None:
    """Best-effort `gh api repos/<repo> --jq .description`. Never raises."""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}", "--jq", ".description"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text if text and text != "null" else None


# --------------------------------------------------------------------------- #
# Clone / pull at a pinned ref
# --------------------------------------------------------------------------- #


def clone_repo(repo: str, cache_dir: Path, ref: str | None) -> tuple[Path, str]:
    """Clone (or fetch+checkout, if already cached) `repo` into `cache_dir`.

    Returns `(repo_dir, resolved_sha)` -- `resolved_sha` is `git rev-parse
    HEAD` after checkout, i.e. the exact pinned commit this ingest run used,
    regardless of whether `ref` was a branch/tag or already a sha.
    """
    dest = cache_dir / repo.split("/")[-1]
    url = f"https://github.com/{repo}.git"

    if dest.is_dir():
        fetch = subprocess.run(
            ["git", "-C", str(dest), "fetch", "--quiet", "--tags", "origin"],
            capture_output=True,
            text=True,
        )
        if fetch.returncode != 0:
            raise IngestError(f"git fetch failed for {repo}: {fetch.stderr.strip()}")
    else:
        cache_dir.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(
            ["git", "clone", "--quiet", url, str(dest)], capture_output=True, text=True
        )
        if clone.returncode != 0:
            raise IngestError(f"git clone failed for {repo}: {clone.stderr.strip()}")

    checkout_target = ref if ref else "origin/HEAD"
    checkout = subprocess.run(
        ["git", "-C", str(dest), "checkout", "--quiet", checkout_target],
        capture_output=True,
        text=True,
    )
    if checkout.returncode != 0:
        stderr = checkout.stderr.strip()
        raise IngestError(f"git checkout {checkout_target} failed for {repo}: {stderr}")

    rev = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return dest, rev.stdout.strip()


# --------------------------------------------------------------------------- #
# Markdown table helpers
# --------------------------------------------------------------------------- #


def _split_row(line: str) -> list[str]:
    """Split a `| a | b | c |` markdown table row into `["a", "b", "c"]`."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_separator_row(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", c) for c in cells)


def _slugify_header(header: str) -> str:
    """`"Corner binding"` -> `"corner_binding"`."""
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def _lead_number_unit(text: str) -> tuple[float | None, str | None]:
    """Parse a leading `"<number> <unit>"` prefix, e.g. `"1.2523 V (limit ...)"`."""
    m = re.match(r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(\S*)", text.strip())
    if not m:
        return None, None
    try:
        value = float(m.group(1))
    except ValueError:
        return None, None
    unit = m.group(2).rstrip(",") or None
    return value, unit


# --------------------------------------------------------------------------- #
# `spec_summary` -- README.md's "Target specification" table
# --------------------------------------------------------------------------- #

_SPEC_HEADING_RE = re.compile(
    r"^#{1,6}\s*Target specification\b\s*(?:\((?P<note>[^)]*)\))?", re.IGNORECASE
)


def parse_spec_summary(readme_text: str) -> dict[str, Any] | None:
    """Extract the ratified/draft target-spec table from a canary README.

    Looks for a `## Target specification (...)` heading (any level, `gf180-
    bandgap`/`sky130-bandgap` both use `##`), then the first markdown table
    immediately following it. Returns `{"rows": [...], "status_note": "..."}`
    (`status_note` from the heading's parenthetical, e.g. `"RATIFIED
    2026-07-31, see issue #1 and #35"` or `"DRAFT — engineering to ratify,
    see issue #1"`) or `None` if no such heading/table is found.

    Each row is a dict keyed by the table's own (slugified) column headers
    -- `parameter`/`target`/`stretch`/`corner_binding` for the two current
    canaries, but deliberately not hardcoded to those names, since a future
    canary's table may add/drop columns. Empty/placeholder cells (`""`,
    `"—"`, `"-"`) are omitted from the row dict rather than kept as noise.
    """
    lines = readme_text.splitlines()
    heading_idx = None
    status_note = None
    for i, line in enumerate(lines):
        m = _SPEC_HEADING_RE.match(line.strip())
        if m:
            heading_idx = i
            status_note = m.group("note")
            break
    if heading_idx is None:
        return None

    table_lines: list[str] = []
    for line in lines[heading_idx + 1 :]:
        stripped = line.strip()
        if stripped.startswith("|"):
            table_lines.append(stripped)
        elif table_lines:
            break
        elif stripped.startswith("#"):
            break
    if len(table_lines) < 2:
        return None

    header_cells = _split_row(table_lines[0])
    data_lines = (
        table_lines[2:] if _is_separator_row(table_lines[1]) else table_lines[1:]
    )
    keys = [_slugify_header(h) for h in header_cells]

    rows: list[dict[str, str]] = []
    for line in data_lines:
        cells = _split_row(line)
        if not cells:
            continue
        row: dict[str, str] = {}
        for key, cell in zip(keys, cells, strict=False):
            cell = cell.strip()
            if key and cell and cell not in ("—", "-"):
                row[key] = cell
        if row.get("parameter"):
            rows.append(row)

    if not rows:
        return None

    result: dict[str, Any] = {"rows": rows}
    if status_note:
        result["status_note"] = status_note.strip()
    return result


# --------------------------------------------------------------------------- #
# `signals` -- the source repo's own PVT sim evidence trail
# --------------------------------------------------------------------------- #

_OVERALL_RE = re.compile(r"\*\*Overall:\s*(PASS|FAIL|ERROR)\*\*", re.IGNORECASE)
_ENGINE_VERSION_RE = re.compile(r"ngspice[- ]([\w.]+)", re.IGNORECASE)
_NOMINAL_CORNER_RE = re.compile(r"^(tt|typical)_27c_", re.IGNORECASE)
_BULLET_CORNER_RE = re.compile(
    r"^\s*-\s*`?(?P<corner_id>[\w.-]+)`?\s*:\s*(?P<verdict>PASS|FAIL|ERROR)\s*\((?P<body>.*)\)\s*$"
)
_MEASUREMENT_VALUE_RE = re.compile(
    r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-z%/°Ω]*)$"
)


def _split_measurements(body: str) -> list[dict[str, Any]]:
    """Parse a `"name1=1.2V; name2=3.4mA"` bullet body into measurement dicts."""
    out: list[dict[str, Any]] = []
    for part in body.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, raw = part.partition("=")
        name = name.strip()
        m = _MEASUREMENT_VALUE_RE.match(raw.strip())
        if not name or not m:
            continue
        out.append(
            {"name": name, "value": float(m.group(1)), "unit": m.group(2) or None}
        )
    return out


def _extract_bullet_nominal(text: str) -> dict[str, Any] | None:
    """Nominal-corner extraction for the `"  - <corner_id>: PASS (k=v; ...)"`
    per-corner bullet-list record format (e.g. `sky130-bandgap`'s device
    characterization records)."""
    matches = [
        m for m in (_BULLET_CORNER_RE.match(line) for line in text.splitlines()) if m
    ]
    if not matches:
        return None
    nominal = next(
        (m for m in matches if _NOMINAL_CORNER_RE.match(m.group("corner_id"))),
        matches[0],
    )
    return {
        "corner_id": nominal.group("corner_id"),
        "status": nominal.group("verdict").lower(),
        "measurements": _split_measurements(nominal.group("body"))[:8],
    }


def _extract_table_nominal(text: str) -> dict[str, Any] | None:
    """Nominal-corner extraction for the `| corner-id | m1 | m2 | ... |
    pass/fail |` per-corner table record format (e.g. `gf180-bandgap`'s
    spec-verifying full-PVT-sweep records)."""
    lines = text.splitlines()
    header_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip().startswith("|")
            and re.search(r"corner.?id", line, re.IGNORECASE)
        ),
        None,
    )
    if header_idx is None:
        return None

    measurement_names = _split_row(lines[header_idx])[1:]
    row_lines: list[str] = []
    for line in lines[header_idx + 2 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        row_lines.append(stripped)

    parsed_rows: list[dict[str, Any]] = []
    for line in row_lines:
        cells = _split_row(line)
        if len(cells) < 2:
            continue
        verdict_match = re.match(r"(PASS|FAIL|ERROR)", cells[-1].strip(), re.IGNORECASE)
        if not verdict_match:
            continue
        measurements = []
        for name, raw_value in zip(measurement_names, cells[1:], strict=False):
            if name.strip().lower() in ("pass/fail", "sweep_points"):
                continue
            try:
                value = float(raw_value.strip())
            except ValueError:
                continue
            measurements.append(
                {"name": _slugify_header(name), "value": value, "unit": None}
            )
        parsed_rows.append(
            {
                "corner_id": cells[0].strip("` "),
                "status": verdict_match.group(1).lower(),
                "measurements": measurements[:8],
            }
        )

    if not parsed_rows:
        return None
    return next(
        (r for r in parsed_rows if _NOMINAL_CORNER_RE.match(r["corner_id"])),
        parsed_rows[0],
    )


def _extract_nominal_result(record_text: str) -> dict[str, Any] | None:
    return _extract_bullet_nominal(record_text) or _extract_table_nominal(record_text)


def _experiment_dirs(sim_dir: Path) -> list[Path]:
    if not sim_dir.is_dir():
        return []
    return sorted(
        d
        for d in sim_dir.iterdir()
        if d.is_dir()
        and (d / "records").is_dir()
        and list((d / "records").glob("*.md"))
    )


def _parse_suite_summary(sim_dir: Path) -> list[dict[str, Any]] | None:
    """Parse `sim/suite/summaries/*.md`'s "Per-spec-line verdict" table
    (the `run_suite.py`-style rollup a canary repo may ship once it has
    full-loop testbenches for every ratified spec row -- see
    `gf180-bandgap/sim/suite/README.md`). Absent for repos without one
    (e.g. `sky130-bandgap`, still at device-characterization stage)."""
    summaries_dir = sim_dir / "suite" / "summaries"
    if not summaries_dir.is_dir():
        return None
    md_files = sorted(summaries_dir.glob("*.md"))
    if not md_files:
        return None

    lines = md_files[-1].read_text().splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines) if line.strip().startswith("| Spec row")),
        None,
    )
    if header_idx is None:
        return None

    rows: list[dict[str, Any]] = []
    for line in lines[header_idx + 2 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        cells = _split_row(stripped)
        if len(cells) < 6:
            continue
        name, _target, _bench, worst, at_corner, verdict = cells[:6]
        value, unit = _lead_number_unit(worst)
        rows.append(
            {
                "name": name.strip(),
                "worst_case_corner_id": at_corner.strip("` "),
                "worst_case_value": value,
                "unit": unit,
                "status": verdict.strip().lower(),
            }
        )
    return rows or None


def build_signals(sim_dir: Path) -> dict[str, Any] | None:
    """Build a `layout.json`-shaped `signals` object from a canary repo's
    `sim/` evidence trail, or `None` if no experiment yields a pass/fail
    claim (e.g. `sim/` absent, or every experiment is device-characterization
    reference data with no `**Overall: ...**` verdict).
    """
    experiments = _experiment_dirs(sim_dir)
    if not experiments:
        return None

    corners: list[dict[str, Any]] = []
    fallback_measurements: list[dict[str, Any]] = []
    passed = failed = errored = 0
    engine_version: str | None = None

    for exp_dir in experiments:
        record_files = sorted((exp_dir / "records").glob("*.md"))
        if not record_files:
            continue
        text = record_files[-1].read_text()

        overall_match = _OVERALL_RE.search(text)
        if overall_match is None:
            # Pure device-characterization "recorded, not claimed" data --
            # no honest pass/fail/error value exists for it (see module
            # docstring). Excluded from signals entirely.
            continue

        overall_status = overall_match.group(1).lower()
        if overall_status == "pass":
            passed += 1
        elif overall_status == "fail":
            failed += 1
        else:
            errored += 1

        if engine_version is None:
            engine_match = _ENGINE_VERSION_RE.search(text)
            if engine_match:
                engine_version = engine_match.group(1)

        nominal = _extract_nominal_result(text)
        if nominal:
            corner_id = f"{exp_dir.name}/{nominal['corner_id']}"
            status = nominal["status"]
            measurements = [
                {
                    "name": m["name"],
                    "value": m["value"],
                    "unit": m["unit"],
                    "status": status,
                    "margin": None,
                }
                for m in nominal["measurements"]
            ]
        else:
            corner_id = f"{exp_dir.name}/overall"
            status = overall_status
            measurements = []

        corners.append(
            {
                "corner_id": corner_id,
                "process": None,
                "supply_v": {},
                "temperature_c": 27,
                "status": status,
                "runtime_s": 0,
                "measurements": measurements,
                "diagnostics": [],
            }
        )
        fallback_measurements.append(
            {
                "name": exp_dir.name.replace("-", " ").replace("_", " ").title(),
                "limits": None,
                "status": status,
                "worst_case": None,
            }
        )

    if not corners:
        return None

    suite_rows = _parse_suite_summary(sim_dir)
    if suite_rows:
        measurements = [
            {
                "name": row["name"],
                **({"unit": row["unit"]} if row["unit"] else {}),
                "limits": None,
                "status": row["status"],
                "worst_case": {
                    "corner_id": row["worst_case_corner_id"],
                    "value": row["worst_case_value"],
                    "margin": None,
                },
            }
            for row in suite_rows
        ]
    else:
        measurements = fallback_measurements

    if errored:
        aggregate_status = "error"
    elif failed:
        aggregate_status = "fail"
    else:
        aggregate_status = "pass"

    return {
        "schema_version": 1,
        "engine": "ngspice",
        "engine_version": engine_version,
        "status": aggregate_status,
        "corner_count": len(corners),
        "default_corner_id": corners[0]["corner_id"],
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "measurements": measurements,
        "corners": corners,
    }


# --------------------------------------------------------------------------- #
# GDS detection (issue #62 forward-compat path; fixed for real canary shapes
# by issue #896 -- see that issue's Curator Enhancement for the two real
# directory shapes this now handles)
# --------------------------------------------------------------------------- #


def _select_best_gds(candidates: list[Path], *, strict: bool = False) -> Path | None:
    """Among multiple GDS candidates in one directory, prefer a
    `*_routed*.gds` (excluding `*_inner*`) match -- the fully assembled,
    pad-ring/guard-ring-closed block -- over sub-block exports like
    `amp_input_pair.gds`/`pnp_ctat.gds` that a `compose`/`draw` step also
    writes into the same report directory.

    A single unambiguous candidate is used as-is, *unless* it is itself an
    `_inner` variant under `strict` mode -- a lone `*_inner.gds` with no
    sibling non-`_inner` file is a pre-pad-ring/guard-ring intermediate,
    not something safe to stage as "the" block layout. With multiple
    candidates and no `*_routed*` match: `strict=False` (the default, used
    for a plain `layout/*.gds`-style directory) falls back to alphabetical
    first, matching this function's original behavior. `strict=True` (used
    for a `reports/<run>/` directory, which is known to routinely hold
    heterogeneous sub-block exports alongside the assembled block) instead
    returns `None` -- silently staging a sub-block export as "the layout"
    would be worse than reporting `IN_DESIGN_STATUS` for that run and
    trying the next search location.
    """
    if not candidates:
        return None
    routed = [p for p in candidates if _ROUTED_GDS_RE.search(p.stem)]
    if routed:
        return sorted(routed)[0]
    if len(candidates) == 1:
        sole = candidates[0]
        if not (strict and "_inner" in sole.stem.lower()):
            return sole
    if strict:
        return None
    return sorted(candidates)[0]


def _gds_candidates_in_dir(d: Path) -> list[Path]:
    """Non-recursive GDS glob directly inside `d`."""
    candidates: list[Path] = []
    for pattern in _GDS_GLOBS:
        candidates.extend(p for p in d.glob(pattern) if p.is_file())
    return candidates


def _find_via_latest_pointer(d: Path) -> Path | None:
    """Follow a `<block>/reports/LATEST` pointer file when present (sky130-
    bandgap's convention: `layout/bandgap-core/reports/LATEST` is a plain-
    text file -- not a symlink -- naming the current timestamped run
    directory, e.g. `20260811-221633-a0ee5e7`). Returns the best GDS found
    directly inside that run directory, or `None` if no such pointer file
    exists under `d` or it does not resolve to a real, non-empty directory.

    Bounded to `reports/LATEST` two levels below `d` (`d/reports/LATEST` and
    `d/<block>/reports/LATEST`) -- not an unbounded `**` glob -- so a large
    canary repo's many other `reports/` trees (DRC/LVS report archives, not
    layout run archives) can't slow this down or get matched by accident.
    """
    for pointer in sorted(d.glob("reports/" + _LATEST_POINTER_NAME)) + sorted(
        d.glob("*/reports/" + _LATEST_POINTER_NAME)
    ):
        if not pointer.is_file():
            continue
        run_name = pointer.read_text().strip()
        if not run_name:
            continue
        run_dir = pointer.parent / run_name
        if not run_dir.is_dir():
            continue
        best = _select_best_gds(_gds_candidates_in_dir(run_dir), strict=True)
        if best is not None:
            return best
    return None


def _find_via_recursive_search(d: Path) -> Path | None:
    """Bounded recursive fallback: walk up to `_MAX_SEARCH_DEPTH` directory
    levels below `d`, skipping `_EXCLUDED_SEARCH_DIR_NAMES` subtrees (report
    archives, DRC/LVS fixture/report trees, this pipeline's own render
    output), and return the best GDS among every candidate found this way.
    Covers gf180-bandgap's flatter `layout/bandgap_top/bandgap_top.gds`
    shape (one level below `layout/`, no `reports/LATEST` pointer) while
    still excluding `layout/drc/fixtures/trivial_poly_res/*.gds`."""
    root_depth = len(d.parts)
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(d):
        depth = len(Path(dirpath).parts) - root_depth
        if depth >= _MAX_SEARCH_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [n for n in dirnames if n not in _EXCLUDED_SEARCH_DIR_NAMES]
        for name in filenames:
            if any(fnmatch.fnmatch(name, pat) for pat in _GDS_GLOBS):
                candidates.append(Path(dirpath) / name)
    return _select_best_gds(candidates)


def find_layout_gds(repo_dir: Path) -> Path | None:
    """Locate a layout stream under a canary repo's conventional `layout/`
    directory (falling back to `gds/` then the repo root). `None` when
    absent (a genuinely pre-layout canary).

    Two strategies, tried in order for each of `_LAYOUT_SEARCH_DIRS`:

    1. Follow a `reports/LATEST` pointer file if present (sky130-bandgap's
       convention -- see `_find_via_latest_pointer`).
    2. A bounded recursive search excluding report/fixture/render subtrees
       (covers gf180-bandgap's flatter shape, and the original flat
       `layout/*.gds` case this function always supported).

    Either way, when a directory holds multiple GDS candidates, a
    `*_routed*.gds` (non-`_inner`) match is preferred over alphabetical
    first (see `_select_best_gds`) -- otherwise a sub-block export like
    `amp_input_pair.gds` could sort ahead of the fully assembled block.
    """
    for sub in _LAYOUT_SEARCH_DIRS:
        d = repo_dir if sub == "." else repo_dir / sub
        if not d.is_dir():
            continue
        found = _find_via_latest_pointer(d) or _find_via_recursive_search(d)
        if found is not None:
            return found
    return None


def _stage_gds(gds_path: Path, block_dir: Path) -> Path:
    """Copy a found layout file into `<block_dir>/output/<name>`, matching
    where `site/scripts/copy-renders.mjs` looks for a `downloadable`
    `layout_file` (relative to `output/`, the same convention `renders`
    values use)."""
    output_dir = block_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / gds_path.name
    shutil.copyfile(gds_path, dest)
    return dest


# --------------------------------------------------------------------------- #
# Assemble + write layout.json
# --------------------------------------------------------------------------- #


def _default_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


def _attach_overview_render(
    slug: str, block_dir: Path, gds_path: Path, layout: dict
) -> None:
    """Render `gds_path` into `output/renders/` and attach every non-empty
    per-layer PNG plus the all-layers composite to `layout["renders"]`
    (issue #942 -- previously only the fixed `{"overview": ...}` entry was
    recorded, "Option A" from #651, discarding the per-layer PNGs the same
    call wrote alongside it), plus one extra zoomed "center crop" composite
    (issue #673's `--bbox`, via `center_crop=True`) so a canary block's
    detail page shows more than the full-die postage stamp.

    Per-layer entries are labeled with `infer_layer_names(slug)` --
    canary repos carry no explicit `pdk` field, so the label table is
    guessed from the slug's `<pdk>-<name>` convention (see that function's
    docstring); an unrecognised prefix just falls back to `layer_<n>_<n>`
    keys instead of a wrong PDK's names.

    Thin wrapper around the shared `_gallery_common.attach_overview_render`
    helper (issue #670 -- deduped from a near-identical copy of this function
    in `scripts/bootstrap-gallery-blocks.py`): a render failure prints a
    warning here and leaves `layout` untouched rather than aborting the
    ingest -- this is the "first link" the render/copy/display chain was
    missing, not a required artifact, so it follows the same opt-in
    treatment as `spec_summary`/`signals` above.
    """
    exc = attach_overview_render(
        gds_path,
        block_dir,
        layout,
        render_fn=render_report,
        layer_names=infer_layer_names(slug),
        center_crop=True,
    )
    if exc is not None:
        print(f"ingest-canary: {slug}: skipping render: {exc}", file=sys.stderr)


def build_layout_json(
    repo_dir: Path,
    *,
    repo: str,
    ref: str,
    slug: str,
    block_dir: Path,
    dry_run: bool = False,
    pdk: str | None = None,
) -> dict[str, Any]:
    """Assemble a `layout.json`-shaped dict for a cloned canary repo.

    Field order mirrors `docs/cli/layout-metrics.md`'s example (`status`
    last) for readability; JSON key order carries no semantic meaning.

    `pdk` (issue #1285) is the block's PDK family, written as the optional
    `pdk` field. A canary repo carries no explicit PDK of its own, so it
    comes from `--pdk` when the operator supplies one (authoritative), and
    otherwise from the same conservative `<pdk>-<name>` slug guess that
    already labels this pipeline's renders (`_gallery_common.infer_pdk`).
    When neither yields a PDK the field is omitted entirely -- no field
    rather than a wrong one; the site keeps its own slug heuristic for
    those blocks.
    """
    if pdk is not None and pdk not in deck_names():
        raise IngestError(f"unknown pdk: {pdk} (known: {', '.join(deck_names())})")

    layout: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "name": _default_name(slug),
    }

    description = repo_description(repo)
    if description:
        layout["description"] = description

    resolved_pdk = pdk or infer_pdk(slug)
    if resolved_pdk is not None:
        layout["pdk"] = resolved_pdk

    gds_path = find_layout_gds(repo_dir)
    source_path = "."
    if gds_path is not None:
        # Record exactly which file within the source repo this render/
        # download came from (issue #896) -- e.g. a sky130-bandgap ingest
        # resolves to `layout/bandgap-core/reports/<run>/bandgap_core_
        # routed.gds`, not just "somewhere in the repo". Computed from the
        # *original* clone-relative path, before staging copies it flat
        # into `block_dir/output/`.
        source_path = str(gds_path.relative_to(repo_dir))
        staged = gds_path if dry_run else _stage_gds(gds_path, block_dir)
        parsed_ok = True
        try:
            layers = layers_report(str(staged))
            layout["layer_count"] = layers["layer_count"]
        except LayersError:
            parsed_ok = False
        try:
            cells = cells_report(str(staged))
            layout["cell_count"] = cells["cell_count"]
            layout["instance_count"] = sum(c["instances"] for c in cells["cells"])
        except CellsError:
            parsed_ok = False
        layout["layout_file"] = staged.name
        status = "ok" if parsed_ok else "partial"
        if not dry_run:
            _attach_overview_render(slug, block_dir, staged, layout)
    else:
        status = IN_DESIGN_STATUS

    readme_path = repo_dir / "README.md"
    if readme_path.is_file():
        spec_summary = parse_spec_summary(readme_path.read_text())
        if spec_summary:
            layout["spec_summary"] = spec_summary

    signals = build_signals(repo_dir / "sim")
    if signals:
        layout["signals"] = signals

    layout["source"] = {"repo": repo, "ref": ref, "path": source_path}
    layout["downloadable"] = True
    layout["status"] = status
    return layout


def ingest(
    repo: str,
    *,
    ref: str | None = None,
    slug: str | None = None,
    blocks_dir: Path = DEFAULT_BLOCKS_DIR,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    dry_run: bool = False,
    pdk: str | None = None,
) -> dict[str, Any]:
    """Gate, clone, and ingest `repo`, returning the emitted `layout.json`
    dict. Raises `IngestError` (writing nothing) if the gate refuses or
    `pdk` names a PDK family this build does not ship."""
    assert_public_repo(repo)

    resolved_slug = slug or repo.split("/")[-1]
    repo_dir, resolved_ref = clone_repo(repo, cache_dir, ref)
    block_dir = blocks_dir / resolved_slug

    layout = build_layout_json(
        repo_dir,
        repo=repo,
        ref=resolved_ref,
        slug=resolved_slug,
        block_dir=block_dir,
        dry_run=dry_run,
        pdk=pdk,
    )

    if not dry_run:
        output_dir = block_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "layout.json").write_text(json.dumps(layout, indent=2) + "\n")

    return layout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument("--repo", required=True, help="e.g. 2AMLogic/gf180-bandgap")
    parser.add_argument(
        "--ref",
        default=None,
        help="Branch/tag/sha to pin (default: default branch HEAD)",
    )
    parser.add_argument(
        "--slug", default=None, help="Block slug override (default: repo name)"
    )
    parser.add_argument(
        "--pdk",
        default=None,
        help=(
            "PDK family this block targets, written as layout.json's `pdk` "
            "field (currently: sky130, gf180mcu, sg13g2). Default: guessed "
            "from the slug's <pdk>-<name> prefix, and omitted entirely when "
            "that guess fails. An unknown name exits 1."
        ),
    )
    parser.add_argument(
        "--blocks-dir",
        type=Path,
        default=DEFAULT_BLOCKS_DIR,
        help="Gallery blocks/ directory",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Where to clone/cache the repo",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print, do not write")
    args = parser.parse_args(argv)

    try:
        layout = ingest(
            args.repo,
            ref=args.ref,
            slug=args.slug,
            blocks_dir=args.blocks_dir,
            cache_dir=args.cache_dir,
            dry_run=args.dry_run,
            pdk=args.pdk,
        )
    except IngestError as exc:
        print(f"ingest-canary: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(layout, indent=2))
    else:
        slug, source, status = layout["slug"], layout["source"], layout["status"]
        print(
            f"{slug}: wrote blocks/{slug}/output/layout.json "
            f"(status={status!r}, source={source['repo']}@{source['ref'][:12]})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Tiny Tapeout corpus defect triage (Epic #520 Phase 1.C, issue #5083).

Reads the per-(design, stage) records ``scripts/tt_corpus_run.py`` (1.B)
wrote, clusters the non-passing ones by a **stable signature**, and emits a
triage report (JSON, or Markdown with ``--markdown``) with, per cluster: the
signature, a count, 1-3 minimal exemplars, attribution for each exemplar
(repo, pinned commit, license, from the 1.A manifest), a reproducer command
and a drafted issue body.

Signature = ``stage`` + a normalized error:

* ``error`` / ``crash`` / ``timeout`` -- the JSON error ``code``/``message``
  (or, failing that, the last non-empty stderr line), with paths, hex
  addresses, numbers and quoted names normalized away so the same defect on
  two designs collapses to one cluster;
* ``findings`` -- the sorted DRC rule ids (``rule_counts``) or LVS mismatch
  categories (``category_counts``) from the stored envelope, not counts.

``pass`` and ``skipped`` records are not defects and are tallied only.

This tool **never files issues**. Filing is human- or Curator-confirmed; the
report's ``issue_drafts`` are text to review.

Usage::

    scripts/tt_corpus_triage.py --shuttle ttgf0p2 --cache-dir .cache/tt-corpus
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
DEFECT_OUTCOMES = ("findings", "error", "crash", "timeout")
MAX_EXEMPLARS = 3
_RULES = (
    (re.compile(r"(?:[A-Za-z]:)?(?:/[^\s'\":,;)]+)+"), "<path>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<q>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def normalize(text: str) -> str:
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text.strip()


def _last_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def error_detail(stderr_tail: str) -> str:
    """Prefer a JSON error envelope's code/message over raw stderr text."""
    for line in reversed(stderr_tail.splitlines()):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return f"{err.get('code') or ''} {err['message']}".strip()
    try:  # a multi-line (indented) envelope: the tail may hold all of it
        doc = json.loads(stderr_tail)
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return f"{err.get('code') or ''} {err['message']}".strip()
    except ValueError:
        pass
    return _last_line(stderr_tail)


def findings_detail(envelope: dict[str, Any] | None) -> str:
    if not envelope:
        return "no_envelope"
    for key in ("rule_counts", "category_counts"):
        counts = envelope.get(key)
        if isinstance(counts, dict) and counts:
            return key.split("_")[0] + ":" + ",".join(sorted(counts))
    return "findings_without_breakdown"


def signature(record: dict[str, Any], envelope: dict[str, Any] | None) -> str:
    outcome = record["outcome"]
    if outcome == "findings":
        detail = findings_detail(envelope)
    elif outcome == "timeout":
        detail = "timeout"
    else:
        detail = normalize(error_detail(record.get("stderr_tail") or ""))
        detail = detail or f"exit_{record.get('exit_code')}"
        if outcome == "crash":
            detail = f"crash {detail}"
    return f"{record['stage']}|{outcome}|{detail}"


def load_records(results_dir: Path, shuttle: str) -> list[dict[str, Any]]:
    out = []
    for path in sorted((results_dir / shuttle).glob("*/*.json")):
        if path.name.endswith(".envelope.json"):
            continue
        try:
            rec = json.loads(path.read_text())
        except ValueError:
            continue
        if isinstance(rec, dict) and "outcome" in rec and "stage" in rec:
            rec["_dir"] = str(path.parent)
            out.append(rec)
    return out


def attribution(cache_dir: Path, shuttle: str, macro: str) -> dict[str, Any]:
    path = cache_dir / "manifests" / shuttle / f"{macro}.json"
    try:
        man = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"macro": macro, "repo": None, "commit": None, "license": None}
    lic = man.get("license") or {}
    return {
        "macro": macro,
        "repo": man.get("repo"),
        "commit": man.get("commit"),
        "license": lic.get("spdx"),
        "license_file": lic.get("file"),
    }


def reproducer(rec: dict[str, Any], shuttle: str, cache_dir: Path) -> str:
    """The exact ``klt`` argv the runner used, plus the cache it ran against."""
    cmd = rec.get("command")
    klt = (
        " ".join(shlex.quote(str(x)) for x in cmd) if cmd else f"klt {rec['stage']} ..."
    )
    return f"{klt}  # layout: {cache_dir}/runs/{shuttle}/{rec['macro']}/work/"


def draft_issue(cluster: dict[str, Any], tool_version: str) -> dict[str, str]:
    ex = cluster["exemplars"]
    lines = [
        f"Corpus regression cluster `{cluster['id']}`: **{cluster['count']}** "
        f"design(s) fail `klt {cluster['stage']}` with the same signature.",
        "",
        f"- Signature: `{cluster['signature']}`",
        f"- Tool version: `{tool_version}`",
        "",
        "## Exemplars",
        "",
    ]
    for e in ex:
        a = e["attribution"]
        lines.append(
            f"- `{a['macro']}` -- {a['repo']} @ `{a['commit']}` "
            f"({a['license'] or 'license unknown'})"
        )
        lines.append(f"  - Reproduce: `{e['reproducer']}`")
    lines += [
        "",
        "Designs are third-party Tiny Tapeout projects, used under their own "
        "licenses; credit the authors above. This reports a measured tool "
        "outcome on the pinned commits, not a claim about the designs.",
    ]
    detail = cluster["signature"].split("|", 2)[2]
    return {
        "title": f"[corpus] klt {cluster['stage']}: {detail[:80]}",
        "body": "\n".join(lines),
    }


def triage(
    *, cache_dir: Path, shuttle: str, results_dir: Path | None = None
) -> dict[str, Any]:
    results_dir = results_dir or cache_dir / "runs"
    records = load_records(results_dir, shuttle)
    tally: dict[str, dict[str, int]] = {}
    clusters: dict[str, dict[str, Any]] = {}
    versions: set[str] = set()
    for rec in records:
        stage_tally = tally.setdefault(rec["stage"], {})
        stage_tally[rec["outcome"]] = stage_tally.get(rec["outcome"], 0) + 1
        versions.add(str(rec.get("tool_version")))
        if rec["outcome"] not in DEFECT_OUTCOMES:
            continue
        envelope = None
        if rec.get("envelope_path"):
            try:
                envelope = json.loads(
                    (Path(rec["_dir"]) / rec["envelope_path"]).read_text()
                )
            except (OSError, ValueError):
                envelope = None
        sig = signature(rec, envelope)
        c = clusters.setdefault(
            sig,
            {
                "id": hashlib.sha256(sig.encode()).hexdigest()[:10],
                "signature": sig,
                "stage": rec["stage"],
                "outcome": rec["outcome"],
                "count": 0,
                "macros": [],
                "_recs": [],
            },
        )
        c["count"] += 1
        c["macros"].append(rec["macro"])
        c["_recs"].append(rec)
    tool_version = ", ".join(sorted(versions)) or "unknown"
    out_clusters = []
    for c in sorted(clusters.values(), key=lambda c: (-c["count"], c["signature"])):
        # minimal exemplars: shortest wall-clock first (cheapest to reproduce)
        recs = sorted(
            c.pop("_recs"), key=lambda r: (r.get("duration_s") or 0.0, r["macro"])
        )[:MAX_EXEMPLARS]
        c["macros"].sort()
        c["exemplars"] = [
            {
                "attribution": attribution(cache_dir, shuttle, r["macro"]),
                "duration_s": r.get("duration_s"),
                "stderr_tail": (r.get("stderr_tail") or "")[-400:],
                "reproducer": reproducer(r, shuttle, cache_dir),
            }
            for r in recs
        ]
        c["issue_draft"] = draft_issue(c, tool_version)
        out_clusters.append(c)
    return {
        "schema_version": SCHEMA_VERSION,
        "shuttle": shuttle,
        "tool_version": tool_version,
        "records": len(records),
        "outcomes": tally,
        "cluster_count": len(out_clusters),
        "clusters": out_clusters,
        "filing": "none -- drafts only; filing is human- or Curator-confirmed",
    }


def to_markdown(report: dict[str, Any]) -> str:
    out = [
        f"# Corpus triage: {report['shuttle']}",
        "",
        f"{report['records']} records, {report['cluster_count']} defect clusters "
        f"(klt {report['tool_version']}). Drafts only -- nothing was filed.",
        "",
    ]
    for c in report["clusters"]:
        out += [f"## `{c['id']}` {c['signature']} ({c['count']})", ""]
        for e in c["exemplars"]:
            a = e["attribution"]
            out.append(
                f"- `{a['macro']}`: {a['repo']} @ `{a['commit']}` "
                f"({a['license'] or 'license unknown'}); `{e['reproducer']}`"
            )
        out += [
            "",
            "<details><summary>Issue draft</summary>",
            "",
            f"**{c['issue_draft']['title']}**",
            "",
            c["issue_draft"]["body"],
            "",
            "</details>",
            "",
        ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--shuttle", required=True)
    p.add_argument("--cache-dir", default=".cache/tt-corpus", type=Path)
    p.add_argument("--results-dir", type=Path, help="default: <cache-dir>/runs")
    p.add_argument("--markdown", action="store_true")
    p.add_argument("-o", "--output", type=Path)
    args = p.parse_args(argv)
    results = args.results_dir or args.cache_dir / "runs"
    if not (results / args.shuttle).is_dir():
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "error": f"no run records under {results / args.shuttle} "
                    "-- run scripts/tt_corpus_run.py first",
                }
            )
        )
        return 2
    report = triage(cache_dir=args.cache_dir, shuttle=args.shuttle, results_dir=results)
    text = (
        to_markdown(report)
        if args.markdown
        else json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compare one area-oriented ABC mapping with the pinned tt06 sky130 baseline.

Populate the Phase 1 cache as in tt_corpus_baseline.py. With the baseline's
tools and Liberty on PATH / PDK_ROOT, run from the repository root:

    python scripts/tt_corpus_optimize.py prepare --work-dir run
    # Run the Phase 2.C checker on run/pairs.json to produce verdicts.json.
    python scripts/tt_corpus_optimize.py report --pairs run/pairs.json \
        --verdicts verdicts.json --output results.json

prepare requires a fresh directory and retains both requests, reports and
mapped netlists. Artifact paths are relative to pairs.json. candidate_count
is raw synthesis evidence for the checker. Only report emits optimized_count
and delta. Without --verdicts it records every design as excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
from collections import defaultdict
from pathlib import Path

import tt_corpus_baseline as baseline
import yaml
from tt_corpus_fetch import safe_macro

SNAPSHOT = baseline.SAMPLE.with_name("tt_sky130_baseline.json")
AREA_SCRIPT = b"strash\ndch -f\nmap -a\n"
ABC_SCRIPT = {"path": "area.abc", "sha256": hashlib.sha256(AREA_SCRIPT).hexdigest()}
REQUEST_DIFF = {"synthesis": {"abc_script": ABC_SCRIPT["path"]}}
SIDES = ("baseline", "candidate")


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def map_design(run: Path, row: dict, blob: Path, pins: dict, side: str) -> dict:
    work = run / row["macro"] / side
    work.mkdir(parents=True)
    top = baseline.write_request(blob, work)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top):
        raise ValueError(f"invalid top module {top!r}")
    if side == "candidate":
        request = work / "request.json"
        write_json(request, {**json.loads(request.read_text()), **REQUEST_DIFF})
        (work / ABC_SCRIPT["path"]).write_bytes(AREA_SCRIPT)
    report = baseline.synthesize(work, top)
    report_file = work / "report.json"
    write_json(report_file, report)
    if baseline.report_pins(report) != pins:
        raise ValueError(f"{side} synthesis pins differ from pinned baseline")
    if side == "candidate" and report["synthesis"]["abc_script"] != ABC_SCRIPT:
        raise ValueError("candidate report has a different ABC script")
    count = report["instance_count"]
    if side == "baseline" and count != row["baseline_count"]:
        raise ValueError("baseline synthesis did not reproduce pinned instance_count")
    run_id = report["run_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("invalid synthesis run_id")
    netlist = work / ".klt" / "synthesize" / run_id / f"{top}_synth.v"
    if sha256(netlist) != report["netlist_sha256"]:
        raise ValueError(f"{side} netlist differs from its synthesis report")
    return {
        "top": top,
        f"{side}_netlist": netlist.relative_to(run).as_posix(),
        f"{side}_sha256": report["netlist_sha256"],
        f"{side}_report": report_file.relative_to(run).as_posix(),
        f"{side}_count": count,
    }


def prepare(snapshot: dict, cache: Path, run: Path) -> dict:
    run.mkdir(parents=True)
    rows = []
    for project in snapshot["designs"]:
        row = project.copy()
        rows.append(row)
        if "instance_count" in row:
            row["baseline_count"] = row.pop("instance_count")
        if "exclusion_reason" in row:
            continue
        try:
            if not safe_macro(row["macro"]):
                raise ValueError("invalid macro in baseline")
            digest = row["source_sha256"]
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("invalid pinned source archive digest")
            for side in SIDES:
                row.update(
                    map_design(
                        run, row, cache / "blobs" / digest, snapshot["pins"], side
                    )
                )
        except (
            OSError,
            KeyError,
            ValueError,
            TypeError,
            tarfile.TarError,
            EOFError,
            yaml.YAMLError,
            subprocess.TimeoutExpired,
        ) as exc:
            row["exclusion_reason"] = str(exc).replace(str(run) + "/", "")
    return {
        "schema_version": 1,
        "shuttle": snapshot["shuttle"],
        "pins": snapshot["pins"],
        "optimization": {
            "name": "abc-area-map",
            "request_difference": REQUEST_DIFF,
            "abc_script": ABC_SCRIPT,
        },
        "total": len(rows),
        "designs": rows,
    }


def matching_verdict(pair: dict, proofs: list[dict], directory: Path) -> dict:
    if not proofs:
        raise ValueError("missing formal verdict")
    for side in SIDES:
        if sha256(directory / pair[f"{side}_netlist"]) != pair[f"{side}_sha256"]:
            raise ValueError(f"{side} netlist changed since synthesis")
    matches = [
        proof
        for proof in proofs
        if all(proof.get(f"{side}_sha256") == pair[f"{side}_sha256"] for side in SIDES)
    ]
    if not matches:
        raise ValueError("formal verdict does not match this netlist pair")
    if len(matches) > 1:
        raise ValueError("ambiguous formal verdicts for this netlist pair")
    return matches[0]


def measured_count(pair: dict, side: str, directory: Path) -> int:
    report = json.loads((directory / pair[f"{side}_report"]).read_text())
    count = report["instance_count"]
    if type(count) is not int or count < 0 or count != pair[f"{side}_count"]:
        raise ValueError(f"{side} instance_count differs from synthesis evidence")
    if report["netlist_sha256"] != pair[f"{side}_sha256"]:
        raise ValueError(f"{side} synthesis report describes a different netlist")
    return count


def result_row(pair: dict, proofs: list[dict], directory: Path) -> dict:
    keys = (
        "macro",
        "repo",
        "commit",
        "license",
        "source_sha256",
        "top",
        "baseline_count",
        "baseline_sha256",
        "candidate_sha256",
    )
    row = {k: pair[k] for k in keys if k in pair}
    row["verdict"] = "unestablished"
    if "exclusion_reason" in pair:
        return {**row, "exclusion_reason": pair["exclusion_reason"]}
    try:
        proof = matching_verdict(pair, proofs, directory)
        verdict = proof["verdict"]
        if verdict not in ("equivalent", "not-equivalent", "unestablished"):
            raise ValueError(f"unknown formal verdict: {verdict!r}")
        row["verdict"] = verdict
        if verdict != "equivalent":
            return {**row, "exclusion_reason": proof.get("exclusion_reason") or verdict}
        original = measured_count(pair, "baseline", directory)
        optimized = measured_count(pair, "candidate", directory)
        return {**row, "optimized_count": optimized, "delta": optimized - original}
    except (OSError, KeyError, ValueError, TypeError) as exc:
        row["exclusion_reason"] = str(exc)
        return row


def results(pairs: Path, verdicts: Path | None) -> dict:
    manifest = json.loads(pairs.read_text())
    proofs = defaultdict(list)
    if verdicts is not None and verdicts.exists():
        for proof in json.loads(verdicts.read_text())["designs"]:
            proofs[proof["macro"], proof["commit"]].append(proof)
    rows = [
        result_row(pair, proofs[pair["macro"], pair["commit"]], pairs.parent)
        for pair in manifest["designs"]
    ]
    reported = sum("optimized_count" in row for row in rows)
    return {
        **manifest,
        "reported": reported,
        "excluded": len(rows) - reported,
        "designs": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser(
        "prepare", help="synthesize both sides into a fresh directory"
    )
    prep.add_argument("--baseline", type=Path, default=SNAPSHOT)
    prep.add_argument("--cache-dir", type=Path, default=Path(".cache/tt-corpus"))
    prep.add_argument("--work-dir", type=Path, required=True)
    report = sub.add_parser("report", help="admit exact pairs with equivalent verdicts")
    report.add_argument("--pairs", type=Path, required=True)
    report.add_argument("--verdicts", type=Path)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        if args.work_dir.exists():
            parser.error("--work-dir must be a new directory")
        data = prepare(
            json.loads(args.baseline.read_text()), args.cache_dir, args.work_dir
        )
        write_json(args.work_dir / "pairs.json", data)
    else:
        write_json(args.output, results(args.pairs, args.verdicts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

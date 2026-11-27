#!/usr/bin/env python3
"""Run or collect pinned GF180 Tiny Tapeout shards with 2am's repo-script harness."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import tt_corpus_run
import tt_corpus_triage
from tt_corpus_fetch import PacedHttpTransport, safe_macro

CATALOG = Path(__file__).resolve().parents[1] / "tests/corpus/tt_gf180_slots.tsv"
OUTPUT = Path("build/tt-gf180-corpus")
SCRIPT = "scripts/tt_corpus_batch.py"
STAGES = ("drc", "extract")
# The first shuttle published one combined macro. The other two provide
# per-project OAS files, apart from the shuttle ROMs in their release bundles.
SHUTTLES = {
    "ttgf0p1": ("fbeb26d1140173582dc5eb0cc907fc5af5081f84", "", ""),
    "ttgf0p2": (
        "622aa779a5e7cf4c1007c6f6ad939678d21897b0",
        "tapeout-ws2512",
        "1ea3ff848a5107dcf602437c98b2771df4c7311e850f534cb165d0b3ccd14bbe",
    ),
    "ttgf0p3": (
        "305047697e4931a74c12202dfc83e1946b9685d9",
        "tapeout-ws2607",
        "86e2611ed48827c8b0bedd98805cc7f331d25cd1d1d3446f2225ddd8818a079b",
    ),
}


def catalog():
    with CATALOG.open() as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    keys = {(r["shuttle"], safe_macro(r["macro"])) for r in rows}
    if len(keys) != len(rows) or any(s not in SHUTTLES or not m for s, m in keys):
        raise ValueError("invalid or duplicate catalog identity")
    return sorted(rows, key=lambda r: (r["shuttle"], r["macro"] + ".json"))


def selected(rows, shard):
    index, count = shard
    if not 0 <= index < count:
        raise ValueError("shard must be I/N with 0 <= I < N")
    return [
        row
        for shuttle in SHUTTLES
        for row in [r for r in rows if r["shuttle"] == shuttle][index::count]
    ]


def skip_reason(row):
    if row["repo"] == "N/A":
        return "no indexed source or individual layout"
    if not row["spdx"]:
        return "no recognized root license at pinned project commit"
    if not row["layout_sha256"]:
        return "no individual layout in combined shuttle macro"
    return None


def plan(rows, count, timeout, job_timeout, overhead):
    """DRC and extract run concurrently, each serial over the assigned designs."""
    if min(count, timeout, job_timeout, overhead) < 1:
        raise ValueError("shard count, timeouts and overhead must be positive")
    shards = []
    for index in range(count):
        assigned = selected(rows, (index, count))
        eligible = sum(skip_reason(r) is None for r in assigned)
        seconds = eligible * timeout + overhead
        if not assigned or seconds > job_timeout:
            raise ValueError(
                f"shard {index}/{count}: {len(assigned)} slots, {seconds}s required"
            )
        shards.append(
            {
                "index": index,
                "slots": len(assigned),
                "eligible": eligible,
                "seconds": seconds,
            }
        )
    return shards


def manifest(row):
    reason = skip_reason(row)
    return {
        "schema_version": "1",
        "shuttle": row["shuttle"],
        "macro": row["macro"],
        "pdk": "gf180mcuD",
        "repo": row["repo"],
        "commit": row["commit"],
        "license": {
            "spdx": row["spdx"],
            "file": row["license_file"],
            "blob": row["license_blob"],
        },
        "artifacts": {} if reason else {"gds": {"sha256": row["layout_sha256"]}},
        "excluded": bool(reason),
        "exclusion_reason": reason,
    }


def verified(data, expected):
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(
            f"layout or release archive differs from pinned SHA-256 {expected}"
        )
    return data


def layout_bytes(row, fetch):
    shuttle, macro = row["shuttle"], row["macro"]
    pin, tag, archive_digest = SHUTTLES[shuttle]
    repo = f"TinyTapeout/tinytapeout-gf-0p{shuttle[-1]}"
    if macro == "tt_um_chip_rom":
        url = f"https://github.com/{repo}/releases/download/{tag}/macros.zip"
        with zipfile.ZipFile(
            io.BytesIO(verified(fetch(url), archive_digest))
        ) as archive:
            data = archive.read(f"ol2/tt_top/gds/{macro}.gds")
    else:
        url = f"https://raw.githubusercontent.com/{repo}/{pin}/projects/{macro}/{macro}.oas"
        data = fetch(url)
    return verified(data, row["layout_sha256"])


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def populate(cache, rows):
    for row in rows:
        write_json(
            cache / "manifests" / row["shuttle"] / (row["macro"] + ".json"),
            manifest(row),
        )


def run_tool(argv, timeout):
    # On the Linux fleet klt can launch KLayout as a child. Give each stage
    # its own process group so a timed-out engine cannot outlive its shard.
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
        cwd=Path(argv[2]).parent if argv[1] in STAGES else None,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        finally:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
    return proc.returncode, stdout, stderr


def run_stage_shards(stage, root, shard, timeout, runner):
    versions = set()
    for shuttle in SHUTTLES:
        summary = tt_corpus_run.run(
            cache_dir=root,
            results_dir=root / stage,
            stages=(stage,),
            shuttle=shuttle,
            shard=shard,
            timeout_s=timeout,
            runner=runner,
        )
        versions.add(summary["tool_version"])
        for rec in summary["records"]:
            src = root / stage / shuttle / rec["macro"]
            target = root / "export" / "records" / shuttle / rec["macro"]
            write_json(target / (rec["stage"] + ".json"), rec)
            if rec.get("envelope_path"):
                shutil.copyfile(
                    src / rec["envelope_path"], target / rec["envelope_path"]
                )
    return versions


def run_shard(rows, shard, timeout, dest, fetch: Callable, runner=run_tool):
    assigned = selected(rows, shard)
    if not assigned or timeout < 1:
        raise ValueError("shard must contain a design and timeout must be positive")
    version = tt_corpus_run.tool_version("klt", runner)
    if not version or version.startswith("unknown"):
        raise ValueError("cannot identify klt on this worker")
    commit = os.environ["RS_SOURCE_COMMIT"]
    if not re.fullmatch("[a-f0-9]{40}", commit):
        raise ValueError("repo-script must supply the pinned source commit")
    if dest.exists():
        raise ValueError(f"output already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as scratch:
        root = Path(scratch).resolve()
        populate(root, rows)
        (root / "blobs").mkdir()
        for row in assigned:
            if not skip_reason(row):
                (root / "blobs" / row["layout_sha256"]).write_bytes(
                    layout_bytes(row, fetch)
                )
        output = root / "export"
        with ThreadPoolExecutor(max_workers=len(STAGES)) as pool:
            stages = [
                pool.submit(run_stage_shards, stage, root, shard, timeout, runner)
                for stage in STAGES
            ]
            versions = set().union(*(stage.result() for stage in stages))
        if versions != {version}:
            raise ValueError(f"cannot identify a consistent klt executable: {versions}")
        write_json(
            output / "shard.json",
            {
                "shard": shard,
                "timeout_s": timeout,
                "tool_version": version,
                "commit": commit,
                "catalog_sha256": hashlib.sha256(CATALOG.read_bytes()).hexdigest(),
            },
        )
        output.rename(dest)


def read_json(path):
    return json.loads(path.read_text())


def validated_record(path, row, stage, meta):
    rec = read_json(path)
    expected = (row["shuttle"], row["macro"], stage, meta["tool_version"])
    actual = tuple(
        rec.get(key) for key in ("shuttle", "macro", "stage", "tool_version")
    )
    if actual != expected or rec.get("pdk") != "gf180mcuD":
        raise ValueError(f"unexpected record identity: {path}")
    reason = skip_reason(row)
    if reason:
        if (
            rec.get("outcome") != "skipped"
            or rec.get("reason") != f"excluded: {reason}"
        ):
            raise ValueError(f"unaccounted exclusion: {path}")
    elif rec.get("outcome") not in ("pass", "findings", "error", "crash", "timeout"):
        raise ValueError(f"eligible design has no measured outcome: {path}")
    elif rec.get("timeout_s") != meta["timeout_s"]:
        raise ValueError(f"stage timeout differs from shard: {path}")
    return rec


def copy_records(payload, target, assigned, meta):
    records = payload / "records"
    expected = set()
    for row in assigned:
        design = Path(row["shuttle"]) / row["macro"]
        for stage in STAGES:
            path = design / f"{stage}.json"
            rec = validated_record(records / path, row, stage, meta)
            expected.add(path)
            envelope = rec.get("envelope_path")
            if envelope is not None:
                if envelope != f"{stage}.envelope.json":
                    raise ValueError(f"invalid envelope path in {path}")
                expected.add(design / envelope)
    actual = {p.relative_to(records) for p in records.rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError(f"unexpected or missing record files: {actual ^ expected}")
    for path in sorted(expected):
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            raise ValueError(f"duplicate collected record: {path}")
        shutil.copyfile(records / path, out)


def fleet_runs(group_dir):
    group = read_json(group_dir / "group.json")
    seen = set()
    for job in group["shards"]:
        match = re.fullmatch(re.escape(group["group"]) + r"-(s[0-9]+)", job["job"])
        if not match or job["ok"] is not True or job["state"] != "done":
            raise ValueError(f"invalid or failed job: {job['job']}")
        label = match[1]
        if label in seen:
            raise ValueError(f"duplicate job: {job['job']}")
        seen.add(label)
        folder = group_dir / label
        run = read_json(folder / "run.json")
        if (
            run["kind"],
            run["repo"].lower(),
            run["script"],
            run["shard"],
            run["exit_code"],
        ) != (
            "repo-script",
            "2amlogic/klayout-tools",
            SCRIPT,
            label,
            0,
        ) or run["outputs"] != {"found": [str(OUTPUT)], "missing": []}:
            raise ValueError(
                f"job did not complete the expected repo script: {job['job']}"
            )
        yield job, run, folder / "files" / OUTPUT
    if not seen or seen != {p.name for p in group_dir.glob("s[0-9]*") if p.is_dir()}:
        raise ValueError(f"incomplete group inventory: {group_dir}")


def triage_reports(report, rows, count, commit, timeout):
    assignments = {
        (row["shuttle"], row["macro"]): index
        for index in range(count)
        for row in selected(rows, (index, count))
    }
    summary = {}
    for shuttle in SHUTTLES:
        result = tt_corpus_triage.triage(cache_dir=report, shuttle=shuttle)
        for cluster in result["clusters"]:
            for exemplar in cluster["exemplars"]:
                index = assignments[shuttle, exemplar["attribution"]["macro"]]
                exemplar["reproducer"] = (
                    f"RS_SOURCE_COMMIT={commit} python3 {SCRIPT} run {index}/{count} "
                    f"--timeout-s {timeout}"
                )
            cluster["issue_draft"] = tt_corpus_triage.draft_issue(
                cluster, result["tool_version"]
            )
        write_json(report / f"{shuttle}.json", result)
        (report / f"{shuttle}.md").write_text(tt_corpus_triage.to_markdown(result))
        summary[shuttle] = {
            "records": result["records"],
            "outcomes": result["outcomes"],
        }
    write_json(report / "summary.json", summary)


def collect(rows, groups, count, commit, dest):
    if dest.exists() or count < 1 or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError(
            "expected fresh output, positive shard count and full source commit"
        )
    digest = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as scratch:
        report = Path(scratch) / "report"
        populate(report, rows)
        seen = set()
        jobs = []
        configs = set()
        for group in groups:
            for job, run, payload in fleet_runs(group):
                meta = read_json(payload / "shard.json")
                index, total = meta["shard"]
                if total != count or index in seen:
                    raise ValueError(
                        f"unexpected or duplicate logical shard {index}/{total}"
                    )
                if (run["commit"], meta["commit"], meta["catalog_sha256"]) != (
                    commit,
                    commit,
                    digest,
                ):
                    raise ValueError(f"source or catalog mismatch: {job['job']}")
                configs.add(
                    (
                        meta["timeout_s"],
                        meta["tool_version"],
                        run["input_sha256"]["source_tarball"],
                    )
                )
                copy_records(
                    payload, report / "runs", selected(rows, (index, count)), meta
                )
                seen.add(index)
                jobs.append({"job": job["job"], "shard": index, "run": run})
        if seen != set(range(count)) or len(configs) != 1:
            raise ValueError(
                "missing shards or inconsistent runtime, timeout or source archive"
            )
        write_json(report / "jobs.json", jobs)
        triage_reports(report, rows, count, commit, configs.pop()[0])
        report.rename(dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    budget = commands.add_parser("plan")
    budget.add_argument("--count", type=int, required=True)
    budget.add_argument("--timeout-s", type=int, required=True)
    budget.add_argument("--job-timeout-s", type=int, required=True)
    budget.add_argument("--overhead-s", type=int, required=True)
    run = commands.add_parser("run")
    run.add_argument("shard", help="zero-based I/N across the full pinned inventory")
    run.add_argument("--timeout-s", type=int, required=True)
    merge = commands.add_parser("collect")
    merge.add_argument("--count", type=int, required=True)
    merge.add_argument("--commit", required=True)
    merge.add_argument("--out", type=Path, required=True)
    merge.add_argument("groups", type=Path, nargs="+")
    args = parser.parse_args()
    rows = catalog()
    if args.command == "plan":
        print(
            json.dumps(
                plan(
                    rows,
                    args.count,
                    args.timeout_s,
                    args.job_timeout_s,
                    args.overhead_s,
                ),
                indent=2,
            )
        )
    elif args.command == "collect":
        collect(rows, args.groups, args.count, args.commit, args.out)
    else:
        match = re.fullmatch(r"([0-9]+)/([0-9]+)", args.shard)
        if not match:
            parser.error("expected zero-based shard I/N")
        run_shard(
            rows,
            (int(match[1]), int(match[2])),
            args.timeout_s,
            OUTPUT,
            PacedHttpTransport(),
        )


if __name__ == "__main__":
    main()

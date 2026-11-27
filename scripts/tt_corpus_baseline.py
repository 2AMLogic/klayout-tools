#!/usr/bin/env python3
"""Measure the pinned tt06 sample using the Phase 1 cache and klt synthesize.

From the repository root, with the snapshot's klt, Yosys (bundled yosys-abc)
and sky130A open_pdks revision installed and klt/yosys on PATH:

    python scripts/tt_corpus_fetch.py --shuttle tt06 --pdk sky130A \
        --index-file tests/corpus/tt06_sky130_sample.json
    python scripts/tt_corpus_baseline.py --output tests/corpus/tt_sky130_baseline.json

Both commands accept --cache-dir. The second reads only local inputs and uses
a fresh synthesis directory for each design. Compare its output across runs
with cmp. The shared pins apply to every row carrying an instance_count.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import yaml
from tt_corpus_triage import error_detail

SAMPLE = Path(__file__).resolve().parents[1] / "tests/corpus/tt06_sky130_sample.json"
PDK = "sky130A"
LIBRARY = "sky130_fd_sc_hd"
CORNER = "tt_025C_1v80"


def tool_builds() -> dict[str, str]:
    """Identify Yosys and the sibling ABC used by its default abc pass."""
    yosys = shutil.which("yosys")
    if not yosys:
        raise ValueError("yosys missing from PATH")
    abc = Path(yosys).resolve().with_name("yosys-abc")
    builds = {}
    for name, cmd in (("yosys", [yosys, "-V"]), ("abc", [str(abc), "-c", "version"])):
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode or not proc.stdout.strip():
            raise ValueError(f"could not identify {name} build")
        builds[name] = proc.stdout.strip().splitlines()[-1]
    builds["abc_sha256"] = hashlib.sha256(abc.read_bytes()).hexdigest()
    return builds


def read_member(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.getmember(name)
    if not member.isfile():
        raise ValueError(f"expected regular archive member: {name}")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"unreadable archive member: {name}")
    with stream:
        return stream.read()


def write_request(blob: Path, work: Path) -> str:
    """Read only the regular files named by the pinned project's info.yaml."""
    raw = blob.read_bytes()
    if hashlib.sha256(raw).hexdigest() != blob.name:
        raise ValueError("source archive sha256 mismatch")
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        infos = [
            name
            for name in archive.getnames()
            if re.fullmatch(r"[^/]+/info\.yaml", name)
        ]
        if len(infos) != 1:
            raise ValueError("expected one project info.yaml")
        info = infos[0]
        root = info.split("/")[0]
        project = yaml.safe_load(read_member(archive, info))["project"]
        top = project["top_module"]
        names = project["source_files"]
        if not isinstance(top, str) or not isinstance(names, list) or not names:
            raise ValueError("info.yaml must declare top_module and source_files")
        sources = []
        for name in names:
            if (
                not isinstance(name, str)
                or Path(name).is_absolute()
                or ".." in Path(name).parts
            ):
                raise ValueError(f"invalid source path: {name!r}")
            source = Path("src") / name
            contents = read_member(archive, f"{root}/{source.as_posix()}")
            destination = work / source
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)
            sources.append(source.as_posix())
    request = {
        "hdl_toplevel": top,
        "sources": sources,
        "pdk": {"cell_library": LIBRARY, "corner": CORNER},
    }
    (work / "request.json").write_text(json.dumps(request))
    return top


def synthesize(work: Path, top: str) -> dict[str, Any]:
    cmd = ["klt", "synthesize", "request.json", "--pdk", PDK, "--format", "json"]
    proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # klt writes its structured error envelope to stderr.
        reason = (
            error_detail(proc.stderr).replace(str(work) + "/", "") or "no JSON report"
        )
        raise ValueError(f"klt synthesize exit {proc.returncode}: {reason}") from None
    if not isinstance(report, dict):
        raise ValueError("synthesis report must be a JSON object")
    # A completed mapping with structural findings exits 3 and still has a count.
    if proc.returncode not in (0, 3) or report.get("status") != "ok":
        reason = report.get("reason") or report.get("status") or "incomplete mapping"
        raise ValueError(f"klt synthesize exit {proc.returncode}: {reason}")
    if report.get("hdl_toplevel") != top:
        raise ValueError("synthesis report has a different top module")
    count = report.get("instance_count")
    if type(count) is not int or count < 0:
        raise ValueError("missing or invalid whole-design instance_count")
    return report


def report_pins(report: dict[str, Any]) -> dict[str, Any]:
    prov = report["provenance"]
    pdk = prov["pdk"]
    deck = prov["deck"]
    if pdk["name"] != PDK or not pdk["version"]:
        raise ValueError("missing or unexpected PDK identity")
    if deck["name"] != f"{LIBRARY}__{CORNER}" or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", deck["content_hash"]
    ):
        raise ValueError("missing or unexpected Liberty identity")
    builds = tool_builds()
    if not prov["klt_version"] or builds["yosys"].split()[:2] != [
        "Yosys",
        report["engine_version"],
    ]:
        raise ValueError("missing or inconsistent synthesis tool identity")
    return {
        **builds,
        "klt_version": prov["klt_version"],
        "klt_git_commit": prov.get("klt_git_commit"),
        "pdk": {"name": pdk["name"], "version": pdk["version"]},
        "deck": {"name": deck["name"], "content_hash": deck["content_hash"]},
    }


def measure(cache: Path, shuttle: str, project: dict, row: dict) -> tuple[int, dict]:
    manifest_path = cache / "manifests" / shuttle / (project["macro"] + ".json")
    if not manifest_path.is_file():
        raise ValueError("missing Phase 1 manifest")
    manifest = json.loads(manifest_path.read_text())
    expected = {**project, "shuttle": shuttle, "pdk": PDK}
    if any(manifest[key] != value for key, value in expected.items()):
        raise ValueError("manifest identity does not match the pinned sample")
    row["license"] = manifest["license"]
    if manifest["excluded"]:
        raise ValueError(manifest["exclusion_reason"] or "excluded at ingest")
    if not row["license"]["spdx"]:
        raise ValueError("no license at the pinned commit")
    digest = manifest["artifacts"]["sources"]["sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid source archive sha256")
    row["source_sha256"] = digest
    blob = cache / "blobs" / digest
    if not blob.is_file():
        raise ValueError("missing Phase 1 source archive")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        top = write_request(blob, work)
        report = synthesize(work, top)
    return report["instance_count"], report_pins(report)


def run(cache: Path) -> dict:
    sample = json.loads(SAMPLE.read_text())
    rows = []
    pins = None
    for project in sample["projects"]:
        row = {**project, "license": None}
        rows.append(row)
        try:
            count, current = measure(cache, sample["id"], project, row)
            if pins is not None and current != pins:
                changed = ", ".join(key for key in current if current[key] != pins[key])
                raise ValueError(
                    f"synthesis pins differ from the first measured design: {changed}"
                )
            pins = current
            row["instance_count"] = count
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
            row["exclusion_reason"] = str(exc)
    measured = sum("instance_count" in row for row in rows)
    return {
        "schema_version": 1,
        "shuttle": sample["id"],
        "total": len(rows),
        "measured": measured,
        "excluded": len(rows) - measured,
        "pins": pins,
        "designs": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/tt-corpus"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.write_text(json.dumps(run(args.cache_dir), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

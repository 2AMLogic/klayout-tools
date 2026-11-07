#!/usr/bin/env python3
"""Tiny Tapeout corpus regression runner (Epic #520 Phase 1.B, issue #5082).

Runs ``klt drc``, ``klt lvs`` and ``klt extract`` over the designs that
``scripts/tt_corpus_fetch.py`` (Phase 1.A, #5081) cached, and writes **one
JSON record per (design, stage)**.

Record fields: ``outcome``, ``exit_code``, ``duration_s``, ``stderr_tail``,
``tool_version`` (plus ``macro``, ``stage``, ``command`` and, when the tool
printed a parsable JSON envelope, ``envelope_path``).

``outcome`` is one of:

* ``pass``     -- exit 0;
* ``findings`` -- non-zero exit with a parsable, non-error JSON envelope on
  stdout (e.g. DRC violations, an LVS mismatch): a result, not a tool failure;
* ``error``    -- non-zero exit with an error envelope or no parsable envelope;
* ``timeout``  -- the per-stage wall-clock budget expired (process killed);
* ``crash``    -- killed by a signal, or the tool could not be launched;
* ``skipped``  -- nothing to run (``reason`` says why: excluded design, no
  GDS artifact, no reference netlist for LVS, unknown PDK).

Nothing is dropped: a crash or timeout in one design never stops the run, and
every (design, stage) pair gets a record, skipped ones included.

Resumable: records live at ``<results-dir>/<shuttle>/<macro>/<stage>.json``;
a pair whose record already exists is skipped on rerun (``--retry`` re-runs
the non-``pass``/``findings`` ones, ``--force`` redoes everything).

LVS needs a reference netlist, which the corpus does not ship: supply
``--reference-dir`` holding ``<macro>.spice``; designs without one record
``skipped`` / ``no_reference_netlist`` for the lvs stage.

Usage::

    scripts/tt_corpus_run.py --shuttle ttgf0p2 --cache-dir .cache/tt-corpus
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
STAGES = ("drc", "lvs", "extract")
STDERR_TAIL_BYTES = 2048
DEFAULT_TIMEOUT_S = 600.0
# manifest ``pdk`` -> klt deck name
PDK_DECKS = {"sky130A": "sky130", "gf180mcuD": "gf180mcu", "ihp-sg13g2": "sg13g2"}
DONE_OUTCOMES = ("pass", "findings")

# runner(argv, timeout_s) -> (exit_code, stdout, stderr); raises
# subprocess.TimeoutExpired on timeout and OSError if it cannot launch.
Runner = Callable[[list[str], float], tuple[int, str, str]]


def subprocess_runner(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, timeout=timeout_s, errors="replace"
    )
    return proc.returncode, proc.stdout, proc.stderr


def tail(text: str, limit: int = STDERR_TAIL_BYTES) -> str:
    return text[-limit:]


def tool_version(klt: str, runner: Runner) -> str:
    try:
        code, out, err = runner([klt, "--version"], 60.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unknown ({exc})"
    return (out or err).strip() if code == 0 else "unknown"


def parse_envelope(stdout: str) -> dict[str, Any] | None:
    try:
        doc = json.loads(stdout)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def classify(code: int, envelope: dict[str, Any] | None) -> str:
    if code == 0:
        return "pass"
    if code < 0:
        return "crash"
    if envelope is not None and not envelope.get("error"):
        return "findings"
    return "error"


def stage_command(
    klt: str,
    stage: str,
    manifest: dict[str, Any],
    layout: Path,
    deck: str,
    reference: Path | None,
    work_dir: Path,
) -> list[str]:
    macro = manifest["macro"]
    if stage == "drc":
        return [klt, "drc", str(layout), "--deck", deck, "--top", macro]
    if stage == "extract":
        return [klt, "extract", str(layout), "--deck", deck, "--top", macro]
    request = {
        "layout": {"file": str(layout), "deck": deck, "top": macro},
        "reference": {"netlist": str(reference), "top": macro},
    }
    request_path = work_dir / f"{macro}.lvs-request.json"
    request_path.write_text(json.dumps(request, indent=2) + "\n")
    return [klt, "lvs", str(request_path)]


def run_stage(
    argv: list[str],
    *,
    timeout_s: float,
    runner: Runner,
    envelope_path: Path,
) -> dict[str, Any]:
    start = time.monotonic()
    result: dict[str, Any] = {"command": argv, "timeout_s": timeout_s}
    try:
        code, stdout, stderr = runner(argv + ["--format", "json"], timeout_s)
    except subprocess.TimeoutExpired as exc:
        err = exc.stderr
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        result.update(
            outcome="timeout",
            exit_code=None,
            stderr_tail=tail(err or f"timed out after {timeout_s}s"),
        )
    except Exception as exc:  # noqa: BLE001 -- one design must never stop the run
        result.update(
            outcome="crash",
            exit_code=None,
            stderr_tail=tail(f"{type(exc).__name__}: {exc}"),
        )
    else:
        envelope = parse_envelope(stdout)
        if envelope is not None:
            envelope_path.parent.mkdir(parents=True, exist_ok=True)
            envelope_path.write_text(json.dumps(envelope, indent=2) + "\n")
            result["envelope_path"] = envelope_path.name
        result.update(
            outcome=classify(code, envelope),
            exit_code=code,
            stderr_tail=tail(stderr),
        )
    result["duration_s"] = round(time.monotonic() - start, 3)
    return result


def resolve_layout(
    manifest: dict[str, Any], cache_dir: Path, work_dir: Path
) -> tuple[Path | None, str | None]:
    """Return ``(layout path, None)`` or ``(None, skip reason)``."""
    if manifest.get("excluded"):
        return None, f"excluded: {manifest.get('exclusion_reason')}"
    gds = (manifest.get("artifacts") or {}).get("gds")
    if not gds or "sha256" not in gds:
        return None, "no_gds"
    blob = cache_dir / "blobs" / gds["sha256"]
    if not blob.is_file():
        return None, "gds_blob_missing_from_cache"
    suffix = ".gds.gz" if blob.read_bytes()[:2] == b"\x1f\x8b" else ".gds"
    link = work_dir / f"{manifest['macro']}{suffix}"
    work_dir.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(blob.resolve())
    return link, None


def is_done(record_path: Path, *, retry: bool, force: bool) -> bool:
    if force or not record_path.is_file():
        return False
    try:
        outcome = json.loads(record_path.read_text()).get("outcome")
    except ValueError:
        return False  # truncated by a killed run: redo
    return outcome in DONE_OUTCOMES or (not retry and outcome is not None)


def run(
    *,
    cache_dir: Path,
    shuttle: str,
    results_dir: Path,
    stages: tuple[str, ...] = STAGES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    limit: int | None = None,
    reference_dir: Path | None = None,
    klt: str = "klt",
    retry: bool = False,
    force: bool = False,
    runner: Runner = subprocess_runner,
) -> dict[str, Any]:
    manifest_paths = sorted((cache_dir / "manifests" / shuttle).glob("*.json"))
    if limit is not None:
        manifest_paths = manifest_paths[:limit]
    version = tool_version(klt, runner)
    out_root = results_dir / shuttle
    records: list[dict[str, Any]] = []
    ran = resumed = 0
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text())
        macro = manifest["macro"]
        design_dir = out_root / macro
        work_dir = design_dir / "work"
        layout, skip = resolve_layout(manifest, cache_dir, work_dir)
        deck = PDK_DECKS.get(manifest.get("pdk") or "")
        if layout is not None and deck is None:
            layout, skip = None, f"no_klt_deck_for_pdk: {manifest.get('pdk')}"
        for stage in stages:
            record_path = design_dir / f"{stage}.json"
            if is_done(record_path, retry=retry, force=force):
                records.append(json.loads(record_path.read_text()))
                resumed += 1
                continue
            reference = reference_dir / f"{macro}.spice" if reference_dir else None
            reason = skip
            if (
                reason is None
                and stage == "lvs"
                and not (reference and reference.is_file())
            ):
                reason = "no_reference_netlist"
            base = {
                "schema_version": SCHEMA_VERSION,
                "macro": macro,
                "shuttle": shuttle,
                "pdk": manifest.get("pdk"),
                "stage": stage,
                "tool_version": version,
            }
            if reason is not None:
                record = {
                    **base,
                    "outcome": "skipped",
                    "reason": reason,
                    "exit_code": None,
                    "duration_s": 0.0,
                    "stderr_tail": "",
                }
            else:
                argv = stage_command(
                    klt, stage, manifest, layout, deck, reference, work_dir
                )
                record = {
                    **base,
                    **run_stage(
                        argv,
                        timeout_s=timeout_s,
                        runner=runner,
                        envelope_path=design_dir / f"{stage}.envelope.json",
                    ),
                }
                ran += 1
            design_dir.mkdir(parents=True, exist_ok=True)
            tmp = record_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
            tmp.replace(record_path)  # atomic: a killed run never leaves a half record
            records.append(record)
    counts: dict[str, dict[str, int]] = {}
    for rec in records:
        by_stage = counts.setdefault(rec["stage"], {})
        by_stage[rec["outcome"]] = by_stage.get(rec["outcome"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "shuttle": shuttle,
        "tool_version": version,
        "designs": len(manifest_paths),
        "stages": list(stages),
        "ran": ran,
        "resumed": resumed,
        "outcomes": counts,
        "records": records,
    }


def main(argv: list[str] | None = None, runner: Runner = subprocess_runner) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--shuttle", required=True)
    parser.add_argument("--cache-dir", default=".cache/tt-corpus", type=Path)
    parser.add_argument(
        "--results-dir",
        type=Path,
        help="default: <cache-dir>/runs",
    )
    parser.add_argument("--stages", default=",".join(STAGES))
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--reference-dir", type=Path)
    parser.add_argument("--klt", default="klt")
    parser.add_argument("--retry", action="store_true", help="redo non-pass/findings")
    parser.add_argument("--force", action="store_true", help="redo every pair")
    args = parser.parse_args(argv)

    stages = tuple(s for s in re.split(r"[,\s]+", args.stages) if s)
    bad = [s for s in stages if s not in STAGES]
    if bad or not stages:
        print(
            json.dumps(
                {"schema_version": SCHEMA_VERSION, "error": f"bad stages: {bad}"}
            )
        )
        return 2
    if not (args.cache_dir / "manifests" / args.shuttle).is_dir():
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "error": f"no manifests for shuttle {args.shuttle!r} under "
                    f"{args.cache_dir} -- run scripts/tt_corpus_fetch.py first",
                }
            )
        )
        return 2
    summary = run(
        cache_dir=args.cache_dir,
        shuttle=args.shuttle,
        results_dir=args.results_dir or args.cache_dir / "runs",
        stages=stages,
        timeout_s=args.timeout_s,
        limit=args.limit,
        reference_dir=args.reference_dir,
        klt=args.klt,
        retry=args.retry,
        force=args.force,
        runner=runner,
    )
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run and strictly verify the pinned Tiny Tapeout DRC CI sample.

Uses the existing fetch/runner CLIs serially. The runner intentionally records
failures without exiting nonzero; this CI adapter requires every expected design
to have a pass record AND a clean DRC envelope. No corpus content is vendored.

The fetch CLI also exits 0 when one layout download fails and records the error
in that design's manifest, so the adapter re-reads the manifests after the
fetch: a design left without a layout is re-fetched a bounded number of times
and otherwise reported as an infrastructure error, never as a DRC result.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLE = ROOT / "tests/corpus/tt_gf0p2_drc_sample.json"
MAX_FETCH_ATTEMPTS = 3
RETRY_DELAY_S = 5.0


def verify(sample: dict[str, Any], summary: dict[str, Any], results_dir: Path) -> None:
    expected = {(p["macro"], sample["id"], "drc") for p in sample["projects"]}
    records = summary["records"]
    actual = {(r["macro"], r["shuttle"], r["stage"]) for r in records}
    if not expected or actual != expected or len(records) != len(expected):
        raise ValueError("sample records are missing, duplicated, or unexpected")
    for record in records:
        print(
            f"{record['macro']}: {record['outcome']}, {record['duration_s']} s",
            flush=True,
        )
    for record in records:
        macro = record["macro"]
        if record["outcome"] != "pass" or record["exit_code"] != 0:
            raise ValueError(f"{macro}: DRC did not pass: {record}")
        envelope = json.loads(
            (results_dir / sample["id"] / macro / "drc.envelope.json").read_text()
        )
        if (
            envelope.get("error")
            or envelope.get("status") != "clean"
            or envelope.get("violation_count") != 0
        ):
            raise ValueError(f"{macro}: DRC envelope is not clean")


def _run(command: list[str], log: Path) -> None:
    with log.open("w") as stdout:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / command[0]), *command[1:]],
            stdout=stdout,
            check=True,
        )


def _missing_gds(sample: dict[str, Any], cache: Path) -> dict[str, str]:
    """``{macro: recorded reason}`` for sampled designs with no fetched layout."""
    missing = {}
    for project in sample["projects"]:
        macro = project["macro"]
        path = cache / "manifests" / sample["id"] / f"{macro}.json"
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            missing[macro] = "no readable manifest"
            continue
        gds = (manifest.get("artifacts") or {}).get("gds")
        if gds and "sha256" in gds:
            continue
        missing[macro] = (
            (gds or {}).get("error")
            or manifest.get("exclusion_reason")
            or "the fetch recorded no GDS"
        )
    return missing


def _describe(missing: dict[str, str]) -> str:
    return "; ".join(f"{macro}: {reason}" for macro, reason in missing.items())


def _fetch(
    sample: dict[str, Any], cache: Path, command: list[str], log: Path
) -> dict[str, str]:
    """Run the fetch until every sampled design has a layout; return those without."""
    missing = {}
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        _run(command, log)
        missing = _missing_gds(sample, cache)
        if not missing:
            break
        if attempt < MAX_FETCH_ATTEMPTS:
            print(
                f"fetch attempt {attempt} of {MAX_FETCH_ATTEMPTS} left no GDS: "
                f"{_describe(missing)}; retrying in {RETRY_DELAY_S * attempt:g} s",
                flush=True,
            )
            time.sleep(RETRY_DELAY_S * attempt)
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--output-dir", type=Path, default=Path(".cache/tt-drc-sample"))
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    sample = json.loads(args.sample.read_text())
    if not 1 <= len(sample["projects"]) <= 10:
        raise ValueError("sample must contain 1-10 designs")
    cache = output / "cache"
    results = output / "results"
    fetch_command = [
        "tt_corpus_fetch.py",
        "--index-file",
        str(args.sample.resolve()),
        "--shuttle",
        sample["id"],
        "--pdk",
        "gf180mcuD",
        "--cache-dir",
        str(cache),
        "--gds-url-template",
        sample["gds_url_template"],
    ]
    run_command = [
        "tt_corpus_run.py",
        "--shuttle",
        sample["id"],
        "--cache-dir",
        str(cache),
        "--results-dir",
        str(results),
        "--stages",
        "drc",
        "--timeout-s",
        "300",
        "--force",
        "--klt",
        str(Path(sys.executable).parent / "klt"),
    ]
    try:
        if missing := _fetch(sample, cache, fetch_command, output / "fetch.json"):
            print(
                "TT DRC sample failed: infrastructure error, not a DRC result: no GDS "
                f"after {MAX_FETCH_ATTEMPTS} fetch attempts: {_describe(missing)}",
                file=sys.stderr,
            )
            return 1
        _run(run_command, output / "run.json")
        verify(sample, json.loads((output / "run.json").read_text()), results)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        print(f"TT DRC sample failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

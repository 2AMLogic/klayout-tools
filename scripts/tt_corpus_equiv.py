#!/usr/bin/env python3
r"""Gate the pinned Tiny Tapeout instance-count distribution on formal equivalence.

    python scripts/tt_corpus_equiv.py --pairs pairs.json --liberty cells.lib \
        --output verdicts.json

The Phase 2.B pairs.json carries the baseline's pins and a designs array. Each
mapped row has macro, commit, source_sha256 and top. Both baseline and candidate
have a _netlist (path relative to pairs.json), _sha256 (hex) and _count (instances).
candidate_report points to the synthesis report measuring the candidate netlist.
An upstream failure carries exclusion_reason. The Phase 2.A baseline determines
the sample and its order. The corpus design kind selects the Yosys engine.
For other samples a row may specify engine (default yosys).

By default each available pair gets a fresh klt equiv proof. Pass --reuse-proofs
with an earlier verdicts.json to report from saved evidence only. Both modes
verify the measured bytes and pinned Liberty. The verdict rows expose the
ordered digests for the sweep, plus the final solver status and any witness.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from statistics import mean

from klayout_tools._digest import sha256_file_required
from klayout_tools.equiv import EquivError, run_equiv

BASELINE = Path(__file__).resolve().parents[1] / "tests/corpus/tt_sky130_baseline.json"
CORPUS_KINDS = BASELINE.parent / "synth_e2e_validation/results.json"
ENGINES = ("yosys", "yosys-sequential")


def prove(work: Path, top: str, engine: str) -> dict:
    """Run klt equiv's SAT or register-correspondence flow on private copies."""
    common = {"top": top, "liberty": str(work / "cells.lib")}
    request = {
        "engine": engine,
        "gold": {**common, "sources": [str(work / "baseline.v")]},
        "gate": {**common, "sources": [str(work / "candidate.v")]},
    }
    path = work / "equiv.json"
    path.write_text(json.dumps(request))
    report = run_equiv(str(path))
    return {
        key: report[key]
        for key in (
            "engine",
            "engine_version",
            "status",
            "diagnostics",
            "counterexample",
        )
        if key in report
    }


def index_rows(rows: list[dict]) -> dict:
    """An ambiguous input is an exclusion for its design."""
    result = {}
    for row in rows:
        macro = row["macro"]
        if macro in result:
            row = {
                "macro": macro,
                "commit": row.get("commit"),
                "exclusion_reason": f"duplicate rows for {macro}",
            }
        result[macro] = row
    return result


def checked_inputs(
    work: Path, root: Path, pair: dict, liberty: Path, deck: str
) -> dict:
    """Snapshot and hash the very files Yosys will open, in gold/gate order."""
    identity = {"macro": pair["macro"], "commit": pair["commit"], "top": pair["top"]}
    for side in ("baseline", "candidate"):
        copy = work / f"{side}.v"
        shutil.copyfile(root / pair[f"{side}_netlist"], copy)
        key = f"{side}_sha256"
        identity[key] = sha256_file_required(str(copy))
        if identity[key] != pair.get(key):
            raise ValueError(f"{side} netlist differs from its synthesis digest")
    cells = work / "cells.lib"
    shutil.copyfile(liberty, cells)
    identity["liberty_sha256"] = sha256_file_required(str(cells))
    if "sha256:" + identity["liberty_sha256"] != deck:
        raise ValueError("Liberty differs from the pinned baseline deck")
    return identity


def saved_proof(saved: dict | None, identity: dict) -> dict:
    if not saved or not isinstance(saved.get("proof"), dict):
        reason = (saved or {}).get("exclusion_reason")
        raise ValueError(reason or "missing formal proof for this design")
    if any(saved.get(key) != value for key, value in identity.items()):
        raise ValueError("stale formal proof for a different pair, top or Liberty")
    return saved["proof"]


def classify(proof: dict, engine: str) -> tuple[str, str | None]:
    if engine not in ENGINES or proof.get("engine") != engine:
        return "unestablished", "missing or unsupported formal engine"
    status = proof.get("status")
    if status == "equivalent":
        return "equivalent", None
    messages = [
        d["message"]
        for d in proof.get("diagnostics", [])
        if isinstance(d, dict) and isinstance(d.get("message"), str)
    ]
    if status == "counterexample":
        return "not-equivalent", "; ".join(["Yosys counterexample", *messages])
    reason = "; ".join(messages) or f"formal status: {status or 'missing'}"
    return "unestablished", reason


def admit_counts(row: dict, pair: dict, root: Path) -> None:
    before, after = row["baseline_count"], pair.get("candidate_count")
    if not all(
        type(n) is int and n >= 0 for n in (before, after, pair.get("baseline_count"))
    ):
        raise ValueError("missing or invalid instance count")
    if before != pair["baseline_count"]:
        raise ValueError("baseline instance count differs from the pinned measurement")
    path = pair.get("candidate_report")
    if not isinstance(path, str) or not path:
        raise ValueError("missing candidate synthesis report path")
    report = json.loads((root / path).read_text())
    if (
        not isinstance(report, dict)
        or report.get("netlist_sha256") != row["candidate_sha256"]
    ):
        raise ValueError("candidate synthesis report does not match the proven netlist")
    measured = report.get("instance_count")
    if type(measured) is not int or measured != after:
        raise ValueError(
            f"candidate instance count {after} differs from measured count {measured!r}"
        )
    if before == 0:
        raise ValueError("percentage undefined for a zero baseline")
    row.update(candidate_count=after, reduction_pct=100 * (before - after) / before)


def check_design(
    base: dict,
    pair: dict,
    root: Path,
    liberty: Path,
    deck: str | None,
    engine: str,
    saved: dict | None,
) -> dict:
    row = {
        "macro": base["macro"],
        "commit": base["commit"],
        "baseline_count": base.get("instance_count"),
        "verdict": "unestablished",
    }
    reason = base.get("exclusion_reason") or pair.get("exclusion_reason")
    if reason:
        return {**row, "exclusion_reason": reason}
    try:
        if not pair:
            raise ValueError("missing mapped pair")
        if pair["commit"] != base["commit"] or pair["top"] != base.get(
            "top", base["macro"]
        ):
            raise ValueError("pair source commit or top differs from pinned design")
        if pair.get("source_sha256") != base.get("source_sha256"):
            raise ValueError("pair source archive digest differs from pinned design")
        if not deck:
            raise ValueError("missing or mismatched synthesis pins")
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            identity = checked_inputs(work, root, pair, liberty, deck)
            row.update(identity)
            proof = (
                prove(work, pair["top"], engine)
                if saved is None
                else saved_proof(saved.get(base["macro"]), identity)
            )
        row["proof"] = proof
        row["verdict"], reason = classify(proof, engine)
        if reason:
            row["exclusion_reason"] = reason
        else:
            admit_counts(row, pair, root)
    except (EquivError, OSError, KeyError, TypeError, ValueError) as exc:
        row["exclusion_reason"] = str(exc)
    return row


def run(
    baseline: dict, pairs_path: Path, liberty: Path, saved: dict | None = None
) -> dict:
    pairs = json.loads(pairs_path.read_text())
    mapped = index_rows(pairs["designs"])
    pins = baseline.get("pins")
    deck = None
    if pins and pins == pairs.get("pins"):
        deck = (pins.get("deck") or {}).get("content_hash")
    kinds = {row["design"]: row["kind"] for row in json.loads(CORPUS_KINDS.read_text())}
    rows = []
    for macro, base in index_rows(baseline["designs"]).items():
        pair = mapped.get(macro, {})
        default_engine = (
            "yosys-sequential" if kinds.get(macro) == "sequential" else "yosys"
        )
        rows.append(
            check_design(
                base,
                pair,
                pairs_path.parent,
                liberty,
                deck,
                pair.get("engine", default_engine),
                saved,
            )
        )
    reductions = [row["reduction_pct"] for row in rows if "reduction_pct" in row]
    included = len(reductions)
    excluded = len(rows) - included
    average = mean(reductions) if included else None
    value = f"{average:g}%" if average is not None else "unavailable"
    return {
        "schema_version": 1,
        "designs": rows,
        "included": included,
        "excluded": excluded,
        "mean_reduction_pct": average,
        "headline": f"mean {value} across {included} designs, {excluded} excluded",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--liberty", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--reuse-proofs",
        type=Path,
        help="gate report to check against the current pair bytes",
    )
    args = parser.parse_args(argv)
    saved = None
    if args.reuse_proofs is not None:
        try:
            saved = index_rows(json.loads(args.reuse_proofs.read_text())["designs"])
        except FileNotFoundError:
            saved = {}
    report = run(json.loads(args.baseline.read_text()), args.pairs, args.liberty, saved)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as tmp:
        staged = Path(tmp) / "report.json"
        staged.write_text(json.dumps(report, indent=2) + "\n")
        staged.replace(args.output)
    print(report["headline"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

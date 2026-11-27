"""Formal admission and per-design accounting for the Tiny Tapeout benchmark."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/corpus/synth_e2e_validation"


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import tt_corpus_equiv

    return tt_corpus_equiv


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_report(netlist, count):
    report = netlist.with_suffix(".report.json")
    report.write_text(
        json.dumps({"netlist_sha256": digest(netlist), "instance_count": count})
    )
    return report.name


def inputs(tmp_path, counts):
    lib = tmp_path / "cells.lib"
    lib.write_text("fixture liberty")
    pins = {"deck": {"content_hash": "sha256:" + digest(lib)}}
    baseline, pairs = [], []
    for i, (before, after) in enumerate(counts):
        top = f"design_{i}"
        identity = {
            "macro": top,
            "commit": f"{i:040x}",
            "source_sha256": f"{i + 1:064x}",
        }
        baseline.append({**identity, "instance_count": before})
        row = {
            **identity,
            "top": top,
            "baseline_count": before,
            "candidate_count": after,
        }
        for side in ("baseline", "candidate"):
            path = tmp_path / f"{top}_{side}.v"
            path.write_text(f"// {side}\nmodule {top}; endmodule\n")
            row[f"{side}_netlist"] = path.name
            row[f"{side}_sha256"] = digest(path)
        row["candidate_report"] = candidate_report(
            tmp_path / row["candidate_netlist"], after
        )
        pairs.append(row)
    return {"pins": pins, "designs": baseline}, {"pins": pins, "designs": pairs}, lib


def cli(gate, tmp, baseline, pairs, lib, *, saved=None):
    args = []
    for name, data in (
        ("baseline", baseline),
        ("pairs", pairs),
        ("reuse-proofs", saved),
    ):
        if data is not None:
            if isinstance(data, Path):
                path = data
            else:
                path = tmp / f"{name}.json"
                path.write_text(json.dumps(data))
            args.extend([f"--{name}", str(path)])
    output = tmp / "results.json"
    assert gate.main([*args, "--liberty", str(lib), "--output", str(output)]) == 0
    return json.loads(output.read_text())


def test_headline_counts_each_design_and_preserves_witness(
    gate, tmp_path, monkeypatch, capsys
):
    baseline, pairs, lib = inputs(tmp_path, [(100, 80), (20, 22), (50, 0), (80, 0)])
    outcomes = iter(
        [
            {"status": "equivalent"},
            {"status": "equivalent"},
            {
                "status": "counterexample",
                "counterexample": {"diverging_outputs": ["y"]},
            },
            {
                "status": "inconclusive",
                "diagnostics": [{"code": "solver_timeout", "message": "timed out"}],
            },
        ]
    )

    def proof(request):
        req = json.loads(Path(request).read_text())
        assert req["gold"]["top"] == req["gate"]["top"]
        assert req["gold"]["liberty"] == req["gate"]["liberty"]
        assert Path(req["gold"]["liberty"]).read_bytes() == lib.read_bytes()
        return {"engine": req["engine"], **next(outcomes)}

    monkeypatch.setattr(gate, "run_equiv", proof)
    report = cli(gate, tmp_path, baseline, pairs, lib)
    assert (
        capsys.readouterr().out.strip()
        == report["headline"]
        == "mean 5% across 2 designs, 2 excluded"
    )
    a, b, c, d = report["designs"]
    assert [a["reduction_pct"], b["reduction_pct"]] == [20, -10]
    assert (
        c["verdict"] == "not-equivalent" and "counterexample" in c["exclusion_reason"]
    )
    assert c["proof"]["counterexample"]["diverging_outputs"] == ["y"]
    assert d["verdict"] == "unestablished" and "timed out" in d["exclusion_reason"]
    assert all("reduction_pct" not in row for row in (c, d))


def test_exclusions_survive_missing_inputs_and_unproven_induction(
    gate, tmp_path, monkeypatch
):
    baseline, pairs, lib = inputs(tmp_path, [(12, 12)] * 5)
    first, _, third, fourth, fifth = pairs["designs"]
    (tmp_path / first["candidate_netlist"]).unlink()
    baseline["designs"][1]["exclusion_reason"] = "source archive unavailable"
    third["exclusion_reason"] = "candidate synthesis failed"
    fifth["engine"] = "yosys-sequential"
    pairs["designs"].remove(fourth)
    monkeypatch.setattr(
        gate,
        "run_equiv",
        lambda req: {
            "engine": "yosys-sequential",
            "status": "inconclusive",
            "diagnostics": [{"message": "induction bound reached"}],
        },
    )
    result = cli(gate, tmp_path, baseline, pairs, lib)
    assert result["headline"] == "mean unavailable across 0 designs, 5 excluded"
    rows = result["designs"]
    assert all(row["verdict"] == "unestablished" for row in rows)
    assert [r["macro"] for r in rows] == [r["macro"] for r in baseline["designs"]]
    reasons = [r["exclusion_reason"] for r in rows]
    assert first["candidate_netlist"] in reasons[0]
    assert reasons[1:3] == ["source archive unavailable", "candidate synthesis failed"]
    assert "missing" in reasons[3] and "induction bound" in reasons[4]


def test_zero_and_unusable_counts_keep_proof_verdict(gate, tmp_path, monkeypatch):
    baseline, pairs, lib = inputs(tmp_path, [(20, 20), (0, 0), (5, None)])
    monkeypatch.setattr(
        gate, "run_equiv", lambda req: {"engine": "yosys", "status": "equivalent"}
    )
    report = cli(gate, tmp_path, baseline, pairs, lib)
    assert report["headline"] == "mean 0% across 1 designs, 2 excluded"
    a, b, c = report["designs"]
    assert a["reduction_pct"] == 0
    assert b["verdict"] == c["verdict"] == "equivalent"
    assert "zero" in b["exclusion_reason"] and "count" in c["exclusion_reason"]


def test_candidate_count_requires_matching_synthesis_evidence(
    gate, tmp_path, monkeypatch
):
    baseline, pairs, lib = inputs(tmp_path, [(38, 39)] * 3)
    monkeypatch.setattr(
        gate, "run_equiv", lambda req: {"engine": "yosys", "status": "equivalent"}
    )
    saved = cli(gate, tmp_path, baseline, pairs, lib)
    assert saved["included"] == 3
    wrong_count, wrong_digest, missing = pairs["designs"]
    wrong_count["candidate_count"] = 0
    # Counts agree, but this report measures another candidate.
    wrong_digest["candidate_report"] = wrong_count["candidate_report"]
    (tmp_path / missing["candidate_report"]).unlink()
    for evidence in (None, saved):
        report = cli(gate, tmp_path, baseline, pairs, lib, saved=evidence)
        assert report["headline"] == "mean unavailable across 0 designs, 3 excluded"
        for row, reason in zip(
            report["designs"],
            ["count", "netlist", missing["candidate_report"]],
            strict=True,
        ):
            assert row["verdict"] == "equivalent"
            assert row["proof"]["status"] == "equivalent"
            assert "reduction_pct" not in row
            assert reason in row["exclusion_reason"]


def test_conflicting_source_archive_excludes_fresh_and_replayed_proofs(
    gate, tmp_path, monkeypatch
):
    baseline, pairs, lib = inputs(tmp_path, [(38, 39)])
    monkeypatch.setattr(
        gate, "run_equiv", lambda req: {"engine": "yosys", "status": "equivalent"}
    )
    saved = cli(gate, tmp_path, baseline, pairs, lib)
    assert saved["included"] == 1
    pairs["designs"][0]["source_sha256"] = "0" * 64
    monkeypatch.setattr(
        gate, "run_equiv", lambda req: pytest.fail("unexpected fresh proof")
    )
    for evidence in (None, saved):
        report = cli(gate, tmp_path, baseline, pairs, lib, saved=evidence)
        assert report["headline"] == "mean unavailable across 0 designs, 1 excluded"
        row = report["designs"][0]
        assert row["verdict"] == "unestablished" and "proof" not in row
        assert "source archive" in row["exclusion_reason"]


@pytest.mark.parametrize(
    "top,engine", [("tt_um_ALU", "yosys"), ("tt_um_LFSR_shivam", "yosys-sequential")]
)
def test_mapped_tt06_pair_through_yosys(gate, tmp_path, monkeypatch, top, engine):
    from klayout_tools.pdk_cells import resolve_liberty_for_cell_library
    from klayout_tools.synthesize import run_synthesize

    if not shutil.which("yosys"):
        pytest.skip("needs Yosys")
    try:
        liberty, _, _ = resolve_liberty_for_cell_library(
            "sky130_fd_sc_hd", "tt_025C_1v80", ValueError, variant="sky130A"
        )
    except ValueError as exc:
        pytest.skip(str(exc))
    monkeypatch.syspath_prepend(str(CORPUS))
    import run_validation

    lib = Path(liberty)
    sources = [str(p) for p in sorted((CORPUS / "sources" / top).glob("*.v"))]
    req = tmp_path / "synth.json"
    req.write_text(
        json.dumps(
            {
                "hdl_toplevel": top,
                "sources": sources,
                "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
            }
        )
    )
    synth = run_synthesize(str(req), pdk_variant="sky130A")
    gold = run_validation.synth_netlist_path(tmp_path, top, synth)
    if engine == "yosys-sequential":
        # Independently check this baseline against the attributed RTL.
        monkeypatch.setattr(run_validation, "LIB", str(lib))
        proven, log = run_validation.seq_equiv_check(
            tmp_path, top, str(gold), 8, sources
        )
        assert proven, log

    candidate = tmp_path / "candidate.v"
    # Route one driven wrapper bit through an extra functional sky130 buffer.
    driver = "assign uio_out = "
    mapped = gold.read_text()
    assert mapped.count(driver) == 1
    candidate.write_text(
        mapped.replace(
            driver,
            "wire [7:0] routed;\n"
            "  sky130_fd_sc_hd__buf_1 added (.A(routed[0]), .X(uio_out[0]));\n"
            "  assign uio_out[7:1] = routed[7:1];\n  assign routed = ",
        )
    )

    pins = {"deck": {"content_hash": synth["provenance"]["deck"]["content_hash"]}}
    count = synth["instance_count"]
    snapshot = json.loads(gate.BASELINE.read_text())
    row = next(design for design in snapshot["designs"] if design["macro"] == top)
    baseline = {"pins": pins, "designs": [{**row, "instance_count": count}]}
    pair = {
        **row,
        "top": top,
        "baseline_count": count,
        "candidate_count": count + 1,
        "baseline_netlist": str(gold),
        "candidate_netlist": candidate.name,
        "baseline_sha256": synth["netlist_sha256"],
        "candidate_sha256": digest(candidate),
        "candidate_report": candidate_report(candidate, count + 1),
    }
    pairs = {"pins": pins, "designs": [pair]}
    saved = cli(gate, tmp_path, baseline, pairs, lib)
    passing = saved["designs"][0]
    assert passing["verdict"] == "equivalent", passing
    assert passing["proof"]["engine"] == engine
    assert saved["included"] == 1
    assert (
        cli(gate, tmp_path, baseline, pairs, lib, saved=saved)["headline"]
        == saved["headline"]
    )
    if engine == "yosys":
        # uio_out is a driven, constant TT wrapper output. Invert it after mapping.
        candidate.write_text(
            candidate.read_text().replace(".A(routed[0])", ".A(~routed[0])")
        )
        pair["candidate_sha256"] = digest(candidate)
        stale = cli(gate, tmp_path, baseline, pairs, lib, saved=saved)["designs"][0]
        assert (
            stale["verdict"] == "unestablished" and "stale" in stale["exclusion_reason"]
        )
        broken = cli(gate, tmp_path, baseline, pairs, lib)
        assert broken["headline"] == "mean unavailable across 0 designs, 1 excluded"
        rejected = broken["designs"][0]
        assert rejected["verdict"] == "not-equivalent", rejected
        assert "uio_out" in rejected["proof"]["counterexample"]["diverging_outputs"]


def test_report_only_rechecks_digests_and_requires_formal_evidence(
    gate, tmp_path, monkeypatch
):
    baseline, pairs, lib = inputs(tmp_path, [(100, 90)] * 6)
    monkeypatch.setattr(
        gate, "run_equiv", lambda req: {"engine": "yosys", "status": "equivalent"}
    )
    saved = cli(gate, tmp_path, baseline, pairs, lib)
    monkeypatch.setattr(
        gate, "run_equiv", lambda req: pytest.fail("unexpected fresh proof")
    )
    # Replace a candidate while keeping its original measurement. Replace a gold
    # *and* its measurement while retaining the proof for the previous pair.
    a, b, _, _, e, _ = pairs["designs"]
    (tmp_path / a["candidate_netlist"]).write_text("different candidate")
    path = tmp_path / b["baseline_netlist"]
    path.write_text("different baseline")
    b["baseline_sha256"] = digest(path)
    saved["designs"][2].pop("proof")
    saved["designs"][3]["proof"] = {"engine": "simulation", "status": "equivalent"}
    pairs["designs"].append(e.copy())
    saved["designs"].append(saved["designs"][5].copy())
    report = cli(gate, tmp_path, baseline, pairs, lib, saved=saved)
    assert report["headline"] == "mean unavailable across 0 designs, 6 excluded"
    assert all(r["verdict"] == "unestablished" for r in report["designs"])
    for row, reason in zip(
        report["designs"],
        ["digest", "stale", "missing", "engine", "duplicate", "duplicate"],
        strict=True,
    ):
        assert reason in row["exclusion_reason"]


def test_changed_liberty_and_source_pin_cannot_admit_results(
    gate, tmp_path, monkeypatch
):
    baseline, pairs, lib = inputs(tmp_path, [(100, 99), (20, 18)])
    monkeypatch.setattr(
        gate, "run_equiv", lambda req: pytest.fail("unexpected fresh proof")
    )
    pairs["designs"][0]["commit"] = "a" * 40
    lib.write_text("another corner")
    report = cli(gate, tmp_path, baseline, pairs, lib)
    reasons = [r["exclusion_reason"] for r in report["designs"]]
    assert "commit" in reasons[0] and "Liberty" in reasons[1]
    assert report["excluded"] == 2


def test_absent_proof_file_excludes_each_pinned_row(gate, tmp_path):
    baseline, pairs, lib = inputs(tmp_path, [(5, 3)])
    report = cli(gate, tmp_path, baseline, pairs, lib, saved=tmp_path / "absent.json")
    assert report["headline"] == "mean unavailable across 0 designs, 1 excluded"
    assert "missing formal proof" in report["designs"][0]["exclusion_reason"]

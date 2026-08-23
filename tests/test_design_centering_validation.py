"""Guards the committed evidence bundle for issue #1327 (Epic #705 Phase 2):
validate that `klt size`'s design-centering/yield-aware objective (#1326)
measurably improves yield/margin on a real canary, not just that it runs.

`examples/kb/pfd-charge-pump-tri-state/design-centering-validation/` carries
the full pipeline's real output -- `klt yield-campaign` (baseline) -> a
bespoke sensitivity harness -> `klt yield-sensitivity` -> yield-aware `klt
size` -> the re-centered netlist -> `klt yield-campaign` (re-centered) --
committed as evidence, per that directory's own README.md. Regenerating it
needs `ngspice` plus a real sky130A ngspice model library and takes
~15-20 minutes wall clock (`docs/cli/size.md`'s "Performance" section
explains why -- the same one-invocation-per-sample library-parse cost the
canary-reproduction tests in `tests/test_size.py` already pay), so this
module does not re-run it; it guards the *committed* numbers stay internally
consistent (schema-shape and the specific measured-improvement claim
`README.md` narrates), the same "guard the finding" pattern
`tests/test_size.py::test_ldo_error_amp_reference_is_behavioral_not_transistor_level`
already uses for its own canary finding. No native extension or engine is
needed -- these are plain JSON structure/consistency checks that always run.
"""

from __future__ import annotations

import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DIR = os.path.join(REPO_ROOT, "examples", "kb", "pfd-charge-pump-tri-state")
VALIDATION_DIR = os.path.join(KB_DIR, "design-centering-validation")


def _load(name: str) -> dict:
    with open(os.path.join(VALIDATION_DIR, name)) as f:
        return json.load(f)


def test_summary_reports_both_baseline_and_recentered_yield():
    """Acceptance criterion: "Measured yield or worst-case margin is
    reported for both the un-centered baseline and the re-centered
    result"."""
    summary = _load("summary.json")
    assert summary["design_centering_applied"] is True
    for key in ("baseline_yield", "recentered_yield"):
        entry = summary[key]
        assert entry["n"] >= 2
        assert 0.0 <= entry["empirical_estimate"] <= 1.0
        assert entry["empirical_ci"]["low"] <= entry["empirical_ci"]["high"]


def test_recentered_result_shows_a_measured_improvement():
    """Acceptance criterion: "The re-centered result shows a measured
    improvement, or the run documents why it does not" -- this canary shows
    a real, partial improvement (see README.md's two honest caveats), not a
    full pass against `target_yield`, so both directions are asserted
    explicitly rather than only checking `status`."""
    summary = _load("summary.json")
    baseline = summary["baseline_yield"]
    recentered = summary["recentered_yield"]

    # The measured improvement: higher empirical yield, tighter spread,
    # better capability -- all in the direction Pelgrom's law predicts for
    # a device-area increase.
    assert recentered["empirical_estimate"] > baseline["empirical_estimate"]
    assert recentered["stddev_pct"] < baseline["stddev_pct"]
    assert recentered["cpk"] > baseline["cpk"]
    assert recentered["sigma_to_spec"] > baseline["sigma_to_spec"]

    # The documented limitation: neither run clears the declared
    # target_yield (0.9), and the systematic mean offset barely moves --
    # growing `mult` addresses random (Pelgrom) mismatch, not the
    # Vds-mismatch bias `charge_pump.sizing.json` already documents.
    assert baseline["status"] == "fail"
    assert recentered["status"] == "fail"
    assert abs(recentered["mean_pct"] - baseline["mean_pct"]) < 1.0


def test_design_centering_result_grew_the_flagged_instance():
    result = _load("design-centering-result.json")
    dc = result["design_centering"]
    assert dc["requested"] is True
    assert dc["applied"] is True
    assert dc["grown"]["device"]["mult_before"] == 1

    summary = _load("summary.json")
    assert dc["grown"]["device"]["mult_after"] == summary["mult_after"]


def test_recentered_netlist_grows_both_mirror_devices_by_the_same_multiplier():
    """`klt size`'s design-centering mode grows only the device it sized
    (`MDN`) -- applying that multiplier to the real charge-pump circuit
    also requires growing `MDN`'s bias-diode counterpart (`MBN`) by the
    same amount, or the current mirror's 1:1 ratio breaks (see
    `run_validation.py`'s "Applying the multiplier to the real circuit").
    This guards that both devices were actually grown, together, by the
    exact suggested multiplier."""
    summary = _load("summary.json")
    mult_after = summary["mult_after"]
    assert summary["dominant_leg"] == "down"

    recentered = open(os.path.join(KB_DIR, "charge_pump_recentered.spice")).read()
    for instance in ("XMBN", "XMDN"):
        match = re.search(rf"^{instance}\s.*\bmult=(\S+)", recentered, re.M)
        assert match, f"could not find {instance} in charge_pump_recentered.spice"
        assert float(match.group(1)) == mult_after

    # The UP leg (not flagged as dominant) is untouched.
    for instance in ("XMBP", "XMUP"):
        match = re.search(rf"^{instance}\s.*\bmult=(\S+)", recentered, re.M)
        assert match and float(match.group(1)) == 1.0


def test_sensitivity_ranking_flagged_the_grown_leg_as_dominant():
    ranking_report = _load("sensitivity-ranking.json")
    ranking = ranking_report["measurements"][0]["ranking"]
    summary = _load("summary.json")
    top_parameter = ranking[0]["parameter"]
    assert f"_{summary['dominant_leg']}_" in top_parameter
    assert [r["parameter"] for r in ranking[:2]] == summary[
        "sensitivity_top_parameters"
    ]


def test_no_absolute_worktree_paths_leaked_into_committed_evidence():
    """A committed evidence JSON carrying an absolute
    `/Users/.../worktrees/issue-1327/...` path (from an un-normalized
    `--out-dir`) would not reproduce across checkouts -- guard against that
    regressing silently."""
    for name in (
        "summary.json",
        "baseline-yield-report.json",
        "recentered-yield-report.json",
        "sensitivity-ranking.json",
        "design-centering-result.json",
    ):
        text = open(os.path.join(VALIDATION_DIR, name)).read()
        assert "/Users/" not in text, f"{name} leaks an absolute host path"
        assert "worktrees" not in text, f"{name} leaks a worktree-scoped path"

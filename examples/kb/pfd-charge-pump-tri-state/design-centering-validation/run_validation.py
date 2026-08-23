#!/usr/bin/env python3
"""Epic #705 Phase 2 (issue #1327): validate that `klt size`'s design-
centering/yield-aware objective (issue #1326, `request.design_centering`)
actually *improves* measured yield/margin, not just that it runs.

Runs the full pipeline the issue describes -- `klt yield-campaign` -> a
sensitivity ranking -> yield-aware `klt size` -- against the PLL charge-pump
canary (`kb/entries/pfd-charge-pump-tri-state`, the second of the two
canaries issue #1325 reproduced), then re-measures yield on the re-centered
result and reports both, honestly.

Pipeline
--------
1. **Baseline** `klt yield-campaign` -- real sky130 `tt_mm` mismatch-corner
   Monte Carlo on the as-authored charge pump (`charge_pump.spice`, 8/1um,
   `mult=1` both legs), measuring UP-vs-DOWN current mismatch
   (`i_mismatch_pct`, limits +/-3%, `target_yield=0.9` -- the KB entry's own
   "closely matched UP/DOWN currents" sizing_approach turned into a spec).
2. **Sensitivity harness** (bespoke, real ngspice runs -- see "Why a
   bespoke harness" below) -- ranks four Pelgrom mismatch mechanisms (Vth
   and beta/width mismatch, each leg) by their contribution to
   `i_mismatch_pct`'s spread, via `klt yield-sensitivity`.
3. **Yield-aware `klt size`** -- single-device mode, `request.
   design_centering` set from step 2's ranking, sizing whichever leg's
   device the ranking flagged as dominant. Grows that device's `mult`.
4. **Apply to the real circuit** -- the suggested multiplier is applied to
   *both* the grown device and its bias-diode counterpart (see "Applying
   the multiplier to the real circuit" below), producing
   `charge_pump_recentered.spice`.
5. **Re-measured** `klt yield-campaign` on the re-centered netlist -- the
   "after" measurement, same spec/seed/n as step 1.
6. Report baseline vs. after.

Why a bespoke harness (not `klt sim`'s own Monte Carlo)
---------------------------------------------------------
`docs/cli/yield-sensitivity.md`'s "Why not a `klt sim` report" section
states plainly that today's `klt sim` mismatch-corner Monte Carlo does not
expose the individual per-device parameter draws it samples internally --
sky130's own mismatch-enabled model sections resolve each instance's
Vth/beta perturbation inline, with no printable per-instance delta -- so
there is no way to read a `(parameters, output)` pair for `klt
yield-sensitivity` out of `klt sim`/`klt yield-campaign` directly. The doc's
own prescribed fallback: "the caller ... is responsible for recording each
sample's parameter values alongside its output value" via a one-off
harness. This script is that harness: it explicitly *injects* a controlled
Vth-mismatch-equivalent gate offset and a controlled beta/width
perturbation on each leg's output device (MUP, MDN) only, one per sample,
via a modified copy of the charge-pump netlist
(`_sensitivity_variant_spice`) whose defaults (`vos=0`, `w=8um`) reproduce
`charge_pump.spice` exactly -- the injection is never active in the
baseline/re-centered mismatch campaigns (steps 1/5), which run the real
`charge_pump.spice`/`charge_pump_recentered.spice` files unmodified.

Sigma values (Vth mismatch 5 mV, beta/width mismatch 2%, both legs,
independent) are illustrative, order-of-magnitude Pelgrom-shaped
perturbations -- not sky130's own characterized AVT/beta-mismatch
constants (deriving those from the vendored model deck is out of scope
here) -- chosen only to exercise the ranking/design-centering pipeline
meaningfully. This mirrors `klt design-centering`'s own documented caveat
that its area-multiplier heuristic is "first-pass, order-of-magnitude", not
a rigorous re-optimization (`docs/cli/design-centering.md`, "What is
computed").

Applying the multiplier to the real circuit
--------------------------------------------
`klt size`'s design-centering mode grows *only* the device it was asked to
size -- it has no notion of that device's bias-diode counterpart, since
single-device mode has exactly one device. Naively growing e.g. MDN's
`mult` alone without also growing MBN would break the current mirror's 1:1
ratio (an `mult`-times current *bug*, not an improvement -- MDN's current
would scale with its own `mult` while MBN's bias current does not). This
script grows *both* devices in the dominant leg by the same suggested
multiplier, preserving the mirror ratio while averaging down each device's
own random mismatch -- the standard way paralleling unit devices improves
current-mirror matching.

Run from this directory:

    uv run python3 run_validation.py
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import subprocess
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_DIR))))
_KB_DIR = os.path.dirname(_DIR)
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from klayout_tools.pdk import find_pdk  # noqa: E402

_OP_VALUE_RE = re.compile(r"^@\S+\[(\w+)\]\s*=\s*([-+0-9.eEgGnN]+)\s*$")

_SIGMA_VTH_MV = 5.0
_SIGMA_BETA_PCT = 2.0
_N_SENSITIVITY_SAMPLES = 24
_SENSITIVITY_SEED = 20260822

_N_CAMPAIGN = 32
_CAMPAIGN_SEED = 20260822
_CAMPAIGN_MAX_WORKERS = 8
_CAMPAIGN_TIMEOUT_S = 240

_LIMITS = {"min": -3.0, "max": 3.0, "target_yield": 0.9}


def _sky130_ngspice_lib() -> str:
    resolution = find_pdk(variant="sky130A")
    return os.path.join(
        resolution["root"], "sky130A", "libs.tech", "ngspice", "sky130.lib.spice"
    )


_LIB = _sky130_ngspice_lib()


def _run_klt(*args: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "klayout_tools.cli", *args, "--format", "json"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"klt {' '.join(args)} produced no JSON (exit {result.returncode})"
        ) from exc


# --------------------------------------------------------------------------- #
# Step 1/5: mismatch-MC yield campaigns (real klt yield-campaign, unmodified
# charge_pump.spice / charge_pump_recentered.spice)
# --------------------------------------------------------------------------- #


def _campaign_spec(netlist_relpath: str, seed: int) -> dict:
    return {
        "netlist": netlist_relpath,
        "engine": "ngspice",
        "backend": "local-parallel",
        "models": {"pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice"},
        "corners": {"process": ["tt_mm"]},
        "monte_carlo": {"n": _N_CAMPAIGN, "seed": seed, "vary": "mismatch"},
        "analysis": {"kind": "tran", "args": "1n 2n"},
        "measurements": [
            {
                "name": "i_up",
                "spice": ".meas tran i_up find i(Vctrlp) at=1n",
                "unit": "A",
            },
            {
                "name": "i_down",
                "spice": ".meas tran i_down find par('-1*i(Vctrln)') at=1n",
                "unit": "A",
            },
            {
                "name": "i_mismatch_pct",
                "spice": (
                    ".meas tran i_mismatch_pct find "
                    "par('100*(i(Vctrlp)-(-1*i(Vctrln)))"
                    "/((i(Vctrlp)+(-1*i(Vctrln)))/2)') "
                    "at=1n"
                ),
                "unit": "%",
                "limits": _LIMITS,
            },
        ],
        "options": {
            "timeout_s": _CAMPAIGN_TIMEOUT_S,
            "keep_artifacts": False,
            "max_workers": _CAMPAIGN_MAX_WORKERS,
        },
        "confidence": 0.9,
        "target_ci_halfwidth": 0.1,
    }


def _run_campaign(label: str, netlist_filename: str, seed: int) -> dict:
    spec_path = os.path.join(_DIR, f"{label}-campaign-spec.json")
    # Relative to _REPO_ROOT (the `_run_klt` subprocess's cwd) rather than an
    # absolute path, so the campaign response's own echoed
    # `sim_report_path`/`request_path` stay repo-relative and reproducible
    # across checkouts -- an absolute `--out-dir` makes `klt yield-campaign`
    # echo an absolute path back into the committed report (see this
    # directory's README.md).
    out_dir = os.path.relpath(os.path.join(_DIR, "raw-campaign", label), _REPO_ROOT)
    with open(spec_path, "w") as f:
        json.dump(_campaign_spec(f"../{netlist_filename}", seed), f, indent=2)
        f.write("\n")

    print(f"--- running {label} yield-campaign (n={_N_CAMPAIGN}) ---", file=sys.stderr)
    t0 = time.time()
    result = _run_klt("yield-campaign", spec_path, "--out-dir", out_dir)
    print(f"--- {label} campaign done in {time.time() - t0:.0f}s ---", file=sys.stderr)

    report_path = os.path.join(_DIR, f"{label}-yield-report.json")
    with open(report_path, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    return result


# --------------------------------------------------------------------------- #
# Step 2: bespoke sensitivity harness
# --------------------------------------------------------------------------- #

_SENSITIVITY_TEMPLATE = """\
* Sensitivity-harness variant of charge_pump.spice (design-centering-validation/
* run_validation.py) -- vos_up/vos_down inject a controlled Vth-mismatch-
* equivalent gate offset (V) on each leg's output device only; w_up/w_down
* apply a controlled beta/width perturbation (um) to the same devices. All
* four default to (0, 8um) below, which reproduces charge_pump.spice's
* electrical behavior exactly -- this variant is used only by this script's
* own per-sample ngspice runs, never by the baseline/re-centered mismatch
* campaigns (those run the real charge_pump.spice/charge_pump_recentered.spice
* files, unmodified).
.param vdd=1.8
.param icp=20u
.param vctrl=0.9
.param vos_up={vos_up}
.param vos_down={vos_down}
.param w_up={w_up}
.param w_down={w_down}

Vdd    vdd 0 DC {{vdd}}
Vctrlp ctrl_up 0 DC {{vctrl}}
Vctrln ctrl_dn 0 DC {{vctrl}}
Iref   nbiasp 0   DC {{icp}}
Iref2  vdd nbiasn DC {{icp}}

* --- UP path: PMOS current source, mirrored 1:1 from the ideal reference,
* with a controlled Vos/W perturbation on the output device only ---
XMBP nbiasp nbiasp vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=8 nf=1 mult=1
Vosup gate_up nbiasp DC {{vos_up}}
XMUP ctrl_up gate_up vdd vdd sky130_fd_pr__pfet_01v8 L=1 W={{w_up}} nf=1 mult=1

* --- DOWN path: NMOS current sink, mirrored 1:1 from the ideal reference,
* with a controlled Vos/W perturbation on the output device only ---
XMBN nbiasn nbiasn 0 0 sky130_fd_pr__nfet_01v8 L=1 W=8 nf=1 mult=1
Vosdn gate_dn nbiasn DC {{vos_down}}
XMDN ctrl_dn gate_dn 0 0 sky130_fd_pr__nfet_01v8 L=1 W={{w_down}} nf=1 mult=1
"""


def _run_sensitivity_sample(
    vos_up_v: float,
    vos_down_v: float,
    w_up_um: float,
    w_down_um: float,
    tmp_dir: str,
    idx: int,
) -> float:
    body = _SENSITIVITY_TEMPLATE.format(
        vos_up=vos_up_v, vos_down=vos_down_v, w_up=w_up_um, w_down=w_down_um
    )
    deck_path = os.path.join(tmp_dir, f"sens_{idx}.cir")
    log_path = os.path.join(tmp_dir, f"sens_{idx}.log")
    lines = [
        "* klt design-centering validation -- sensitivity sample",
        f".lib {_LIB} tt_mm",
        ".temp 27",
        body,
        ".control",
        "tran 1n 2n",
        "meas tran i_up find i(Vctrlp) at=1n",
        "meas tran i_down_raw find i(vctrln) at=1n",
        "quit",
        ".endc",
        ".end",
    ]
    with open(deck_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    subprocess.run(
        ["ngspice", "-b", deck_path, "-o", log_path],
        capture_output=True,
        text=True,
        timeout=180,
    )
    log_text = open(log_path, errors="replace").read()
    i_up = i_down_raw = None
    for raw in log_text.splitlines():
        line = raw.strip()
        m = re.match(r"^i_up\s*=\s*([-+0-9.eE]+)", line)
        if m:
            i_up = float(m.group(1))
        m = re.match(r"^i_down_raw\s*=\s*([-+0-9.eE]+)", line)
        if m:
            i_down_raw = float(m.group(1))
    if i_up is None or i_down_raw is None:
        raise RuntimeError(
            f"sample {idx}: could not measure i_up/i_down -- see {log_path}"
        )
    i_down = -i_down_raw
    return 100.0 * (i_up - i_down) / ((i_up + i_down) / 2.0)


def _build_sensitivity_document(tmp_dir: str) -> dict:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    rng = random.Random(_SENSITIVITY_SEED)
    draws = []
    for _i in range(_N_SENSITIVITY_SAMPLES):
        vth_up_mv = rng.gauss(0.0, _SIGMA_VTH_MV)
        vth_down_mv = rng.gauss(0.0, _SIGMA_VTH_MV)
        beta_up_pct = rng.gauss(0.0, _SIGMA_BETA_PCT)
        beta_down_pct = rng.gauss(0.0, _SIGMA_BETA_PCT)
        draws.append(
            {
                "vth_mismatch_up_mv": vth_up_mv,
                "beta_mismatch_up_pct": beta_up_pct,
                "vth_mismatch_down_mv": vth_down_mv,
                "beta_mismatch_down_pct": beta_down_pct,
            }
        )

    # ngspice runs are single-threaded subprocesses each writing to their own
    # idx-suffixed deck/log files -- safe to fan out across a thread pool
    # (this repo has 28 cores available; each run is ~45-60s standalone, so
    # this cuts a ~20-minute serial harness down to ~2 minutes).
    outputs: dict[int, float] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                _run_sensitivity_sample,
                d["vth_mismatch_up_mv"] / 1000.0,
                d["vth_mismatch_down_mv"] / 1000.0,
                8.0 * (1.0 + d["beta_mismatch_up_pct"] / 100.0),
                8.0 * (1.0 + d["beta_mismatch_down_pct"] / 100.0),
                tmp_dir,
                i,
            ): i
            for i, d in enumerate(draws)
        }
        for future in as_completed(futures):
            i = futures[future]
            outputs[i] = future.result()
            print(
                f"  sensitivity sample {len(outputs)}/{_N_SENSITIVITY_SAMPLES} "
                f"(idx {i}): i_mismatch_pct={outputs[i]:.4f}",
                file=sys.stderr,
            )

    samples = [
        {"parameters": draws[i], "output": outputs[i]}
        for i in range(_N_SENSITIVITY_SAMPLES)
    ]
    return {
        "measurements": [{"name": "i_mismatch_pct", "unit": "%", "samples": samples}]
    }


# --------------------------------------------------------------------------- #
# Step 3: yield-aware klt size (single-device mode, request.design_centering)
# --------------------------------------------------------------------------- #


def _measure_leg_gm_id(leg: str, tmp_dir: str) -> dict:
    """Measures the as-authored charge_pump.spice leg's gm/Id and Id at
    nominal tt/27C -- the `target` `klt size` re-derives against, exactly
    like `tests/test_size.py::_measure_pll_charge_pump_gm_id`."""
    body = open(os.path.join(_KB_DIR, "charge_pump.spice")).read()
    deck_path = os.path.join(tmp_dir, f"nominal_{leg}.cir")
    log_path = os.path.join(tmp_dir, f"nominal_{leg}.log")
    instance = {"up": "xmup", "down": "xmdn"}[leg]
    model = {
        "up": "msky130_fd_pr__pfet_01v8",
        "down": "msky130_fd_pr__nfet_01v8",
    }[leg]
    lines = [
        "* klt design-centering validation -- nominal gm/Id target",
        f".lib {_LIB} tt",
        ".temp 27",
        body,
        ".control",
        "op",
        f"print @m.{instance}.{model}[gm]",
        f"print @m.{instance}.{model}[id]",
        "quit",
        ".endc",
        ".end",
    ]
    with open(deck_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    subprocess.run(
        ["ngspice", "-b", deck_path, "-o", log_path],
        capture_output=True,
        text=True,
        timeout=180,
    )
    values = {}
    for raw in open(log_path, errors="replace"):
        m = _OP_VALUE_RE.match(raw.strip())
        if m:
            values[m.group(1)] = float(m.group(2))
    gm_id = abs(values["gm"]) / abs(values["id"])
    return {"gm_id": gm_id, "id_a": abs(values["id"])}


def _run_design_centering(dominant_leg: str, ranking: list, tmp_dir: str) -> dict:
    kind = {"up": "pmos", "down": "nmos"}[dominant_leg]
    model = {
        "up": "sky130_fd_pr__pfet_01v8",
        "down": "sky130_fd_pr__nfet_01v8",
    }[dominant_leg]
    target = _measure_leg_gm_id(dominant_leg, tmp_dir)
    parameter_map = {
        p["parameter"]: "device"
        for p in ranking
        if p["parameter"].endswith(f"_{dominant_leg}_mv")
        or p["parameter"].endswith(f"_{dominant_leg}_pct")
    }
    request = {
        "device": {
            "kind": kind,
            "model": model,
            "l_um": 1.0,
            "w_min_um": 1.0,
            "w_max_um": 40.0,
        },
        "models": {"pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice"},
        "corner": {"process": "tt", "vdd_v": 1.8, "temperature_c": 27},
        "target": target,
        "options": {"sweep_points": 12, "timeout_s": 300},
        "design_centering": {"ranking": ranking, "parameter_map": parameter_map},
    }
    request_path = os.path.join(_DIR, "design-centering-request.json")
    with open(request_path, "w") as f:
        json.dump(request, f, indent=2)
        f.write("\n")

    print(
        f"--- running yield-aware klt size (dominant leg: {dominant_leg}) ---",
        file=sys.stderr,
    )
    result = _run_klt("size", request_path)
    result_path = os.path.join(_DIR, "design-centering-result.json")
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    return result


# --------------------------------------------------------------------------- #
# Step 4: apply the suggested multiplier to the real circuit
# --------------------------------------------------------------------------- #


def _write_recentered_netlist(dominant_leg: str, mult_after: int) -> str:
    body = open(os.path.join(_KB_DIR, "charge_pump.spice")).read()
    bias_instance, output_instance = {
        "up": ("XMBP", "XMUP"),
        "down": ("XMBN", "XMDN"),
    }[dominant_leg]
    header = (
        f"* klt design-centering validation (issue #1327) -- re-centered variant\n"
        f"* of charge_pump.spice: design-centering-result.json flagged the "
        f"{dominant_leg.upper()} leg's\n"
        f"* Vth/beta mismatch as dominant and suggested growing its device by "
        f"{mult_after}x. Applied to\n"
        f"* *both* {bias_instance} (bias diode) and {output_instance} (output "
        f"device) to preserve the\n"
        f"* mirror's 1:1 ratio -- see run_validation.py's 'Applying the "
        f"multiplier to the real\n"
        f"* circuit' docstring section for why single-device `klt size` growth "
        f"cannot do this on\n"
        f"* its own. Everything else is identical to charge_pump.spice.\n"
    )
    new_body = body
    for instance in (bias_instance, output_instance):
        pattern = re.compile(rf"^({re.escape(instance)}\s.*\bmult=)1\b", re.M)
        new_body, n = pattern.subn(rf"\g<1>{mult_after}", new_body)
        if n != 1:
            raise RuntimeError(f"expected exactly one mult=1 on {instance}, found {n}")
    out_path = os.path.join(_KB_DIR, "charge_pump_recentered.spice")
    with open(out_path, "w") as f:
        f.write(header + new_body)
    return "charge_pump_recentered.spice"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def _yield_summary(report: dict) -> dict | None:
    if "measurements" not in report:
        return None
    m = report["measurements"][0]
    return {
        "status": m["status"],
        "n": m["n"],
        "empirical_estimate": m["yield"]["empirical"]["estimate"],
        "empirical_ci": m["yield"]["empirical"]["confidence_interval"],
        "mean_pct": m["distribution"]["mean"],
        "stddev_pct": m["distribution"]["stddev"],
        "cpk": m["capability"]["cpk"],
        "sigma_to_spec": m["capability"]["sigma_to_spec"],
    }


def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="klt-dcv-") as tmp_dir:
        baseline_report = _run_campaign("baseline", "charge_pump.spice", _CAMPAIGN_SEED)

        print("--- building sensitivity dataset (bespoke harness) ---", file=sys.stderr)
        sensitivity_doc = _build_sensitivity_document(tmp_dir)
        sensitivity_path = os.path.join(_DIR, "sensitivity-samples.json")
        with open(sensitivity_path, "w") as f:
            json.dump(sensitivity_doc, f, indent=2)
            f.write("\n")

        ranking_report = _run_klt("yield-sensitivity", sensitivity_path)
        ranking_path = os.path.join(_DIR, "sensitivity-ranking.json")
        with open(ranking_path, "w") as f:
            json.dump(ranking_report, f, indent=2)
            f.write("\n")
        ranking = ranking_report["measurements"][0]["ranking"]
        top_parameter = ranking[0]["parameter"]
        dominant_leg = "up" if "_up_" in top_parameter else "down"
        print(
            f"--- sensitivity ranking: top parameter '{top_parameter}' -> "
            f"{dominant_leg} leg ---",
            file=sys.stderr,
        )

        dc_result = _run_design_centering(dominant_leg, ranking, tmp_dir)
        if dc_result.get("status") != "pass":
            raise SystemExit(
                "design-centering klt size run did not pass: "
                f"{json.dumps(dc_result)[:500]}"
            )
        dc = dc_result["design_centering"]
        if not dc["applied"]:
            summary = {
                "dominant_leg": dominant_leg,
                "design_centering_applied": False,
                "note": dc.get("note"),
                "baseline_yield": _yield_summary(baseline_report),
            }
            with open(os.path.join(_DIR, "summary.json"), "w") as f:
                json.dump(summary, f, indent=2)
                f.write("\n")
            print(
                "--- design centering suggested no growth (nothing to re-measure) ---",
                file=sys.stderr,
            )
            print(json.dumps(summary, indent=2))
            return

        grown_key = "device"
        mult_after = int(math.ceil(dc["grown"][grown_key]["mult_after"]))
        print(
            f"--- design centering: grow {dominant_leg} leg mult to {mult_after}x ---",
            file=sys.stderr,
        )

        recentered_filename = _write_recentered_netlist(dominant_leg, mult_after)
        recentered_report = _run_campaign(
            "recentered", recentered_filename, _CAMPAIGN_SEED
        )

        summary = {
            "dominant_leg": dominant_leg,
            "mult_after": mult_after,
            "sensitivity_top_parameters": [r["parameter"] for r in ranking[:2]],
            "design_centering_applied": True,
            "baseline_yield": _yield_summary(baseline_report),
            "recentered_yield": _yield_summary(recentered_report),
        }
        with open(os.path.join(_DIR, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
            f.write("\n")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

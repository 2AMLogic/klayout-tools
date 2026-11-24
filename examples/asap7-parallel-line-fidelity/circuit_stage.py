#!/usr/bin/env python3
"""Circuit stage of the ASAP7 parallel-line fidelity example (issue #5202,
phase 2 of #5199): drive the phase-1 extracted and reference networks through
`klt sim` and compare delay and energy.

Split out of `generate_and_measure.py` to stay under the per-file size limit.
Run it as `python circuit_stage.py` after `generate_and_measure.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

from generate_and_measure import HERE, REPO_ROOT, _sha256

from klayout_tools.decks import get_parasitics_deck
from klayout_tools.extract import run_extract

#: Weak driver in front of every line (ohm). With 0.1-0.2 fF of line load this
#: gives tau ~ 1-2 ps, so the line's own R (tens of ohm) is a small correction
#: and the capacitance error dominates -- stated in CIRCUIT_IDEALISATIONS.
DRIVER_OHM = 10_000.0
VDD_V = 1.0
EDGE_AT_S = 2e-12
EDGE_S = 1e-12
T_STOP_S = 40e-12
T_STEP_S = 1e-15
SIM_TIMEOUT_S = 120
CAPACITY_WAIT_S = 600
CASES = ("deck", "reference")
MODES = ("quiet", "opposite")
EVIDENCE_DIR = os.path.join(
    REPO_ROOT, "evidence", "sim", "asap7-parallel-line-fidelity"
)

CIRCUIT_IDEALISATIONS = [
    "each line is driven by an ideal step through a 10 kohm resistor and "
    "measured at its lumped RC hub (the deck's own topology: pin - R - hub - C)",
    "only the victim's two nearest same-level neighbours switch; every other "
    "line is held at 0 V by an ideal source (a stiff quiet line)",
    "reference network: R from klt mom's PEEC resistance; hub-to-ground C = "
    "klt mom ground + adjacent-level + non-nearest same-level coupling; the "
    "nearest-neighbour couplings are explicit hub-to-hub capacitors; each "
    "neighbour copies the victim's R and ground C (symmetry ASSUMED) and "
    "neighbour-to-neighbour coupling is dropped",
    "deck network: the netlist `klt extract --deck asap7 --parasitics` wrote, "
    "verbatim; it has no same-level coupling, but every hub's C returns to the "
    "floating `vsubs` node (1e12 ohm to ground), which couples the lines weakly, "
    "so quiet and opposite differ slightly even there",
    "critical-net path: refused by every FinFET deck (phase 1), reported "
    "'unsupported', never zero-filled",
    "quasi-static lumped RC, no inductance, no device nonlinearity; the "
    "energy is the integral of v*i at the victim's source over 0-40 ps",
]


def _read_subckt(path: str) -> tuple[list[str], list[str]]:
    """(ports, body lines without comments) of the extracted `.SUBCKT TOP`."""
    lines = [
        ln.strip()
        for ln in open(path, encoding="utf-8").read().splitlines()
        if ln.strip() and not ln.lstrip().startswith("*")
    ]
    head = next(ln for ln in lines if ln.upper().startswith(".SUBCKT"))
    ports = head.split()[2:]
    start = lines.index(head)
    end = next(i for i, ln in enumerate(lines) if ln.upper().startswith(".ENDS"))
    return ports, lines[start : end + 1]


def _neighbours(victim: str) -> tuple[str, str]:
    k = int(victim[1:])
    return f"N{k - 1}", f"N{k + 1}"


def reference_subckt(point: dict) -> tuple[list[str], list[str]]:
    """The reference network (module docstring of the circuit stage) built from
    the phase-1 `reference_klt_mom` numbers, as (ports, `.SUBCKT REF` lines)."""
    ref = point["reference_klt_mom"]
    victim = point["victim"]
    left, right = _neighbours(victim)
    each = ref["coupling_each_ff"]
    near = {left: each[left], right: each[right]}
    c_gnd_ff = (
        ref["c_ground_ff"]
        + ref["c_adjacent_levels_ff"]
        + sum(v for k, v in each.items() if k.startswith("N") and k not in near)
    )
    r_ohm = ref["r_ohm_per_um"] * point["length_um"]
    ports = [left, victim, right]
    body = [f".SUBCKT REF {' '.join(ports)}"]
    for net in ports:
        body.append(f"R{net} {net} {net}__par {r_ohm:.6g}")
        body.append(f"C{net} {net}__par 0 {c_gnd_ff * 1e-15:.6g}")
    for net, c_ff in near.items():
        body.append(f"Ccc_{victim}_{net} {victim}__par {net}__par {c_ff * 1e-15:.6g}")
    body.append(".ENDS REF")
    return ports, body


def circuit_deck(
    ports: list[str], body: list[str], subckt: str, victim: str, mode: str
) -> str:
    """One ngspice deck: the network, a driver per line, the measurements."""
    left, right = _neighbours(victim)
    edge = f"{EDGE_AT_S:g} {EDGE_S:g} {EDGE_S:g} 1 2"
    rail = f"PULSE(0 {VDD_V} {edge})"
    fall = f"PULSE({VDD_V} 0 {edge})"
    out = [
        f"* ASAP7 parallel-line circuit stage: {victim}, neighbours {mode}",
        *body,
        f"Xdut {' '.join(ports)} {subckt}",
        f"Vv sv 0 {rail}",
        f"Rdv sv {victim} {DRIVER_OHM:g}",
    ]
    for net in ports:
        if net == victim:
            continue
        if net in (left, right):
            wave = fall if mode == "opposite" else "DC 0"
            out += [f"V{net} s{net} 0 {wave}", f"Rd{net} s{net} {net} {DRIVER_OHM:g}"]
        else:
            out.append(f"V{net} {net} 0 DC 0")
    out += [
        ".control",
        "set measureprec=10",
        f"tran {T_STEP_S:g} {T_STOP_S:g}",
        f"meas tran delay_s TRIG v(sv) VAL={VDD_V / 2:g} RISE=1 "
        f"TARG v(xdut.{victim.lower()}__par) VAL={VDD_V / 2:g} RISE=1",
        "let pwr = v(sv) * (-vv#branch)",
        f"meas tran energy_j INTEG pwr FROM=0 TO={T_STOP_S:g}",
        "quit",
        ".endc",
        ".end",
        "",
    ]
    return "\n".join(out)


def _parse_meas(text: str) -> dict[str, float]:
    found = {}
    for name in ("delay_s", "energy_j"):
        match = re.search(rf"^{name}\s*=\s*([-+0-9.eE]+)", text, re.MULTILINE)
        if match:
            found[name] = float(match.group(1))
    return found


def build_circuit_decks(points: list[dict], work_dir: str) -> list[dict]:
    """Write one deck per (point, case, mode) and return the `decks[]` entries.
    The deck case is the netlist `klt extract` writes for that point's GDS."""
    os.makedirs(work_dir, exist_ok=True)
    entries = []
    for point in points:
        victim = point["victim"]
        spice = os.path.join(work_dir, f"{point['id']}.extracted.spice")
        run_extract(
            os.path.join(HERE, point["gds"]), "asap7", output=spice, parasitics=True
        )
        deck_ports, deck_body = _read_subckt(spice)
        ref_ports, ref_body = reference_subckt(point)
        networks = {
            "deck": (deck_ports, deck_body, "TOP"),
            "reference": (ref_ports, ref_body, "REF"),
        }
        for case, (ports, body, subckt) in networks.items():
            for mode in MODES:
                unit = f"{point['id']}.{case}.{mode}"
                path = os.path.join(work_dir, f"{unit}.spice")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(circuit_deck(ports, body, subckt, victim, mode))
                entries.append(
                    {
                        "id": unit,
                        "deck": os.path.basename(path),
                        "timeout_s": SIM_TIMEOUT_S,
                    }
                )
    return entries


def run_circuit_sims(
    entries: list[dict],
    work_dir: str,
    backend: str | None,
    wait_for_capacity_s: float = CAPACITY_WAIT_S,
) -> dict:
    """One `klt sim` deck-list request for every unit. `backend=None` leaves the
    choice to `$KLT_SIM_BACKEND` (the fleet on a dispatch worker); a multi-unit
    request is never hand-looped. `--wait-for-capacity` is sent on every batch
    call (the CLI rejects it for an explicit local backend)."""
    from klayout_tools.sim import run_sim

    request = {
        "decks": entries,
        "options": {"timeout_s": SIM_TIMEOUT_S},
        "batch": {"capacity_wait_s": wait_for_capacity_s},
    }
    request_path = os.path.join(work_dir, "request.json")
    with open(request_path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)
        handle.write("\n")
    return run_sim(
        request_path,
        artifacts_dir=os.path.join(work_dir, "out"),
        backend=backend,
        capacity_wait_s=None if backend == "local" else wait_for_capacity_s,
    )


def _pct(value: float, ref: float) -> float | None:
    return None if ref == 0 else round(100.0 * (value - ref) / abs(ref), 2)


def collect_circuit(points: list[dict], report: dict) -> list[dict]:
    """Per point: delay (ps) and energy (fJ) for quiet/opposite per case, plus
    deck-vs-reference relative errors. A unit that failed raises."""
    outdir = report["outdir"]
    units = {u["id"]: u for u in report["units"]}
    rows = []
    for point in points:
        cases: dict = {}
        for case in CASES:
            cases[case] = {}
            for mode in MODES:
                unit = units[f"{point['id']}.{case}.{mode}"]
                if unit["status"] != "pass":
                    raise SystemExit(
                        f"unit {unit['id']} {unit['status']}: {unit.get('diagnostics')}"
                    )
                with open(
                    os.path.join(outdir, unit["stdout"]),
                    encoding="utf-8",
                    errors="replace",
                ) as handle:
                    meas = _parse_meas(handle.read())
                if set(meas) != {"delay_s", "energy_j"}:
                    raise SystemExit(f"unit {unit['id']}: measurements missing: {meas}")
                cases[case][mode] = {
                    "delay_ps": round(meas["delay_s"] * 1e12, 5),
                    "energy_fj": round(meas["energy_j"] * 1e15, 6),
                }
        err = {
            f"{what}_{mode}": _pct(
                cases["deck"][mode][key], cases["reference"][mode][key]
            )
            for mode in MODES
            for what, key in (("delay", "delay_ps"), ("energy", "energy_fj"))
        }
        rows.append(
            {
                "id": point["id"],
                "level": point["level"],
                "pitch_mult": point["pitch_mult"],
                "density": point["density"],
                "deck": cases["deck"],
                "critical_net": "unsupported",
                "reference": cases["reference"],
                "rel_err_pct": {"deck": err, "critical_net": "unsupported"},
            }
        )
    return rows


def write_circuit_evidence(circuit: dict, report: dict, results_sha: str) -> str:
    """Append one `evidence-record/1` under `evidence/sim/asap7-parallel-line-fidelity/`
    (the sky130 mom-coupling-fidelity shape: `supersedes`, `HEAD`)."""
    script_sha = _sha256(os.path.abspath(__file__))
    now = datetime.now(timezone.utc)
    name = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{script_sha[:7]}.json"
    scope_dir = os.path.join(EVIDENCE_DIR, "circuit-fidelity")
    os.makedirs(scope_dir, exist_ok=True)
    head = os.path.join(scope_dir, "HEAD")
    previous = open(head).read().strip() if os.path.exists(head) else None
    env = report.get("environment", {})
    record = {
        "schema": "evidence-record/1",
        "recorded_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "block": "asap7-parallel-line-fidelity",
        "scope": "circuit-fidelity",
        "request_path": "examples/asap7-parallel-line-fidelity/circuit_stage.py",
        "request_sha256": script_sha,
        "subset_reason": None,
        "pdk_pin": None,
        "extraction_pin": {
            "deck": "asap7",
            "deck_content_hash": circuit["tools"].get("deck_content_hash"),
            "method": "--parasitics lumped RC vs klt mom reference network",
        },
        "supersedes": [previous] if previous else [],
        "note": (
            "not a PDK-model klt sim record -- a klt extract + klt mom + klt sim "
            "(deck-list, linear RC) comparison for issue #5202 (phase 2 of "
            "#5199); request_path/request_sha256 pin the generation script, "
            "which with results.json (sha256 in result.inputs) fully determines "
            "every number; pdk_pin is null because the geometry is authored "
            "directly and the netlists contain no PDK device models."
        ),
        "result": {
            **circuit,
            "inputs": {"results_json_sha256": results_sha},
            "sim": {
                "engine_version": report.get("environment", {}).get("engine_version"),
                "backend": env.get("backend") or report.get("backend"),
                "remote": env.get("remote"),
                "unit_count": report.get("unit_count"),
            },
        },
    }
    with open(os.path.join(scope_dir, name), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    with open(head, "w", encoding="utf-8") as handle:
        handle.write(name + "\n")
    return os.path.join(scope_dir, name)


def circuit_stage(
    points: list[dict], work_dir: str, backend: str | None = None
) -> tuple[dict, dict]:
    """Run the whole circuit stage on `points` (phase-1 result points)."""
    entries = build_circuit_decks(points, work_dir)
    report = run_circuit_sims(entries, work_dir, backend)
    deck = get_parasitics_deck("asap7")
    circuit = {
        "schema": "asap7-parallel-line-circuit-fidelity/1",
        "issue": "2AMLogic/klayout-tools#5202 (phase 2 of #5199)",
        "tools": {"deck_content_hash": getattr(deck, "content_hash", None)},
        "stimulus": {
            "driver_ohm": DRIVER_OHM,
            "vdd_v": VDD_V,
            "edge_s": EDGE_S,
            "t_stop_s": T_STOP_S,
            "delay": "50% of the victim source to 50% of the victim hub",
            "energy": "integral of v*i at the victim source, 0 to t_stop",
        },
        "idealisations": CIRCUIT_IDEALISATIONS,
        "points": collect_circuit(points, report),
    }
    return circuit, report


def run_circuit(out_dir: str, backend: str | None) -> int:
    results_path = os.path.join(out_dir, "results.json")
    with open(results_path, encoding="utf-8") as handle:
        results = json.load(handle)
    with tempfile.TemporaryDirectory() as tmp:
        circuit, report = circuit_stage(results["points"], tmp, backend)
        circuit["tools"]["deck_content_hash"] = results["tools"]["deck_content_hash"]
        with open(os.path.join(out_dir, "circuit-results.json"), "w") as handle:
            json.dump(circuit, handle, indent=2)
            handle.write("\n")
        record = write_circuit_evidence(circuit, report, _sha256(results_path))
    print(f"wrote {len(circuit['points'])} points; evidence {record}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--out-dir", default=HERE)
    parser.add_argument(
        "--backend",
        default=None,
        help="klt sim backend (default: follows $KLT_SIM_BACKEND)",
    )
    args = parser.parse_args(argv)
    return run_circuit(args.out_dir, args.backend)


if __name__ == "__main__":
    sys.exit(main())

"""Cross-validation of `klt sim`'s ngspice results against **Xyce**,
Sandia's from-scratch SPICE implementation -- issue #2016, pairing #5 of
tracking issue #2007 ("independent cross-validation oracles for klt
verdicts").

`klt sim`'s reference engine is ngspice, a SPICE 3f5 derivative: every
numerical answer `klt sim` has ever produced came from that one codebase.
Xyce shares no code and no history with it (C++/Trilinos vs C; different
parsers, device evaluators, and solvers), so agreement between the two is
independent evidence about `klt sim`'s numerics -- the same argument
`tests/test_mom_capacitance_oracle.py` makes for FastCap vs `klt mom`.

What is compared: the **same request document** -- same circuit body, same
corner definition, same analysis card arguments, same `.measure` cards,
same limits -- is run once per engine via the one engine-dispatch seam
`klt sim` already gates on (`request.engine`), never by generating two
different decks by hand. What is left to differ is the engine, which is the
thing under test. `tests/helpers/xyce_oracle.py` drives both sides and
`docs/design/xyce-oracle.md` is the methodology: the measured agreement,
the tolerance derivation below, and the SPICE syntax each engine parses
differently (issue #2016's acceptance criterion 5).

## How this tier is gated

Real-binary-gated, exactly like `tests/test_mom_capacitance_oracle.py`:
the whole module skips with a specific reason when `Xyce` or `ngspice` is
not on `$PATH`. Absence must never fail CI. `xyce-oracle.yml` is the
workflow that provisions both and asserts these tests were *not* skipped
there -- the "evidence the work actually ran" half of #2007's criterion 2.

## Agreement tolerance, and where it comes from

Every comparison below is a relative difference inside a stated band
(:data:`XYCE_AGREEMENT_TOL`), never equality: two independent SPICE
implementations with different time-step placement (tran), different Newton
convergence criteria (dc/op) and different matrix ordering do not agree
bit-for-bit, and asserting that they do would encode one engine's numerical
noise as the specification.

Measured with the pins recorded in docs/design/xyce-oracle.md (XyceNF
7.10.0 vs ngspice 47, this repo's fixtures): worst-case ~2.9e-4 relative
(the transient ``vout_at_5n`` FIND measurement -- interpolating a single
timepoint on a fast exponential edge is where independent time-step
placement drifts most), ~3-7e-5 on the TRIG/TARG delay, the MAX amplitude
and the DC sweeps. The band is 3e-3: ~10x the worst observed difference,
loose enough for a different host's arithmetic and small version drift,
and still ~290x below the seeded defect below (~88% shift), so the band
cannot hide a real device-parameter regression.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from helpers.xyce_oracle import (
    measurement_value,
    measurement_value_in_corner,
    netlist_sha256,
    ngspice_version,
    oracle_skip_reason,
    relative_difference,
    run_sim_engine,
    write_fixture,
    xyce_version,
)

_SKIP_REASON = oracle_skip_reason()
if _SKIP_REASON:
    pytest.skip(_SKIP_REASON, allow_module_level=True)

#: Agreement band for every fixture below -- see this module's docstring
#: for the derivation. Expressed as a relative difference (denominator: the
#: larger magnitude of the two values, per
#: `helpers.xyce_oracle.relative_difference`).
XYCE_AGREEMENT_TOL = 3e-3

#: The declared DC sweep of the divider fixture, as analysis-card arguments:
#: 0 V to 5 V in 0.25 V steps = 21 sweep points. The waveform artifact each
#: engine produces must carry at least this many points -- the "the
#: analysis actually swept" assertion (#2007 criterion 2).
DIVIDER_SWEEP_POINTS = 21


# --------------------------------------------------------------------------- #
# Fixtures: the same request document through both engines
# --------------------------------------------------------------------------- #


def _divider_body(r1: str = "4k") -> str:
    """A resistive divider: ``v(out) = v(vdd) * 1k/(R1+1k)`` -- an analytic
    anchor, so a shared error in *both* engines cannot pass silently."""
    return f"Vdd vdd 0 5\nR1 vdd out {r1}\nR2 out 0 1k\n"


def _divider_request(
    engine: str, *, limits: dict[str, float] | None = None
) -> dict[str, Any]:
    """Sweep that divider with a DC analysis (0..5 V in 0.25 V steps).
    ``vout_mid`` interpolates at 2.5 V (0.5 V by the divider law) and
    ``vout_upper`` at 4.0 V (0.8 V). ``limits`` turns ``vout_mid`` into a
    pass/fail check (the seeded-defect control below)."""
    return {
        "netlist": "body.spice",
        "engine": engine,
        "analysis": {"kind": "dc", "args": "Vdd 0 5 0.25"},
        "measurements": [
            {
                "name": "vout_mid",
                "spice": ".meas dc vout_mid FIND v(out) AT=2.5",
                "unit": "V",
                **({"limits": limits} if limits else {}),
            },
            {
                "name": "vout_upper",
                "spice": ".meas dc vout_upper FIND v(out) AT=4.0",
                "unit": "V",
            },
        ],
        "options": {"waveforms": True, "keep_artifacts": True},
    }


def _tran_body() -> str:
    return "V1 in 0 PULSE(0 1 2n 0.5n 0.5n 4n 10n)\nR1 in out 1k\nC1 out 0 2p\n"


def _tran_request(engine: str) -> dict[str, Any]:
    """A pulse-driven RC edge: the transient family, with a TRIG/TARG delay
    measurement (interpolated crossing time -- the driftriest comparison
    class) and a MAX."""
    return {
        "netlist": "body.spice",
        "engine": engine,
        "analysis": {"kind": "tran", "args": "5p 8n"},
        "measurements": [
            {
                "name": "tphl",
                "spice": ".meas tran tphl TRIG v(in) VAL=0.5 RISE=1 "
                "TARG v(out) VAL=0.5 RISE=1",
                "unit": "s",
            },
            {
                "name": "vmax",
                "spice": ".meas tran vmax MAX v(out)",
                "unit": "V",
            },
            {
                "name": "vout_at_5n",
                "spice": ".meas tran vout_at_5n FIND v(out) AT=5n",
                "unit": "V",
            },
        ],
    }


def _temp_body() -> str:
    return "I1 0 a DC 1m\nD1 a 0 DMOD\n.model DMOD D (IS=1e-14)\n"


def _temp_request(engine: str) -> dict[str, Any]:
    """A diode at a forced current, swept across two temperature corners:
    exercises the corners.temperature_c axis (ngspice `.temp` vs Xyce's
    `.options device temp=` -- Xyce silently ignores `.temp`, verified
    against 7.10.0; see docs/design/xyce-oracle.md) with real device
    physics: the forward drop moves ~-1.8 mV/K between the corners."""
    return {
        "netlist": "body.spice",
        "engine": engine,
        "analysis": {"kind": "dc", "args": "I1 0.5m 1.5m 0.1m"},
        "corners": {"temperature_c": [27, 85]},
        "measurements": [
            {
                "name": "vdiode",
                "spice": ".meas dc vdiode FIND v(a) AT=1m",
                "unit": "V",
            }
        ],
    }


def _run_engine(
    work: Path,
    body: str,
    request: dict[str, Any],
    engine: str,
    *,
    min_waveform_points: int | None = None,
) -> dict[str, Any]:
    """One engine's run of one (body, request) fixture in ``work``,
    with the "actually ran" evidence checks applied."""
    work.mkdir(parents=True, exist_ok=True)
    request_path = write_fixture(work, body, request)
    version = ngspice_version() if engine == "ngspice" else xyce_version()
    return run_sim_engine(
        request_path,
        engine,
        expected_version=version,
        min_waveform_points=min_waveform_points,
    )


def _run_pair(
    tmp_path: Path,
    body: str,
    request_maker: Callable[[str], dict[str, Any]],
    *,
    min_waveform_points: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one fixture through both engines; return ``(ngspice_report,
    xyce_report)``."""
    ng = _run_engine(
        tmp_path / "ngspice",
        body,
        request_maker("ngspice"),
        "ngspice",
        min_waveform_points=min_waveform_points,
    )
    xy = _run_engine(
        tmp_path / "xyce",
        body,
        request_maker("xyce"),
        "xyce",
        min_waveform_points=min_waveform_points,
    )
    return ng, xy


# --------------------------------------------------------------------------- #
# Agreement: same request, two independent engines
# --------------------------------------------------------------------------- #


def test_dc_divider_agrees_within_tolerance(tmp_path):
    """The DC family: both engines sweep the same 21 points, every
    measurement lands inside the band, and both hit the divider law's
    analytic anchor."""
    ng, xy = _run_pair(
        tmp_path,
        _divider_body(),
        _divider_request,
        min_waveform_points=DIVIDER_SWEEP_POINTS,
    )
    for name, expected_v in (("vout_mid", 0.5), ("vout_upper", 0.8)):
        ng_value = measurement_value(ng, name)
        xy_value = measurement_value(xy, name)
        difference = relative_difference(ng_value, xy_value)
        assert difference <= XYCE_AGREEMENT_TOL, (
            f"{name}: ngspice {ng_value} vs Xyce {xy_value} "
            f"(rel.diff {difference * 100:.4f}% > band)"
        )
        for value, engine in ((ng_value, "ngspice"), (xy_value, "xyce")):
            assert relative_difference(value, expected_v) <= XYCE_AGREEMENT_TOL, (
                f"{engine}: {name} {value} vs the divider-law anchor "
                f"{expected_v} -- both engines disagreeing with the analytic "
                "value would mean the fixtures, not the engines, are wrong"
            )


def test_rc_transient_agrees_within_tolerance(tmp_path):
    """The transient family: TRIG/TARG delay and MAX amplitude, where
    independent time-step placement shows up most."""
    ng, xy = _run_pair(tmp_path, _tran_body(), _tran_request)
    for name in ("tphl", "vmax", "vout_at_5n"):
        ng_value = measurement_value(ng, name)
        xy_value = measurement_value(xy, name)
        difference = relative_difference(ng_value, xy_value)
        assert difference <= XYCE_AGREEMENT_TOL, (
            f"{name}: ngspice {ng_value} vs Xyce {xy_value} "
            f"(rel.diff {difference * 100:.4f}% > band)"
        )


def test_temperature_corners_agree_per_corner(tmp_path):
    """The corner axis: both engines move the diode drop with temperature,
    and the two engines' per-corner values agree."""
    ng, xy = _run_pair(tmp_path, _temp_body(), _temp_request)
    assert len(ng["corners"]) == 2 and len(xy["corners"]) == 2
    for index in range(2):
        assert (
            ng["corners"][index]["temperature_c"]
            == xy["corners"][index]["temperature_c"]
        )
        ng_value = measurement_value_in_corner(ng, index, "vdiode")
        xy_value = measurement_value_in_corner(xy, index, "vdiode")
        difference = relative_difference(ng_value, xy_value)
        assert difference <= XYCE_AGREEMENT_TOL, (
            f"corner {index} ({ng['corners'][index]['temperature_c']} C): "
            f"ngspice {ng_value} vs Xyce {xy_value} "
            f"(rel.diff {difference * 100:.4f}% > band)"
        )
    # The physics must actually move with temperature: the 85 C drop is
    # lower than the 27 C drop in both engines (an identical pair of values
    # would pass the band above while proving the corner axis dead -- the
    # `.temp`-is-a-no-op-in-Xyce divergence this pairing exists to catch).
    for report, engine in ((ng, "ngspice"), (xy, "xyce")):
        hot = measurement_value_in_corner(report, 1, "vdiode")
        cold = measurement_value_in_corner(report, 0, "vdiode")
        assert hot < cold, (
            f"{engine}: diode drop did not fall from 27 C to 85 C "
            f"({cold} -> {hot}) -- the temperature corner did not bite"
        )


# --------------------------------------------------------------------------- #
# Falsifiability: the seeded defect both engines must detect
# --------------------------------------------------------------------------- #


def test_the_comparison_can_actually_fail(tmp_path):
    """Negative control for the comparison itself (#2007 criterion 3, the
    house pattern `test_mom_capacitance_oracle.py` established): a 10x R1
    seed must (a) move vout_mid far outside the agreement band in *both*
    engines -- so the comparison can tell the two circuits apart -- and
    (b) flip the limits verdict to ``fail`` in *both* engines -- so a real
    defect cannot hide behind one engine's pass.

    Without this, every passing test above would also pass if one engine's
    request silently ignored the netlist edit and re-solved the clean
    circuit.
    """
    limits = {"min": 0.47, "max": 0.53}

    def run(engine: str, body: str) -> dict[str, Any]:
        return _run_engine(
            tmp_path / f"control-{engine}-{body.count('40k') or 'clean'}",
            body,
            _divider_request(engine, limits=limits),
            engine,
        )

    clean = {engine: run(engine, _divider_body()) for engine in ("ngspice", "xyce")}
    for engine, report in clean.items():
        assert report["status"] == "pass", (
            f"{engine}: clean circuit did not pass its own limits: "
            f"{report['corners'][0]['measurements']}"
        )

    # The seed: R1 4k -> 40k. The divider law moves vout_mid from 0.5 V to
    # 1/41 V (~0.061 V) -- an ~88% shift, ~290x the agreement band.
    defect_body = _divider_body(r1="40k")
    defect = {engine: run(engine, defect_body) for engine in ("ngspice", "xyce")}
    clean_mid = {
        engine: measurement_value(report, "vout_mid")
        for engine, report in clean.items()
    }
    defect_mid = {
        engine: measurement_value(report, "vout_mid")
        for engine, report in defect.items()
    }
    for engine in ("ngspice", "xyce"):
        shift = relative_difference(clean_mid[engine], defect_mid[engine])
        assert shift > 100 * XYCE_AGREEMENT_TOL, (
            f"{engine}: seeded defect moved vout_mid by only "
            f"{shift * 100:.4f}% -- this comparison cannot distinguish the "
            "two circuits, so the agreement tests above prove nothing"
        )
        assert defect[engine]["status"] == "fail", (
            f"{engine}: defective circuit was not flagged by its limits "
            f"(status {defect[engine]['status']})"
        )
    # Both engines saw the same defective physics, too: their defective
    # values agree with each other inside the band.
    assert (
        relative_difference(defect_mid["ngspice"], defect_mid["xyce"])
        <= XYCE_AGREEMENT_TOL
    )


# --------------------------------------------------------------------------- #
# Provenance (#2007 criterion 4): versions and input hashes
# --------------------------------------------------------------------------- #


def test_provenance_records_both_engine_versions_and_input_hashes(tmp_path):
    """Both engines' versions come from the binaries that ran, and both
    responses hash the same netlist bytes -- what makes a future
    disagreement attributable rather than ambiguous. The full block (pins,
    install recipe, measured agreement) lives in
    docs/design/xyce-oracle.md."""
    ng = _run_engine(
        tmp_path / "ng",
        _divider_body(),
        _divider_request("ngspice"),
        "ngspice",
    )
    xy = _run_engine(
        tmp_path / "xy",
        _divider_body(),
        _divider_request("xyce"),
        "xyce",
    )

    assert ng["environment"]["engine"] == "ngspice"
    assert xy["environment"]["engine"] == "xyce"
    assert ng["environment"]["engine_version"] == ngspice_version()
    assert xy["environment"]["engine_version"] == xyce_version()
    body_sha = netlist_sha256(tmp_path / "ng" / "request.json")
    assert ng["environment"]["netlist_sha256"] == body_sha
    assert xy["environment"]["netlist_sha256"] == body_sha

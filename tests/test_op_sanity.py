"""Tests for the per-device operating-point sanity lint (`klt sim --op-lint`,
`klt eval`'s `op-sanity` gate) and the `klayout_tools.op_sanity` library.

Two tiers, mirroring `test_sim.py`/`test_size.py`:

- **Unit tests** (the majority) exercise the netlist parser, the rail
  classification, every check, the corner-aware triode margin, and the full
  `run_op_sanity` pipeline with `subprocess.run` stubbed by a fake ngspice
  that *reads the generated deck* and answers exactly the vectors it asks
  for. These always run, everywhere, and are what a check/parse regression
  actually gets caught by.
- **Integration tests** run the real `ngspice -b` against the installed
  sky130A/gf180mcu ngspice model libraries -- the acceptance criterion that
  per-device `Vth` comes from the model rather than a constant can only be
  checked that way. They skip (never silently pass) when ngspice or the PDK
  model library is absent, as CI installs neither ngspice model tree.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import eval as eval_mod
from klayout_tools import op_sanity
from klayout_tools.cli import main
from klayout_tools.pdk import PdkNotFoundError, find_pdk

HAVE_NGSPICE = shutil.which("ngspice") is not None

EXAMPLES = Path(__file__).parent.parent / "examples"


def _pdk_ngspice_lib(variant: str, *relative: str) -> str | None:
    try:
        resolution = find_pdk(variant=variant)
    except PdkNotFoundError:
        return None
    path = Path(resolution["root"]).joinpath(resolution["variant"], *relative)
    return str(path) if path.is_file() else None


SKY130_NGSPICE_LIB = _pdk_ngspice_lib(
    "sky130A", "libs.tech", "ngspice", "sky130.lib.spice"
)
GF180_NGSPICE_LIB = _pdk_ngspice_lib(
    "gf180mcuD", "libs.tech", "ngspice", "sm141064.ngspice"
)
GF180_NGSPICE_DESIGN = _pdk_ngspice_lib(
    "gf180mcuD", "libs.tech", "ngspice", "design.ngspice"
)

_SKIP_NO_SKY130 = pytest.mark.skipif(
    not HAVE_NGSPICE or SKY130_NGSPICE_LIB is None,
    reason="needs ngspice plus an installed sky130A ngspice model library",
)
_SKIP_NO_GF180 = pytest.mark.skipif(
    not HAVE_NGSPICE or GF180_NGSPICE_LIB is None or GF180_NGSPICE_DESIGN is None,
    reason="needs ngspice plus an installed gf180mcu ngspice model library",
)


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #

#: A minimal but complete common-source stage: one NMOS driver, one PMOS
#: load, both correctly wired, on a synthetic (non-PDK) model library.
_GOOD_NETLIST = """\
.param vdd=1.8
Vdd vdd 0 DC {vdd}
Vin in 0 DC 0.9
Vbias bias 0 DC 0.6
XM1 out in 0 0 nmos_demo w=2 l=0.5
XM2 out bias vdd vdd pmos_demo w=4 l=0.5
CL out 0 1p
"""


def _write_request(
    tmp_path: Path,
    netlist: str,
    *,
    name: str = "request.json",
    **extra: object,
) -> Path:
    netlist_path = tmp_path / "body.spice"
    netlist_path.write_text(netlist)
    request: dict[str, object] = {
        "netlist": "body.spice",
        "corners": {"supply_v": {"vdd": [1.8]}, "temperature_c": [27]},
        **extra,
    }
    path = tmp_path / name
    path.write_text(json.dumps(request))
    return path


_PRINT_OP_RE = re.compile(r"^print @(\S+)\[(\w+)\]$")
_PRINT_V_RE = re.compile(r"^print v\((.+)\)$")
_MARKER_RE = re.compile(r"^echo KLT_OP_DEVICE (\d+) (\S+)$")


def _install_fake_ngspice(
    monkeypatch,
    values: dict[str, dict[str, float]],
    *,
    node_voltages: dict[str, float] | None = None,
    candidate_index: int = 0,
    prologue: str = "",
    side_effect: BaseException | None = None,
):
    """Stub `subprocess.run` with a fake ngspice that reads the generated
    deck and answers exactly the vectors it asks for.

    Reading the deck (rather than replaying a canned log) is what makes
    these tests real: the device order, the candidate instance paths, and
    the probed node list all come from the code under test, so a deck-
    generation regression shows up as a parse failure here instead of
    passing against a stale fixture. `candidate_index` picks *which* of a
    device's candidate paths answers -- everything else gets ngspice's real
    non-fatal "no such device or model name" line, exactly as the PDK whose
    convention was not matched does.
    """

    def fake_run(cmd, capture_output, text, timeout):
        if side_effect is not None:
            raise side_effect
        deck_path = cmd[cmd.index("-b") + 1]
        log_path = cmd[cmd.index("-o") + 1]
        out: list[str] = [
            "Circuit: * klt sim --op-lint",
            "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000",
        ]
        if prologue:
            out.append(prologue)
        current: str | None = None
        candidates: list[str] = []
        with open(deck_path, encoding="utf-8") as handle:
            deck_lines = [line.strip() for line in handle.read().splitlines()]
        for line in deck_lines:
            marker = _MARKER_RE.match(line)
            if marker is not None:
                current = marker.group(2)
                candidates = []
                out.append(f"KLT_OP_DEVICE {marker.group(1)} {marker.group(2)}")
                continue
            if line == "echo KLT_OP_NODES":
                current = None
                out.append("KLT_OP_NODES")
                continue
            op_match = _PRINT_OP_RE.match(line)
            if op_match is not None and current is not None:
                path, param = op_match.groups()
                if path not in candidates:
                    candidates.append(path)
                if candidates.index(path) != candidate_index:
                    out.append(f"Error: no such device or model name {path}")
                    continue
                value = values.get(current, {}).get(param)
                if value is None:
                    out.append(f"Error: no such parameter {param}.")
                else:
                    out.append(f"@{path}[{param}] = {value:.6e}")
                continue
            node_match = _PRINT_V_RE.match(line)
            if node_match is not None:
                node = node_match.group(1)
                out.append(f"v({node}) = {(node_voltages or {}).get(node, 0.0):.6e}")
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(out) + "\nngspice-99 done\n")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(op_sanity.subprocess, "run", fake_run)


#: A saturated NMOS: comfortably above Vth, Vds well above Vdsat.
_SATURATED_NMOS = {"vgs": 0.9, "vth": 0.7, "vds": 1.2, "vdsat": 0.15, "id": 4.5e-5}
#: A saturated PMOS (BSIM4 reports every value as a positive magnitude).
_SATURATED_PMOS = {"vgs": 1.2, "vth": 0.75, "vds": 0.9, "vdsat": 0.39, "id": 4.5e-5}


def _findings(report: dict, check: str) -> list[dict]:
    return [entry for entry in report["findings"] if entry["check"] == check]


# --------------------------------------------------------------------------- #
# Netlist parsing (structural, no simulator)
# --------------------------------------------------------------------------- #


def test_parse_netlist_reads_subckt_calls_and_plain_elements():
    parsed = op_sanity.parse_netlist(
        ".model nch nmos level=1\n"
        "Vdd vdd 0 DC 1.8\n"
        "XM1 out in 0 0 sky130_fd_pr__nfet_01v8 L=0.15 W=1\n"
        "M2 out2 in vdd vdd nch w=2u l=0.5u\n"
        "R1 out out2 1k\n",
        {},
    )

    assert [device.name for device in parsed.devices] == ["XM1", "M2"]
    assert parsed.devices[0].kind == "nmos"
    assert parsed.devices[0].nodes == {
        "drain": "out",
        "gate": "in",
        "source": "0",
        "bulk": "0",
    }
    # Channel type of a plain `M` element comes from its own `.model` card.
    assert parsed.devices[1].kind == "nmos"
    assert parsed.sources["dd"] == ("vdd", "0", 1.8)


def test_parse_netlist_ignores_devices_inside_a_subckt_definition():
    parsed = op_sanity.parse_netlist(
        ".subckt myamp a b\n"
        "XMinner a b 0 0 nfet_03v3 w=1u\n"
        ".ends myamp\n"
        "XTOP out in 0 0 nfet_03v3 w=1u\n",
        {},
    )

    assert [device.name for device in parsed.devices] == ["XTOP"]


def test_parse_netlist_joins_continuation_lines_and_strips_params():
    parsed = op_sanity.parse_netlist(
        "XM1 out in 0 0 sky130_fd_pr__pfet_01v8\n+ L = 0.15 W={2*wu} nf=1\n", {}
    )

    assert parsed.devices[0].model == "sky130_fd_pr__pfet_01v8"
    assert parsed.devices[0].kind == "pmos"
    assert parsed.devices[0].nodes["bulk"] == "0"


def test_parse_netlist_resolves_param_referenced_source_value():
    parsed = op_sanity.parse_netlist(".param vdd=1.8\nVdd vdd 0 DC {vdd}\n", {})

    assert parsed.sources["dd"][2] == pytest.approx(1.8)


def test_op_candidates_cover_both_pdk_inner_element_conventions():
    # sky130 names the inner element `m<subckt>`, gf180mcu names it `m0`
    # (both verified against the installed PDKs -- see op_sanity.py).
    assert op_sanity._op_candidates("XM1", "sky130_fd_pr__nfet_01v8", None)[0] == (
        "m.xm1.msky130_fd_pr__nfet_01v8"
    )
    assert "m.xm1.m0" in op_sanity._op_candidates("XM1", "nfet_03v3", None)
    # A plain `M` element's vectors live directly under its own name.
    assert op_sanity._op_candidates("M2", "nch", None) == ["m2"]
    # An explicit override replaces the whole candidate list.
    assert op_sanity._op_candidates("XM1", "weird", "mcore") == ["m.xm1.mcore"]


# --------------------------------------------------------------------------- #
# Corner-aware triode margin
# --------------------------------------------------------------------------- #


def test_triode_margin_widens_with_temperature():
    cold = op_sanity._triode_margin_v(-40, {})
    nominal = op_sanity._triode_margin_v(27, {})
    hot = op_sanity._triode_margin_v(125, {})

    assert cold < nominal < hot
    # 2 kT/q at 27 C is ~51.8 mV.
    assert nominal == pytest.approx(0.0518, abs=1e-3)


def test_triode_margin_honours_explicit_overrides():
    assert op_sanity._triode_margin_v(27, {"triode_margin_v": 0.1}) == 0.1
    assert op_sanity._triode_margin_v(27, {"triode_margin_kt": 4.0}) == pytest.approx(
        0.1035, abs=1e-3
    )


# --------------------------------------------------------------------------- #
# Operating-point checks
# --------------------------------------------------------------------------- #


def test_golden_path_reports_no_findings(tmp_path, monkeypatch):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS},
        node_voltages={"vdd": 1.8, "in": 0.9, "bias": 0.6, "out": 1.2},
    )

    report = op_sanity.run_op_sanity(str(request))

    assert report["status"] == "clean"
    assert report["findings"] == []
    assert report["finding_count"] == 0
    assert report["device_count"] == 2
    assert [device["region"] for device in report["devices"]] == [
        "saturation",
        "saturation",
    ]


def test_device_biased_off_produces_exactly_one_finding(tmp_path, monkeypatch):
    """The issue's own headline acceptance criterion: one deliberately-off
    device, exactly one finding, naming the device, the condition, and the
    measured values."""
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {
            "XM1": {"vgs": 0.21, "vth": 0.70, "vds": 1.7, "vdsat": 0.0, "id": 1.2e-13},
            "XM2": _SATURATED_PMOS,
        },
        node_voltages={"vdd": 1.8, "in": 0.21, "bias": 0.6, "out": 1.7},
    )

    report = op_sanity.run_op_sanity(str(request))

    assert report["status"] == "findings"
    assert report["finding_count"] == 1
    assert report["error_count"] == 1
    (finding,) = report["findings"]
    assert finding["check"] == "off"
    assert finding["severity"] == "error"
    assert finding["device"] == "XM1"
    assert finding["kind"] == "nmos"
    assert finding["values"]["vgs_v"] == pytest.approx(0.21)
    assert finding["values"]["vth_v"] == pytest.approx(0.70)
    assert "0.21 V" in finding["message"] and "0.7 V" in finding["message"]
    assert finding["suggestion"]
    assert report["devices"][0]["region"] == "off"


def test_subthreshold_but_conducting_device_is_not_reported_off(tmp_path, monkeypatch):
    """`|Vgs| < |Vth|` alone is not enough -- a device conducting real
    current is in weak inversion, which is a legitimate design point."""
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {
            "XM1": {"vgs": 0.65, "vth": 0.70, "vds": 1.2, "vdsat": 0.08, "id": 2e-6},
            "XM2": _SATURATED_PMOS,
        },
    )

    report = op_sanity.run_op_sanity(str(request))

    assert _findings(report, "off") == []
    assert report["devices"][0]["region"] == "subthreshold"


def test_triode_device_is_reported_as_a_warning_with_values(tmp_path, monkeypatch):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {
            "XM1": {"vgs": 0.9, "vth": 0.7, "vds": 0.10, "vdsat": 0.15, "id": 3e-5},
            "XM2": _SATURATED_PMOS,
        },
    )

    report = op_sanity.run_op_sanity(str(request))

    (finding,) = _findings(report, "triode")
    assert finding["severity"] == "warning"
    assert finding["device"] == "XM1"
    assert finding["values"]["vds_v"] == pytest.approx(0.10)
    assert finding["values"]["vdsat_v"] == pytest.approx(0.15)
    # Warnings alone never escalate the process-level verdict.
    assert report["error_count"] == 0
    assert report["devices"][0]["region"] == "triode"


def test_triode_margin_is_what_catches_a_marginal_device(tmp_path, monkeypatch):
    """`|Vds|` just *above* `|Vdsat|` but inside the corner-aware margin is
    still reported -- that margin is the whole point of the check."""
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {
            "XM1": {"vgs": 0.9, "vth": 0.7, "vds": 0.17, "vdsat": 0.15, "id": 3e-5},
            "XM2": _SATURATED_PMOS,
        },
    )

    report = op_sanity.run_op_sanity(str(request))
    assert [entry["device"] for entry in _findings(report, "triode")] == ["XM1"]

    # Pinning the margin to zero makes the same device read as saturated.
    request = _write_request(
        tmp_path,
        _GOOD_NETLIST,
        name="tight.json",
        op_lint={"thresholds": {"triode_margin_v": 0.0}},
    )
    report = op_sanity.run_op_sanity(str(request))
    assert _findings(report, "triode") == []


def test_pmos_magnitudes_are_compared_not_raw_signs(tmp_path, monkeypatch):
    """A PMOS whose model reports negative values (level-1 convention)
    classifies identically to one reporting positive magnitudes (BSIM4)."""
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {
            "XM1": _SATURATED_NMOS,
            "XM2": {
                "vgs": -1.2,
                "vth": -0.75,
                "vds": -0.9,
                "vdsat": -0.39,
                "id": -4.5e-5,
            },
        },
    )

    report = op_sanity.run_op_sanity(str(request))

    assert report["devices"][1]["region"] == "saturation"
    assert report["findings"] == []


# --------------------------------------------------------------------------- #
# Wiring smells
# --------------------------------------------------------------------------- #


def test_nmos_drain_tied_to_ground_is_flagged(tmp_path, monkeypatch):
    request = _write_request(
        tmp_path,
        "Vdd vdd 0 DC 1.8\nVin in 0 DC 0.9\nXM1 0 in 0 0 nmos_demo w=2 l=0.5\n"
        "XM2 outp in vdd vdd pmos_demo w=2 l=0.5\nR1 outp 0 1k\n",
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    report = op_sanity.run_op_sanity(str(request))

    (finding,) = _findings(report, "drain_tied_to_rail")
    assert finding["device"] == "XM1"
    assert finding["severity"] == "error"
    assert finding["nodes"]["drain"] == "0"


def test_pmos_drain_tied_to_supply_is_flagged(tmp_path, monkeypatch):
    request = _write_request(
        tmp_path,
        "Vdd vdd 0 DC 1.8\nVin in 0 DC 0.9\nXM1 outn in 0 0 nmos_demo w=2 l=0.5\n"
        "XM2 vdd in vdd vdd pmos_demo w=2 l=0.5\nR1 outn 0 1k\n",
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    report = op_sanity.run_op_sanity(str(request))

    (finding,) = _findings(report, "drain_tied_to_rail")
    assert finding["device"] == "XM2"
    assert finding["kind"] == "pmos"
    # The supply rail is the one the request's own `corners.supply_v` names.
    assert report["rails"]["supply"] == ["vdd"]


def test_stimulus_source_node_is_not_treated_as_a_rail(tmp_path, monkeypatch):
    """A testbench's own input source drives a node to a nonzero DC voltage
    without that node being a rail -- treating it as one would turn every
    input-driven drain into a false `drain_tied_to_rail`."""
    request = _write_request(
        tmp_path,
        "Vdd vdd 0 DC 1.8\nVin in 0 DC 0.9\nXM1 in bias 0 0 nmos_demo w=2 l=0.5\n"
        "Vbias bias 0 DC 0.6\n",
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS})

    report = op_sanity.run_op_sanity(str(request))

    assert _findings(report, "drain_tied_to_rail") == []
    assert "in" not in report["rails"]["supply"]


def test_gate_shorted_to_source_is_flagged(tmp_path, monkeypatch):
    request = _write_request(
        tmp_path,
        "Vdd vdd 0 DC 1.8\nXM1 outn tail tail 0 nmos_demo w=2 l=0.5\n"
        "R1 outn vdd 1k\nR2 tail 0 1k\nR3 tail vdd 1k\n",
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS})

    report = op_sanity.run_op_sanity(str(request))

    (finding,) = _findings(report, "gate_shorted_to_source")
    assert finding["device"] == "XM1"
    assert finding["severity"] == "error"
    assert "tail" in finding["message"]


def test_bulk_not_tied_to_source_or_rail_is_flagged(tmp_path, monkeypatch):
    request = _write_request(
        tmp_path,
        "Vdd vdd 0 DC 1.8\nVin in 0 DC 0.9\nVb bodyn 0 DC 0.4\n"
        "XM1 outn in 0 bodyn nmos_demo w=2 l=0.5\nR1 outn vdd 1k\nR2 bodyn 0 1k\n",
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS})

    report = op_sanity.run_op_sanity(str(request))

    (finding,) = _findings(report, "bulk_not_tied")
    assert finding["device"] == "XM1"
    assert finding["severity"] == "warning"
    assert finding["nodes"]["bulk"] == "bodyn"


def test_bulk_tied_to_its_own_source_is_not_flagged(tmp_path, monkeypatch):
    """A cascode/diff-pair device with bulk on its own source is correct --
    only a bulk on neither its source nor the matching rail is a smell."""
    request = _write_request(
        tmp_path,
        "Vdd vdd 0 DC 1.8\nVin in 0 DC 0.9\n"
        "XM1 outn in tail tail nmos_demo w=2 l=0.5\n"
        "R1 outn vdd 1k\nR2 tail 0 1k\n",
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS})

    report = op_sanity.run_op_sanity(str(request))

    assert _findings(report, "bulk_not_tied") == []


# --------------------------------------------------------------------------- #
# Netlist hygiene
# --------------------------------------------------------------------------- #


def test_measurement_referencing_a_nonexistent_node_is_flagged(tmp_path, monkeypatch):
    request = _write_request(
        tmp_path,
        _GOOD_NETLIST,
        measurements=[
            {"name": "vout", "spice": ".meas tran vout FIND v(vout_node) AT=1n"}
        ],
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    report = op_sanity.run_op_sanity(str(request))

    (finding,) = _findings(report, "missing_node")
    assert finding["node"] == "vout_node"
    assert finding["severity"] == "error"
    assert "'vout'" in finding["message"]


def test_declared_io_node_present_in_netlist_is_not_flagged(tmp_path, monkeypatch):
    request = _write_request(
        tmp_path,
        _GOOD_NETLIST,
        op_lint={"nodes": {"inputs": ["in"], "outputs": ["out"]}},
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    report = op_sanity.run_op_sanity(str(request))

    assert _findings(report, "missing_node") == []


def test_declared_io_node_absent_from_netlist_is_flagged(tmp_path, monkeypatch):
    request = _write_request(tmp_path, _GOOD_NETLIST, op_lint={"nodes": ["outp"]})
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    report = op_sanity.run_op_sanity(str(request))

    (finding,) = _findings(report, "missing_node")
    assert finding["node"] == "outp"
    assert "op_lint.nodes" in finding["message"]


def test_floating_node_is_flagged_as_a_warning(tmp_path, monkeypatch):
    request = _write_request(
        tmp_path,
        "Vdd vdd 0 DC 1.8\nVin in 0 DC 0.9\nXM1 outn in 0 0 nmos_demo w=2 l=0.5\n"
        "R1 outn vdd 1k\nCstub dangle 0 1p\nR2 dangle2 outn 1k\n",
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS})

    report = op_sanity.run_op_sanity(str(request))

    # `dangle2` has one resistor terminal and nothing else; `dangle` has only
    # a capacitor terminal, which is no DC path either -- both are exactly
    # what ngspice's singular-matrix narration is usually complaining about.
    floating = {finding["node"] for finding in _findings(report, "floating_node")}
    assert floating == {"dangle", "dangle2"}
    assert all(
        finding["severity"] == "warning"
        for finding in _findings(report, "floating_node")
    )


# --------------------------------------------------------------------------- #
# Diagnostics / failure handling
# --------------------------------------------------------------------------- #


def test_unanswered_op_vectors_produce_an_actionable_diagnostic(tmp_path, monkeypatch):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    # candidate_index far past the end: no candidate path answers at all.
    _install_fake_ngspice(
        monkeypatch,
        {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS},
        candidate_index=99,
        node_voltages={"out": 1.2},
    )

    report = op_sanity.run_op_sanity(str(request))

    codes = {diagnostic["code"] for diagnostic in report["diagnostics"]}
    assert codes == {"op_point_unavailable"}
    assert report["devices"][0]["op_point_element"] is None
    assert report["devices"][0]["region"] == "unknown"
    message = report["diagnostics"][0]["message"]
    assert "op_point_element" in message


def test_op_point_element_override_resolves_a_nonstandard_pdk(tmp_path, monkeypatch):
    request = _write_request(
        tmp_path,
        _GOOD_NETLIST,
        op_lint={"models": {"nmos_demo": {"op_point_element": "mcore"}}},
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    report = op_sanity.run_op_sanity(str(request))

    assert report["devices"][0]["op_point_element"] == "m.xm1.mcore"
    assert report["devices"][0]["region"] == "saturation"


def test_wiring_findings_survive_an_ngspice_launch_failure(tmp_path, monkeypatch):
    """The structural checks never need the simulator -- which is exactly
    the case where an agent most needs them."""
    request = _write_request(
        tmp_path,
        "Vdd vdd 0 DC 1.8\nVin in 0 DC 0.9\nXM1 0 in 0 0 nmos_demo w=2 l=0.5\n"
        "XM2 outp in vdd vdd pmos_demo w=2 l=0.5\nR1 outp 0 1k\n",
    )
    _install_fake_ngspice(monkeypatch, {}, side_effect=FileNotFoundError("no ngspice"))

    report = op_sanity.run_op_sanity(str(request))

    assert report["status"] == "error"
    assert [finding["check"] for finding in _findings(report, "drain_tied_to_rail")]
    assert any(diagnostic["code"] == "unknown" for diagnostic in report["diagnostics"])


def test_recovered_gmin_stepping_narration_is_downgraded(tmp_path, monkeypatch):
    """Issue #205's recovery rule, applied to an `op` run: ngspice narrates
    its own stepping attempts even on a run that converges."""
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS},
        node_voltages={"out": 1.2},
        prologue="Warning: singular matrix:  check node out\nTrying gmin stepping",
    )

    report = op_sanity.run_op_sanity(str(request))

    (diagnostic,) = [
        entry for entry in report["diagnostics"] if entry["code"] == "singular_matrix"
    ]
    assert diagnostic["severity"] == "warning"
    assert report["status"] == "clean"


def test_unrecovered_singular_matrix_is_an_error_status(tmp_path, monkeypatch):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {},
        prologue="Warning: singular matrix:  check node out\nsimulation(s) aborted",
    )

    report = op_sanity.run_op_sanity(str(request))

    assert report["status"] == "error"


def test_unknown_corner_is_a_request_error(tmp_path, monkeypatch):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(monkeypatch, {})

    with pytest.raises(op_sanity.OpSanityError, match="not in this request's matrix"):
        op_sanity.run_op_sanity(str(request), corner="ff/1.8V/27C")


def test_missing_netlist_is_a_request_error(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"netlist": "nope.spice"}))

    with pytest.raises(op_sanity.OpSanityError, match="netlist not found"):
        op_sanity.run_op_sanity(str(request))


def test_named_corner_selects_that_point(tmp_path, monkeypatch):
    netlist_path = tmp_path / "body.spice"
    netlist_path.write_text(_GOOD_NETLIST)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "corners": {
                    "supply_v": {"vdd": [1.62, 1.98]},
                    "temperature_c": [27, 125],
                },
            }
        )
    )
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    report = op_sanity.run_op_sanity(str(request), corner="default/1.980V/125C")

    assert report["corner"]["temperature_c"] == 125
    assert report["corner"]["supply_v"] == {"vdd": 1.98}
    # The margin follows the *selected* corner's temperature.
    assert report["thresholds"]["triode_margin_v"] == pytest.approx(0.0686, abs=1e-3)


# --------------------------------------------------------------------------- #
# CLI (`klt sim --op-lint`)
# --------------------------------------------------------------------------- #


def test_cli_op_lint_clean_exits_0(tmp_path, monkeypatch, capsys):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    assert main(["sim", "--op-lint", str(request), "--format", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == op_sanity.SCHEMA_VERSION
    assert payload["status"] == "clean"


def test_cli_op_lint_error_finding_exits_3(tmp_path, monkeypatch, capsys):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {
            "XM1": {"vgs": 0.2, "vth": 0.7, "vds": 1.7, "vdsat": 0.0, "id": 1e-13},
            "XM2": _SATURATED_PMOS,
        },
    )

    assert main(["sim", "--op-lint", str(request), "--format", "json"]) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"][0]["check"] == "off"


def test_cli_op_lint_warning_only_still_exits_0(tmp_path, monkeypatch, capsys):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {
            "XM1": {"vgs": 0.9, "vth": 0.7, "vds": 0.05, "vdsat": 0.15, "id": 3e-5},
            "XM2": _SATURATED_PMOS,
        },
    )

    assert main(["sim", "--op-lint", str(request), "--format", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "findings"
    assert payload["error_count"] == 0


def test_cli_op_lint_failed_analysis_exits_4(tmp_path, monkeypatch, capsys):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(monkeypatch, {}, side_effect=FileNotFoundError("no ngspice"))

    assert main(["sim", "--op-lint", str(request), "--format", "json"]) == 4

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"


def test_cli_op_lint_bad_request_exits_1(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"netlist": "nope.spice"}))

    assert main(["sim", "--op-lint", str(request), "--format", "json"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["command"] == "sim"


def test_cli_op_lint_text_output_names_the_device(tmp_path, monkeypatch, capsys):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {
            "XM1": {"vgs": 0.2, "vth": 0.7, "vds": 1.7, "vdsat": 0.0, "id": 1e-13},
            "XM2": _SATURATED_PMOS,
        },
    )

    assert main(["sim", "--op-lint", str(request)]) == 3

    out = capsys.readouterr().out
    assert "status: findings" in out
    assert "[error] off: XM1 (nmos)" in out
    assert "XM2 [pmos] saturation" in out


def test_cli_without_op_lint_still_runs_the_corner_sweep(tmp_path, monkeypatch):
    """The default path is untouched: no `--op-lint`, no behaviour change."""
    request = _write_request(
        tmp_path, _GOOD_NETLIST, analysis={"kind": "op", "args": ""}
    )
    calls: list[str] = []

    def fake_run_sim(request_path, **kwargs):
        calls.append(request_path)
        return {
            "schema_version": 3,
            "netlist": {"path": "body.spice", "scope": "repo"},
            "status": "pass",
            "corner_count": 1,
            "passed": 1,
            "failed": 0,
            "errored": 0,
            "environment": {
                "engine": "ngspice",
                "engine_version": "46",
                "models_lib": {"path": None, "scope": "external"},
            },
            "measurements": [],
            "corners": [],
        }

    monkeypatch.setattr("klayout_tools.cli.sim_cmd.run_sim", fake_run_sim)

    assert main(["sim", str(request), "--format", "json"]) == 0
    assert calls == [str(request)]


# --------------------------------------------------------------------------- #
# `klt eval`'s `op-sanity` gate
# --------------------------------------------------------------------------- #


def _eval_descriptor(tmp_path: Path, request: Path, **gate_extra: object) -> Path:
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(
        json.dumps(
            {
                "gates": [
                    {
                        "check": "op-sanity",
                        "args": {"request": str(request)},
                        **gate_extra,
                    }
                ],
                "objective": {
                    "check": "op-sanity",
                    "metric": "finding_count",
                    "polarity": "minimize",
                    "args": {"request": str(request)},
                },
            }
        )
    )
    return descriptor


def test_eval_gate_passes_on_a_clean_lint(tmp_path, monkeypatch):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    descriptor = _eval_descriptor(tmp_path, request)
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    report = eval_mod.run_eval(str(descriptor))

    assert report["valid"] is True
    assert report["gates"][0] == {
        "check": "op-sanity",
        "name": "op-sanity",
        "status": "pass",
        "exit_code": 0,
        "count": 0,
    }
    assert report["objective"]["value"] == 0


def test_eval_gate_fails_with_exit_code_3_on_an_error_finding(tmp_path, monkeypatch):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    descriptor = _eval_descriptor(tmp_path, request)
    _install_fake_ngspice(
        monkeypatch,
        {
            "XM1": {"vgs": 0.2, "vth": 0.7, "vds": 1.7, "vdsat": 0.0, "id": 1e-13},
            "XM2": _SATURATED_PMOS,
        },
    )

    report = eval_mod.run_eval(str(descriptor))

    assert report["valid"] is False
    assert report["gates"][0]["status"] == "fail"
    assert report["gates"][0]["exit_code"] == 3


def test_eval_gate_warnings_alone_pass_unless_a_threshold_says_otherwise(
    tmp_path, monkeypatch
):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    _install_fake_ngspice(
        monkeypatch,
        {
            "XM1": {"vgs": 0.9, "vth": 0.7, "vds": 0.05, "vdsat": 0.15, "id": 3e-5},
            "XM2": _SATURATED_PMOS,
        },
    )

    lenient = eval_mod.run_eval(str(_eval_descriptor(tmp_path, request)))
    assert lenient["valid"] is True

    strict_descriptor = _eval_descriptor(
        tmp_path, request, threshold={"metric": "finding_count", "max": 0}
    )
    strict = eval_mod.run_eval(str(strict_descriptor))
    assert strict["valid"] is False


def test_eval_gate_reports_exit_code_4_when_the_analysis_never_ran(
    tmp_path, monkeypatch
):
    request = _write_request(tmp_path, _GOOD_NETLIST)
    descriptor = _eval_descriptor(tmp_path, request)
    _install_fake_ngspice(monkeypatch, {}, side_effect=FileNotFoundError("no ngspice"))

    report = eval_mod.run_eval(str(descriptor))

    assert report["gates"][0]["exit_code"] == 4
    assert report["valid"] is False


def test_eval_gate_bad_request_is_an_eval_error(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"netlist": "nope.spice"}))
    descriptor = _eval_descriptor(tmp_path, request)

    with pytest.raises(eval_mod.EvalError, match="failed to run"):
        eval_mod.run_eval(str(descriptor))


# --------------------------------------------------------------------------- #
# Integration: real ngspice against real PDK model libraries
# --------------------------------------------------------------------------- #


@_SKIP_NO_SKY130
def test_integration_sky130_ota_is_clean_and_reads_vth_from_the_model():
    """The repo's own sky130 5T OTA (the reference Loop A block) lints clean
    at its nominal corner, and every device's `Vth` comes back from the
    model -- distinct per device and per channel type, which a hardcoded
    constant could not be."""
    request = EXAMPLES / "design-pipeline" / "sim-op.request.json"

    report = op_sanity.run_op_sanity(str(request), corner="tt/1.620V/-40C")

    assert report["status"] == "clean"
    assert report["error_count"] == 0
    assert report["device_count"] == 6
    assert {device["region"] for device in report["devices"]} == {"saturation"}

    vths = {device["name"]: device["values"]["vth_v"] for device in report["devices"]}
    assert all(value is not None for value in vths.values())
    # Per-device, model-derived: NMOS and PMOS Vth differ, and the two
    # channel types do not share a single value.
    nmos = {
        device["values"]["vth_v"]
        for device in report["devices"]
        if device["kind"] == "nmos"
    }
    pmos = {
        device["values"]["vth_v"]
        for device in report["devices"]
        if device["kind"] == "pmos"
    }
    assert nmos and pmos and not (nmos & pmos)
    assert len(nmos) > 1  # different W/L -> different model-binned Vth

    # sky130's inner-element convention resolved without an override.
    assert report["devices"][0]["op_point_element"].endswith("msky130_fd_pr__nfet_01v8")
    assert report["provenance"]["pdk"]["name"] == "sky130A"


@_SKIP_NO_SKY130
def test_integration_sky130_off_device_is_named(tmp_path):
    """The issue's acceptance criterion against a *real* PDK: one
    deliberately-off device, exactly one finding, naming it."""
    netlist = tmp_path / "off.spice"
    netlist.write_text(
        "Vdd vdd 0 DC 1.8\n"
        "Vgood good 0 DC 0.9\n"
        "Vdead dead 0 DC 0.1\n"
        "XMgood n1 good 0 0 sky130_fd_pr__nfet_01v8 L=0.5 W=4 nf=1 mult=1\n"
        "XMdead n2 dead 0 0 sky130_fd_pr__nfet_01v8 L=0.5 W=4 nf=1 mult=1\n"
        "R1 vdd n1 100k\n"
        "R2 vdd n2 100k\n"
    )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "off.spice",
                "models": {
                    "pdk": "sky130A",
                    "lib": "libs.tech/ngspice/sky130.lib.spice",
                },
                "corners": {
                    "process": ["tt"],
                    "supply_v": {"dd": [1.8]},
                    "temperature_c": [27],
                },
            }
        )
    )

    report = op_sanity.run_op_sanity(str(request))

    off = _findings(report, "off")
    assert [finding["device"] for finding in off] == ["XMdead"]
    assert off[0]["values"]["vth_v"] is not None
    assert abs(off[0]["values"]["vgs_v"]) < abs(off[0]["values"]["vth_v"])
    assert "XMdead" in off[0]["message"]


@_SKIP_NO_GF180
def test_integration_gf180_resolves_its_own_inner_element_convention(tmp_path):
    """gf180mcu names the inner element `m0`, not `m<subckt>` -- the
    candidate-probing deck resolves it with no per-PDK configuration."""
    netlist = tmp_path / "gf180.spice"
    netlist.write_text(
        f".include {GF180_NGSPICE_DESIGN}\n"
        "Vdd vdd 0 DC 3.3\n"
        "Vin in 0 DC 1.5\n"
        "XM1 outn in 0 0 nfet_03v3 w=1u l=0.28u\n"
        "R1 vdd outn 10k\n"
    )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "gf180.spice",
                "models": {"lib": GF180_NGSPICE_LIB},
                "corners": {
                    "process": ["typical"],
                    "supply_v": {"dd": [3.3]},
                    "temperature_c": [27],
                },
            }
        )
    )

    report = op_sanity.run_op_sanity(str(request))

    (device,) = report["devices"]
    assert device["op_point_element"] == "m.xm1.m0"
    assert device["values"]["vth_v"] is not None
    assert device["values"]["vdsat_v"] is not None
    assert device["region"] in ("saturation", "triode")

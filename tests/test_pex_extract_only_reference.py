"""`klt pex --extract-only --reference-netlist`, and the forwarded `klt
extract` device-coverage fields (issue #5477).

Split out of `tests/test_pex.py` (which is at its per-file size ceiling)
alongside the existing `tests/test_pex_*.py` modules; the shared layout
fixtures and harness helpers still come from that module.
"""

# ruff: noqa: F401, F811
from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.pex import PexError, run_pex
from test_pex import (
    _write_gds,
    _write_harness,
    measure_reference,
    resistor_layout,
)


def _write_reference_with_extra_device(path: Path, model: str = "pnp_missing") -> Path:
    """The schematic DUT the other fixtures use, plus one bipolar instance
    whose model no sky130 curated-deck extraction of the resistor layout can
    produce -- the shape issue #5477 exists for: the layout's device family
    has no class in the resolved deck, so those devices are simply absent
    from the extracted netlist rather than flagged."""
    path.write_text(
        f".SUBCKT RES RA RB\nR1 RA RB 289.2\nQ1 RA RB 0 {model}\n.ENDS RES\n"
    )
    return path


def test_run_pex_extract_only_reference_netlist_names_missing_device(
    tmp_path, resistor_layout
):
    """Issue #5477: `--extract-only` accepts `--reference-netlist` purely to
    compute `model_mismatch`, so an extraction missing a whole device family
    is named in an envelope whose `delta[]` is not yet runnable."""
    reference = _write_reference_with_extra_device(tmp_path / "reference.spice")

    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        extract_only=True,
        reference_netlist=str(reference),
    )

    mismatch = report["model_mismatch"]
    assert mismatch is not None
    assert mismatch["kind"] == "model_set"
    assert "pnp_missing" in mismatch["reference_only"]
    assert "pnp_missing" in mismatch["counts"]["reference"]
    assert "pnp_missing" not in mismatch["counts"]["extracted"]

    # The reference is reported as provenance, not as a measured leg. It
    # lives under tmp_path, outside any repo, so the committable
    # `{path, scope}` shape is `external` -- the point is that it is no
    # longer `absent`, which is what a bare `--extract-only` reports.
    from klayout_tools._digest import content_hash

    assert report["reference_netlist"]["scope"] == "external"
    assert report["reference_netlist_sha256"] == content_hash(str(reference))

    # ...and nothing else about an `--extract-only` envelope moves.
    assert report["measurement"]["mode"] == "extract-only"
    assert report["measurement"]["sides"] is None
    assert report["testbenches"] == []
    assert report["delta"] == []
    assert report["status"] == "not_checked"
    assert report["coverage"]["nothing_checked_reasons"] == ["no_delta_rows"]
    # The three measurement-leg diagnostics describe testbench `X...`-line
    # reuse, which this mode never does: a supplied reference must not wake
    # any of them (the `pin_order_mismatch` pre-flight reads
    # `reference_netlist_path` directly and is gated on `extract_only`).
    assert report["pin_count_mismatch"] is None
    assert report["flat_dut_mismatch"] is None
    assert report["pin_order_mismatch"] is None


def test_run_pex_extract_only_reference_netlist_agreeing_reports_no_mismatch(
    tmp_path, resistor_layout
):
    """The same call with a reference whose device set the extraction matches
    reports `model_mismatch: null` -- the field stays a signal, not a
    by-product of passing a reference at all."""
    netlist = tmp_path / "res.spice"
    run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(netlist),
        artifacts_dir=str(tmp_path / "art"),
        extract_only=True,
    )
    # Use the extraction's own output as the reference: same device multiset
    # by construction.
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res2.spice"),
        artifacts_dir=str(tmp_path / "art2"),
        extract_only=True,
        reference_netlist=str(netlist),
    )

    assert report["model_mismatch"] is None
    assert report["reference_netlist"]["scope"] == "external"


def test_run_pex_extract_only_still_rejects_unreadable_reference(
    tmp_path, resistor_layout
):
    """A reference that is not a DUT is refused in `--extract-only` mode by
    the same Gap-1 check (#1255) `--measure-command` mode applies."""
    params = tmp_path / "corners.spice"
    params.write_text(".param sw_stat_mismatch=0\n")

    with pytest.raises(PexError, match="declares no `.SUBCKT`"):
        run_pex(
            resistor_layout,
            [],
            "sky130",
            output=str(tmp_path / "res.spice"),
            artifacts_dir=str(tmp_path / "art"),
            extract_only=True,
            reference_netlist=str(params),
        )


def test_cli_pex_extract_only_reference_netlist_json(tmp_path, resistor_layout, capsys):
    """Issue #5477 end to end through the CLI: the flag pair is accepted and
    the exit code is still 4 (`model_mismatch` is reported, not enforced)."""
    reference = _write_reference_with_extra_device(tmp_path / "reference.spice")

    exit_code = main(
        [
            "pex",
            resistor_layout,
            "--deck",
            "sky130",
            "--extract-only",
            "--reference-netlist",
            str(reference),
            "-o",
            str(tmp_path / "res.spice"),
            "--outdir",
            str(tmp_path / "art"),
            "--format",
            "json",
        ]
    )

    assert exit_code == 4
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "not_checked"
    assert report["measurement"]["mode"] == "extract-only"
    assert report["model_mismatch"]["kind"] == "model_set"
    assert "pnp_missing" in report["model_mismatch"]["reference_only"]


# --- forwarded extraction device coverage ---------------------------------


def test_run_pex_extraction_forwards_device_coverage_fields(tmp_path, resistor_layout):
    """`klt pex`'s `extraction` block carries `klt extract`'s own
    `device_classes` / `unsupported_devices` disclosure verbatim."""
    from klayout_tools.extract import run_extract

    extract_report = run_extract(
        resistor_layout,
        "sky130",
        output=str(tmp_path / "direct.spice"),
    )
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        extract_only=True,
    )

    extraction = report["extraction"]
    assert extraction["device_classes"] == extract_report["device_classes"]
    assert extraction["device_classes"]  # the sky130 deck declares classes
    assert extraction["unsupported_devices"] == extract_report["unsupported_devices"]


def _make_gf180mcu_bare_mos_cap_layout() -> kdb.Layout:
    """An *unclassifiable* gf180mcu MOS-capacitor stack: Comp (22/0) + Poly2
    (30/0) under `MOS_CAP_MK` (166/5), with no Nplus/Pplus implant, so no
    `mos_capacitors` entry claims it (issue #2919). The mirror of
    `tests/test_extract.py`'s `_make_gf180mcu_mos_cap_layout`, minus the NMOS
    the extraction side does not need here."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(22, 0)).insert(kdb.Box(10000, 0, 14000, 4000))
    top.shapes(layout.layer(30, 0)).insert(kdb.Box(10500, -300, 13500, 4300))
    top.shapes(layout.layer(166, 5)).insert(kdb.Box(10000, 0, 14000, 4000))
    return layout


def test_run_pex_extraction_forwards_non_empty_unsupported_devices(tmp_path):
    """Issue #5477: the forwarding carries a real finding, not just `[]` --
    a gf180mcu layout with a bare `MOS_CAP_MK` shape shows the same
    `unsupported_devices[]` entry in the `klt pex` envelope that `klt
    extract` reports (issue #2919)."""
    layout = _write_gds(_make_gf180mcu_bare_mos_cap_layout(), tmp_path / "moscap.gds")

    report = run_pex(
        layout,
        [],
        "gf180mcu",
        output=str(tmp_path / "moscap.spice"),
        artifacts_dir=str(tmp_path / "art"),
        extract_only=True,
    )

    (entry,) = report["extraction"]["unsupported_devices"]
    assert entry["kind"] == "mos_capacitor"
    assert entry["marker"] == "166/5"
    assert entry["count"] == 1


def test_run_pex_extraction_forwards_device_coverage_in_measured_mode(
    tmp_path, resistor_layout, measure_reference
):
    """Forwarded in every mode, not only `--extract-only`."""
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path),
        reference_netlist=measure_reference,
    )

    assert report["extraction"]["device_classes"]
    assert report["extraction"]["unsupported_devices"] == []

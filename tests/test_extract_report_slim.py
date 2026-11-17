"""`klt extract --report-parasitics {full,slim}` (issue #5816).

When `--spef` writes the per-net R/C model as SPEF beside the JSON report,
the report's `parasitics.nets[]` mostly re-states what the SPEF already
carries (per-net RC network, every coupling capacitor from both sides) or
repeats one constant per net (the lumped model's vacuous `integrity`
sub-keys, empty `terminals`/`segments`/`pin_order`). `slim` drops exactly
that, keeps everything the SPEF lacks (`by_layer`, `integrity.capacitance`,
the scalars) and adds one global `coupling_by_layer_pair` histogram so the
`coupled[].levels` distribution survives. `full` (the default) is
byte-identical to the pre-#5816 output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_extract as te
from klayout_tools.extract import ExtractError, run_extract

_COUPLED_ENTRY_KEYS = {"net", "capacitance_ff", "levels", "lateral_levels"}
_CONSTANT_INTEGRITY_SUBKEYS = (
    "open",
    "islands",
    "orphan_nodes",
    "suspicious_resistors",
    "short_candidates",
)


def _coupling_fixture(tmp_path: Path, name: str) -> str:
    return te._write_gds(te._make_overlap_layout(), tmp_path / name)


def _run_pair(tmp_path: Path, **extra):
    """The same extraction twice: once `full`, once `slim`. Returns the two
    reports; the SPEF/SPICE paths are distinct per run so neither run can
    read the other's files."""
    gds = _coupling_fixture(tmp_path, "overlap.gds")
    full = run_extract(
        gds,
        "sky130",
        output=str(tmp_path / "full.spice"),
        parasitics=True,
        spef_output=str(tmp_path / "full.spef"),
        **extra,
    )
    slim = run_extract(
        gds,
        "sky130",
        output=str(tmp_path / "slim.spice"),
        parasitics=True,
        spef_output=str(tmp_path / "slim.spef"),
        report_parasitics="slim",
        **extra,
    )
    return full, slim


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_slim_without_spef_is_a_clean_error(tmp_path):
    """`slim` is meaningful only with `--spef` -- there is nothing to slim
    against without the SPEF it drops data in favour of."""
    gds = _coupling_fixture(tmp_path, "overlap.gds")
    with pytest.raises(ExtractError, match=r"--report-parasitics slim requires --spef"):
        run_extract(
            gds,
            "sky130",
            output=str(tmp_path / "out.spice"),
            parasitics=True,
            report_parasitics="slim",
        )


def test_slim_without_spef_no_outputs_written(tmp_path):
    gds = _coupling_fixture(tmp_path, "overlap.gds")
    with pytest.raises(ExtractError):
        run_extract(
            gds,
            "sky130",
            output=str(tmp_path / "out.spice"),
            parasitics=True,
            report_parasitics="slim",
        )
    assert not (tmp_path / "out.spice").exists()
    assert list(tmp_path.glob("*.json")) == []


def test_unknown_report_shape_value_is_a_clean_error(tmp_path):
    """The request form carries the value as a plain string (argparse's
    `choices` only guards the CLI form), so the value is re-validated where
    both forms meet."""
    gds = _coupling_fixture(tmp_path, "overlap.gds")
    with pytest.raises(ExtractError, match="must be 'full' or 'slim'"):
        run_extract(
            gds,
            "sky130",
            output=str(tmp_path / "out.spice"),
            parasitics=True,
            spef_output=str(tmp_path / "out.spef"),
            report_parasitics="tiny",
        )


# --------------------------------------------------------------------------- #
# `full` (and omitted) stays byte-identical
# --------------------------------------------------------------------------- #


def test_full_and_omitted_reports_are_identical_except_timing(tmp_path):
    """Explicit `--report-parasitics full` and the omitted default produce
    the same report (modulo the wall-clock `timing` block) and the same
    SPEF/SPICE bytes -- the default is byte-identical to the pre-#5816
    output. Both runs share one set of output paths (the second overwrites
    the first) so the reports' own path fields cannot differ."""
    gds = _coupling_fixture(tmp_path, "overlap.gds")
    kwargs = dict(
        output=str(tmp_path / "out.spice"),
        parasitics=True,
        spef_output=str(tmp_path / "out.spef"),
    )
    omitted = run_extract(gds, "sky130", **kwargs)
    omitted_spef = Path(kwargs["spef_output"]).read_bytes()
    omitted_spice = Path(kwargs["output"]).read_bytes()
    full = run_extract(gds, "sky130", report_parasitics="full", **kwargs)
    assert omitted.pop("timing") is not None
    assert full.pop("timing") is not None
    assert json.dumps(full, sort_keys=True) == json.dumps(omitted, sort_keys=True)
    assert Path(kwargs["spef_output"]).read_bytes() == omitted_spef
    assert Path(kwargs["output"]).read_bytes() == omitted_spice
    assert "report_shape" not in omitted["parasitics"]
    assert "coupling_by_layer_pair" not in omitted["parasitics"]


# --------------------------------------------------------------------------- #
# slim leaves the SPEF and the SPICE untouched
# --------------------------------------------------------------------------- #


def test_slim_spef_and_spice_are_byte_identical_to_full(tmp_path):
    full, slim = _run_pair(tmp_path)
    assert Path(full["spef_path"]).read_bytes() == Path(slim["spef_path"]).read_bytes()
    assert (
        Path(full["netlist_path"]).read_bytes()
        == Path(slim["netlist_path"]).read_bytes()
    )


def test_slim_keeps_summary_scalars_integrity_and_metrics(tmp_path):
    full, slim = _run_pair(tmp_path)
    for key in (
        "r_count",
        "c_count",
        "cc_count",
        "l_count",
        "total_resistance_ohm",
        "total_capacitance_ff",
        "total_coupling_capacitance_ff",
        "total_inductance_nh",
        "substrate_dc_tie",
        "model",
        "integrity",
    ):
        assert slim["parasitics"][key] == full["parasitics"][key], key
    # Top-level verdict fields and the metrics re-keying are untouched too.
    assert slim["metrics"] == full["metrics"]
    assert slim["status"] == full["status"] == "extracted"
    assert slim["spef_path"] == str(tmp_path / "slim.spef")


# --------------------------------------------------------------------------- #
# The slim shape itself
# --------------------------------------------------------------------------- #


def test_slim_report_shape_is_marked(tmp_path):
    _, slim = _run_pair(tmp_path)
    assert slim["parasitics"]["report_shape"] == "slim"


def test_slim_drops_spef_carried_and_constant_per_net_fields(tmp_path):
    full, slim = _run_pair(tmp_path)
    assert [n["net"] for n in slim["parasitics"]["nets"]] == [
        n["net"] for n in full["parasitics"]["nets"]
    ]
    full_by_net = {n["net"]: n for n in full["parasitics"]["nets"]}
    for entry in slim["parasitics"]["nets"]:
        ref = full_by_net[entry["net"]]
        # Identity, scalars and the data the SPEF lacks all survive.
        assert entry["net_id"] == ref["net_id"]
        assert entry["rc_model"] == ref["rc_model"] == "lumped"
        for key in (
            "resistance_ohm",
            "capacitance_ff",
            "inductance_nh",
            "resistance_ohm_top_cell",
            "capacitance_ff_top_cell",
        ):
            assert entry[key] == ref[key], key
        assert entry["by_layer"] == ref["by_layer"]
        assert entry["by_layer"]
        # Everything the SPEF beside it carries is dropped.
        assert "coupled" not in entry
        assert "hub_net" not in entry
        # Empty/constant lumped-model fields are dropped.
        assert "terminals" not in entry
        assert "segments" not in entry
        assert "pin_order" not in entry
        # The constant integrity sub-keys go; the net's own capacitance
        # reconciliation stays.
        for key in _CONSTANT_INTEGRITY_SUBKEYS:
            assert key not in entry["integrity"], key
        assert entry["integrity"]["capacitance"] == ref["integrity"]["capacitance"]


def test_full_report_carries_the_fields_slim_drops(tmp_path):
    """Guard on the fixture itself: the `full` run really does carry the
    coupled[] entries, hub_net and constant integrity blocks this flag's
    slim mode exists to drop."""
    full, _ = _run_pair(tmp_path)
    coupled = [n for n in full["parasitics"]["nets"] if n["coupled"]]
    assert coupled, "fixture must produce at least one coupled net"
    for entry in full["parasitics"]["nets"]:
        assert entry["hub_net"]
        assert entry["terminals"] == []
        assert entry["segments"] == []
        assert entry["pin_order"] is None
        for c in entry["coupled"]:
            assert set(c) == _COUPLED_ENTRY_KEYS
        assert set(_CONSTANT_INTEGRITY_SUBKEYS) <= set(entry["integrity"])
    assert "report_shape" not in full["parasitics"]


def test_coupling_histogram_equals_the_full_aggregate(tmp_path):
    """Each capacitor counted once (not once per side), keyed by layer pair,
    summing the pair's capacitance -- exactly the aggregate over `full`'s
    `coupled[].levels`."""
    full, slim = _run_pair(tmp_path)
    expected = _histogram_from_full(full["parasitics"])
    histogram = slim["parasitics"]["coupling_by_layer_pair"]
    assert histogram == expected
    assert histogram, "fixture must produce at least one layer pair"
    # And it agrees with the report's own scalar totals.
    assert sum(b["count"] for b in histogram.values()) == full["parasitics"]["cc_count"]
    assert sum(b["capacitance_ff"] for b in histogram.values()) == pytest.approx(
        full["parasitics"]["total_coupling_capacitance_ff"]
    )


def test_net_without_coupling_is_unchanged_except_the_drops(tmp_path):
    """A no-coupling net slims to the same shape as a coupled one (its
    `coupled` was already `[]`); nothing about the histogram treats it
    specially."""
    full, slim = _run_pair(tmp_path)
    ref = next(n for n in full["parasitics"]["nets"] if n["net"] == "C")
    assert ref["coupled"] == []
    got = next(n for n in slim["parasitics"]["nets"] if n["net"] == "C")
    assert got["capacitance_ff"] == ref["capacitance_ff"]
    assert got["by_layer"] == ref["by_layer"]
    assert "coupled" not in got


# --------------------------------------------------------------------------- #
# Distributed-RC nets keep their populated fields
# --------------------------------------------------------------------------- #


def test_distributed_net_keeps_segments_terminals_pin_order(tmp_path):
    """A distributed net's populated ladder fields are per-net data no SPEF
    row spells out, so slim keeps them (only the *empty* lumped-model forms
    go). Exercised through the pure function: `--distributed-rc --spef`
    currently crashes in the SPEF writer itself (a pre-existing gap
    unrelated to this flag -- the report's `terminals[]` entries carry no
    `resistance_ohm` key the writer indexes), tracked in #6218."""
    from klayout_tools.extract_parasitics_slim import slim_parasitics_report

    gds = te._write_gds(te._make_series_nmos_layout(), tmp_path / "series.gds")
    baseline = run_extract(
        gds, "sky130", output=str(tmp_path / "b.spice"), parasitics=True
    )
    net_name = te._distributed_rc_internal_net_name(baseline)
    report = run_extract(
        gds,
        "sky130",
        output=str(tmp_path / "d.spice"),
        parasitics=True,
        critical_nets=[net_name],
        distributed_rc=True,
    )
    entry = next(
        n for n in report["parasitics"]["nets"] if n["rc_model"] == "distributed"
    )
    assert entry["terminals"]
    assert entry["segments"]
    assert entry["pin_order"] is not None

    slimmed = slim_parasitics_report(report["parasitics"])
    got = next(n for n in slimmed["nets"] if n["rc_model"] == "distributed")
    assert got["terminals"] == entry["terminals"]
    assert got["segments"] == entry["segments"]
    assert got["pin_order"] == entry["pin_order"]
    # A distributed net's real open check (not the lumped constant) stays.
    assert got["integrity"]["open"] == entry["integrity"]["open"]
    assert entry["integrity"]["open"]["status"] in ("pass", "open")


# --------------------------------------------------------------------------- #
# `--parasitic-variant drop-c-coupling` interplay
# --------------------------------------------------------------------------- #


def test_drop_c_coupling_variant_keeps_the_measured_histogram(tmp_path):
    """`drop-c-coupling` omits the coupling `Ccc_*` cards from the injected
    SPICE (`cc_count`/`total_coupling_capacitance_ff` drop to zero) but the
    JSON still *measures* every pair -- `coupled[]` stays populated (the
    documented #3056 behavior), so slim's histogram, which aggregates the
    measurement, stays populated too."""
    expected_hist: dict | None = None
    for shape in ("full", "slim"):
        gds = _coupling_fixture(tmp_path, f"overlap_{shape}.gds")
        report = run_extract(
            gds,
            "sky130",
            output=str(tmp_path / f"{shape}.spice"),
            parasitics=True,
            spef_output=str(tmp_path / f"{shape}.spef"),
            parasitic_variants=["drop-c-coupling"],
            report_parasitics=shape,
        )
        para = report["parasitics"]
        assert para["cc_count"] == 0
        assert para["total_coupling_capacitance_ff"] == 0.0
        if shape == "full":
            coupled = [n for n in para["nets"] if n["coupled"]]
            assert coupled, "the measurement survives the variant on full"
            expected_hist = _histogram_from_full(para)
        else:
            assert all("coupled" not in n for n in para["nets"])
            assert para["coupling_by_layer_pair"] == expected_hist


def _histogram_from_full(para: dict) -> dict:
    """The expected slim histogram, re-derived from a full report (the same
    aggregation `test_coupling_histogram_equals_the_full_aggregate`
    asserts)."""
    expected: dict[str, dict] = {}
    seen: set[tuple] = set()
    for entry in para["nets"]:
        for c in entry["coupled"]:
            pair = (tuple(sorted((entry["net"], c["net"]))), c["capacitance_ff"])
            if pair in seen:
                continue
            seen.add(pair)
            for lo, hi in c["levels"]:
                bucket = expected.setdefault(
                    f"{lo}/{hi}", {"count": 0, "capacitance_ff": 0.0}
                )
                bucket["count"] += 1
                bucket["capacitance_ff"] += c["capacitance_ff"]
    for bucket in expected.values():
        bucket["capacitance_ff"] = round(bucket["capacitance_ff"], 6)
    return expected


# --------------------------------------------------------------------------- #
# Request document form
# --------------------------------------------------------------------------- #


def test_request_form_carries_report_parasitics(tmp_path):
    from klayout_tools.cli.extract_cmd import _apply_request
    from klayout_tools.cli.parser import create_parser

    request_path = tmp_path / "extract.request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema": "klt.extract.request/1",
                "file": "design.gds",
                "deck": "sky130",
                "parasitics": True,
                "spef": "design.spef",
                "report_parasitics": "slim",
            }
        )
    )
    resolved = _apply_request(
        create_parser().parse_args(["extract", str(request_path), "--format", "json"])
    )
    assert resolved.report_parasitics == "slim"
    assert resolved.spef == str(tmp_path / "design.spef")


def test_request_form_slim_without_spef_is_a_clean_error(tmp_path, capsys):
    from klayout_tools.cli import main

    request_path = tmp_path / "extract.request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema": "klt.extract.request/1",
                "file": str(_coupling_fixture(tmp_path, "overlap.gds")),
                "deck": "sky130",
                "parasitics": True,
                "report_parasitics": "slim",
            }
        )
    )
    assert main(["extract", str(request_path), "--format", "json"]) == 1
    payload = json.loads(capsys.readouterr().err)
    assert "--report-parasitics slim requires --spef" in payload["error"]["message"]


def test_request_form_rejects_an_unknown_value(tmp_path, capsys):
    from klayout_tools.cli import main

    request_path = tmp_path / "extract.request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema": "klt.extract.request/1",
                "file": str(_coupling_fixture(tmp_path, "overlap.gds")),
                "deck": "sky130",
                "parasitics": True,
                "spef": "design.spef",
                "report_parasitics": "tiny",
            }
        )
    )
    assert main(["extract", str(request_path), "--format", "json"]) == 1
    payload = json.loads(capsys.readouterr().err)
    assert "must be 'full' or 'slim'" in payload["error"]["message"]

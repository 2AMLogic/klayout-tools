"""Tests for ``klt lvs`` ``"engine": "klayout-runset"`` (issue #2849).

The standalone ``klayout`` application is mocked at the ``subprocess.run``
boundary: the fake "runset" writes a *real* ``.lvsdb`` (built with the pip
``klayout.db`` API) so the verdict-from-database path is exercised end to end.
A real-binary smoke test is skipped when ``klayout`` is not on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import klayout.db as kdb
import pytest

from klayout_tools import lvs_runset
from klayout_tools.coverage import coverage_validation_error
from klayout_tools.env_provenance import find_absolute_path_fields
from klayout_tools.lvs import LvsError, check_lvs_report, run_lvs

SPICE_ONE = ".subckt TOP\nM1 c b a d NMOS L=0.2u W=0.4u\n.ends\n"


def _make_gds(path: str, n_dev: int) -> None:
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    act, poly, bulk = ly.layer(1, 0), ly.layer(2, 0), ly.layer(9, 0)
    top.shapes(bulk).insert(kdb.Box(-1000, -1000, 100000, 2000))
    for i in range(n_dev):
        x = i * 2000
        top.shapes(act).insert(kdb.Box(x, 0, x + 1000, 400))
        top.shapes(poly).insert(kdb.Box(x + 400, -100, x + 600, 500))
    ly.write(path)


def _write_lvsdb(
    gds: str, schem: str, out: str, extract: bool = True, compare: bool = True
) -> None:
    ly = kdb.Layout()
    ly.read(gds)
    l2n = kdb.LayoutVsSchematic(kdb.RecursiveShapeIterator(ly, ly.top_cell(), []))
    act = l2n.make_layer(ly.layer(1, 0), "act")
    poly = l2n.make_layer(ly.layer(2, 0), "poly")
    bulk = l2n.make_layer(ly.layer(9, 0), "bulk")
    gate, sd = act & poly, act - poly
    if extract:
        l2n.extract_devices(
            kdb.DeviceExtractorMOS4Transistor("NMOS"),
            {"SD": sd, "G": gate, "tG": poly, "W": bulk},
        )
    l2n.connect(sd)
    l2n.connect(poly)
    l2n.connect(gate)
    l2n.connect(bulk)
    l2n.extract_netlist()
    ref = kdb.Netlist()
    ref.read(schem, kdb.NetlistSpiceReader())
    l2n.reference = ref
    if compare:
        l2n.compare(kdb.NetlistComparer())
    l2n.write(out)


@pytest.fixture
def fake_klayout(monkeypatch, tmp_path):
    """Replace the subprocess boundary; ``state`` controls the fake runset."""
    state = {
        "returncode": 0,
        "write_db": True,
        "extract": True,
        "compare": True,
        "stdout": "",
    }
    fake_bin = tmp_path / "klayout"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("KLT_KLAYOUT_BINARY", str(fake_bin))
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[0] != str(fake_bin):
            return real_run(cmd, *a, **kw)
        if cmd[1:] == ["-v"]:
            return subprocess.CompletedProcess(cmd, 0, "KLayout 0.30.12\n", "")
        args = dict(c.split("=", 1) for c in cmd if "=" in c and c != "-rd")
        state["cmd"] = cmd
        if state["write_db"]:
            _write_lvsdb(
                args["input"],
                args["schematic"],
                args["report"],
                state["extract"],
                state["compare"],
            )
        # The driver exits 0 even when the netlists do not match.
        return subprocess.CompletedProcess(
            cmd, state["returncode"], state["stdout"], ""
        )

    monkeypatch.setattr(lvs_runset.subprocess, "run", fake_run)
    return state


def _request(tmp_path, n_dev, **extra):
    gds = tmp_path / "l.gds"
    _make_gds(str(gds), n_dev)
    sp = tmp_path / "ref.sp"
    sp.write_text(SPICE_ONE)
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    request = {
        "engine": "klayout-runset",
        "layout": {"file": str(gds)},
        "reference": {"netlist": str(sp)},
        "deck_file": str(deck),
        **extra,
    }
    return json.dumps(request)


def test_match_verdict_from_database(tmp_path, fake_klayout):
    report = run_lvs(_request(tmp_path, 1, deck_vars={"feol": True}))
    assert report["status"] == "match"
    assert report["engine"] == "klayout-runset"
    assert report["device_census"] == {"layout": {"NMOS": 1}, "reference": {"NMOS": 1}}
    assert report["counts"]["devices"]["matched"] == 1
    assert coverage_validation_error(report["coverage"]) is None
    assert report["coverage"]["checked"] == ["NMOS"]
    assert report["provenance"]["runset"]["content_hash"].startswith("sha256:")
    assert report["power_connectivity"]["status"] == "unchecked"
    assert "feol=true" in fake_klayout["cmd"]


def test_match_reports_matched_nets_as_net_correspondence(tmp_path, fake_klayout):
    report = run_lvs(_request(tmp_path, 1))
    rows = report["net_correspondence"]
    assert rows, "a matched compare must list the nets it paired (issue #2870)"
    assert all({"layout", "reference", "pin"} <= set(r) for r in rows)
    assert all(r["layout"] and r["reference"] for r in rows)


def test_driver_exit_zero_on_mismatch_is_a_mismatch(tmp_path, fake_klayout):
    fake_klayout["returncode"] = 0
    report = run_lvs(_request(tmp_path, 2))
    assert report["status"] == "mismatch"
    assert report["error_count"] > 0
    cats = {m["category"] for m in report["mismatches"]}
    assert "device.unmatched" in cats
    assert report["device_census"]["layout"]["NMOS"] == 2
    assert report["device_census"]["reference"]["NMOS"] == 1


def test_unresolved_x_call_in_reference_is_reported(tmp_path, fake_klayout):
    request = json.loads(_request(tmp_path, 1))
    sp = tmp_path / "ref.sp"
    sp.write_text(
        SPICE_ONE.replace(
            ".ends", "XC1 a b sky130_fd_pr__cap_mim_m3_1 W=1u L=1u\n.ends"
        )
    )
    report = run_lvs(json.dumps(request))
    assert report["status"] == "mismatch"
    found = [
        m
        for m in report["mismatches"]
        if m["category"] == "reference.unresolved_subckt_call"
    ]
    assert len(found) == 1
    assert found[0]["subcircuit"]["reference"] == "SKY130_FD_PR__CAP_MIM_M3_1"
    assert found[0]["instance"]["reference"].upper() == "C1"


def test_defined_x_call_in_reference_is_not_reported(tmp_path, fake_klayout):
    request = json.loads(_request(tmp_path, 1))
    sp = tmp_path / "ref.sp"
    sp.write_text(
        ".subckt CELL p q\nR1 p q 1k\n.ends\n"
        ".subckt TOP\nM1 c b a d NMOS L=0.2u W=0.4u\nX1 a b CELL\n.ends\n"
    )
    report = run_lvs(json.dumps(request))
    assert not [
        m
        for m in report["mismatches"]
        if m["category"] == "reference.unresolved_subckt_call"
    ]


def test_zero_devices_recognized_is_not_clean(tmp_path, fake_klayout):
    fake_klayout["extract"] = False
    sp = tmp_path / "empty.sp"
    sp.write_text(".subckt TOP\n.ends\n")
    req = json.loads(_request(tmp_path, 1))
    req["reference"]["netlist"] = str(sp)
    report = run_lvs(json.dumps(req))
    assert report["status"] != "match"
    assert report["coverage"]["nothing_checked"] is True
    assert any(
        m["category"] == "coverage.no_devices_recognized" for m in report["mismatches"]
    )


def test_empty_extraction_without_compare_is_a_graded_mismatch(tmp_path, fake_klayout):
    # The runset recognized nothing, so it never compared (issue #4081).
    fake_klayout["extract"] = False
    fake_klayout["compare"] = False
    report = run_lvs(_request(tmp_path, 1))
    assert report["status"] == "mismatch"
    assert report["device_census"] == {"layout": {}, "reference": {"NMOS": 1}}
    assert report["error_count"] > 0
    unmatched = [m for m in report["mismatches"] if m["category"] == "device.unmatched"]
    assert len(unmatched) == 1
    assert unmatched[0]["side"] == "reference"
    assert unmatched[0]["device"]["class"] == "NMOS"
    assert report["counts"]["devices"] == {"layout": 0, "reference": 1, "matched": 0}
    assert coverage_validation_error(report["coverage"]) is None
    assert report["coverage"]["recognized_device_classes"]["reference"] == ["NMOS"]
    assert "declared_not_recognized" in report["coverage"]


def test_empty_extraction_without_compare_and_empty_reference_stays_inconclusive(
    tmp_path, fake_klayout
):
    fake_klayout["extract"] = False
    fake_klayout["compare"] = False
    sp = tmp_path / "empty.sp"
    sp.write_text(".subckt TOP\n.ends\n")
    req = json.loads(_request(tmp_path, 1))
    req["reference"]["netlist"] = str(sp)
    report = run_lvs(json.dumps(req))
    assert report["status"] == "inconclusive"
    assert report["coverage"]["nothing_checked"] is True
    assert any(
        m["category"] == "coverage.no_devices_recognized" for m in report["mismatches"]
    )


def test_no_cross_reference_with_extracted_devices_is_still_an_error(
    tmp_path, fake_klayout
):
    fake_klayout["compare"] = False  # devices extracted, but never compared
    with pytest.raises(LvsError, match="no layout-vs-schematic cross-reference"):
        run_lvs(_request(tmp_path, 1))


def test_nonzero_exit_is_an_error(tmp_path, fake_klayout):
    fake_klayout["write_db"] = False  # crashed before writing a db
    fake_klayout["returncode"] = 1
    fake_klayout["stdout"] = "boom"
    with pytest.raises(LvsError, match="exited with status 1"):
        run_lvs(_request(tmp_path, 1))


def test_nonzero_exit_with_database_reports_the_mismatch(tmp_path, fake_klayout):
    # sky130.lvs exits 1 itself when compare() is false (issue #3987).
    fake_klayout["returncode"] = 1
    fake_klayout["stdout"] = "ERROR : Netlists don't match\n"
    report = run_lvs(_request(tmp_path, 2))
    assert report["status"] == "mismatch"
    assert "device.unmatched" in {m["category"] for m in report["mismatches"]}
    assert report["device_census"]["layout"]["NMOS"] == 2
    assert report["provenance"]["runset"]["exit_status"] == 1
    assert report["provenance"]["runset"]["engine_error_lines"] == [
        "ERROR : Netlists don't match"
    ]


def test_nonzero_exit_with_matching_database_is_a_match(tmp_path, fake_klayout):
    fake_klayout["returncode"] = 1
    report = run_lvs(_request(tmp_path, 1))
    assert report["status"] == "match"
    assert report["provenance"]["runset"]["exit_status"] == 1


def test_zero_exit_records_exit_status(tmp_path, fake_klayout):
    report = run_lvs(_request(tmp_path, 1))
    assert report["provenance"]["runset"]["exit_status"] == 0


def test_nonzero_exit_with_unusable_database_is_an_error(
    tmp_path, monkeypatch, fake_klayout
):
    fake_klayout["returncode"] = 1
    fake_klayout["stdout"] = "ERROR: died after extraction\n"

    def no_xref(path, reference=None):
        raise LvsError("database has no cross-reference")

    monkeypatch.setattr(lvs_runset, "read_lvsdb_verdict", no_xref)
    with pytest.raises(LvsError) as ei:
        run_lvs(_request(tmp_path, 1))
    msg = str(ei.value)
    assert msg.startswith("last error: ERROR: died after extraction")
    assert "exited with status 1" in msg
    assert "klayout application 0.30.12" in msg


def test_nonzero_exit_leads_with_last_error_line(tmp_path, fake_klayout):
    fake_klayout["write_db"] = False  # crashed before writing a db
    fake_klayout["returncode"] = 1
    fake_klayout["stdout"] = (
        "extracting...\nERROR: undefined method `purge_devices' for RBA::Netlist\n"
    )
    with pytest.raises(LvsError) as ei:
        run_lvs(_request(tmp_path, 1))
    msg = str(ei.value)
    assert msg.startswith("last error: ERROR: undefined method `purge_devices'")
    assert "no trustworthy verdict" in msg
    assert "klayout application 0.30.12" in msg
    assert "extracting..." in msg  # full log still present


def test_nonzero_exit_without_error_lines_has_no_cause_prefix(tmp_path, fake_klayout):
    fake_klayout["write_db"] = False  # crashed before writing a db
    fake_klayout["returncode"] = 1
    fake_klayout["stdout"] = "boom"
    with pytest.raises(LvsError) as ei:
        run_lvs(_request(tmp_path, 1))
    assert str(ei.value).startswith("klayout exited with status 1")


def test_unreadable_app_version_does_not_mask_error(
    tmp_path, monkeypatch, fake_klayout
):
    fake_klayout["write_db"] = False  # crashed before writing a db
    fake_klayout["returncode"] = 1
    fake_klayout["stdout"] = "ERROR: bad"
    monkeypatch.setattr(lvs_runset, "_application_version", lambda binary: None)
    with pytest.raises(LvsError, match="klayout application unknown"):
        run_lvs(_request(tmp_path, 1))


def test_runset_runs_in_temp_work_dir(tmp_path, monkeypatch, fake_klayout):
    seen = {}
    inner = lvs_runset.subprocess.run

    def spy(cmd, *a, **kw):
        if "-b" in cmd[1:]:
            seen["cwd"] = kw.get("cwd")
        return inner(cmd, *a, **kw)

    monkeypatch.setattr(lvs_runset.subprocess, "run", spy)
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    run_lvs(_request(tmp_path, 1))
    assert seen["cwd"] and os.path.basename(seen["cwd"]).startswith("klt-lvs-runset-")
    assert seen["cwd"] != str(caller)
    assert not os.path.exists(seen["cwd"])  # cleaned up with the work dir


def test_no_database_is_an_error(tmp_path, fake_klayout):
    fake_klayout["write_db"] = False
    with pytest.raises(LvsError, match="did not write an .lvsdb"):
        run_lvs(_request(tmp_path, 1))


def test_timeout_is_an_error(tmp_path, monkeypatch, fake_klayout):
    def boom(cmd, *a, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(lvs_runset.subprocess, "run", boom)
    with pytest.raises(LvsError, match="did not complete within"):
        run_lvs(_request(tmp_path, 1))


def test_missing_deck_file(tmp_path, fake_klayout):
    req = json.loads(_request(tmp_path, 1))
    req["deck_file"] = str(tmp_path / "nope.lvs")
    with pytest.raises(LvsError, match="deck_file not found"):
        run_lvs(json.dumps(req))


def test_missing_binary_is_actionable(tmp_path, monkeypatch):
    monkeypatch.delenv("KLT_KLAYOUT_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(LvsError, match="no klayout binary found"):
        run_lvs(_request(tmp_path, 1))


def test_reserved_deck_var_and_unsupported_fields(tmp_path, fake_klayout):
    with pytest.raises(LvsError, match="may not set 'input'"):
        run_lvs(_request(tmp_path, 1, deck_vars={"input": "x"}))
    with pytest.raises(LvsError, match="hints"):
        run_lvs(_request(tmp_path, 1, hints={"same_nets": []}))


def test_check_hashes_runset(tmp_path, fake_klayout):
    report = run_lvs(_request(tmp_path, 1))
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    result = check_lvs_report(str(path))
    assert result["status"] == "match", [c for c in result["checks"] if not c["match"]]
    assert "provenance.runset.content_hash" in [c["field"] for c in result["checks"]]
    (tmp_path / "pdk.lvs").write_text("# changed\n")
    assert check_lvs_report(str(path))["status"] != "match"


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_real_binary_version_probe():
    assert lvs_runset._application_version(shutil.which("klayout")) is not None


def _spice_netlist(text: str) -> kdb.Netlist:
    netlist = kdb.Netlist()
    reader_input = kdb.NetlistSpiceReader()
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "n.sp")
        with open(path, "w") as fh:
            fh.write(text)
        netlist.read(path, reader_input)
    return netlist


_CELL = ".subckt CELL A VPWR VGND\nM1 A A VGND VGND NMOS L=0.2u W=0.4u\n.ends\n"


def test_runset_power_connectivity_flags_untied_supply():
    text = _CELL + (
        ".subckt TOP A B VPWR VGND VOTHER\n"
        "X1 A VPWR VGND CELL\n"
        "X2 B VOTHER VGND CELL\n"
        ".ends\n"
    )
    block = lvs_runset.runset_power_connectivity(_spice_netlist(text))
    assert block["status"] == "mismatch"
    assert block["finding_count"] == 1
    assert block["power_pins"] == ["VGND", "VPWR"]
    assert block["power_pins_derivation"]["masters"] == ["CELL"]


def test_runset_power_connectivity_clean_when_ties_agree():
    text = _CELL + (
        ".subckt TOP A B VPWR VGND\nX1 A VPWR VGND CELL\nX2 B VPWR VGND CELL\n.ends\n"
    )
    block = lvs_runset.runset_power_connectivity(_spice_netlist(text))
    assert block["status"] == "match"
    assert block["findings"] == []


def test_runset_power_connectivity_unchecked_without_netlist_or_instances():
    none_block = lvs_runset.runset_power_connectivity(None)
    assert none_block["status"] == "unchecked"
    assert "no extracted netlist" in none_block["reason"]
    flat = lvs_runset.runset_power_connectivity(_spice_netlist(SPICE_ONE))
    assert flat["status"] == "unchecked"
    assert flat["reason"]


def test_deck_flags_fill_request(tmp_path, fake_klayout):
    request = json.loads(_request(tmp_path, 1))
    deck = request.pop("deck_file")
    report = run_lvs(json.dumps(request), deck_file=deck, deck_vars={"feol": "true"})
    assert report["deck_vars"] == {"feol": "true"}
    assert "feol=true" in fake_klayout["cmd"]


def test_deck_flags_conflict_with_request_fields(tmp_path, fake_klayout):
    with pytest.raises(LvsError, match="--deck-file conflicts"):
        run_lvs(_request(tmp_path, 1), deck_file=str(tmp_path / "pdk.lvs"))
    with pytest.raises(LvsError, match="--deck-var conflicts"):
        run_lvs(_request(tmp_path, 1, deck_vars={"a": "1"}), deck_vars={"b": "2"})


def test_deck_flags_refused_for_other_engines(tmp_path):
    request = json.dumps(
        {"layout": {"file": "x.gds"}, "reference": {"netlist": "x.sp"}}
    )
    with pytest.raises(LvsError, match="only applies to engine 'klayout-runset'"):
        run_lvs(request, deck_file="d.lvs")


def test_cli_deck_var_parsing():
    from klayout_tools.cli.lvs_cmd import _parse_deck_vars

    assert _parse_deck_vars(["a=1", "b=x=y", "a=2"]) == {"a": "2", "b": "x=y"}
    assert _parse_deck_vars(None) == {}
    with pytest.raises(LvsError, match="invalid --deck-var"):
        _parse_deck_vars(["novalue"])


def test_portable_paths_runset_has_no_absolute_paths(tmp_path, fake_klayout):
    default = run_lvs(_request(tmp_path, 1))
    assert find_absolute_path_fields(default)
    assert os.path.isabs(default["environment"]["klayout_binary"])

    portable = run_lvs(_request(tmp_path, 1, portable_paths=True))
    assert find_absolute_path_fields(portable) == []
    assert portable["environment"]["klayout_binary"] == "klayout"
    assert portable["status"] == default["status"]


def test_portable_paths_runset_rejects_non_boolean(tmp_path, fake_klayout):
    with pytest.raises(LvsError):
        run_lvs(_request(tmp_path, 1, portable_paths="yes"))
